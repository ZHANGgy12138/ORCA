"""Offline adapter contract: real HTTP session, no Microsoft requests."""
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]


def main():
    import requests
    from reader_bing import ReaderBingTranslator

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/translator"
            self.server.gets += 1
            token = "session-" + str(self.server.gets)
            self.server.token = token
            body = ('{"ig":"test-ig"}<div data-iid="test-iid"></div> params_AbusePreventionHelper = [123,"' + token + '",3600];').encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            self.server.posts.append(data)
            status = self.server.status
            if status == 401:
                self.server.status = 200
            value = [{"translations": [{"text": "译文:" + data["text"][0]}]}] if status == 200 else {"statusCode": status}
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.gets, server.posts, server.status = 0, [], 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Avoid unrelated upstream cache setup: exercise the adapter over actual
    # HTTP using precisely the state established by its parent constructor.
    adapter = ReaderBingTranslator.__new__(ReaderBingTranslator)
    adapter.session = requests.Session()
    adapter.session.trust_env = False
    adapter.endpoint = f"http://127.0.0.1:{server.server_port}/translator"
    adapter.headers, adapter.lang_in, adapter.lang_out = {}, "en", "zh-Hans"
    adapter._reader_tokens, adapter._reader_tokens_at = None, 0
    waits = []
    adapter.rate_limiter = type("Limiter", (), {"wait": lambda self, value: waits.append(value)})()
    try:
        assert adapter.do_translate("First paragraph.") == "译文:First paragraph."
        assert adapter.do_translate("Second paragraph.") == "译文:Second paragraph."
        assert server.gets == 1 and len(server.posts) == 2
        assert server.posts[0]["token"] == server.posts[1]["token"]
        adapter._reader_tokens_at = 0
        adapter.do_translate("Refresh age.")
        assert server.gets == 2
        server.status = 401
        adapter.do_translate("Refresh rejected token.")
        assert server.gets == 3 and len(server.posts) == 5
        before = (server.gets, len(server.posts))
        server.status = 429
        try:
            adapter.do_translate("Rate limited.")
            raise AssertionError("Expected rate-limit error")
        except ValueError as error:
            assert "限制" in str(error)
        assert (server.gets, len(server.posts)) == (before[0], before[1] + 1)
        server.status = 200
        text = "A scientific paragraph. " * 100
        offset = len(server.posts)
        result = adapter.do_translate(text)
        sent = [item["text"][0] for item in server.posts[offset:]]
        assert "".join(sent) == text and all(len(chunk) <= 900 for chunk in sent)
        assert len(waits) == len(sent) - 1
        assert "译文:" in result
        print("PASS: one page GET across multiple translations, expiry refresh, one authentication refresh, no retry on429, full long-text chunking.")
    finally:
        adapter.session.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


if __name__ == "__main__":
    main()
