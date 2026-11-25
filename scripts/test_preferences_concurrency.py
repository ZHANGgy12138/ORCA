"""Persistence isolation test; provider validation is covered separately."""
import multiprocessing
from pathlib import Path
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# These tests exercise disk coordination without loading network-provider caches.
registry = types.ModuleType("engine_registry")
registry.engine_id = lambda value: value
registry.safe_settings = lambda _, values: values
sys.modules["engine_registry"] = registry
from preferences import LocalStore


def writer(directory, number, gate):
    store = LocalStore(directory)
    gate.wait(10)
    store.add_history({"id": f"worker-{number}", "created_at": number + 10, "file_name": "example.pdf", "status": "done", "results": {}})
    store.update({"engine_settings": {f"service-{number}": {"model": f"example-{number}"}}})


def main():
    (ROOT / "test-data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "test-data") as directory:
        first, stale = LocalStore(directory), LocalStore(directory)
        first.add_history({"id": "initial", "created_at": 1, "file_name": "example.pdf", "status": "done", "results": {}})
        stale.update({"theme": "dark"})
        assert len(LocalStore(directory).history) == 1
        first.update({"lang_out": "ja"})
        stale.update({"output": "mono"})
        reloaded = LocalStore(directory)
        assert reloaded.preferences["theme"] == "dark"
        assert reloaded.preferences["lang_out"] == "ja"
        assert reloaded.preferences["output"] == "mono"
        gate = multiprocessing.Event()
        children = [multiprocessing.Process(target=writer, args=(directory, i, gate)) for i in range(5)]
        try:
            for child in children:
                child.start()
            gate.set()
            for child in children:
                child.join(15)
                assert child.exitcode == 0
            final = LocalStore(directory)
            assert len(final.history) == 6
            assert len(final.preferences["engine_settings"]) == 5
            assert len(stale.history_for_ui()) == 6
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join()
    print("PASS: stale-instance preferences, five separate writer processes, complete history, refresh, no lost provider profiles.")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
