"""Exercise real worker exits and UI cancellation without translation requests."""
import asyncio
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data" / "lifecycle"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache"))


def crash_worker(*args):
    os._exit(23)


def completed_worker(settings, file, progress, cancel, logs):
    progress.send({"type": "finish", "translate_result": "finished"})
    # Simulate a worker stuck after reporting success. Cleanup must not turn
    # a successfully completed result into a crash error.
    time.sleep(20)


def running_worker(settings, file, progress, cancel, logs):
    progress.send({"type": "progress_update", "overall_progress": 5})
    time.sleep(20)


async def subprocess_checks(hl):
    original = hl._translate_wrapper
    try:
        hl._translate_wrapper = crash_worker
        started = time.monotonic()
        try:
            async for _ in hl._translate_in_subprocess(None, Path("unused.pdf")):
                pass
            raise AssertionError("Early child exit was not reported")
        except hl.SubprocessCrashError:
            assert time.monotonic() - started < 8
        assert not multiprocessing.active_children()

        hl._translate_wrapper = completed_worker
        events = [event async for event in hl._translate_in_subprocess(None, Path("unused.pdf"))]
        assert events == [{"type": "finish", "translate_result": "finished"}]
        assert not multiprocessing.active_children()

        hl._translate_wrapper = running_worker
        stream = hl._translate_in_subprocess(None, Path("unused.pdf"))
        assert (await anext(stream))["type"] == "progress_update"
        wait_task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.1)
        wait_task.cancel()
        try:
            await wait_task
            raise AssertionError("Cancellation was swallowed")
        except asyncio.CancelledError:
            pass
        await stream.aclose()
        assert not multiprocessing.active_children()
    finally:
        hl._translate_wrapper = original


def wait_for(event, description):
    assert event.wait(10), description


def desktop_checks(hl):
    from desktop_api import DesktopApi
    original = hl.do_translate_async_stream
    try:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            cleanup_started, cleanup_release = threading.Event(), threading.Event()

            async def completed_stream(settings, filename):
                try:
                    output = Path(settings.translation.output) / "result.pdf"
                    output.write_bytes(b"test result marker")
                    yield {"type": "finish", "translate_result": SimpleNamespace(mono_pdf_path=output, dual_pdf_path=None)}
                finally:
                    cleanup_started.set()
                    while not cleanup_release.is_set():
                        await asyncio.sleep(0.02)

            hl.do_translate_async_stream = completed_stream
            api = DesktopApi(ROOT, ROOT, data, "test")
            api.prepare_assets = lambda: None
            api.update(file={"name": "test.pdf", "path": "test.pdf", "pages": 1})
            assert api.start_translation({"engine": "bing", "output": "mono"})["ok"]
            wait_for(cleanup_started, "Success cleanup did not start")
            assert api.get_state()["status"] == "running"
            assert api.is_busy()
            assert not api.start_translation({"engine": "bing"})["ok"]
            cleanup_release.set()
            api.thread.join(10)
            assert api.get_state()["status"] == "done" and not api.is_busy()
            assert api.loop is None
            api.cancel()  # A late click must not access a closed loop.

            running, cleanup_started, cleanup_release = threading.Event(), threading.Event(), threading.Event()

            async def cancellable_stream(settings, filename):
                try:
                    running.set()
                    while True:
                        await asyncio.sleep(0.02)
                        yield {"type": "progress_update", "overall_progress": 5}
                finally:
                    cleanup_started.set()
                    while not cleanup_release.is_set():
                        await asyncio.sleep(0.02)

            hl.do_translate_async_stream = cancellable_stream
            assert api.start_translation({"engine": "bing"})["ok"]
            wait_for(running, "Translation did not start")
            api.cancel()
            wait_for(cleanup_started, "Cancellation cleanup did not start")
            api.cancel()  # Repeated clicks must not interrupt child cleanup.
            assert api.is_busy() and api.get_state()["status"] == "running"
            cleanup_release.set()
            api.thread.join(10)
            assert api.get_state()["status"] == "cancelled" and not api.is_busy()
            api.close()
            assert not api.start_translation({"engine": "bing"})["ok"]
    finally:
        hl.do_translate_async_stream = original


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from pdf2zh_next import high_level
    asyncio.run(subprocess_checks(high_level))
    desktop_checks(high_level)
    print("PASS: child import crash, success cleanup, cancellation, busy lifecycle, repeated/late cancel, and close.")
