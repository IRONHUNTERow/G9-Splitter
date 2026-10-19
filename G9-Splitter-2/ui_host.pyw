"""
G9 Splitter 2 - the interface window.

Opens the interface (a local web page served by g9_splitter.pyw) in its own
borderless app window using pywebview + Microsoft Edge WebView2 (built into
Windows 11). Exit codes: 0 = closed normally, 2 = pywebview not installed,
3 = window could not start (the engine then falls back to Edge app mode).
"""
import ctypes
import os
import sys
import time
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(APP_DIR, "g9_splitter_error.log")
TITLE = "G9 Splitter"


def log_error():
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S [ui] ") + traceback.format_exc() + "\n")
    except Exception:
        pass


def find_my_window():
    """Our window's handle (same process, title 'G9 Splitter')."""
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    found = []
    pid = os.getpid()
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == TITLE:
                found.append(hwnd)
        return True

    user32.EnumWindows(PROC(cb), 0)
    return found[0] if found else None


def set_window_app_id(hwnd, app_id, command=None, name=None, icon=None):
    """Give a window its own taskbar identity + what to start when its pinned icon is clicked."""
    try:
        import uuid
        from ctypes import POINTER, byref, c_ushort, c_void_p, c_long, c_ulong, c_uint32

        class GUID(ctypes.Structure):
            _fields_ = [("d1", c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                        ("d4", ctypes.c_ubyte * 8)]

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", c_uint32)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                        ("p", c_void_p), ("p2", c_void_p)]

        def guid(s):
            return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 2)
        except Exception:
            pass
        get = ctypes.WinDLL("shell32").SHGetPropertyStoreForWindow
        get.restype, get.argtypes = c_long, [c_void_p, POINTER(GUID), POINTER(c_void_p)]
        store = c_void_p()
        hr = get(hwnd, byref(guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")), byref(store))   # IPropertyStore
        if hr < 0 or not store.value:
            return False
        vtbl = ctypes.cast(store, POINTER(POINTER(c_void_p))).contents
        set_value = ctypes.WINFUNCTYPE(c_long, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(vtbl[6])
        commit = ctypes.WINFUNCTYPE(c_long, c_void_p)(vtbl[7])
        release = ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])
        keep = []

        def put(pid, text):          # System.AppUserModel.* (2 command, 3 icon, 4 name, 5 ID)
            buf = ctypes.create_unicode_buffer(text)
            keep.append(buf)
            pv = PROPVARIANT()
            pv.vt, pv.p = 31, ctypes.cast(buf, c_void_p).value          # VT_LPWSTR
            key = PROPERTYKEY(guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), pid)
            return set_value(store, byref(key), byref(pv))
        try:
            if command:
                put(2, command)
            if icon:
                put(3, icon)
            if name:
                put(4, name)
            put(5, app_id)
            commit(store)
        finally:
            release(store)
        return True
    except Exception:
        log_error()
        return False


def relaunch_command(script):
    pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pw if os.path.exists(pw) else sys.executable}" "{script}"'



def set_icon(ico):
    try:
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
        hwnd = None
        for _ in range(40):
            hwnd = find_my_window()
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd or not os.path.exists(ico):
            return
        big = user32.LoadImageW(None, ico, 1, 32, 32, 0x10)
        small = user32.LoadImageW(None, ico, 1, 16, 16, 0x10)
        user32.SendMessageW(hwnd, 0x80, 1, big)     # WM_SETICON, ICON_BIG
        user32.SendMessageW(hwnd, 0x80, 0, small)   # WM_SETICON, ICON_SMALL
        # pinning this window to the taskbar pins G9 Splitter 2 itself (not Python)
        set_window_app_id(hwnd, "OleJakob.G9Splitter2", relaunch_command(os.path.join(APP_DIR, "g9_splitter.pyw")),
                          "G9 Splitter 2", ico + ",0")
    except Exception:
        log_error()


def main():
    url = sys.argv[1]
    ico = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        import webview
    except Exception:
        sys.exit(2)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OleJakob.G9Splitter2")
    except Exception:
        pass

    state = {"window": None, "max": False}

    class Api:
        def minimize(self):
            try:
                state["window"].minimize()
            except Exception:
                log_error()

        def toggle_max(self):
            w = state["window"]
            try:
                if state["max"]:
                    w.restore()
                else:
                    w.maximize()
                state["max"] = not state["max"]
            except Exception:
                log_error()

        def close(self):
            state["window"].destroy()

        def restart_engine(self):
            """The engine (g9_splitter.pyw) stopped: start it again - it opens its own window."""
            try:
                import subprocess
                exe = sys.executable
                pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S [ui] ") + "engine unreachable - restarting it\n")
                subprocess.Popen([pw if os.path.exists(pw) else exe, os.path.join(APP_DIR, "g9_splitter.pyw"),
                                  "--restarted", "--from-ui"], cwd=APP_DIR, close_fds=True,
                                 creationflags=0x00000008 | 0x00000200)   # detached
            except Exception:
                log_error()
            try:
                state["window"].destroy()
            except Exception:
                log_error()
            return True

        def allow_fg(self):
            # Lets the engine bring your game to the front after you drop it.
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
            return True

    api = Api()
    window = None
    # Newest options first; older pywebview versions get a simpler window.
    for extra in ({"min_size": (1080, 760), "frameless": True, "easy_drag": False, "background_color": "#0b0c10"},
                  {"min_size": (1080, 760), "frameless": True},
                  {}):
        try:
            window = webview.create_window(TITLE, url, js_api=api, width=1300, height=940, **extra)
            break
        except TypeError:
            continue
    state["window"] = window
    try:
        window.events.shown += lambda: set_icon(ico)
    except Exception:
        log_error()
    try:
        storage = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "G9Splitter2", "webview")
        try:
            webview.start(gui="edgechromium", private_mode=False, storage_path=storage)
        except TypeError:
            webview.start(gui="edgechromium")
    except Exception:
        log_error()
        sys.exit(3)
    sys.exit(0)


if __name__ == "__main__":
    main()
