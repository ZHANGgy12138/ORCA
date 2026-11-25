"""Batch behavior tests with real PDFs and a controlled, offline translator."""
import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data" / "batch-tests"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache"))

import pymupdf
from desktop_api import DesktopApi
from pdf2zh_next import high_level


def make_pdf(path, pages):
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page()
            page.insert_text((50, 70), f"Offline test document, page {index + 1}.")
        document.save(path)


def completed_result(settings, filename):
    output = Path(settings.translation.output) / (Path(filename).stem + ".mono.pdf")
    shutil.copy2(filename, output)
    return {"type": "finish", "translate_result": SimpleNamespace(mono_pdf_path=output, dual_pdf_path=None)}


def wait_finished(api):
    api.thread.join(15)
    assert not api.thread.is_alive(), "Batch did not stop"
    assert not api.is_busy()
    return api.get_state()


def main():
    (ROOT / "test-data").mkdir(exist_ok=True)
    original = high_level.do_translate_async_stream
    try:
        with tempfile.TemporaryDirectory(dir=ROOT / "test-data") as directory:
            base = Path(directory)
            a, b, c = base / "a.pdf", base / "b.pdf", base / "c.pdf"
            make_pdf(a, 2)
            make_pdf(b, 7)
            make_pdf(c, 3)
            corrupt = base / "broken.pdf"
            corrupt.write_bytes(b"not a PDF")
            api = DesktopApi(ROOT, ROOT, base / "partial", "test")
            api.prepare_assets = lambda: None

            imported = api.select_files([str(a), str(b), str(a), str(corrupt)])
            assert imported["ok"] and imported["added"] == 2 and imported["duplicates"] == 1
            assert len(imported["rejected"]) == 1 and len(api.get_state()["files"]) == 2
            assert not api.select_file(str(corrupt))["ok"]
            assert len(api.get_state()["files"]) == 2  # Failed replacement retains queue.
            api.select_files([str(c)])
            valid = api.validate_options({"engine": "bing", "page_mode": "first5"})
            assert valid["page_count"] == 10 and valid["file_count"] == 3
            assert [item["page_count"] for item in valid["per_file"]] == [2, 5, 3]
            assert not api.validate_options({"page_mode": "custom", "pages": "1-3"})["ok"]
            assert api.validate_options({"page_mode": "custom", "pages": "1,1-2"})["page_count"] == 6

            calls, progress = [], []
            async def partial_translation(settings, filename):
                calls.append(Path(filename).name)
                assert settings.pdf.pages == {"a.pdf": "1-2", "b.pdf": "1-5", "c.pdf": "1-3"}[Path(filename).name]
                yield {"type": "progress_update", "overall_progress": 35, "stage": "Translate Paragraphs"}
                await asyncio.sleep(0.02)
                progress.append(api.get_state()["batch"]["progress"])
                if Path(filename).name == "b.pdf":
                    raise ValueError("offline simulated failure")
                yield completed_result(settings, filename)

            high_level.do_translate_async_stream = partial_translation
            result = api.start_translation({"engine": "bing", "page_mode": "first5", "output": "mono"})
            assert result["ok"] and result["file_count"] == 3
            assert not api.remove_file(api.get_state()["files"][0]["id"])["ok"]
            assert not api.clear_files()["ok"]
            final = wait_finished(api)
            assert calls == ["a.pdf", "b.pdf", "c.pdf"]
            assert final["status"] == "partial"
            assert [item["status"] for item in final["files"]] == ["done", "error", "done"]
            assert final["batch"]["completed"] == 2 and final["batch"]["failed"] == 1
            assert final["batch"]["progress"] == 100
            assert progress == sorted(progress)
            assert len(final["history"]) == 3
            assert len({Path(item["results"]["mono"]).parent for item in final["files"] if item["results"]}) == 2
            assert not api.open_queue_result(final["files"][0]["id"], "executable")["ok"]

            # Native multi-select uses append rather than silently replacing files.
            class Window:
                def create_file_dialog(self, *args, **kwargs):
                    assert kwargs["allow_multiple"] is True
                    return [str(a), str(b)]
            api.window = Window()
            assert api.pick_file()["duplicates"] == 2
            assert api.remove_file(final["files"][1]["id"])["ok"]
            assert api.clear_files()["ok"] and api.get_state()["file"] is None

            # Cancel active + pending while retaining an earlier completed result.
            api = DesktopApi(ROOT, ROOT, base / "cancel", "test")
            api.prepare_assets = lambda: None
            api.select_files([str(a), str(b), str(c)])
            second_started = threading.Event()
            calls = []
            async def cancellable(settings, filename):
                calls.append(Path(filename).name)
                if Path(filename).name == "a.pdf":
                    yield completed_result(settings, filename)
                    return
                yield {"type": "progress_update", "overall_progress": 20, "stage": "Translate Paragraphs"}
                second_started.set()
                await asyncio.sleep(60)
            high_level.do_translate_async_stream = cancellable
            assert api.start_translation({"engine": "bing", "page_mode": "all", "output": "mono"})["ok"]
            assert second_started.wait(10)
            api.cancel()
            api.cancel()
            final = wait_finished(api)
            assert final["status"] == "cancelled"
            assert calls == ["a.pdf", "b.pdf"]
            assert [item["status"] for item in final["files"]] == ["done", "cancelled", "cancelled"]
            assert Path(final["files"][0]["results"]["mono"]).is_file()
            assert final["batch"]["completed"] == 1 and final["batch"]["cancelled"] == 2
            assert len(final["history"]) == 3
            api.close()
            assert not api.start_translation({"engine": "bing"})["ok"]
    finally:
        high_level.do_translate_async_stream = original
    print("PASS: multi-select, append/dedup, invalid retention, per-document page validation, sequential partial success, output isolation, weighted progress, cancel preserves completed results and stops pending, history, busy guards.")


if __name__ == "__main__":
    main()
