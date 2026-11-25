"""Offline release checks for dynamic engine imports and SDK request paths.

Only run through the explicit --engine-test entry point. Fresh subprocesses
exercise the same frozen importer used by translation workers. Cloud sockets
are denied; test SDK traffic is redirected to a loopback HTTP fixture.
"""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import logging
import multiprocessing
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_KEY = "orca-release-diagnostic-fake-key-not-a-credential"
TRANSLATION = "本机模拟接口返回的测试译文"
MARKER_TRANSLATION = "平均值 {v1} 和 <style id='2'>心率</style>。"
EXPECTED_ENGINES = 23


def _deny_external_network():
    allowed = {"127.0.0.1", "::1", "localhost"}
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def address_ok(address):
        if not isinstance(address, tuple) or str(address[0]).lower() not in allowed:
            raise RuntimeError("External network access is disabled during engine diagnostics")

    def connect(sock, address):
        address_ok(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        address_ok(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if str(host).lower() not in allowed:
            raise RuntimeError("External DNS access is disabled during engine diagnostics")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"


def _fixture_options(key):
    from engine_registry import is_secret, metadata
    values = {}
    for name, field in metadata()[key].setting_model_type.model_fields.items():
        if is_secret(name):
            values[name] = FAKE_KEY + (":fx" if name == "deepl_auth_key" else "")
        elif name.endswith(("base_url", "endpoint", "host", "_url")):
            values[name] = "http://127.0.0.1:9/v1"
    if key == "zhipu":
        values["zhipu_model"] = "glm-4.7-flash"
    if key == "openaicompatible":
        values.update(openai_compatible_model="orca-custom-model-release-check", openai_compatible_timeout="5")
    return {"engine": key, "engine_settings": values, "lang_in": "en", "lang_out": "zh"}


def _settings(options):
    from engine_registry import build_engine
    from pdf2zh_next.config.model import SettingsModel, TranslationSettings
    settings = SettingsModel(translate_engine_settings=build_engine(options), translation=TranslationSettings(
        lang_in=options["lang_in"], lang_out=options["lang_out"], no_auto_extract_glossary=True,
        ignore_cache=True, qps=1))
    settings.validate_settings()
    return settings


def _resolve_catalog():
    from engine_registry import metadata
    from pathlib import Path
    from pdf2zh_next.translator.cache import db
    configured_data = Path(os.environ["PDFMT_DATA_DIR"]).resolve()
    cache_in_app_data = Path(db.database).resolve().is_relative_to(configured_data)
    rows = []
    for key, item in metadata().items():
        row = {"engine": key, "name": item.translate_engine_type, "ok": False}
        try:
            settings = _settings(_fixture_options(key))
            resolved = settings.translate_engine_settings.translate_engine_type
            if key == "indextranslate":
                assert not item.support_llm, "Index must use the paragraph translation path"
            row.update(ok=True, resolved_engine=resolved,
                resolved_module="pdf2zh_next.translator.translator_impl." + resolved.lower(),
                resolved_class=resolved + "Translator")
        except Exception as exc:
            row["error"] = _safe_error(exc)
        rows.append(row)
    return {"ok": cache_in_app_data and len(rows) == EXPECTED_ENGINES and all(row["ok"] for row in rows),
        "engines": rows, "cache_in_app_data": cache_in_app_data}


def _safe_error(exc):
    # Fixtures never receive user credentials. Suppress the fake values as well.
    return (type(exc).__name__ + ": " + str(exc)).replace(FAKE_KEY + ":fx", "[test-key]").replace(FAKE_KEY, "[test-key]")[:1500]


def _import_engine(spec):
    module = importlib.import_module(spec["resolved_module"])
    provider = getattr(module, spec["resolved_class"])
    from pdf2zh_next.translator.base_translator import BaseTranslator
    if not issubclass(provider, BaseTranslator):
        raise TypeError("Resolved class does not implement BaseTranslator")
    return {"ok": True, "module": spec["resolved_module"], "class": spec["resolved_class"],
        "frozen_importer": bool(getattr(sys, "frozen", False))}


class _FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            authorization = self.headers.get("Authorization", "")
            if self.path == "/v2/translate":
                assert authorization == "DeepL-Auth-Key " + FAKE_KEY + ":fx"
                assert body["source_lang"] == "EN" and body["target_lang"] == "ZH-HANS"
                response = {"translations": [{"detected_source_language": "EN", "text": TRANSLATION} for _ in body["text"]]}
            else:
                assert self.path == "/v1/chat/completions", self.path
                assert body.get("model")
                translated_text = TRANSLATION
                if self.server.engine == "indextranslate":
                    assert authorization == "", "Index must not send an API key"
                    assert self.headers.get("User-Agent") == "Index-Translate-Client/1.0"
                    assert body["model"] == "Index-Translate-35B-A3B"
                    assert body.get("chat_template_kwargs") == {"enable_thinking": False}
                    assert body.get("temperature") == 0.0 and body.get("stream") is False
                    assert body.get("max_tokens") == 4096
                    assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
                    prompt = body["messages"][0]["content"]
                    assert "中文" in prompt and "英语" in prompt
                    if "ORCA marker check" in prompt:
                        translated_text = MARKER_TRANSLATION
                else:
                    assert authorization == "Bearer " + FAKE_KEY
                response = {"id": "orca-local-diagnostic", "object": "chat.completion", "created": 1,
                    "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": translated_text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}}
            self.server.records.append({"path": self.path, "body": body})
            encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as exc:
            self.server.errors.append(_safe_error(exc))
            encoded = json.dumps({"error": "Invalid diagnostic request"}).encode()
            self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _loopback_test(key):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.daemon_threads = True
    server.engine = key
    server.records, server.errors = [], []
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:" + str(server.server_port)
    translated_clients = []
    try:
        options = _fixture_options(key)
        if key == "indextranslate":
            from pdf2zh_next.translator.translator_impl.indextranslate import IndexTranslateTranslator
            # Scoped to this test child; production endpoint remains fixed.
            IndexTranslateTranslator.ENDPOINT = base_url + "/v1/chat/completions"
        elif key == "deepl":
            import deepl
            original = deepl.Translator
            def local_deepl(auth_key, *args, **kwargs):
                assert auth_key == FAKE_KEY + ":fx"
                client = original(auth_key, server_url=base_url, max_retries=0, timeout_s=3)
                translated_clients.append(client)
                return client
            # This is test-child-local. Production endpoints are unchanged.
            deepl.Translator = local_deepl
        else:
            import openai
            original = openai.OpenAI
            def local_openai(*args, **kwargs):
                assert kwargs["api_key"] == FAKE_KEY
                kwargs["base_url"] = base_url + "/v1"
                kwargs["max_retries"] = 0
                kwargs["timeout"] = 3
                client = original(*args, **kwargs)
                translated_clients.append(client)
                return client
            openai.OpenAI = local_openai
        # Connection-test path: the upstream factory dynamically imports the
        # transformed implementation and sends its one Hello request.
        from pdf2zh_next.translator.utils import get_translator
        translator = get_translator(_settings(options))
        assert len(server.records) == 1 and not server.errors, server.errors
        # Reading path: use ORCA's actual provider factory, then translate an
        # ordinary selection. This must not add another Hello request.
        from reader_service import _worker_factory
        reading_translator = _worker_factory(options)()
        translated = reading_translator.translate("A selected scientific paragraph.", ignore_cache=True)
        assert translated == TRANSLATION
        assert len(server.records) == 2 and not server.errors, server.errors
        assert "Hello" in json.dumps(server.records[0]["body"])
        assert "A selected scientific paragraph." in json.dumps(server.records[1]["body"])
        if key == "zhipu":
            assert server.records[1]["body"]["model"] == "glm-4.7-flash"
        if key == "openaicompatible":
            assert server.records[1]["body"]["model"] == "orca-custom-model-release-check"
        if key == "indextranslate":
            source = "ORCA marker check: Mean {v1} and <style id='2'>heart rate</style>."
            assert reading_translator.translate(source, ignore_cache=True) == MARKER_TRANSLATION
            assert len(server.records) == 3 and not server.errors, server.errors
            translated_clients.extend((translator.session, reading_translator.session))
        return {"ok": True, "engine": key, "connection_factory": True, "reader_factory": True,
            "requests": len(server.records), "translation_matched": True, "network": "loopback-only",
            **({"no_api_key": True, "markers_preserved": True} if key == "indextranslate" else {})}
    finally:
        for client in translated_clients:
            close = getattr(client, "close", None)
            if close:
                close()
        server.shutdown()
        server.server_close()
        thread.join(1)


def _child(mode, payload, pipe):
    logging.disable(logging.CRITICAL)
    try:
        _deny_external_network()
        if mode == "catalog":
            result = _resolve_catalog()
        elif mode == "import":
            result = _import_engine(payload)
        elif mode == "loopback":
            result = _loopback_test(payload)
        else:
            raise ValueError("Unknown engine diagnostic mode")
        pipe.send(result)
    except Exception as exc:
        pipe.send({"ok": False, "error": _safe_error(exc)})
    finally:
        pipe.close()


def _run_child(mode, payload=None, timeout=25):
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(mode, payload, send), daemon=True)
    started = time.monotonic()
    try:
        process.start()
        send.close()
        try:
            if receive.poll(timeout):
                result = receive.recv()
            else:
                result = {"ok": False, "error": "Engine diagnostic timed out"}
        except (EOFError, OSError):
            result = {"ok": False, "error": "Engine diagnostic process exited before reporting"}
    finally:
        send.close()
        receive.close()
        if process.pid is not None:
            process.join(.2)
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(1)
        process.close()
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def run_engine_diagnostics():
    """Return a JSON-compatible release report without contacting providers."""
    started = time.monotonic()
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "external_network": "blocked-in-test-children",
        "scope": "configuration-and-imports; five loopback API tests; no live account verification"}
    catalog = _run_child("catalog")
    report["engines"] = catalog.get("engines", [])
    report["cache_in_app_data"] = catalog.get("cache_in_app_data", False)
    report["catalog_count"] = len(report["engines"])
    if not catalog.get("ok"):
        report["error"] = catalog.get("error", "One or more engine configurations failed")
        return report
    specs = {row["resolved_module"]: row for row in report["engines"]}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(_run_child, "import", spec) for name, spec in specs.items()}
        report["modules"] = [{"module": name, **future.result()} for name, future in futures.items()]
    report["unique_module_count"] = len(report["modules"])
    results = {row["module"]: row for row in report["modules"]}
    for row in report["engines"]:
        check = results[row["resolved_module"]]
        row["ok"] = check["ok"]
        if not check["ok"]:
            row["error"] = check.get("error", "Import failed")
    # These are in separate fresh children as well, protecting each check from
    # another SDK's previously imported DLLs or test-local endpoint override.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {key: pool.submit(_run_child, "loopback", key, 30) for key in ("deepl", "openaicompatible", "zhipu", "modelscope", "indextranslate")}
        report["loopback_tests"] = [{"engine": key, **future.result()} for key, future in futures.items()]
    report["ok"] = all(row["ok"] for row in report["engines"] + report["loopback_tests"])
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return report
