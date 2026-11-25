"""Exercise desktop exports with real PDFs, an isolated profile and offline translators."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__" and not getattr(sys, "frozen", False):
    sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]


def _pdf(path, message):
    import pymupdf
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 60), message)
        doc.save(path)


async def _success(settings, filename):
    folder = Path(settings.translation.output)
    outputs = {}
    for kind, disabled in (("mono", settings.pdf.no_mono), ("dual", settings.pdf.no_dual)):
        if not disabled:
            destination = folder / (Path(filename).stem + "." + kind + ".pdf")
            _pdf(destination, kind + " " + Path(filename).name)
            outputs[kind] = str(destination)
    return outputs


async def _dns_error(settings, filename):
    raise socket.gaierror(11001, "getaddrinfo failed")


def _join(api):
    api.thread.join(30)
    assert not api.thread.is_alive(), "Desktop translation did not stop"
    return api.get_state()


def _scenario(base, name):
    from desktop_api import DesktopApi

    folder = base / name
    exports = folder / "exports"
    exports.mkdir(parents=True)
    previous = exports / "earlier-translation"
    previous.mkdir()
    sentinel = previous / "old.pdf"
    _pdf(sentinel, "Earlier translation")
    original = sentinel.read_bytes()
    source = folder / "chapter.pdf"
    _pdf(source, "An example chapter.")

    api = DesktopApi(ROOT, ROOT, folder / "profile", "test")
    api.prepare_assets = lambda: None
    assert api.save_preferences({"output_dir": str(exports)})["ok"]
    files = [source]
    if name == "partial_batch":
        other = folder / "second.pdf"
        _pdf(other, "A second chapter.")
        files.append(other)
    assert api.select_files([str(path) for path in files])["added"] == len(files)
    entered = threading.Event()

    async def blocked(settings, filename):
        # PDFs can already exist before the pipeline has finished.
        await _success(settings, filename)
        entered.set()
        await asyncio.Future()

    async def partial(settings, filename):
        if Path(filename) == files[1]:
            return await _dns_error(settings, filename)
        return await _success(settings, filename)

    async def appendix_failure(source_path, outputs, *args):
        assert all(Path(path).is_file() for path in outputs.values())
        entered.set()
        raise OSError("simulated image appendix failure")

    options = {"engine": "bing", "output": "mono", "page_mode": "all"}
    if name in ("cancel", "pause"):
        api._translate = blocked
    elif name == "dns_failure":
        options["engine"] = "indextranslate"
        api._translate = _dns_error
    elif name == "partial_batch":
        options.update(output="both", auto_save=True)
        api._translate = partial
    else:
        api._translate = _success
        if name == "appendix_failure":
            options.update(engine="indextranslate", translate_images=True)
        else:
            options["output"] = name

    with patch("image_translation.append_image_translations", appendix_failure):
        try:
            started = api.start_translation(options)
            assert started["ok"], started
            if name in ("cancel", "pause"):
                assert entered.wait(30), "Translator did not start"
                assert set(exports.iterdir()) == {previous}, "Visible export during translation"
                stop = api.cancel if name == "cancel" else api.pause_and_save
                assert stop()["ok"]
            final = _join(api)
        finally:
            if api.is_busy():
                api.cancel()
                _join(api)

    expected = {"cancel": "cancelled", "pause": "paused", "dns_failure": "error",
                "appendix_failure": "error", "partial_batch": "partial"}.get(name, "done")
    assert final["status"] == expected, (final["status"], final["error"])
    if name == "appendix_failure":
        assert entered.is_set() and "simulated image appendix failure" in final["error"]
    if name == "pause":
        assert api.list_recoveries()["recoveries"][0]["id"] == final["task_id"]

    assert sentinel.read_bytes() == original and set(previous.iterdir()) == {sentinel}
    published = set(exports.iterdir()) - {previous}
    history = {row["file_name"]: row for row in final["history"]}
    if expected not in ("done", "partial"):
        assert not published, "Unfinished translation left an export"
        assert not final["files"][0]["results"] and not final["results"]
        assert all(not row["results"] for row in history.values())
        return

    completed = final["files"][0]
    assert completed["status"] == "done" and len(published) == 1
    expected_kinds = {"mono", "dual"} if options["output"] == "both" else {options["output"]}
    results = completed["results"]
    assert set(results) == expected_kinds and results == history[source.name]["results"]
    exported_files = {Path(path) for path in results.values()}
    published_folder, = published
    assert published_folder.is_dir() and set(published_folder.iterdir()) == exported_files
    import pymupdf
    for kind, path in results.items():
        with pymupdf.open(path) as doc:
            assert doc[0].get_text().strip() == kind + " " + source.name
    if name == "partial_batch":
        assert final["files"][1]["status"] == "error" and not final["files"][1]["results"]
        assert not history[files[1].name]["results"]
        saved = api.recovery.load(final["task_id"])["files"][0]
        assert saved["results"] == results
        assert saved["result_hashes"] == {k: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                          for k, p in results.items()}


def run_export_tests():
    scenarios = {}
    with tempfile.TemporaryDirectory(prefix="orca-export-test-") as temporary:
        base = Path(temporary)
        with patch.dict(os.environ, {"PDFMT_DATA_DIR": temporary, "PDFMT_CACHE_DIR": str(base / "cache")}):
            # Both translator modules open SQLite at import time. Release those
            # handles on this thread before Windows removes the temporary profile.
            from babeldoc.translator.cache import db as layout_cache
            from pdf2zh_next.translator.cache import db as translation_cache
            for database in (layout_cache, translation_cache):
                if Path(database.database).is_relative_to(base):
                    database.close()
            for name in ("cancel", "dns_failure", "pause", "appendix_failure", "mono", "dual", "partial_batch"):
                try:
                    _scenario(base, name)
                except Exception as error:
                    import traceback
                    scenarios[name] = {"ok": False, "error": str(error), "traceback": traceback.format_exc()}
                else:
                    scenarios[name] = {"ok": True}
    return {"ok": all(scenario["ok"] for scenario in scenarios.values()), "scenarios": scenarios}


if __name__ == "__main__":
    report = run_export_tests()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
