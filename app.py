"""ORCA. AGPL-3.0; desktop modifications 2026-11-16."""
import multiprocessing
import os
import sys

# Frozen translation workers must dispatch before the desktop window is created.
if __name__ == "__main__":
    multiprocessing.freeze_support()

import json
import logging
from pathlib import Path
import threading
import time

APP_STARTED = time.perf_counter()
VERSION = "0.9.1"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PORTABLE_ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else ROOT
DATA = Path(os.environ.get("PDFMT_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ORCA")
os.environ["PDFMT_DATA_DIR"] = str(DATA)
os.environ["PDFMT_CACHE_DIR"] = str(DATA / "cache" / "babeldoc")
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
os.environ["TIKTOKEN_CACHE_DIR"] = str(DATA / "cache" / "babeldoc" / "tiktoken")
os.environ.setdefault("PYTHONUTF8", "1")


def configure_logging():
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    log_file = DATA / "logs" / "desktop.log"
    if sys.stdout is None:
        sys.stdout = log_file.open("a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(log_file, encoding="utf-8")], force=True)
    for name in ("httpx", "httpcore", "openai", "pdfminer", "peewee"):
        logging.getLogger(name).setLevel(logging.ERROR)
    if not getattr(sys, "_orca_truststore_ready", False):
        import truststore
        truststore.inject_into_ssl()
        sys._orca_truststore_ready = True


class DeferredApi:
    """Keep window controls available before the real backend is published."""
    def __init__(self, startup, data):
        self._startup, self.data = startup, data
        self.window = None

    def __getattr__(self, name):
        api = self._startup.api
        if api is None:
            raise RuntimeError("ORCA 正在准备，请稍候。")
        return getattr(api, name)

    def is_busy(self):
        api = self._startup.api
        return api.is_busy() if api else False

    def close(self):
        self._startup.close()


def initial_theme(data):
    try:
        value = json.loads((data / "preferences.v2.json").read_text(encoding="utf-8"))
        return "dark" if value.get("preferences", {}).get("theme") == "dark" else "light"
    except (OSError, ValueError, AttributeError):
        return "light"


class WindowBridge:
    """Expose only the intended desktop actions to the embedded page."""
    def __init__(self, api, startup=None, theme="light"):
        self._api = api
        self._startup, self._initial_theme = startup, theme
        self._ui_ready_ms = None
        self._chrome = None
        from reader_service import ReaderService
        self._reader = ReaderService(api.data)

    def reader_pick_file(self):
        import webview
        selected = self._api.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=("PDF 文档 (*.pdf)",))
        return self._reader.open_file(selected[0]) if selected else {"ok": False, "cancelled": True}

    def reader_close_document(self):
        return self._reader.close_document()

    def reader_translate(self, payload):
        return self._reader.translate(payload)

    def reader_translate_image(self, payload):
        return self._reader.translate_image(payload)

    def reader_prepare(self, options):
        return self._reader.prepare(options)

    def reader_cancel(self, request_id=None):
        return self._reader.cancel(request_id)

    def get_state(self):
        return self._api.get_state()

    def get_bootstrap(self):
        if self._startup and self._startup.api is None:
            return self._startup.bootstrap()
        return self._api.get_bootstrap()

    def get_startup_state(self):
        return {**(self._startup.get_state() if self._startup else {"status": "ready", "stage": "启动完成", "elapsed_seconds": 0}), "theme": self._initial_theme}

    def retry_startup(self):
        return self._startup.retry() if self._startup else {"ok": True}

    def startup_ui_ready(self):
        if self._ui_ready_ms is None:
            self._ui_ready_ms = round((time.perf_counter() - APP_STARTED) * 1000, 1)
        return {"ok": True}

    def load_engine_credentials(self, engine, engine_settings):
        return self._api.load_engine_credentials(engine, engine_settings)

    def save_engine_credentials(self, engine, engine_settings):
        return self._api.save_engine_credentials(engine, engine_settings)

    def forget_engine_credentials(self, engine):
        return self._api.forget_engine_credentials(engine)

    def save_preferences(self, patch):
        result = self._api.save_preferences(patch)
        if result.get("ok") and self._chrome and "theme" in patch:
            self._chrome.set_theme(result["preferences"]["theme"])
        return result

    def validate_options(self, options):
        return self._api.validate_options(options)

    def test_engine(self, options):
        return self._api.test_engine(options)

    def open_history_result(self, task_id, kind):
        return self._api.open_history_result(task_id, kind)

    def select_history(self, task_id):
        return self._api.select_history(task_id)

    def pick_file(self):
        return self._api.pick_file()

    def remove_file(self, file_id):
        return self._api.remove_file(file_id)

    def clear_files(self):
        return self._api.clear_files()

    def open_queue_result(self, file_id, kind):
        return self._api.open_queue_result(file_id, kind)

    def window_get_state(self):
        return self._chrome.state() if self._chrome else {"maximized": False}

    def window_minimize(self):
        self._api.window.minimize()
        return {"ok": True}

    def window_toggle_maximize(self):
        if self.window_get_state().get("floating"):
            return self._chrome.exit_floating()
        if self.window_get_state()["maximized"]:
            self._api.window.restore()
        else:
            self._api.window.maximize()
        return {"ok": True, **self.window_get_state()}

    def window_toggle_floating(self):
        if self._startup and self._startup.get_state()["status"] != "ready":
            return {"ok": False, "error": "启动完成后即可切换悬浮窗。"}
        return self._chrome.toggle_floating() if self._chrome else {"ok": False}

    def window_exit_floating(self):
        return self._chrome.exit_floating() if self._chrome else {"ok": False}

    def window_set_floating_on_top(self, enabled):
        return self._chrome.set_floating_on_top(bool(enabled)) if self._chrome else {"ok": False}

    def window_close(self):
        self._api.window.destroy()
        return {"ok": True}

    def window_start_drag(self):
        return self._chrome.start_drag() if self._chrome else {"ok": False}

    def window_start_resize(self, edge):
        return self._chrome.start_resize(edge) if self._chrome else {"ok": False}

    def pick_output(self):
        return self._api.pick_output()

    def start_translation(self, options):
        return self._api.start_translation(options)

    def cancel(self):
        return self._api.cancel()

    def pause_and_save(self):
        return self._api.pause_and_save()

    def list_recoveries(self):
        return self._api.list_recoveries()

    def resume_saved(self, task_id, options):
        return self._api.resume_saved(task_id, options)

    def discard_saved(self, task_id):
        return self._api.discard_saved(task_id)

    def relocate_saved(self, task_id, file_id):
        return self._api.relocate_saved(task_id, file_id)

    def open_result(self, kind):
        return self._api.open_result(kind)

    def open_output(self):
        return self._api.open_output()

    def open_source(self):
        return self._api.open_source()


def run_application(instance=None):
    configure_logging()
    if "--export-test" in sys.argv:
        from scripts.test_export_publication import run_export_tests
        report_path = Path(os.environ.get("PDFMT_REPORT", DATA / "export-test.json"))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            report = run_export_tests()
        except Exception as exc:
            report = {"ok": False, "error": str(exc)}
        report.update(version=VERSION, frozen=bool(getattr(sys, "frozen", False)))
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report.get("ok") else 1
    if "--recovery-probe" in sys.argv:
        from scripts.recovery_acceptance import run_probe
        return run_probe(ROOT, PORTABLE_ROOT, DATA, VERSION)
    if "--credential-cleanup-test" in sys.argv:
        from credential_store import CredentialStore
        from scripts.credential_native_probe import test_namespace
        vault = CredentialStore(test_namespace())
        result = vault.forget("deepl")
        return 0 if result.get("ok") else 1
    if "--free-engine-test" in sys.argv:
        from engine_registry import test_connection
        report_path = Path(os.environ.get("PDFMT_REPORT", DATA / "free-engine-test.json"))
        results = {key: test_connection({"engine": key}) for key in ("bing", "google", "siliconflowfree", "indextranslate")}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {"ok": all(row.get("ok") for row in results.values()), "version": VERSION, "frozen": bool(getattr(sys, "frozen", False)), "engines": results}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report["ok"] else 1
    if "--engine-test" in sys.argv:
        from engine_diagnostics import run_engine_diagnostics
        report_path = Path(os.environ.get("PDFMT_REPORT", DATA / "engine-test.json"))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            report = {**run_engine_diagnostics(), "version": VERSION, "frozen": bool(getattr(sys, "frozen", False))}
        except Exception as exc:
            report = {"ok": False, "version": VERSION, "error": str(exc)}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report.get("ok") else 1
    credential_stage = 1 if "--credential-ui-test-stage1" in sys.argv else 2 if "--credential-ui-test-stage2" in sys.argv else 0
    if any(flag in sys.argv for flag in ("--diagnostics", "--self-test", "--cancel-test", "--batch-test", "--index-test")):
        from desktop_api import DesktopApi
        api = DesktopApi(ROOT, PORTABLE_ROOT, DATA, VERSION)
        report_path = Path(os.environ.get("PDFMT_REPORT", DATA / "diagnostics.json"))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            api.prepare_assets()
            import webview
            import onnxruntime
            from pdf2zh_next.config.model import SettingsModel
            report = {"ok": True, "frozen": bool(getattr(sys, "frozen", False)), "version": VERSION, "providers": onnxruntime.get_available_providers(), "data_dir": str(DATA)}
            if any(flag in sys.argv for flag in ("--self-test", "--cancel-test", "--batch-test", "--index-test")):
                selected = api.select_files(json.loads(os.environ["PDFMT_TEST_INPUTS"]), append=False) if "--batch-test" in sys.argv else api.select_file(os.environ["PDFMT_TEST_INPUT"])
                if not selected.get("ok"):
                    raise RuntimeError(selected["error"])
                test_engine = "indextranslate" if "--index-test" in sys.argv else "bing"
                test_pages = os.environ.get("PDFMT_TEST_PAGES", "7") if "--index-test" in sys.argv else "1"
                started = api.start_translation({"engine": test_engine, "lang_in": "en", "lang_out": "zh", "page_mode": "custom", "pages": test_pages, "output": "both",
                                                 "translate_images": "--test-figure-labels" in sys.argv})
                if not started.get("ok"):
                    raise RuntimeError(started["error"])
                deadline = time.monotonic() + 600
                cancel_at = time.monotonic() + 3 if "--cancel-test" in sys.argv else None
                while api.get_state()["status"] in ("preparing", "running") and time.monotonic() < deadline:
                    if cancel_at and time.monotonic() >= cancel_at:
                        api.cancel()
                        cancel_at = None
                    time.sleep(0.3)
                report["translation"] = api.get_state()
                report["ok"] = report["translation"]["status"] == ("cancelled" if "--cancel-test" in sys.argv else "done")
                if "--batch-test" in sys.argv:
                    report["ok"] = report["ok"] and len(report["translation"]["files"]) >= 2 and all(item["status"] == "done" and item["results"] for item in report["translation"]["files"])
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            api.close()
            return 0 if report["ok"] else 1
        except Exception as exc:
            logging.exception("Desktop self-check failed")
            report_path.write_text(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1
    import webview
    from window_chrome import WindowChrome
    from startup_service import StartupService
    attempts = [0]
    def create_api(root, portable_root, data, version):
        if "--startup-test" in sys.argv:
            attempts[0] += 1
            time.sleep(min(5, max(0, float(os.environ.get("ORCA_STARTUP_TEST_DELAY", "0")))))
            if os.environ.get("ORCA_STARTUP_TEST_FAIL_ONCE") == "1" and attempts[0] == 1:
                raise RuntimeError("Isolated startup retry probe")
        from desktop_api import DesktopApi
        backend = DesktopApi(root, portable_root, data, version)
        if credential_stage:
            from credential_store import CredentialStore
            from scripts.credential_native_probe import test_namespace
            backend.credentials = CredentialStore(test_namespace())
        return backend
    startup = StartupService(ROOT, PORTABLE_ROOT, DATA, VERSION, api_factory=create_api)
    api = DeferredApi(startup, DATA)
    theme = initial_theme(DATA)
    bridge = WindowBridge(api, startup, theme)
    # Native dragging replaces pywebview's JavaScript coordinate movement.
    webview.settings["DRAG_REGION_SELECTOR"] = ".orca-unused-drag-region"
    startup_url = str(ROOT / "ui" / "index.html") + "#theme=" + theme
    window = webview.create_window("ORCA · PDF 文档翻译", startup_url, js_api=bridge, width=1180, height=850, min_size=(980, 720), background_color="#111111" if theme == "dark" else "#f6f6f6", text_select=True, frameless=True, easy_drag=False, resizable=True, shadow=True)
    api.window = window
    chrome = WindowChrome(window, theme)
    bridge._chrome = chrome
    window.events.before_show += chrome.install
    activation_count = [0]
    def activate_existing():
        import ctypes
        from ctypes import wintypes
        from System import Action
        def activate():
            if window.native.IsDisposed:
                return
            chrome.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            chrome.user32.ShowWindow.restype = wintypes.BOOL
            chrome.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            chrome.user32.SetForegroundWindow.restype = wintypes.BOOL
            if chrome.user32.IsIconic(chrome.hwnd):
                chrome.user32.ShowWindow(chrome.hwnd, 9)
            window.native.Show()
            window.native.Activate()
            window.native.BringToFront()
            chrome.user32.SetForegroundWindow(chrome.hwnd)
            activation_count[0] += 1
        if not window.native.IsDisposed:
            window.native.BeginInvoke(Action(activate))
    def on_shown():
        if instance:
            instance.start_listener(activate_existing)
        if "--startup-test" in sys.argv:
            (DATA / "shell-shown.json").write_text(json.dumps({"at": time.time(), "pid": os.getpid()}), encoding="utf-8")
    def begin_startup():
        startup.start(window)
    window.events.shown += on_shown
    window.events.loaded += begin_startup
    if "--startup-test" in sys.argv:
        from scripts.startup_native_probe import run_startup_probe
        window.events.loaded += lambda: threading.Thread(target=run_startup_probe, args=(window, bridge, startup, chrome, DATA, activation_count), daemon=True).start()
    if credential_stage:
        from scripts.credential_native_probe import run_credential_probe
        window.events.loaded += lambda: threading.Thread(target=run_credential_probe, args=(window, bridge, api, DATA, credential_stage), daemon=True).start()
    if "--reader-test" in sys.argv:
        from scripts.reader_native_probe import run_reader_probe
        window.events.loaded += lambda: threading.Thread(target=run_reader_probe, args=(window, bridge, api, chrome, DATA), daemon=True).start()
    if "--floating-test" in sys.argv:
        def floating_probe():
            report = {"ok": False, "version": VERSION}
            report_path = Path(os.environ.get("PDFMT_REPORT", DATA / "floating-test.json"))

            def wait_until(check, timeout=10):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if check():
                        return
                    time.sleep(0.1)
                raise RuntimeError("Floating-window check timed out")

            def click_and_wait(identifier, floating):
                window.evaluate_js(f"document.getElementById('{identifier}').click()")
                wait_until(lambda: chrome.state().get("floating") == floating)
                wait_until(lambda: window.evaluate_js("document.documentElement.dataset.windowMode") == ("floating" if floating else "main"))

            try:
                wait_until(lambda: window.evaluate_js("window.__orcaReady === true"), 30)
                selected = api.select_file(os.environ["PDFMT_TEST_INPUT"])
                if not selected.get("ok"):
                    raise RuntimeError(selected["error"])
                started = api.start_translation({"engine": "bing", "lang_in": "en", "lang_out": "zh", "page_mode": "custom", "pages": "1", "output": "both"})
                if not started.get("ok"):
                    raise RuntimeError(started["error"])
                initial = api.get_state()
                thread, hwnd = api.thread, chrome.hwnd
                report["main_before"] = chrome.diagnostics()
                click_and_wait("window-floating", True)
                report["floating"] = chrome.diagnostics()
                report["compact_layout"] = window.evaluate_js("({width:innerWidth,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth||document.documentElement.scrollHeight>innerHeight,visible:!document.getElementById('floating-panel').hidden})")
                assert report["floating"]["floating"] and report["compact_layout"]["width"] < 600 and not report["compact_layout"]["overflow"]
                before_pin = chrome.state()["always_on_top"]
                window.evaluate_js("document.getElementById('floating-pin').click()")
                wait_until(lambda: chrome.state()["always_on_top"] != before_pin)
                report["pin_toggle"] = True
                click_and_wait("floating-restore", False)
                report["main_restored"] = chrome.diagnostics()
                assert report["main_restored"]["size"] == report["main_before"]["size"]
                bridge.window_toggle_maximize()
                wait_until(lambda: chrome.state()["maximized"])
                click_and_wait("window-floating", True)
                report["pin_choice_remembered"] = chrome.state()["always_on_top"] != before_pin
                assert report["pin_choice_remembered"]
                click_and_wait("floating-restore", False)
                report["maximized_restored"] = chrome.state()["maximized"]
                assert report["maximized_restored"]
                click_and_wait("window-floating", True)
                during = api.get_state()
                report["same_window_and_task"] = chrome.hwnd == hwnd and api.thread is thread and during["task_id"] == initial["task_id"] and during["started_at"] == initial["started_at"]
                assert report["same_window_and_task"]
                wait_until(lambda: api.get_state()["status"] not in ("preparing", "running"), 600)
                report["translation"] = api.get_state()
                assert report["translation"]["status"] == "done", report["translation"].get("error")
                time.sleep(1)
                report["finished_in_floating"] = chrome.state()["floating"]
                report["mini_completion"] = window.evaluate_js("({percent:document.getElementById('floating-percent').textContent,cancelDisabled:document.getElementById('floating-cancel').disabled,cancelHidden:document.getElementById('floating-cancel').hidden})")
                assert "100" in report["mini_completion"]["percent"]
                click_and_wait("floating-restore", False)
                completed_paths = list(report["translation"]["results"].values())
                second = api.start_translation({"engine": "bing", "lang_in": "en", "lang_out": "zh", "page_mode": "custom", "pages": "1", "output": "both"})
                assert second.get("ok"), second
                click_and_wait("window-floating", True)
                wait_until(lambda: window.evaluate_js("!document.getElementById('floating-cancel').hidden"))
                window.evaluate_js("document.getElementById('floating-cancel').click()")
                wait_until(lambda: api.get_state()["status"] == "cancelled", 30)
                report["floating_cancel_works"] = all(Path(path).is_file() for path in completed_paths)
                assert report["floating_cancel_works"]
                click_and_wait("floating-restore", False)
                report["ok"] = True
            except Exception as exc:
                logging.exception("Floating window self-check failed")
                report["error"] = str(exc)
            finally:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                api.close()
                window.destroy()
        window.events.loaded += lambda: threading.Thread(target=floating_probe, daemon=True).start()
    if "--ui-test" in sys.argv:
        def ui_probe():
            try:
                deadline = time.monotonic() + 30
                bridge_ready = False
                while time.monotonic() < deadline:
                    bridge_ready = window.evaluate_js("window.__orcaReady === true")
                    if bridge_ready:
                        break
                    time.sleep(0.25)
                if not bridge_ready:
                    raise RuntimeError("Desktop bridge did not initialize")
                if os.environ.get("PDFMT_TEST_INPUTS"):
                    api.select_files(json.loads(os.environ["PDFMT_TEST_INPUTS"]), append=False)
                elif os.environ.get("PDFMT_TEST_INPUT"):
                    api.select_file(os.environ["PDFMT_TEST_INPUT"])
                time.sleep(1)
                original_theme = window.evaluate_js("document.documentElement.dataset.theme")
                window.evaluate_js("document.getElementById('theme-toggle').click(); document.getElementById('settings-button').click(); document.getElementById('engine').value='openaicompatible'; document.getElementById('engine').dispatchEvent(new Event('change'))")
                time.sleep(0.5)
                window.evaluate_js("document.getElementById('setting-openai_compatible_model').value='orca-custom-test'; document.getElementById('setting-openai_compatible_model').dispatchEvent(new Event('input')); document.getElementById('setting-openai_compatible_api_key').value='orca-ui-test-secret'; document.getElementById('setting-openai_compatible_api_key').dispatchEvent(new Event('input'))")
                time.sleep(1)
                report = window.evaluate_js("({title:document.title, engineCount:document.getElementById('engine').options.length, settingsOpen:document.getElementById('settings-dialog').open, theme:document.documentElement.dataset.theme, customModel:!!document.getElementById('setting-openai_compatible_model'), customUrl:!!document.getElementById('setting-openai_compatible_base_url'), fileName:document.getElementById('file-name').textContent, translateEnabled:!document.getElementById('translate-button').disabled, trialEnabled:!document.getElementById('trial-button').disabled, overflow:document.documentElement.scrollWidth>window.innerWidth})")
                prefs = api.get_bootstrap()["preferences"]
                report["theme_saved"] = prefs["theme"] == report["theme"] and original_theme != report["theme"]
                report["custom_model_saved"] = prefs.get("engine_settings", {}).get("openaicompatible", {}).get("openai_compatible_model") == "orca-custom-test"
                report["secret_not_saved"] = "orca-ui-test-secret" not in json.dumps(prefs)
                report["native_frame"] = chrome.diagnostics()
                bridge.window_toggle_maximize()
                time.sleep(0.3)
                report["maximize_works"] = chrome.state()["maximized"]
                bridge.window_toggle_maximize()
                time.sleep(0.3)
                report["restore_works"] = not chrome.state()["maximized"]
                bridge.window_minimize()
                time.sleep(0.2)
                report["minimize_works"] = chrome.state()["minimized"]
                window.restore()
                time.sleep(0.2)
                report["native_resize_available"] = chrome.diagnostics().get("native_resize_style")
                report["no_main_engine_entry"] = window.evaluate_js("document.getElementById('engine-summary') === null")
                report["queue_file_count"] = len(api.get_state().get("files", []))
                report["ok"] = (report["engineCount"] == 23 and report["settingsOpen"] and report["theme_saved"] and report["customModel"] and report["customUrl"] and report["custom_model_saved"] and report["secret_not_saved"] and not report["overflow"] and report["native_frame"].get("native_caption_removed") and report["native_frame"].get("client_fills_frame") and report["maximize_works"] and report["restore_works"] and report["minimize_works"] and report["native_resize_available"] and report["no_main_engine_entry"] and (report["translateEnabled"] and report["trialEnabled"] if os.environ.get("PDFMT_TEST_INPUT") else True))
                (DATA / "ui-test.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                (DATA / "ui-test.json").write_text(json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
            finally:
                window.destroy()
        window.events.loaded += lambda: threading.Thread(target=ui_probe, daemon=True).start()

    def closing():
        if api.is_busy():
            state = api.get_state()
            if state["status"] == "pausing":
                return False
            autosaving = (state.get("task") or {}).get("auto_save", False)
            message = ("下次可从最近保存的进度继续。确定退出吗？" if autosaving else
                       "此任务未开启自动保存。要保留续译进度，请先返回并点击“暂停并保存”。确定现在退出吗？")
            if not window.create_confirmation_dialog("翻译还在进行", message):
                return False
        bridge._reader.close()
        api.close()

    window.events.closing += closing
    webview.start(gui="edgechromium", debug=False, private_mode=True, icon=str(ROOT / "app.ico"))
    startup.close()
    return 0


def main():
    diagnostic_flags = {"--credential-cleanup-test", "--free-engine-test", "--engine-test", "--diagnostics", "--self-test", "--cancel-test", "--batch-test", "--index-test", "--ui-test", "--reader-test", "--floating-test", "--credential-ui-test-stage1", "--credential-ui-test-stage2"}
    if "--recovery-probe" in sys.argv or "--export-test" in sys.argv or diagnostic_flags.intersection(sys.argv):
        return run_application()
    from single_instance import SingleInstance
    instance = SingleInstance(DATA)
    try:
        if not instance.acquire():
            return 0
        if "--startup-test" in sys.argv:
            DATA.mkdir(parents=True, exist_ok=True)
            (DATA / "owner-started.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        return run_application(instance)
    finally:
        instance.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Unable to open desktop")
        import ctypes
        message = str(exc) if type(exc).__name__ == "SingleInstanceError" else f"软件启动失败。请确认已安装 Microsoft Edge WebView2 Runtime。\n详细日志：{DATA / 'logs' / 'desktop.log'}"
        ctypes.windll.user32.MessageBoxW(0, message, "ORCA", 0x10)
        raise SystemExit(1)
