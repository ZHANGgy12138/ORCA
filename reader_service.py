"""Local PDF reading and bounded, independent selection translation.

The loopback server exposes only the currently selected PDF via a random URL.
Selection translation never changes the full-document queue or writes selections
or credentials to ORCA preferences/history.
"""
from dataclasses import dataclass, field
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import importlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import secrets
import threading
import time

MAX_SELECTION = 6000
TRANSLATION_TIMEOUT = 30
CACHE_ENTRIES = 64
CACHE_CHARACTERS = 400000


def _worker_factory(options):
    """Resolve a provider without invoking a constructor or making requests."""
    import truststore
    truststore.inject_into_ssl()
    from desktop_compat import install
    install()
    from engine_registry import build_engine, metadata
    from pdf2zh_next.config.model import SettingsModel, TranslationSettings
    from pdf2zh_next.translator.rate_limiter.qps_rate_limiter import QPSRateLimiter
    settings = SettingsModel(
        translate_engine_settings=build_engine(options),
        translation=TranslationSettings(lang_in=options.get("lang_in", "en"), lang_out=options.get("lang_out", "zh"),
            no_auto_extract_glossary=True, qps=2, ignore_cache=True),
    )
    settings.validate_settings()
    config = settings.translate_engine_settings
    item = next(m for m in metadata().values() if isinstance(config, m.setting_model_type))
    name = item.translate_engine_type
    if name == "Bing":
        from reader_bing import ReaderBingTranslator
        provider = ReaderBingTranslator
    else:
        module = importlib.import_module("pdf2zh_next.translator.translator_impl." + name.lower())
        provider = getattr(module, name + "Translator")
    # Calling get_translator here would perform a redundant Hello request.
    return lambda: provider(settings, QPSRateLimiter(2))


def _translate_worker(options, pipe):
    """A single provider session, alive until cancelled or configuration changes."""
    logging.disable(logging.CRITICAL)
    import contextlib
    with open(os.devnull, "w", encoding="utf-8") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        try:
            from engine_registry import redact, secret_values, service_error
            factory = _worker_factory(options)
            translator = None
            pipe.send({"kind": "ready", "ok": True})
            while True:
                command = pipe.recv()
                if command.get("kind") == "close":
                    return
                if command.get("kind") != "translate":
                    continue
                setup_ms = 0.0
                translated_at = time.monotonic()
                try:
                    if translator is None:
                        setup_at = time.monotonic()
                        translator = factory()
                        setup_ms = (time.monotonic() - setup_at) * 1000
                    translated_at = time.monotonic()
                    if command.get("image_data"):
                        translation = translator.translate_image(command["image_data"])
                        if translation.strip() == "[NO_TEXT]":
                            translation = "图中没有识别到清晰文字。"
                    else:
                        translation = translator.translate(command["text"], ignore_cache=True)
                    if not isinstance(translation, str) or not translation.strip():
                        raise ValueError("翻译服务未返回译文，请重试或更换引擎。")
                    result = {"ok": True, "translation": translation.strip()}
                except Exception as exc:
                    result = {"ok": False, "error": service_error(exc, secret_values(options))}
                pipe.send({"kind": "result", "request_id": command["request_id"], **result,
                    "worker_setup_ms": round(setup_ms, 1), "translation_ms": round((time.monotonic() - translated_at) * 1000, 1)})
        except (EOFError, BrokenPipeError, ConnectionError):
            pass
        except Exception as exc:
            from engine_registry import redact, secret_values, service_error
            try:
                pipe.send({"kind": "ready", "ok": False, "error": service_error(exc, secret_values(options))})
            except (EOFError, OSError):
                pass
        finally:
            pipe.close()


@dataclass
class _Job:
    request_id: int
    document_id: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Worker:
    process: object
    pipe: object
    key: str
    document_id: str


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


class _PdfServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class _PdfHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # Do not log capability URLs or private document names.

    def _headers(self, status, length, content_range=None):
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Accept-Ranges, Content-Length, Content-Range")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        self.close_connection = True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.end_headers()

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)

    def _serve(self, body):
        source = None
        try:
            # The capability is an exact opaque path, never a filesystem path.
            service = self.server.reader_service
            with service._lock:
                if service._closed or not service._document or self.path != service._document["route"]:
                    self._headers(404, 0)
                    return
                source = service._document["path"].open("rb")
            total = os.fstat(source.fileno()).st_size
            start, end, status = 0, total - 1, 200
            raw_range = self.headers.get("Range")
            if raw_range:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range.strip())
                valid = bool(match and any(match.groups()))
                if valid:
                    first, last = match.groups()
                    if not first:
                        count = int(last)
                        start = max(0, total - count)
                        valid = count > 0
                    else:
                        start = int(first)
                        end = min(int(last), total - 1) if last else total - 1
                    valid = valid and 0 <= start <= end < total
                if not valid:
                    self._headers(416, 0, f"bytes */{total}")
                    return
                status = 206
            length = end - start + 1
            self._headers(status, length, f"bytes {start}-{end}/{total}" if status == 206 else None)
            if body:
                source.seek(start)
                while length > 0:
                    data = source.read(min(length, 65536))
                    if not data:
                        break
                    self.wfile.write(data)
                    length -= len(data)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass  # PDF.js may stop an initial full request and switch to ranges.
        except OSError:
            if source is None:
                self._headers(404, 0)
        finally:
            if source:
                source.close()


class ReaderService:
    def __init__(self, data):
        self.data = Path(data)
        self._lock = threading.RLock()
        self._closed = False
        self._document = None
        self._server = self._server_thread = None
        self._latest_request = 0
        self._active = None
        self._jobs = {}
        self._warm_sequence = 0
        self._worker_lock = threading.Lock()
        self._worker = None
        self._cache = OrderedDict()
        self._cache_characters = 0
        self.timeout = TRANSLATION_TIMEOUT

    def _start_server_locked(self):
        if not self._server:
            self._server = _PdfServer(("127.0.0.1", 0), _PdfHandler)
            self._server.reader_service = self
            self._server_thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True, name="orca-reader-pdf")
            self._server_thread.start()

    def open_file(self, filename):
        try:
            import pymupdf
            path = Path(filename).resolve(strict=True)
            if path.suffix.lower() != ".pdf" or not path.is_file():
                raise ValueError("请选择 PDF 文件。")
            with pymupdf.open(path) as pdf:
                if pdf.needs_pass:
                    raise ValueError("这份 PDF 需要密码，请先解密再打开。")
                if not len(pdf):
                    raise ValueError("这份 PDF 没有可读取的页面。")
                pages = len(pdf)
                has_text = any(pdf[i].get_text().strip() for i in range(min(3, pages)))
            with self._lock:
                if self._closed:
                    raise ValueError("阅读窗口正在关闭。")
                self._start_server_locked()
                for job in self._jobs.values():
                    job.cancelled.set()
                self._cache.clear()
                self._cache_characters = 0
                doc_id = secrets.token_urlsafe(24)
                route = "/pdf/" + secrets.token_urlsafe(32) + ".pdf"
                self._document = {"id": doc_id, "path": path, "route": route}
                document = {"id": doc_id, "name": path.name, "pages": pages, "has_text": has_text,
                    "url": f"http://127.0.0.1:{self._server.server_port}{route}"}
            self._retire_idle_worker()
            return {"ok": True, "document": document}
        except Exception as exc:
            if isinstance(exc, ValueError):
                message = str(exc)
            else:
                message = "无法打开这份 PDF，请检查文件是否存在、可读取且未损坏。"
            return {"ok": False, "error": message}

    def _cancel_locked(self, request_id=None):
        if self._active and (request_id is None or self._active.request_id == request_id):
            self._active.cancelled.set()

    def cancel(self, request_id=None):
        with self._lock:
            # Bridge calls can arrive out of order. Remember an explicitly
            # cancelled request even when its translate call has not arrived.
            if isinstance(request_id, int) and not isinstance(request_id, bool) and request_id > 0:
                self._latest_request = max(self._latest_request, request_id)
            self._cancel_locked(request_id)
        return {"ok": True}

    def close_document(self):
        with self._lock:
            for job in self._jobs.values():
                job.cancelled.set()
            self._document = None
            self._cache.clear()
            self._cache_characters = 0
        self._retire_idle_worker()
        return {"ok": True}

    @staticmethod
    def _options(payload):
        values = payload.get("engine_settings") or {}
        if not isinstance(values, dict):
            raise ValueError("引擎设置无效，请重新填写。")
        options = {"engine": str(payload.get("engine") or "bing").lower(),
            "lang_in": str(payload.get("lang_in") or "en"), "lang_out": str(payload.get("lang_out") or "zh"),
            "engine_settings": {key: value.strip() if isinstance(value, str) else value for key, value in values.items()}}
        # Raw credentials are never stored in the LRU key or returned to the UI.
        key = hashlib.sha256(json.dumps(options, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        return options, key

    def _check_job(self, job, deadline):
        if job.cancelled.is_set():
            raise _Cancelled()
        if time.monotonic() >= deadline:
            raise _TimedOut()

    def _acquire_worker(self, job, deadline):
        while True:
            self._check_job(job, deadline)
            if self._worker_lock.acquire(timeout=0.05):
                return

    def _stop_worker_locked(self):
        worker, self._worker = self._worker, None
        if worker is None:
            return
        try:
            # A blocked provider cannot consume close commands, so termination
            # also bounds cancellation and broken network-client cleanup.
            if worker.process.is_alive():
                worker.process.terminate()
            worker.process.join(2)
            if worker.process.is_alive():
                worker.process.kill()
                worker.process.join(1)
        finally:
            worker.pipe.close()
            worker.process.close()

    def _retire_idle_worker(self):
        if not self._worker_lock.acquire(blocking=False):
            return  # Its owning request observes cancellation and cleans up.
        try:
            with self._lock:
                current_id = self._document["id"] if self._document and not self._closed else None
            if self._worker and self._worker.document_id != current_id:
                self._stop_worker_locked()
        finally:
            self._worker_lock.release()

    def _receive(self, worker, job, deadline):
        while True:
            self._check_job(job, deadline)
            try:
                if worker.pipe.poll(0.05):
                    return worker.pipe.recv()
            except (EOFError, OSError):
                raise RuntimeError("翻译进程已退出，请重试。") from None
            if not worker.process.is_alive():
                raise RuntimeError("翻译进程已退出，请重试。")

    def _ensure_worker(self, options, key, job, deadline):
        worker = self._worker
        if worker and worker.key == key and worker.document_id == job.document_id and worker.process.is_alive():
            return worker, True, 0.0
        self._stop_worker_locked()
        self._check_job(job, deadline)
        setup_at = time.monotonic()
        context = multiprocessing.get_context("spawn")
        parent_pipe, child_pipe = context.Pipe()
        process = context.Process(target=_translate_worker, args=(options, child_pipe), daemon=True, name="orca-reader-translation")
        try:
            process.start()
        except Exception:
            parent_pipe.close()
            child_pipe.close()
            process.close()
            raise
        child_pipe.close()
        worker = self._worker = _Worker(process, parent_pipe, key, job.document_id)
        ready = self._receive(worker, job, deadline)
        if ready.get("kind") != "ready" or not ready.get("ok"):
            raise ValueError(ready.get("error") or "翻译引擎未能初始化，请检查设置。")
        return worker, False, (time.monotonic() - setup_at) * 1000

    def _finish_job(self, job):
        with self._lock:
            if self._active is job:
                self._active = None
            self._jobs.pop(job.request_id, None)
            job.done.set()

    def prepare(self, options):
        """Warm imports/configuration only; no provider requests or health check."""
        started = time.monotonic()
        with self._lock:
            if self._closed or not self._document:
                return {"ok": False, "cancelled": True}
            if self._active:
                return {"ok": False, "busy": True}
            self._warm_sequence -= 1
            job = _Job(self._warm_sequence, self._document["id"])
            self._jobs[job.request_id] = job
        held = False
        keep_worker = False
        try:
            prepared, key = self._options(options)
            deadline = started + self.timeout
            self._acquire_worker(job, deadline)
            held = True
            _, reused, setup_ms = self._ensure_worker(prepared, key, job, deadline)
            self._check_job(job, deadline)
            keep_worker = True
            return {"ok": True, "worker_reused": reused, "worker_setup_ms": round(setup_ms, 1), "total_ms": round((time.monotonic() - started) * 1000, 1)}
        except _Cancelled:
            return {"ok": False, "cancelled": True}
        except _TimedOut:
            return {"ok": False, "error": "翻译引擎准备超时，划选文字时可重试。"}
        except Exception as exc:
            from engine_registry import redact, secret_values, service_error
            return {"ok": False, "error": service_error(exc, secret_values(options))}
        finally:
            if held:
                if not keep_worker or job.cancelled.is_set():
                    self._stop_worker_locked()
                elif self._worker and not self._worker.process.is_alive():
                    self._stop_worker_locked()
                self._worker_lock.release()
            self._finish_job(job)

    def translate(self, payload):
        return self._translate_selection(payload, image=False)

    def translate_image(self, payload):
        return self._translate_selection(payload, image=True)

    def _translate_selection(self, payload, image):
        if not isinstance(payload, dict):
            return {"ok": False, "error": "划选翻译请求无效。"}
        request_id = payload.get("request_id")
        image_data = None
        if image:
            from image_translation import validate_crop
            try:
                if payload.get("engine") != "indextranslate":
                    raise ValueError("图片翻译需要选择 Index-Translate。")
                image_data = validate_crop(payload.get("image_data"))
            except ValueError as exc:
                return {"ok": False, "request_id": request_id, "error": str(exc)}
        text = "图片选区" if image else payload.get("text")
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
            return {"ok": False, "request_id": request_id, "error": "划选翻译请求无效。"}
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "request_id": request_id, "error": "请先在 PDF 中选中文字。"}
        text = text.strip()
        if len(text) > MAX_SELECTION:
            return {"ok": False, "request_id": request_id, "error": f"每次最多翻译 {MAX_SELECTION} 个字符，请缩小选区。"}
        cancelled = {"ok": False, "request_id": request_id, "cancelled": True}
        with self._lock:
            if self._closed or not self._document or payload.get("document_id") != self._document["id"]:
                return {**cancelled, "error": "文档已关闭或已更换，请重新选择文字。"}
            if request_id <= self._latest_request:
                return cancelled
            self._latest_request = request_id
            previous = self._active
            self._cancel_locked()
            job = _Job(request_id, self._document["id"])
            self._active = job
            self._jobs[request_id] = job
        started = time.monotonic()
        deadline = started + self.timeout
        held = False
        keep_worker = False
        setup_ms = 0.0
        translation_ms = 0.0
        reused = False
        cache_hit = False
        response = None

        def respond(result):
            nonlocal response
            response = {**result, "request_id": request_id, "worker_setup_ms": round(setup_ms, 1),
                "translation_ms": round(translation_ms, 1), "total_ms": round((time.monotonic() - started) * 1000, 1),
                "cache_hit": cache_hit, "worker_reused": reused}
            return response

        try:
            # Wait only for previous cancellation cleanup, never its network call.
            if previous and not previous.done.wait(4):
                return respond({"ok": False, "error": "上一段翻译正在结束，请稍后重试。"})
            self._check_job(job, deadline)
            options, key = self._options(payload)
            cache_key = (key, "image" if image else "text", hashlib.sha256((image_data or text).encode("utf-8")).hexdigest())
            with self._lock:
                self._check_job(job, deadline)
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    cache_hit = True
                    return respond({"ok": True, "text": text, "translation": cached})
            self._acquire_worker(job, deadline)
            held = True
            worker, reused, setup_ms = self._ensure_worker(options, key, job, deadline)
            self._check_job(job, deadline)
            worker.pipe.send({"kind": "translate", "request_id": request_id, "text": text, "image_data": image_data})
            result = self._receive(worker, job, deadline)
            if result.get("kind") != "result" or result.get("request_id") != request_id:
                raise RuntimeError("翻译响应已失效，请重新选择文字。")
            setup_ms += result.pop("worker_setup_ms", 0.0)
            translation_ms = result.pop("translation_ms", 0.0)
            result.pop("kind", None)
            with self._lock:
                self._check_job(job, deadline)
                if self._active is not job or not self._document or self._document["id"] != job.document_id:
                    return respond(cancelled)
                keep_worker = True
                translated = result.get("translation", "")
                if result.get("ok") and len(translated) <= CACHE_CHARACTERS:
                    self._cache[cache_key] = translated
                    self._cache_characters += len(translated)
                    while len(self._cache) > CACHE_ENTRIES or self._cache_characters > CACHE_CHARACTERS:
                        _, removed = self._cache.popitem(last=False)
                        self._cache_characters -= len(removed)
            return respond({**result, "text": text})
        except _Cancelled:
            return respond(cancelled)
        except _TimedOut:
            return respond({"ok": False, "error": "翻译等待超时，请重试或更换引擎。"})
        except Exception as exc:
            from engine_registry import redact, secret_values, service_error
            return respond({"ok": False, "error": service_error(exc, secret_values(payload))})
        finally:
            if held:
                if not keep_worker or job.cancelled.is_set():
                    self._stop_worker_locked()
                self._worker_lock.release()
            self._finish_job(job)
            if response is not None:
                response["total_ms"] = round((time.monotonic() - started) * 1000, 1)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._document = None
            self._cache.clear()
            self._cache_characters = 0
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancelled.set()
            server, server_thread = self._server, self._server_thread
        deadline = time.monotonic() + 6
        for job in jobs:
            job.done.wait(max(0, deadline - time.monotonic()))
        self._retire_idle_worker()
        if server:
            server.shutdown()
            server.server_close()
            server_thread.join(1)
