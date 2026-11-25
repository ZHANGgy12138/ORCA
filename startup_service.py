"""Prepare application services in the background after the window is visible."""
import copy
import logging
from pathlib import Path
import threading
import time


class StartupService:
    def __init__(self, root, portable_root, data, version, api_factory=None):
        self.root, self.portable_root, self.data = Path(root), Path(portable_root), Path(data)
        self.version = version
        self._factory = api_factory
        self._lock = threading.RLock()
        self._window = None
        self._api = None
        self._bootstrap = None
        self._thread = None
        self._running = False
        self._closed = False
        self._status = "waiting"
        self._stage = "正在打开窗口"
        self._error = None
        self._attempt = 0
        self._started = None
        self._finished = None

    @property
    def api(self):
        with self._lock:
            return self._api if self._status == "ready" and not self._closed else None

    def _state_locked(self):
        elapsed = 0.0 if self._started is None else (self._finished or time.monotonic()) - self._started
        return {"status": self._status, "stage": self._stage,
                "elapsed_seconds": round(max(0.0, elapsed), 1), "error": self._error,
                "attempt": self._attempt}

    def get_state(self):
        with self._lock:
            return self._state_locked()

    def _begin_locked(self):
        self._attempt += 1
        self._started = time.monotonic()
        self._finished = None
        self._error = None
        self._status = "loading"
        self._stage = "正在读取设置"
        self._running = True
        thread = threading.Thread(target=self._prepare, args=(self._attempt,), daemon=True, name="orca-startup")
        self._thread = thread
        try:
            thread.start()
        except Exception:
            self._running = False
            self._thread = None
            self._status = "error"
            self._stage = "启动未完成"
            self._finished = time.monotonic()
            self._error = "无法开始准备应用，请重试。"
            return {"ok": False, **self._state_locked()}
        return {"ok": True, **self._state_locked()}

    def start(self, window):
        with self._lock:
            if self._closed:
                return {"ok": False, **self._state_locked()}
            if self._status != "waiting":
                return {"ok": self._status in ("loading", "ready"), **self._state_locked()}
            self._window = window
            return self._begin_locked()

    def retry(self):
        with self._lock:
            if self._closed or self._running or self._status != "error":
                return {"ok": False, **self._state_locked()}
            return self._begin_locked()

    def _advance(self, attempt, stage):
        with self._lock:
            if self._closed or attempt != self._attempt:
                return False
            self._stage = stage
            return True

    @staticmethod
    def _friendly_error(exc):
        if isinstance(exc, PermissionError):
            return "无法读取应用设置，请检查当前账户的数据目录权限后重试。"
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            return "启动组件加载失败，请完整解压最新版 ORCA 后重试。"
        return "应用准备失败，请重试；如果仍然失败，请重新打开 ORCA。"

    @staticmethod
    def _close_api(api):
        if api is not None:
            try:
                api.close()
            except Exception:
                logging.getLogger(__name__).warning("Application service cleanup failed")

    def _prepare(self, attempt):
        candidate = None
        bootstrap = None
        error = None
        try:
            factory = self._factory
            if factory is None:
                from desktop_api import DesktopApi
                factory = DesktopApi
            candidate = factory(self.root, self.portable_root, self.data, self.version)
            if not self._advance(attempt, "正在准备翻译功能"):
                return
            candidate.window = self._window
            # This method returns nonsecret preferences, engine descriptions,
            # and recent tasks. Saved credentials are loaded by a separate UI
            # action and are never included in the startup-state response.
            bootstrap = candidate.get_bootstrap()
            if not isinstance(bootstrap, dict) or not bootstrap.get("engines"):
                raise ValueError("Application bootstrap data is incomplete")
            bootstrap = copy.deepcopy(bootstrap)
            with self._lock:
                if not self._closed and attempt == self._attempt:
                    self._api = candidate
                    self._bootstrap = bootstrap
                    candidate = None  # Transfer cleanup ownership to close().
        except Exception as exc:
            error = self._friendly_error(exc)
            logging.getLogger(__name__).warning("Application preparation failed (%s)", type(exc).__name__)
        finally:
            # Finish cleanup before exposing retry, so attempts never overlap.
            self._close_api(candidate)
            with self._lock:
                if attempt == self._attempt:
                    self._running = False
                    self._thread = None
                    if not self._closed:
                        self._finished = time.monotonic()
                        if self._api is not None:
                            self._status, self._stage = "ready", "启动完成"
                            self._error = None
                        else:
                            self._status, self._stage = "error", "启动未完成"
                            self._error = error or "应用准备失败，请重试。"

    def bootstrap(self):
        with self._lock:
            if self._status != "ready" or self._closed:
                return {"ok": False, "error": "应用尚未准备完成。"}
            return copy.deepcopy(self._bootstrap)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._status, self._stage = "closed", "应用已关闭"
            self._finished = time.monotonic()
            self._error = None
            api, self._api = self._api, None
            self._bootstrap = None
        # A loading worker still owns its candidate and will dispose it when
        # its current operation returns. Closing the window never waits for an
        # import or an unresponsive initializer on the UI thread.
        self._close_api(api)
