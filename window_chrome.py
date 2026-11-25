"""Integrated ORCA window chrome with native Windows move/resize behavior.

Only the application's own HWND is subclassed. The HTML bar draws all colors;
Windows retains resize, minimize/maximize, taskbar and close behavior.
"""
import ctypes
from ctypes import wintypes
import logging
import sys

EDGES = {"left": 10, "right": 11, "top": 12, "top-left": 13,
         "top-right": 14, "bottom": 15, "bottom-left": 16, "bottom-right": 17}

MAIN_MINIMUM = (980, 720)
FLOAT_MINIMUM = (360, 220)
FLOAT_SIZE = (400, 248)


def clamp_bounds(bounds, work_area, minimum=(1, 1)):
    """Fit physical screen bounds to a monitor, including negative origins."""
    x, y, width, height = bounds
    left, top, right, bottom = work_area
    available_width, available_height = max(1, right-left), max(1, bottom-top)
    width = min(available_width, max(minimum[0], width))
    height = min(available_height, max(minimum[1], height))
    return (max(left, min(x, right-width)), max(top, min(y, bottom-height)), width, height)


def scaled_size(size, from_dpi, to_dpi):
    return tuple(max(1, round(value * to_dpi / max(1, from_dpi))) for value in size)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT),
                ("ptMaxPosition", POINT), ("ptMinTrackSize", POINT), ("ptMaxTrackSize", POINT)]


class WindowChrome:
    def __init__(self, window, theme="light"):
        self.window, self.theme = window, theme
        self.hwnd = None
        self.callback = None
        self.installed = False
        self.pending_actions = []
        self._floating = False
        self._float_on_top = True
        self._saved_main = None
        self._float_bounds = None
        self._float_dpi = 96

    def install(self):
        if sys.platform != "win32":
            return
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        pointer = ctypes.c_ssize_t
        self.callback_type = ctypes.WINFUNCTYPE(pointer, wintypes.HWND, wintypes.UINT,
                                                ctypes.c_size_t, pointer, ctypes.c_size_t, ctypes.c_size_t)
        self.comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, pointer]
        self.comctl32.DefSubclassProc.restype = pointer
        self.comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, self.callback_type, ctypes.c_size_t, ctypes.c_size_t]
        self.comctl32.SetWindowSubclass.restype = wintypes.BOOL
        self.comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, self.callback_type, ctypes.c_size_t]
        self.comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
        self.user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongPtrW.restype = pointer
        self.user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, pointer]
        self.user32.SetWindowLongPtrW.restype = pointer
        self.user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, pointer]
        self.user32.SendMessageW.restype = pointer
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        self.user32.IsZoomed.argtypes = self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HMONITOR
        self.user32.MonitorFromRect.argtypes = [ctypes.POINTER(RECT), wintypes.DWORD]
        self.user32.MonitorFromRect.restype = wintypes.HMONITOR
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        self.user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self.hwnd = int(self.window.native.Handle.ToInt64())
        self.callback = self.callback_type(self._procedure)
        if not self.comctl32.SetWindowSubclass(self.hwnd, self.callback, 0x4F524341, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        style = self.user32.GetWindowLongPtrW(self.hwnd, -16)
        # Resize and system controls without Windows' native caption strip.
        style = (style | 0x00040000 | 0x00020000 | 0x00010000 | 0x00080000) & ~0x00C00000
        self.user32.SetWindowLongPtrW(self.hwnd, -16, style)
        self.installed = True
        self._set_theme_ui()
        self.user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0010 | 0x0001 | 0x0002 | 0x0004)

    def _procedure(self, hwnd, message, wparam, lparam, subclass_id, reference):
        try:
            if message == 0x0083:  # WM_NCCALCSIZE: client occupies the entire frame.
                return 0
            if message == 0x0112 and (wparam & 0xFFF0) == 0xF030 and self._floating:
                # Native system-menu or snap maximize must not stretch the compact UI.
                self._dispatch(self.exit_floating)
                return 0
            if message == 0x0024:  # WM_GETMINMAXINFO: respect taskbar and monitor work area.
                self.comctl32.DefSubclassProc(hwnd, message, wparam, lparam)
                monitor = self.user32.MonitorFromWindow(hwnd, 2)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(info)
                if self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    limits = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
                    limits.ptMaxPosition = POINT(info.rcWork.left - info.rcMonitor.left, info.rcWork.top - info.rcMonitor.top)
                    limits.ptMaxSize = POINT(info.rcWork.right - info.rcWork.left, info.rcWork.bottom - info.rcWork.top)
                    dpi = self.user32.GetDpiForWindow(hwnd) or 96
                    minimum = FLOAT_MINIMUM if self._floating else MAIN_MINIMUM
                    limits.ptMinTrackSize = POINT(
                        min(info.rcWork.right-info.rcWork.left, round(minimum[0]*dpi/96)),
                        min(info.rcWork.bottom-info.rcWork.top, round(minimum[1]*dpi/96)),
                    )
                return 0
            if message == 0x0084 and not self.user32.IsZoomed(hwnd):
                bounds = RECT()
                self.user32.GetWindowRect(hwnd, ctypes.byref(bounds))
                x, y = ctypes.c_short(lparam & 0xffff).value, ctypes.c_short((lparam >> 16) & 0xffff).value
                margin = max(5, round(5*(self.user32.GetDpiForWindow(hwnd) or 96)/96))
                left, right = x < bounds.left+margin, x >= bounds.right-margin
                top, bottom = y < bounds.top+margin, y >= bounds.bottom-margin
                edge = "top-left" if top and left else "top-right" if top and right else "bottom-left" if bottom and left else "bottom-right" if bottom and right else "left" if left else "right" if right else "top" if top else "bottom" if bottom else None
                if edge:
                    return EDGES[edge]
            if message == 0x0082:  # WM_NCDESTROY
                self.comctl32.RemoveWindowSubclass(hwnd, self.callback, subclass_id)
                self.installed = False
        except Exception:
            logging.exception("ORCA window frame message failed")
        return self.comctl32.DefSubclassProc(hwnd, message, wparam, lparam)

    def _dispatch(self, action):
        if not self.hwnd or not self.installed:
            return {"ok": False, "error": "窗口尚未就绪。"}
        from System import Action
        delegate = Action(action)
        # Keep pending delegates alive until WinForms has consumed them.
        self.pending_actions = [item for item in self.pending_actions if not item[0].IsCompleted]
        result = self.window.native.BeginInvoke(delegate)
        self.pending_actions.append((result, delegate))
        return {"ok": True}

    def _run_ui(self, action):
        """Serialize geometry changes on WinForms and return the settled state."""
        if not self.hwnd or not self.installed:
            return {"ok": False, "error": "窗口尚未就绪。"}
        from System import Action
        outcome = {}

        def run():
            try:
                action()
                outcome.update(ok=True, **self.state())
            except Exception:
                logging.exception("ORCA window mode change failed")
                outcome.update(ok=False, error="窗口模式暂时无法切换，请重试。")

        if self.window.native.InvokeRequired:
            self.window.native.Invoke(Action(run))
        else:
            run()
        return outcome

    def _dpi(self):
        return self.user32.GetDpiForWindow(self.hwnd) or 96

    def _bounds(self):
        bounds = RECT()
        self.user32.GetWindowRect(self.hwnd, ctypes.byref(bounds))
        return (bounds.left, bounds.top, bounds.right-bounds.left, bounds.bottom-bounds.top)

    def _work_area(self, bounds=None):
        if bounds is None:
            monitor = self.user32.MonitorFromWindow(self.hwnd, 2)
        else:
            x, y, width, height = bounds
            rect = RECT(x, y, x+width, y+height)
            monitor = self.user32.MonitorFromRect(ctypes.byref(rect), 2)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom)

    def _set_bounds(self, bounds):
        x, y, width, height = map(int, bounds)
        if not self.user32.SetWindowPos(self.hwnd, None, x, y, width, height, 0x0004 | 0x0010):
            raise ctypes.WinError(ctypes.get_last_error())

    def _set_minimum(self, work_area):
        from System.Drawing import Size
        minimum = scaled_size(FLOAT_MINIMUM if self._floating else MAIN_MINIMUM, 96, self._dpi())
        minimum = (min(minimum[0], work_area[2]-work_area[0]), min(minimum[1], work_area[3]-work_area[1]))
        self.window.native.MinimumSize = Size(*minimum)
        return minimum

    def enter_floating(self):
        def enter():
            if self._floating:
                return
            from System.Drawing import Size
            from System.Windows.Forms import FormWindowState
            form = self.window.native
            current = self.state()
            native_bounds = form.RestoreBounds if current["maximized"] or current["minimized"] else form.Bounds
            main_bounds = (native_bounds.X, native_bounds.Y, native_bounds.Width, native_bounds.Height)
            work = self._work_area()
            self._saved_main = {"bounds": main_bounds, "dpi": self._dpi(),
                                "maximized": current["maximized"], "on_top": current["always_on_top"],
                                "maximize_box": bool(self.user32.GetWindowLongPtrW(self.hwnd, -16) & 0x00010000)}
            self._floating = True  # Update the native min-track rule before resizing.
            form.MinimumSize = Size(0, 0)
            form.WindowState = FormWindowState.Normal
            self.user32.SetWindowLongPtrW(self.hwnd, -16, self.user32.GetWindowLongPtrW(self.hwnd, -16) & ~0x00010000)
            dpi = self._dpi()
            if self._float_bounds:
                x, y, width, height = self._float_bounds
                width, height = scaled_size((width, height), self._float_dpi, dpi)
            else:
                width, height = scaled_size(FLOAT_SIZE, 96, dpi)
                margin = round(24*dpi/96)
                x, y = work[2]-width-margin, work[3]-height-margin
            minimum = self._set_minimum(work)
            self._set_bounds(clamp_bounds((x, y, width, height), work, minimum))
            form.TopMost = self._float_on_top
        return self._run_ui(enter)

    def exit_floating(self):
        def leave():
            if not self._floating:
                return
            from System.Drawing import Size
            from System.Windows.Forms import FormWindowState
            form = self.window.native
            saved = self._saved_main
            if not saved:
                raise RuntimeError("Main window geometry was not saved")
            # Read actual position/size, so a dragged/resized compact window is remembered.
            if not self.user32.IsIconic(self.hwnd) and not self.user32.IsZoomed(self.hwnd):
                self._float_bounds, self._float_dpi = self._bounds(), self._dpi()
            self._floating = False
            form.MinimumSize = Size(0, 0)
            form.WindowState = FormWindowState.Normal
            style = self.user32.GetWindowLongPtrW(self.hwnd, -16)
            style = style | 0x00010000 if saved["maximize_box"] else style & ~0x00010000
            self.user32.SetWindowLongPtrW(self.hwnd, -16, style)
            work = self._work_area(saved["bounds"])
            # Move to the original monitor first, then use its current DPI for sizing.
            self._set_bounds(clamp_bounds(saved["bounds"], work))
            x, y, width, height = saved["bounds"]
            width, height = scaled_size((width, height), saved["dpi"], self._dpi())
            minimum = self._set_minimum(work)
            self._set_bounds(clamp_bounds((x, y, width, height), work, minimum))
            form.TopMost = saved["on_top"]
            if saved["maximized"]:
                form.WindowState = FormWindowState.Maximized
            self._saved_main = None
        return self._run_ui(leave)

    def toggle_floating(self):
        # Choose on the UI thread to avoid concurrent clicks making stale decisions.
        def toggle():
            result = self.exit_floating() if self._floating else self.enter_floating()
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "Mode change failed"))
        return self._run_ui(toggle)

    def set_floating_on_top(self, enabled):
        def set_pin():
            if not self._floating:
                return
            self._float_on_top = bool(enabled)
            self.window.native.TopMost = self._float_on_top
        return self._run_ui(set_pin)

    def start_drag(self):
        def move():
            self.user32.ReleaseCapture()
            self.user32.SendMessageW(self.hwnd, 0x00A1, 2, 0)
        return self._dispatch(move)

    def start_resize(self, edge):
        if edge not in EDGES or self.state()["maximized"]:
            return {"ok": False}
        def resize():
            self.user32.ReleaseCapture()
            self.user32.SendMessageW(self.hwnd, 0x00A1, EDGES[edge], 0)
        return self._dispatch(resize)

    def set_theme(self, theme):
        self.theme = "dark" if theme == "dark" else "light"
        if self.installed:
            return self._dispatch(self._set_theme_ui)
        return {"ok": True}

    def _set_theme_ui(self):
        from System.Drawing import ColorTranslator
        self.window.native.BackColor = ColorTranslator.FromHtml("#111111" if self.theme == "dark" else "#f6f6f6")
        dark = ctypes.c_int(self.theme == "dark")
        self.dwm.DwmSetWindowAttribute(self.hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        # Windows 11 border color; older systems ignore unsupported attributes.
        border = ctypes.c_uint(0x333333 if self.theme == "dark" else 0xe4e4e4)
        self.dwm.DwmSetWindowAttribute(self.hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))

    def state(self):
        if not self.hwnd:
            return {"maximized": False, "minimized": False, "frameless": True, "installed": False,
                    "floating": self._floating, "always_on_top": False, "mode": "floating" if self._floating else "main"}
        return {"maximized": bool(self.user32.IsZoomed(self.hwnd)), "minimized": bool(self.user32.IsIconic(self.hwnd)), "frameless": True, "installed": self.installed,
                "floating": self._floating, "always_on_top": bool(self.user32.GetWindowLongPtrW(self.hwnd, -20) & 0x8), "mode": "floating" if self._floating else "main"}

    def diagnostics(self):
        result = self.state()
        if self.installed:
            outer, client = RECT(), RECT()
            self.user32.GetWindowRect(self.hwnd, ctypes.byref(outer))
            self.user32.GetClientRect(self.hwnd, ctypes.byref(client))
            style = self.user32.GetWindowLongPtrW(self.hwnd, -16)
            result.update(native_caption_removed=not bool(style & 0x00C00000), native_resize_style=bool(style & 0x00040000),
                          client_fills_frame=(outer.right-outer.left == client.right and outer.bottom-outer.top == client.bottom),
                          size=[outer.right-outer.left, outer.bottom-outer.top],
                          position=[outer.left, outer.top], dpi=self._dpi(),
                          minimum_logical=list(FLOAT_MINIMUM if self._floating else MAIN_MINIMUM))
        return result
