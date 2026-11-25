"""Real PDF/range and spawned text-worker checks; all APIs are loopback stubs."""
import concurrent.futures
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "vendor"), str(ROOT / "scripts")]
os.environ.setdefault("PDFMT_DATA_DIR", str(ROOT / "test-data" / "reader-backend"))
os.environ.setdefault("PDFMT_CACHE_DIR", str(Path(os.environ["PDFMT_DATA_DIR"]) / "cache"))
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]


def http(url, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def main():
    import pymupdf
    import reader_service
    from reader_service import ReaderService, MAX_SELECTION
    from test_custom_engine import LocalService, options_for, next_request, assert_no_children, TRANSLATION, FAKE_KEY

    baseline = {p.pid for p in multiprocessing.active_children()}
    stub = LocalService()
    stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    stub_thread.start()
    service = ReaderService(Path(os.environ["PDFMT_DATA_DIR"]))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    try:
        directory = Path(os.environ["PDFMT_DATA_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            pdf_path = Path(temporary) / "scientific article.pdf"
            pdf = pymupdf.open()
            pdf.new_page().insert_text((72, 72), "The recovery period improves performance.")
            pdf.new_page().insert_text((72, 72), "Second page.")
            pdf.save(pdf_path)
            pdf.close()
            raw = pdf_path.read_bytes()
            opened = service.open_file(pdf_path)
            assert opened["ok"], opened
            document = opened["document"]
            assert document["pages"] == 2 and document["has_text"]
            assert "path" not in document and str(pdf_path) not in json.dumps(document)
            url = document["url"]
            status, headers, body = http(url)
            assert status == 200 and body == raw
            assert headers["Content-Length"] == str(len(raw)) and headers["Accept-Ranges"] == "bytes"
            assert headers["Access-Control-Allow-Origin"] == "*"
            assert http(url, "HEAD")[2] == b""
            assert http(url, headers={"Range": "bytes=10-39"})[2] == raw[10:40]
            assert http(url, headers={"Range": "bytes=-12"})[2] == raw[-12:]
            assert http(url, headers={"Range": "bytes=20-"})[2] == raw[20:]
            assert http(url, headers={"Range": "bytes=0-99999999"})[2] == raw
            status, headers, body = http(url, headers={"Range": "bytes=10-39"})
            assert status == 206 and headers["Content-Range"] == f"bytes 10-39/{len(raw)}"
            for invalid_range in ("bytes=99999999-", "bytes=4-2", "bytes=1-2,5-9", "bytes=-0", "bytes=-"):
                assert http(url, headers={"Range": invalid_range})[0] == 416
            origin = url.split("/pdf/")[0]
            for invalid in ("/", "/pdf/not-the-token.pdf", "/../reader_service.py", "/pdf/" + str(pdf_path), url[len(origin):] + "?path=" + str(pdf_path)):
                assert http(origin + urllib.parse.quote(invalid, safe="/:?="))[0] == 404

            def payload(request_id, text="A selected paragraph.", route="", doc=document):
                return {**options_for(stub, route), "document_id": doc["id"], "request_id": request_id, "text": text, "lang_in": "en", "lang_out": "zh"}

            result = service.translate(payload(1))
            assert result["ok"] and result["translation"] == TRANSLATION, result
            assert not result["cache_hit"] and not result["worker_reused"], result
            assert result["worker_setup_ms"] >= 0 and result["translation_ms"] >= 0 and result["total_ms"] >= result["translation_ms"]
            record = next_request(stub, "/v1/chat/completions")
            assert any("A selected paragraph." in str(m.get("content")) for m in record["body"]["messages"])
            assert stub.requests_seen.empty(), "A redundant Hello health-check was sent."
            first_pid = service._worker.process.pid
            assert {p.pid for p in multiprocessing.active_children()} - baseline == {first_pid}

            reused = service.translate(payload(2, "A second paragraph using the same session."))
            assert reused["ok"] and reused["worker_reused"] and not reused["cache_hit"], reused
            assert service._worker.process.pid == first_pid
            next_request(stub, "/v1/chat/completions")
            cached = service.translate(payload(3))
            assert cached["ok"] and cached["cache_hit"] and cached["translation_ms"] == 0, cached
            assert stub.requests_seen.empty(), "Cached text triggered another provider request."

            changed_model = payload(4)
            changed_model["engine_settings"]["openai_compatible_model"] = "different-model"
            changed = service.translate(changed_model)
            assert changed["ok"] and not changed["cache_hit"] and not changed["worker_reused"]
            assert stub.requests_seen.get(timeout=3)["body"]["model"] == "different-model"
            assert service._worker.process.pid != first_pid
            changed_key = payload(5)
            changed_key["engine_settings"]["openai_compatible_api_key"] = FAKE_KEY + "-second"
            changed = service.translate(changed_key)
            assert changed["ok"] and not changed["cache_hit"]
            assert not stub.requests_seen.get(timeout=3)["credential_matched"]
            changed = service.translate(payload(6, route="/alternate"))
            assert changed["ok"] and not changed["cache_hit"]
            next_request(stub, "/alternate/v1/chat/completions")
            changed_language = payload(7)
            changed_language["lang_out"] = "fr"
            changed = service.translate(changed_language)
            assert changed["ok"] and not changed["cache_hit"]
            next_request(stub, "/v1/chat/completions")
            assert FAKE_KEY not in repr(service._cache.keys())

            prepared = service.prepare(options_for(stub, "/warm"))
            assert prepared["ok"] and not prepared["worker_reused"], prepared
            assert stub.requests_seen.empty(), "Warmup made a network or Hello request."
            warm_pid = service._worker.process.pid
            warm = service.translate(payload(8, route="/warm"))
            assert warm["ok"] and warm["worker_reused"] and service._worker.process.pid == warm_pid
            next_request(stub, "/warm/v1/chat/completions")
            assert service.prepare(options_for(stub, "/warm"))["worker_reused"]
            assert stub.requests_seen.empty()
            print("TIMINGS", json.dumps({name: {field: response[field] for field in ("worker_setup_ms", "translation_ms", "total_ms", "cache_hit", "worker_reused")} for name, response in (("cold", result), ("reused", reused), ("cache", cached), ("warmed", warm))}))

            reader_service.CACHE_ENTRIES = 3
            for index in range(9, 13):
                bounded = service.translate(payload(index, f"Cache entry {index}.", "/warm"))
                assert bounded["ok"]
                next_request(stub, "/warm/v1/chat/completions")
                assert len(service._cache) <= 3 and service._cache_characters <= reader_service.CACHE_CHARACTERS
            reader_service.CACHE_ENTRIES = 64
            assert service.translate(payload(1))["cancelled"]
            assert not service.translate(payload(20, "x" * (MAX_SELECTION + 1)))["ok"]
            assert stub.requests_seen.empty(), "Invalid or stale selection made an API request."

            old = pool.submit(service.translate, payload(20, "A slow old paragraph.", "/slow"))
            assert stub.slow_seen.wait(15), "Old request did not reach local server."
            next_request(stub, "/slow/v1/chat/completions")
            current = pool.submit(service.translate, payload(21, "New selected paragraph."))
            service.cancel(20)  # A stale cancel must not cancel the new selection.
            assert old.result(timeout=6)["cancelled"]
            latest = current.result(timeout=15)
            assert latest["ok"] and latest["request_id"] == 21, latest
            next_request(stub, "/v1/chat/completions")
            assert {p.pid for p in multiprocessing.active_children()} - baseline == {service._worker.process.pid}

            stub.slow_seen.clear()
            pending = pool.submit(service.translate, payload(22, "Explicit cancellation.", "/slow"))
            assert stub.slow_seen.wait(15)
            next_request(stub, "/slow/v1/chat/completions")
            started = time.monotonic()
            service.cancel(22)
            assert pending.result(timeout=5)["cancelled"]
            assert time.monotonic() - started < 5
            assert_no_children(baseline)

            rejected = service.translate(payload(23, "Rejected fake key.", "/reject"))
            assert not rejected["ok"] and FAKE_KEY not in json.dumps(rejected), rejected
            next_request(stub, "/reject/v1/chat/completions")
            assert {p.pid for p in multiprocessing.active_children()} - baseline == {service._worker.process.pid}

            service.timeout = 8
            stub.slow_seen.clear()
            started = time.monotonic()
            timed_out = service.translate(payload(24, "Timeout paragraph.", "/slow"))
            assert stub.slow_seen.is_set()
            assert not timed_out["ok"] and "超时" in timed_out["error"], timed_out
            assert time.monotonic() - started < 12
            next_request(stub, "/slow/v1/chat/completions")
            assert_no_children(baseline)
            service.timeout = 30

            # Unexpected worker death on an idle or busy session must not poison
            # future requests or leave a response waiting forever.
            recovered = service.translate(payload(25, "Ready before idle crash."))
            assert recovered["ok"]
            next_request(stub, "/v1/chat/completions")
            service._worker.process.terminate()
            service._worker.process.join(3)
            recovered = service.translate(payload(26, "Recovered after idle crash."))
            assert recovered["ok"] and not recovered["worker_reused"], recovered
            next_request(stub, "/v1/chat/completions")
            stub.slow_seen.clear()
            crashing = pool.submit(service.translate, payload(27, "Busy crash.", "/slow"))
            assert stub.slow_seen.wait(15)
            next_request(stub, "/slow/v1/chat/completions")
            service._worker.process.terminate()
            failed = crashing.result(timeout=5)
            assert not failed["ok"] and "退出" in failed["error"], failed
            assert_no_children(baseline)
            recovered = service.translate(payload(28, "Recovered after busy crash."))
            assert recovered["ok"]
            next_request(stub, "/v1/chat/completions")

            stub.slow_seen.clear()
            closing_doc = pool.submit(service.translate, payload(29, "The document will change.", "/slow"))
            assert stub.slow_seen.wait(15)
            next_request(stub, "/slow/v1/chat/completions")
            replacement = service.open_file(pdf_path)["document"]
            assert replacement["id"] != document["id"] and http(url)[0] == 404
            assert closing_doc.result(timeout=5)["cancelled"]
            assert service.translate(payload(30))["cancelled"]
            assert http(replacement["url"])[0] == 200
            assert not service._cache and service._cache_characters == 0
            assert_no_children(baseline)

            after_open = service.translate(payload(31, doc=replacement))
            assert after_open["ok"] and not after_open["cache_hit"]
            next_request(stub, "/v1/chat/completions")
            service.close_document()
            assert_no_children(baseline)
            assert not service._cache and service._cache_characters == 0
            replacement = service.open_file(pdf_path)["document"]

            service.cancel(1000)
            assert service.translate(payload(1000, doc=replacement))["cancelled"]

            stub.slow_seen.clear()
            closing_app = pool.submit(service.translate, payload(1001, "Close with live worker.", "/slow", replacement))
            assert stub.slow_seen.wait(15)
            next_request(stub, "/slow/v1/chat/completions")
            service.close()
            assert closing_app.result(timeout=5)["cancelled"]
            assert_no_children(baseline)
            assert service.translate(payload(1002, doc=replacement))["cancelled"]
        print("PASS: PDF scope/ranges, persistent worker reuse, zero-network warmup, bounded memory cache with model/key/URL/language separation, exact text/no Hello, latest selection/cancel floor, timeout/crash recovery, document/app close cleanup.")
    finally:
        service.close()
        stub.release_slow.set()
        pool.shutdown(wait=True, cancel_futures=True)
        stub.shutdown()
        stub.server_close()
        stub_thread.join(2)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
