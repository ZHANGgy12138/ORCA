"""Real Windows process checks for ownership, activation and crash recovery."""
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def owner_process(data, pipe, start_gate=None):
    from single_instance import SingleInstance
    guard = SingleInstance(data)
    count = [0]
    try:
        if start_gate:
            start_gate.wait(10)
        primary = guard.acquire()
        pipe.send({"primary": primary, "pid": os.getpid(), "owner_pid": guard.owner_pid})
        if not primary:
            return
        def activated():
            count[0] += 1
        while True:
            command = pipe.recv()
            if command == "listen":
                guard.start_listener(activated)
                pipe.send({"listening": True})
            elif command == "count":
                pipe.send({"count": count[0]})
            elif command == "close":
                guard.close()
                guard.close()
                pipe.send({"closed": True})
                return
    finally:
        guard.close()
        pipe.close()


def start_process(context, data, gate=None):
    parent, child = context.Pipe()
    process = context.Process(target=owner_process, args=(str(data), child, gate))
    process.start()
    child.close()
    return process, parent


def receive(pipe, timeout=10):
    assert pipe.poll(timeout), "Single-instance child did not report"
    return pipe.recv()


def stop(process, pipe, primary=True):
    try:
        if process.is_alive():
            if primary:
                pipe.send("close")
                assert receive(pipe)["closed"]
            process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(3)
            raise AssertionError("Guard process did not stop cleanly")
        assert process.exitcode == 0
    finally:
        pipe.close()
        process.close()


def main():
    if os.name != "nt":
        raise RuntimeError("These checks require Windows")
    context = multiprocessing.get_context("spawn")
    test_root = ROOT / "test-data" / "single-instance"
    test_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=test_root) as temporary:
        data = Path(temporary) / "Profile"
        primary, first = start_process(context, data)
        primary_result = receive(first)
        assert primary_result["primary"]
        try:
            # Notification precedes the existence of any ready UI/listener.
            secondary, second = start_process(context, str(data).upper() + "\\.")
            result = receive(second)
            assert not result["primary"] and result["owner_pid"] == primary.pid, result
            stop(secondary, second, False)
            first.send("listen")
            assert receive(first)["listening"]
            deadline = time.monotonic() + 3
            seen = 0
            while time.monotonic() < deadline:
                first.send("count")
                seen = receive(first)["count"]
                if seen:
                    break
                time.sleep(.03)
            assert seen >= 1, "Activation before listener registration was lost"

            # Several nearly simultaneous duplicate launches only notify owner.
            extras = [start_process(context, data) for _ in range(3)]
            for process, pipe in extras:
                result = receive(pipe)
                assert not result["primary"] and result["owner_pid"] == primary.pid
                stop(process, pipe, False)
            first.send("count")
            assert receive(first)["count"] > seen

            # A different data profile can run independently.
            other, other_pipe = start_process(context, Path(temporary) / "Other")
            assert receive(other_pipe)["primary"]
            stop(other, other_pipe)
        finally:
            stop(primary, first)

        # Normal shutdown immediately releases ownership; an abrupt crash also
        # leaves no lock file that could permanently prevent the next startup.
        primary, first = start_process(context, data)
        assert receive(first)["primary"]
        primary.terminate()
        primary.join(5)
        first.close()
        primary.close()
        replacement, pipe = start_process(context, data)
        assert receive(pipe)["primary"]
        stop(replacement, pipe)

        # True first-launch contention still produces exactly one owner.
        gate = context.Event()
        contenders = [start_process(context, data, gate) for _ in range(4)]
        gate.set()
        outcomes = [(process, pipe, receive(pipe)) for process, pipe in contenders]
        assert sum(result["primary"] for _, _, result in outcomes) == 1
        for process, pipe, result in outcomes:
            stop(process, pipe, result["primary"])

        # A listener error must not silently release the still-running app's
        # ownership. Only shutdown may allow another process to become owner.
        from single_instance import SingleInstance
        guard = SingleInstance(data)
        assert guard.acquire()
        kernel = guard._windows.kernel
        original_wait = kernel.WaitForMultipleObjects
        try:
            kernel.WaitForMultipleObjects = lambda *args: 0xFFFFFFFF
            kernel.SetEvent(guard._activation)
            deadline = time.monotonic() + 3
            while not guard._error and time.monotonic() < deadline:
                time.sleep(.01)
            assert guard._error, "Fault injection did not reach listener"
            challenger = SingleInstance(data)
            try:
                assert not challenger.acquire(), "Listener failure released ownership"
            finally:
                challenger.close()
        finally:
            kernel.WaitForMultipleObjects = original_wait
            guard.close()
        recovered = SingleInstance(data)
        assert recovered.acquire()
        recovered.close()
        assert not list(Path(temporary).rglob("*")), "Guard should not leave profile/lock files"
    assert not multiprocessing.active_children(), "Guard checks left child processes"
    print("PASS: normalized profile ownership, queued activation, duplicate coalescing, profile isolation, clean shutdown, crash recovery, simultaneous first launches and fail-closed listener")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
