"""Exercise Index's actual request protocol against a local HTTP fixture."""
import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data/index-unit"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache/babeldoc"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"


def main():
    from engine_registry import build_engine, get_catalog
    from pdf2zh_next.config.model import SettingsModel, TranslationSettings
    from pdf2zh_next.translator.translator_impl.indextranslate import (
        IndexTranslateTranslator, MAX_PROTECTED_CHUNK, SOFT_CHUNK, split_preserving_markers, validate_markers,
    )

    waits = []
    limiter = type("Limiter", (), {"wait": lambda self, params: waits.append(params)})()
    settings = SettingsModel(translate_engine_settings=build_engine({"engine": "indextranslate"}),
        translation=TranslationSettings(lang_in="en", lang_out="zh", qps=1, ignore_cache=True))
    settings.validate_settings()
    row = next(item for item in get_catalog() if item["id"] == "indextranslate")
    assert not row["fields"] and row["category"] == "免费服务"
    assert settings.translate_engine_settings.support_llm == "no"
    assert settings.term_extraction_engine_settings is None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append((dict(self.headers), body))
            mode = self.server.mode
            if mode == "rate":
                status, result = 429, {"error": {"message": "limited"}}
            elif mode == "server":
                status, result = 503, {"error": {"message": "unavailable"}}
            elif mode == "malformed":
                status, result = 200, {"not_choices": []}
            else:
                source = body["messages"][0]["content"].split("\n\n", 1)[1]
                output = source if mode == "echo" else "本机译文"
                if mode == "markers":
                    output = "均值为 {v1}，<style id='2'>心率</style>。"
                if mode == "missing-marker":
                    output = "均值和心率。"
                if mode == "empty":
                    output = ""
                if mode == "think":
                    output = "<think>Only reasoning</think>"
                if mode == "partial-think":
                    output = "<think>Only reasoning"
                if mode == "prefix-think":
                    output = "<think>Reasoning</think>本机译文"
                status, result = 200, {"choices": [{"finish_reason": "length" if mode == "truncated" else "stop", "message": {"content": output}}]}
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests, server.mode = [], "normal"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    translator = IndexTranslateTranslator(settings, limiter)
    translator.ENDPOINT = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        with patch("requests.sessions.get_netrc_auth", return_value=("unexpected", "private")) as netrc:
            assert translator.translate("A normal paragraph.", ignore_cache=True) == "本机译文"
            netrc.assert_not_called()
        headers, body = server.requests[-1]
        assert headers["User-Agent"] == "Index-Translate-Client/1.0"
        assert "Authorization" not in headers
        assert body["model"] == "Index-Translate-35B-A3B"
        assert body["temperature"] == 0 and body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["max_tokens"] == 4096 and body["stream"] is False
        assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
        assert "英语" in body["messages"][0]["content"] and "中文" in body["messages"][0]["content"]
        translator.raise_if_failed()

        marker_text = "Mean {v1} and <style id='2'>heart rate</style>."
        server.mode = "markers"
        assert "{v1}" in translator.translate(marker_text, ignore_cache=True)
        assert "硬性要求" in server.requests[-1][1]["messages"][0]["content"]
        for invalid in ("<style id='2'>心率</style> {v1}", "{v1} {v1} <style id='2'>心率</style>"):
            try:
                validate_markers(marker_text, invalid)
                raise AssertionError("Invalid marker output accepted")
            except ValueError:
                pass

        # The exact text survives all chunks, including whitespace, markers,
        # and one protected span larger than the soft chunk threshold.
        server.mode = "echo"
        long = " First paragraph. " * 160 + "<style id='7'>" + "Styled text. " * 170 + "</style> {v99}\n" + "Tail. " * 200
        chunks = split_preserving_markers(long)
        assert len(chunks) > 2 and "".join(chunks) == long
        assert all(len(chunk) <= MAX_PROTECTED_CHUNK for chunk in chunks)
        before = len(server.requests)
        assert translator.translate(long, ignore_cache=True) == long
        assert len(server.requests) - before == len([chunk for chunk in chunks if chunk.strip()])
        assert len(split_preserving_markers("中" * 6000)) == 4
        assert all(len(chunk) <= SOFT_CHUNK for chunk in split_preserving_markers("word " * 1400))
        decimal_chunks = split_preserving_markers("A " * 895 + "value 3.14159 should not be split. " * 40)
        assert sum(chunk.count("3.14159") for chunk in decimal_chunks) == 40

        for mode in ("rate", "server", "malformed", "empty", "think", "partial-think", "truncated", "missing-marker"):
            server.mode = mode
            before = len(server.requests)
            try:
                translator.translate(marker_text if mode == "missing-marker" else "Test input", ignore_cache=True)
                raise AssertionError("Invalid result accepted: " + mode)
            except (ValueError, __import__("requests").HTTPError):
                pass
            assert len(server.requests) == before + 1, "Unexpected automatic retry"
        assert translator.failed_count == 8
        try:
            translator.raise_if_failed()
            raise AssertionError("Whole-PDF failure was not propagated")
        except ValueError as error:
            assert "8" in str(error)
        server.mode = "prefix-think"
        assert translator.translate("Short input", ignore_cache=True) == "本机译文"
        before = len(server.requests)
        try:
            translator.translate("<style id='3'>" + "x" * 6500 + "</style>", ignore_cache=True)
            raise AssertionError("Oversized protected block accepted")
        except ValueError:
            pass
        assert len(server.requests) == before
        print("PASS: credential-free catalog and paragraph routing; official HTTP/body; marker integrity; exact long-text chunks; truncation/empty/reasoning/errors rejected; no automatic429retry; final PDF error propagation")
    finally:
        translator.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


if __name__ == "__main__":
    main()
