"""Exercise custom OpenAI-compatible services against a loopback-only stub.

No cloud service is contacted and the credential below is deliberately fake.
The test includes real upstream adapter calls and spawned connection probes.
"""
import json
import multiprocessing
import os
from pathlib import Path
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data" / "custom-engine"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache"))
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]

MODEL = "orca-custom-model-not-in-any-preset-v17"
FAKE_KEY = "orca-loopback-test-only-not-a-real-api-key"
TRANSLATION = "本地测试译文"


class LocalService(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), LocalHandler)
        self.requests_seen = queue.Queue()
        self.slow_seen = threading.Event()
        self.release_slow = threading.Event()


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.server.requests_seen.put({
            "path": self.path,
            "body": body,
            "credential_matched": self.headers.get("Authorization") == "Bearer " + FAKE_KEY,
        })
        if self.path == "/slow/v1/chat/completions":
            self.server.slow_seen.set()
            self.server.release_slow.wait(20)
        if self.path == "/reject/v1/chat/completions":
            response = {"error": {"message": "Rejected test credential " + FAKE_KEY, "type": "authentication_error", "code": "invalid_api_key"}}
            status = 401
        else:
            response = {
                "id": "chatcmpl-local-orca-test",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": TRANSLATION}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            }
            status = 200
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Expected when the timeout probe is deliberately terminated.
            pass


def options_for(service, route=""):
    return {
        "engine": "openaicompatible",
        "engine_settings": {
            "openai_compatible_base_url": f"http://127.0.0.1:{service.server_port}{route}/v1",
            "openai_compatible_model": MODEL,
            "openai_compatible_api_key": FAKE_KEY,
            "openai_compatible_timeout": "15",
            "openai_compatible_send_temperature": True,
            "openai_compatible_temperature": "0.25",
        },
    }


def next_request(service, path):
    record = service.requests_seen.get(timeout=3)
    assert record["path"] == path, record
    assert record["credential_matched"], "Custom API credential did not reach the local service."
    assert record["body"]["model"] == MODEL, record
    assert record["body"]["temperature"] == 0.25, record
    return record


def assert_no_children(baseline):
    remaining = {child.pid for child in multiprocessing.active_children()}
    assert remaining <= baseline, f"Connection probe left child processes: {remaining - baseline}"


def main():
    from engine_registry import build_engine, test_connection
    from pdf2zh_next.config.model import SettingsModel, TranslationSettings
    from pdf2zh_next.translator.utils import get_translator

    baseline = {child.pid for child in multiprocessing.active_children()}
    service = LocalService()
    server_thread = threading.Thread(target=service.serve_forever, daemon=True)
    server_thread.start()
    translator = None
    try:
        options = options_for(service)
        settings = SettingsModel(
            translate_engine_settings=build_engine(options),
            translation=TranslationSettings(lang_in="en", lang_out="zh", no_auto_extract_glossary=True, qps=1),
        )
        settings.validate_settings()
        assert settings.translate_engine_settings.translate_engine_type == "OpenAI"
        assert settings.translate_engine_settings.openai_base_url == options["engine_settings"]["openai_compatible_base_url"]
        translator = get_translator(settings)
        hello = next_request(service, "/v1/chat/completions")
        assert any("Hello" in str(message.get("content", "")) for message in hello["body"]["messages"])
        value = translator.translate("An arbitrary private model should translate this sentence.", ignore_cache=True)
        assert value == TRANSLATION, value
        next_request(service, "/v1/chat/completions")

        # The public registry probe spawns a fresh interpreter and should send
        # exactly the one health-check request already performed upstream.
        connected = test_connection(options, timeout=20)
        assert connected.get("ok"), connected
        next_request(service, "/v1/chat/completions")
        assert service.requests_seen.empty(), "Connection test sent redundant translation requests."
        assert_no_children(baseline)

        rejected = test_connection(options_for(service, "/reject"), timeout=20)
        assert not rejected.get("ok"), rejected
        assert FAKE_KEY not in json.dumps(rejected), "Credential leaked in connection-test error."
        next_request(service, "/reject/v1/chat/completions")
        assert_no_children(baseline)

        started = time.monotonic()
        timed_out = test_connection(options_for(service, "/slow"), timeout=8)
        elapsed = time.monotonic() - started
        assert service.slow_seen.is_set(), "Slow-service probe never reached the local service."
        assert not timed_out.get("ok") and "超时" in timed_out.get("error", ""), timed_out
        assert elapsed < 12, f"Probe did not respect the total timeout: {elapsed:.2f}s"
        next_request(service, "/slow/v1/chat/completions")
        assert_no_children(baseline)
        print("PASS: custom endpoint/model/temperature/credential mapping, upstream translation result, one-request spawned connection probe, redacted authentication failure, bounded timeout with no surviving child.")
    finally:
        if translator is not None:
            translator.client.close()
        service.release_slow.set()
        service.shutdown()
        service.server_close()
        server_thread.join(2)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
