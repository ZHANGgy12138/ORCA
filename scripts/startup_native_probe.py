"""Exercise startup/retry/activation in the application's own test window."""
import json
import os
from pathlib import Path
import sys
import time


def run_startup_probe(window, bridge, startup, chrome, data, activation_count):
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    path = Path(os.environ.get("PDFMT_REPORT", data / "startup-test.json"))
    def js(code):
        return window.evaluate_js(code)
    def wait(check, label, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(.05)
        raise AssertionError("Timed out: " + label)
    def capture(name):
        from System import Action
        from System.IO import MemoryStream
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
        stream = MemoryStream(); holder = {}
        def begin():
            holder["task"] = window.native.webview.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream)
        window.native.Invoke(Action(begin))
        wait(lambda: holder["task"].IsCompleted, "screenshot")
        holder["task"].GetAwaiter().GetResult()
        target = data / name; target.write_bytes(bytes(stream.ToArray())); stream.Dispose()
        return str(target)
    try:
        wait(lambda: js("!!document.getElementById('startup-overlay')"), "shell HTML")
        if os.environ.get("ORCA_STARTUP_TEST_FAIL_ONCE") == "1":
            report["loading_visible"] = js("!document.getElementById('startup-overlay').hidden && !window.__orcaReady")
            assert report["loading_visible"]
            report["loading_screenshot"] = capture("startup-loading.png")
            wait(lambda: startup.get_state()["status"] == "error", "injected initialization failure")
            wait(lambda: js("!document.getElementById('startup-actions').hidden && !document.getElementById('startup-retry').disabled"), "retry action")
            report["error_screenshot"] = capture("startup-error.png")
            js("document.getElementById('startup-retry').click()")
            report["retry_clicked"] = True
        wait(lambda: js("window.__orcaReady===true && document.getElementById('startup-overlay').hidden"), "ready")
        ready_at = time.time()
        report["ready"] = True
        report["attempt"] = startup.get_state()["attempt"]
        assert report["attempt"] == (2 if os.environ.get("ORCA_STARTUP_TEST_FAIL_ONCE") == "1" else 1)
        report["backend_seconds"] = startup.get_state()["elapsed_seconds"]
        launched = float(os.environ["ORCA_STARTUP_TEST_LAUNCHED_AT"])
        shown = json.loads((data / "shell-shown.json").read_text(encoding="utf-8"))
        report["shell_shown_seconds"] = round(shown["at"] - launched, 3)
        report["ui_ready_seconds"] = round(ready_at - launched, 3)
        report["injected_delay_seconds"] = float(os.environ.get("ORCA_STARTUP_TEST_DELAY", "0"))
        report["pdf_import_deferred"] = "pdf2zh_next.high_level" not in sys.modules and "cv2" not in sys.modules and "sklearn" not in sys.modules
        assert report["pdf_import_deferred"]
        report["engine_count"] = js("document.getElementById('engine').options.length")
        assert report["engine_count"] == 23
        wait(lambda: activation_count[0] >= 1, "queued early duplicate activation")
        report["early_duplicate_activated"] = True
        initial_activations = activation_count[0]
        window.minimize()
        wait(lambda: chrome.state()["minimized"], "minimize owner")
        (data / "ready-for-duplicate.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        wait(lambda: activation_count[0] > initial_activations and not chrome.state()["minimized"], "duplicate restored owner")
        report["minimized_owner_restored"] = True
        report["activation_count"] = activation_count[0]
        report["ready_screenshot"] = capture("startup-ready.png")
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)
        report["startup_state"] = startup.get_state()
    finally:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        bridge._reader.close()
        startup.close()
        window.destroy()
