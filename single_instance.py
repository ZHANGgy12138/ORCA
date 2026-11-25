"""Windows single-instance activation without sockets or persistent lock files.

The guard is scoped to the normalized data directory, Windows user and logon
session. A dedicated thread owns the mutex so close() is safe from any thread.
Activation events received before the UI is ready are retained and coalesced.
"""
import ctypes
from ctypes import wintypes
import hashlib
import logging
import mmap
import os
from pathlib import Path
import struct
import threading
import time

WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 128
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
_OWNER = struct.Struct("<4sI")


class SingleInstanceError(RuntimeError):
    pass


class _Windows:
    def __init__(self):
        if os.name != "nt":
            raise SingleInstanceError("启动保护目前需要 Windows。")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.SetEvent.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
        self.kernel.WaitForMultipleObjects.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.advapi.OpenProcessToken.restype = wintypes.BOOL
        self.advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.advapi.GetTokenInformation.restype = wintypes.BOOL
        self.advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        self.advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.user.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        self.user.AllowSetForegroundWindow.restype = wintypes.BOOL

    def sid(self):
        token = wintypes.HANDLE()
        if not self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            length = wintypes.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            if not length.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(length.value)
            if not self.advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            result = wintypes.LPWSTR()
            if not self.advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(result)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return result.value
            finally:
                self.kernel.LocalFree(ctypes.cast(result, ctypes.c_void_p))
        finally:
            self.kernel.CloseHandle(token)

    def is_alive(self, pid):
        handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            return False
        try:
            return self.kernel.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            self.kernel.CloseHandle(handle)


class SingleInstance:
    def __init__(self, data):
        self.data = os.path.normcase(str(Path(data).expanduser().resolve()))
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._shutdown = threading.Event()
        self._thread = None
        self._windows = None
        self._stop = self._activation = None
        self._primary = False
        self._closed = False
        self._pending = False
        self._callback = None
        self._error = None
        self.owner_pid = None

    def acquire(self):
        """True means this process owns the profile; False means it notified it."""
        with self._lock:
            if self._closed:
                raise SingleInstanceError("启动保护已经关闭。")
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="orca-single-instance", daemon=True)
                self._thread.start()
        if not self._ready.wait(5):
            self.close()
            raise SingleInstanceError("启动保护暂时没有响应，请稍后重新打开 ORCA。")
        with self._lock:
            error, primary = self._error, self._primary
        if error:
            self.close()
            raise SingleInstanceError(error)
        return primary

    def start_listener(self, callback):
        """Call after the first window is shown; callback runs on our thread."""
        if not callable(callback):
            raise TypeError("Activation callback must be callable")
        with self._lock:
            if self._closed or not self._primary:
                return
            if self._error:
                raise SingleInstanceError(self._error)
            self._callback = callback
            if self._pending and self._activation:
                self._windows.kernel.SetEvent(self._activation)

    def close(self):
        """Release the guard from any thread, including a UI closing callback."""
        with self._lock:
            self._closed = True
            self._shutdown.set()
            if self._stop and self._windows:
                self._windows.kernel.SetEvent(self._stop)
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(3)

    def _run(self):
        mutex = activation = stop = None
        owner = None
        acquired = False
        try:
            win = self._windows = _Windows()
            identity = hashlib.sha256((win.sid() + "\0" + self.data).encode("utf-8")).hexdigest()
            base = "Local\\ORCA.Startup.v1." + identity
            mutex = win.kernel.CreateMutexW(None, False, base + ".mutex")
            if not mutex:
                raise ctypes.WinError(ctypes.get_last_error())
            # Open the event BEFORE claiming the mutex. A second process can
            # signal immediately without racing a not-yet-created event.
            activation = win.kernel.CreateEventW(None, False, False, base + ".activate")
            stop = win.kernel.CreateEventW(None, True, False, None)
            if not activation or not stop:
                raise ctypes.WinError(ctypes.get_last_error())
            owner = mmap.mmap(-1, _OWNER.size, tagname=base + ".owner", access=mmap.ACCESS_WRITE)
            with self._lock:
                self._activation, self._stop = activation, stop
                if self._closed:
                    return
            deadline = time.monotonic() + .75
            while True:
                result = win.kernel.WaitForSingleObject(mutex, 0)
                if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
                    acquired = True
                    self.owner_pid = os.getpid()
                    owner[:_OWNER.size] = _OWNER.pack(b"ORCA", self.owner_pid)
                    with self._lock:
                        if self._closed:
                            return
                        self._primary = True
                    self._ready.set()
                    break
                if result != WAIT_TIMEOUT:
                    raise ctypes.WinError(ctypes.get_last_error())
                magic, pid = _OWNER.unpack(owner[:_OWNER.size])
                if magic == b"ORCA" and pid and win.is_alive(pid):
                    self.owner_pid = pid
                    # The OS may deny foreground permission (e.g. an unrelated
                    # background launch). We still restore/notify the same UI.
                    win.user.AllowSetForegroundWindow(pid)
                    break
                if time.monotonic() >= deadline:
                    break
                with self._lock:
                    if self._closed:
                        return
                time.sleep(.01)
            if not acquired:
                if not win.kernel.SetEvent(activation):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._ready.set()
                return
            handles = (wintypes.HANDLE * 2)(stop, activation)
            while True:
                result = win.kernel.WaitForMultipleObjects(2, handles, False, INFINITE)
                if result == WAIT_OBJECT_0:
                    break
                if result != WAIT_OBJECT_0 + 1:
                    raise ctypes.WinError(ctypes.get_last_error())
                with self._lock:
                    if self._closed:
                        break
                    callback = self._callback
                    self._pending = callback is None
                if callback:
                    try:
                        callback()
                    except Exception:
                        logging.getLogger(__name__).warning("ORCA window activation failed")
        except Exception:
            with self._lock:
                self._error = "无法建立 ORCA 启动保护，请关闭已有窗口后重试。"
            self._ready.set()
            # If event monitoring ever fails after ownership was published,
            # retain the mutex until the app closes. Failing open would permit
            # a second UI while the original app is still running.
            if acquired and not self._closed:
                logging.getLogger(__name__).error("ORCA activation listener stopped; ownership retained until shutdown")
                self._shutdown.wait()
        finally:
            # No PID file remains after normal exit or a process crash. Only
            # the current owning thread may release a Win32 mutex.
            if acquired and owner:
                owner[:_OWNER.size] = b"\0" * _OWNER.size
            if owner:
                owner.close()
            with self._lock:
                self._stop = self._activation = None
                if self._windows:
                    if acquired:
                        self._windows.kernel.ReleaseMutex(mutex)
                    for handle in (stop, activation, mutex):
                        if handle:
                            self._windows.kernel.CloseHandle(handle)
                if self._closed and not self._ready.is_set():
                    self._error = "启动保护已经关闭。"
                self._ready.set()
