"""Run the same offline engine suite used by the final portable executable."""
import json
import multiprocessing
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data" / "engine-diagnostics-source"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache" / "babeldoc"))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from engine_diagnostics import run_engine_diagnostics
    report = run_engine_diagnostics()
    target = Path(os.environ["PDFMT_DATA_DIR"]) / "engine-test.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert report["ok"], "Engine release checks failed; inspect " + str(target)
    assert report["catalog_count"] == 23 and report["unique_module_count"] == 15
    assert {row["engine"] for row in report["loopback_tests"]} == {"deepl", "openaicompatible", "zhipu", "modelscope", "indextranslate"}
    assert not multiprocessing.active_children(), "Engine checks left child processes running"
