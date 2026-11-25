"""Background startup lifecycle and import boundaries; no external requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data" / "startup-service"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache" / "babeldoc"))


def wait_for(check, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if check():
            return
        time.sleep(.01)
    raise AssertionError("Expected startup transition did not arrive")


class FakeApi:
    def __init__(self, gate=None, failure=None):
        self.window = None
        self.closed = 0
        self.bootstrap_calls = 0
        self.gate = gate
        self.failure = failure

    def get_bootstrap(self):
        self.bootstrap_calls += 1
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.failure:
            raise self.failure
        return {"version": "test", "engines": [{"id": "bing"}], "preferences": {"theme": "dark"}, "history": []}

    def close(self):
        self.closed += 1


def lifecycle_checks():
    from startup_service import StartupService
    gate, entered = threading.Event(), threading.Event()
    created = []

    def factory(*args):
        entered.set()
        assert gate.wait(5)
        api = FakeApi()
        created.append(api)
        return api

    service = StartupService(ROOT, ROOT, ROOT / "test-data/startup-service", "test", factory)
    assert not entered.is_set() and service.api is None
    assert service.get_state()["status"] == "waiting" and service.get_state()["attempt"] == 0
    window = object()
    started = time.monotonic()
    assert service.start(window)["ok"]
    assert time.monotonic() - started < .5
    assert entered.wait(1)
    assert service.get_state()["status"] == "loading" and service.api is None
    assert service.get_state()["stage"] == "正在读取设置"
    assert not service.bootstrap()["ok"]
    assert service.start(object())["attempt"] == 1
    assert not service.retry()["ok"]
    gate.set()
    wait_for(lambda: service.get_state()["status"] == "ready")
    assert service.api is created[0] and service.api.window is window
    first = service.bootstrap()
    first["preferences"]["theme"] = "mutated"
    assert service.bootstrap()["preferences"]["theme"] == "dark"
    assert service.api.bootstrap_calls == 1
    final_elapsed = service.get_state()["elapsed_seconds"]
    time.sleep(.12)
    assert service.get_state()["elapsed_seconds"] == final_elapsed
    service.close()
    service.close()
    assert created[0].closed == 1 and service.api is None
    assert service.get_state()["status"] == "closed" and not service.retry()["ok"]
    assert not service.start(window)["ok"] and not service.bootstrap()["ok"]

    # A failed candidate is closed before retry can create another instance.
    failed = FakeApi(failure=RuntimeError("private-api-key-in-error /private/path"))
    recovered = FakeApi()
    calls = []
    def failing_factory(*args):
        calls.append(1)
        if len(calls) > 1:
            assert failed.closed == 1
        return failed if len(calls) == 1 else recovered
    service = StartupService(ROOT, ROOT, ROOT, "test", failing_factory)
    service.start(window)
    wait_for(lambda: service.get_state()["status"] == "error")
    assert failed.closed == 1 and service.api is None
    assert "private" not in json.dumps(service.get_state())
    assert service.retry()["ok"]
    wait_for(lambda: service.get_state()["status"] == "ready")
    assert service.get_state()["attempt"] == 2 and service.api is recovered
    service.close()
    assert recovered.closed == 1

    # Closing while construction runs must return promptly, suppress publication,
    # and dispose the late object without ever loading its bootstrap data.
    gate, entered = threading.Event(), threading.Event()
    late = FakeApi()
    def delayed_factory(*args):
        entered.set()
        assert gate.wait(5)
        return late
    service = StartupService(ROOT, ROOT, ROOT, "test", delayed_factory)
    service.start(window)
    assert entered.wait(1)
    started = time.monotonic()
    service.close()
    assert time.monotonic() - started < .5
    gate.set()
    wait_for(lambda: late.closed == 1)
    assert not late.bootstrap_calls and service.api is None
    assert service.get_state()["status"] == "closed"

    # Closing while bootstrap runs has the same ownership and late-result rules.
    gate = threading.Event()
    late = FakeApi(gate=gate)
    service = StartupService(ROOT, ROOT, ROOT, "test", lambda *args: late)
    service.start(window)
    wait_for(lambda: late.bootstrap_calls == 1)
    assert service.get_state()["stage"] == "正在准备翻译功能"
    service.close()
    gate.set()
    wait_for(lambda: late.closed == 1)
    assert service.api is None and not service.bootstrap()["ok"]
    print("PASS: no eager initialization, real stages, nonblocking start, cached bootstrap, retry, sanitized errors, close during initialization and bootstrap")


def import_checks():
    # A fresh interpreter prevents previously loaded modules masking this test.
    script = r'''
import json, pickle, sys, time
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root),str(root/'vendor')]
started=time.perf_counter()
import startup_service
assert 'desktop_api' not in sys.modules and 'pdf2zh_next' not in sys.modules
from engine_registry import get_catalog
catalog=get_catalog()
assert len(catalog)==23
for name in ('pdf2zh_next.high_level','pdf2zh_next.config.main','cv2','scipy','sklearn','numpy','pymupdf','onnxruntime'):
    assert name not in sys.modules, name
from pdf2zh_next import BingSettings, IndexTranslateSettings, SettingsModel
from pdf2zh_next.config import BasicSettings, TRANSLATION_ENGINE_METADATA
from pdf2zh_next.config.translate_engine_model import BingSettings as DefinedBing
from pdf2zh_next.config.model import SettingsModel as DefinedSettings
assert BingSettings is DefinedBing and SettingsModel is DefinedSettings
assert pickle.loads(pickle.dumps(BingSettings())).__class__ is BingSettings
assert 'pdf2zh_next.high_level' not in sys.modules
light_seconds=round(time.perf_counter()-started,3)
from pdf2zh_next import do_translate_async_stream, create_babeldoc_config
from pdf2zh_next import high_level
assert do_translate_async_stream is high_level.do_translate_async_stream
assert create_babeldoc_config is high_level.create_babeldoc_config
from pdf2zh_next.config import ConfigManager
from pdf2zh_next.config.main import ConfigManager as DefinedManager
assert ConfigManager is DefinedManager
print(json.dumps({'catalog_count':len(catalog),'light_catalog_seconds':light_seconds,'public_exports_compatible':True}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(ROOT)], cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8", timeout=45)
    if result.returncode:
        raise AssertionError(result.stderr[-3000:] or result.stdout[-3000:])
    print("PASS: lazy import boundary and original export identity " + result.stdout.strip())


if __name__ == "__main__":
    lifecycle_checks()
    import_checks()
