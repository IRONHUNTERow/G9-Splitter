"""
G9 Splitter - split one super-ultrawide monitor into Left | Game | Right.

Built for a 49" Samsung Odyssey G9 (5120x1440): the middle 2560x1440 is
exactly the size of a 27" 1440p monitor, with a 1280x1440 strip each side.

Version 2: the window logic runs here (Python), the interface is a web page
shown in its own app window (pywebview / Microsoft Edge WebView2), which lets
the themes look like real game menus. Windows only.
"""

import ctypes
import json
import tkinter
import os
import queue
import sys
import threading
import time
import traceback

APP_NAME = "G9 Splitter"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "g9_splitter_config.json")
LOG_PATH = os.path.join(APP_DIR, "g9_splitter_error.log")


# ---- troubleshooting: a hard crash (inside Windows code) leaves no Python error, so record
# ---- every start / stop, and let Python write its last words to a crash log if it dies.
RUN_LOG = os.path.join(APP_DIR, "g9_splitter_run.log")


def run_log(msg):
    try:
        lines = []
        if os.path.exists(RUN_LOG):
            with open(RUN_LOG, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()[-150:]
        lines.append(time.strftime("%Y-%m-%d %H:%M:%S") + f"  [{os.getpid()}] " + msg)
        with open(RUN_LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def start_crash_log():
    try:
        import atexit
        import faulthandler
        f = open(os.path.join(APP_DIR, "g9_splitter_crash.log"), "a", encoding="utf-8")
        f.write(time.strftime("\n%Y-%m-%d %H:%M:%S") + f"  started [{os.getpid()}]\n")
        f.flush()
        faulthandler.enable(f, all_threads=True)
        start_crash_log.f = f
        if sys.stderr is None:          # no console (pythonw): keep stray errors instead of losing them
            sys.stderr = f
        atexit.register(lambda: run_log("exited"))
    except Exception:
        pass


# ---- one app on the taskbar: pinning the window pins G9 Splitter (not Python), and starting it
# ---- again while it's running just brings the running copy to the front.
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


def single_instance(name, wait):
    """Only one copy may run (two would fight over the hotkeys)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    deadline = time.time() + (8 if wait else 0)
    while True:
        h = k32.CreateMutexW(None, False, name)
        if h and ctypes.get_last_error() != 183:          # ERROR_ALREADY_EXISTS
            return h
        if h:
            k32.CloseHandle(h)
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def tray_class(tip):
    return "G9SplitterTray_" + "".join(ch for ch in tip if ch.isalnum()) + "_"


def wake_running_copy(tip):
    """Already running: open its window (same as clicking its tray icon). True if it was found."""
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint32]
        prefix, found = tray_class(tip), []

        def cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, buf, 256)
            if buf.value.startswith(prefix):
                found.append(hwnd)
            return True
        proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(cb)
        u32.EnumWindows(proc, None)
        for hwnd in found:
            pid = ctypes.c_uint32()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            u32.AllowSetForegroundWindow(pid.value)
            u32.PostMessageW(hwnd, TrayIcon.WM_TRAY, 1, 0x0202)          # "left click"
        return bool(found)
    except Exception:
        log_error()
        return False


def report_last_crash(exe_hint="pythonw"):
    """If Windows recorded G9 Splitter crashing (Event Viewer > Application Error), copy that into
    g9_splitter_run.log - it names the part that crashed, which Python itself can't always see."""
    def work():
        try:
            import subprocess
            ps = ("Get-WinEvent -FilterHashtable @{LogName='Application';ProviderName='Application Error';"
                  "StartTime=(Get-Date).AddDays(-2)} -MaxEvents 30 -ErrorAction SilentlyContinue | "
                  "Where-Object { $_.Message -match '" + exe_hint + "' } | ForEach-Object { "
                  "$_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss') + ' ' + (($_.Message -split '\\r?\\n' | "
                  "Where-Object { $_ -match 'Faulting module name|Exception code|Fault offset|"
                  "Faulting process id|Faulting application path' }) -join ' / ') }")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, text=True, timeout=60, creationflags=0x08000000)
            seen = open(RUN_LOG, encoding="utf-8", errors="replace").read() if os.path.exists(RUN_LOG) else ""
            for line in reversed([x.strip() for x in r.stdout.splitlines() if x.strip()]):
                if line[:19] not in seen:
                    run_log("Windows recorded a crash at " + " ".join(line.split()))
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()
ICON_ICO = os.path.join(APP_DIR, "g9_splitter.ico")
ICON_PNG = os.path.join(APP_DIR, "g9_splitter.png")

DEFAULT_CONFIG = {
    "monitor": "",          # device name like \\.\DISPLAY1 ; "" = pick widest
    "center_width": 0,      # 0 = automatic 16:9 (2560 on a G9)
    "center_height": 0,     # 0 = full monitor height
    "rules": [],            # remembered apps
    "auto_apply": True,
    "drag_snap": True,      # drag a window onto a box to snap it there
    "game_on_top": True,    # middle game window stays above the taskbar
    "fps_overlay": False,   # FPS counter on the game in the middle
    "auto_detect_games": True,   # recognise games automatically and put them in the middle
    "not_games": [], "games": [], "ignored": [], "tray_hint_shown": False,        # programs you told us are / aren't games
    "game_ratio": 16 / 9,   # game area shape (width/height); 0 = exact pixel size (center_width)
    "game_height_pct": 100,  # game area height, % of the monitor; 0 = center_height pixels
    "game_position": "center",   # center / left / right edge
    "game_valign": "middle",     # middle / top / bottom (when it's shorter than the monitor)
    "new_game_action": "game",   # newly recognised games: game / middle / ignore
    "new_game_lock": True,       # lock the mouse to newly recognised games
    "monitor_shape": "auto",     # preview: auto / curved / flat
    "theme": "",
}

ZONE_LABELS = {"left": "LEFT SCREEN", "center": "GAME", "right": "RIGHT SCREEN"}
ZONE_NAMES = {"left": "the left screen", "center": "the middle", "right": "the right screen"}

# Shape presets for the middle (game) area: name -> width/height ratio. The game area keeps
# its shape on any monitor - if it would be too wide it shrinks until it fits.
PRESETS = [
    ("16:9  (looks like a 27\" monitor)", 16 / 9),
    ("16:10", 16 / 10),
    ("3:2", 3 / 2),
    ("4:3  (classic)", 4 / 3),
    ("5:4", 5 / 4),
    ("1:1  (square)", 1.0),
    ("21:9  (looks like a 34\" ultrawide, 3440 x 1440)", 43 / 18),
    ("24:10  (looks like a 38\" ultrawide, 3840 x 1600)", 24 / 10),
    ("32:9  (super ultrawide)", 32 / 9),
    ("Custom", None),
]
# game area height as a share of the monitor's height
HEIGHT_CHOICES = [100, 95, 90, 85, 80, 75, 70, 65, 60, 50]
POSITION_CHOICES = [("center", "In the middle"), ("left", "At the left edge"), ("right", "At the right edge")]
VALIGN_CHOICES = [("middle", "Centred"), ("top", "At the top"), ("bottom", "At the bottom")]
NEW_GAME_CHOICES = [("game", "Put it in GAME (no border)"), ("middle", "Put it in Middle (keep border)"),
                    ("ignore", "Leave it alone (ignore)")]
KNOWN_ASPECTS = [("32:9", 32 / 9), ("24:10", 2.4), ("21:9", 43 / 18), ("21:9", 64 / 27), ("16:9", 16 / 9),
                 ("16:10", 1.6), ("3:2", 1.5), ("4:3", 4 / 3), ("5:4", 1.25), ("1:1", 1.0)]


def aspect_name(w, h):
    """Friendly shape name for a size: 5120x1440 -> 32:9, 3440x1440 -> 21:9."""
    if w <= 0 or h <= 0:
        return ""
    r = w / h
    name, ratio = min(KNOWN_ASPECTS, key=lambda kv: abs(kv[1] - r))
    if abs(ratio - r) / r < 0.03:
        return name
    from math import gcd
    g = gcd(w, h)
    return f"{w // g}:{h // g}"


def game_area_options(cfg):
    """The game-area settings from the config, for compute_zones()."""
    try:
        ratio = float(cfg.get("game_ratio") or 0)
    except (TypeError, ValueError):
        ratio = 0
    try:
        pct = int(cfg.get("game_height_pct") or 0)
    except (TypeError, ValueError):
        pct = 0
    pos = cfg.get("game_position", "center")
    valign = cfg.get("game_valign", "middle")
    return {"aspect": ratio if ratio > 0 else None, "height_pct": pct if 0 < pct <= 100 else 0,
            "position": pos if pos in dict(POSITION_CHOICES) else "center",
            "valign": valign if valign in dict(VALIGN_CHOICES) else "middle"}


def preset_name_for(cfg, cw, ch):
    """Which shape preset the current game area matches (for the menus)."""
    try:
        ratio = float(cfg.get("game_ratio") or 0)
    except (TypeError, ValueError):
        ratio = 0
    for name, r in PRESETS:
        if r and ratio and abs(r - ratio) < 1e-3:
            return name
    for name, r in PRESETS:
        if r and abs(round(ch * r) - cw) <= 2:
            return name
    return "Custom"

# ---------------------------------------------------------------------------
# Themes. The game-inspired ones only borrow a mood (colours, font feel,
# decorations) - no logos or artwork from the games.
# ---------------------------------------------------------------------------
_ZONES_DEFAULT = {"preview_bg": "#1a1a1a", "zone_side": "#2c5282", "zone_center": "#276749",
                  "zone_hot": "#3b82f6", "zone_outline": "#e2e8f0", "zone_text": "#ffffff"}

THEMES = {
    "Light": dict(_ZONES_DEFAULT, native=True, fg="#000000", accent="#2563eb", accent_fg="#ffffff",
                  heading="#111111", hint="#555555", border="#9ca3af",
                  head=["Segoe UI Semibold", "Segoe UI"], body=["Segoe UI"], banner="plain"),
    "Dark": dict(_ZONES_DEFAULT, bg="#1e1f22", fg="#e6e6e6", field="#2b2d31", field_fg="#e6e6e6",
                 button="#2f3136", button_fg="#e6e6e6", hover="#3a3d43", border="#3a3c41",
                 accent="#3b82f6", accent_fg="#ffffff", heading="#f2f2f2", hint="#9aa0a6",
                 preview_bg="#141518", head=["Segoe UI Semibold", "Segoe UI"], body=["Segoe UI"],
                 banner="plain"),
    "Overwatch style": dict(
        bg="#0f1a2e", fg="#ffffff", field="#182640", field_fg="#ffffff", button="#22365c",
        button_fg="#ffffff", hover="#2d4777", border="#3a5a91", accent="#f99e1a", accent_fg="#0f1a2e",
        heading="#ffffff", hint="#a9bbd8", preview_bg="#0a1222", zone_side="#1f4f8f",
        zone_center="#2b6fd0", zone_hot="#f99e1a", zone_outline="#ffffff", zone_text="#ffffff",
        head=["Bahnschrift SemiBold Condensed", "Bahnschrift Condensed", "Bahnschrift", "Arial Black"],
        body=["Bahnschrift", "Segoe UI"], banner="overwatch", upper=True, slant="italic"),
    "Destiny 2 style": dict(
        bg="#0e1013", fg="#e8e9eb", field="#171a1f", field_fg="#e8e9eb", button="#1d2127",
        button_fg="#e8e9eb", hover="#2a2f37", border="#3b4049", accent="#e8e9eb", accent_fg="#0e1013",
        heading="#ffffff", hint="#8c929b", preview_bg="#08090b", zone_side="#2a2e35",
        zone_center="#4a2f63", zone_hot="#ceae33", zone_outline="#8c929b", zone_text="#ffffff",
        head=["Bahnschrift Light", "Segoe UI Light", "Segoe UI"], body=["Segoe UI"], head_weight="normal",
        banner="destiny", upper=True, spacing=4),
    "No Man's Sky style": dict(
        bg="#0c0e1a", fg="#f3efe4", field="#151931", field_fg="#f3efe4", button="#1c2142",
        button_fg="#f3efe4", hover="#283063", border="#3a4380", accent="#e8452c", accent_fg="#ffffff",
        heading="#f6c343", hint="#9aa3c7", preview_bg="#070814", zone_side="#1b7f86",
        zone_center="#a8361f", zone_hot="#f6c343", zone_outline="#f3efe4", zone_text="#ffffff",
        head=["Bahnschrift SemiBold", "Bahnschrift", "Segoe UI Black"], body=["Bahnschrift", "Segoe UI"],
        banner="nms", upper=True, spacing=5, blocks=["#e8452c", "#f6c343", "#1fb5ad"]),
    "Cyberpunk 2077 style": dict(
        bg="#0b0c10", fg="#e6f7f7", field="#12141b", field_fg="#e6f7f7", button="#161922",
        button_fg="#fcee0a", hover="#222634", border="#2a5560", accent="#fcee0a", accent_fg="#0b0c10",
        heading="#fcee0a", hint="#ff6b5e", preview_bg="#050608", zone_side="#0f3b44",
        zone_center="#4a0f1a", zone_hot="#fcee0a", zone_outline="#00f0ff", zone_text="#e6f7f7",
        head=["Bahnschrift SemiBold Condensed", "Bahnschrift Condensed", "Bahnschrift", "Consolas"],
        body=["Bahnschrift", "Segoe UI"], banner="cyber", upper=True, spacing=2,
        glitch1="#00f0ff", glitch2="#ff003c"),
}
for _t in THEMES.values():
    for _k, _v in _ZONES_DEFAULT.items():
        _t.setdefault(_k, _v)


# ---------------------------------------------------------------------------
# Pure logic (no Windows calls) - easy to test
# ---------------------------------------------------------------------------
def intersect(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def compute_zones(mon_rect, center_w, center_h, work_rect=None, aspect=None, height_pct=0,
                  position="center", valign="middle"):
    """mon_rect = (x, y, w, h). Returns dict of zone -> (x, y, w, h). Works for any monitor
    size or shape.

    The middle (game) area: `aspect` (width/height) keeps its shape on any monitor, `height_pct`
    is its height as a share of the monitor (0 = center_h pixels, or the full height), `position`
    puts it in the middle or at the left/right edge, `valign` places it vertically when it's
    shorter than the monitor. If it would be wider than the monitor it shrinks, keeping its shape.
    The middle can sit on top of the taskbar; the side screens stop at the taskbar (work_rect)
    so apps there aren't hidden behind it."""
    x, y, w, h = mon_rect
    if height_pct and 0 < height_pct <= 100:
        ch = round(h * height_pct / 100)
    else:
        ch = center_h if 0 < center_h <= h else h
    if aspect:
        cw = round(ch * aspect)
    else:
        cw = center_w if center_w > 0 else round(ch * 16 / 9)
    if cw > w:                       # too wide for this monitor: shrink to fit, keeping the shape
        if aspect:
            ch = round(w / aspect)
        cw = w
    cw = max(200, min(cw, w))
    ch = max(200, min(ch, h))
    if position == "left":
        left_w = 0
    elif position == "right":
        left_w = w - cw
    else:
        left_w = (w - cw) // 2
    right_w = w - cw - left_w
    if valign == "top":
        cy = y
    elif valign == "bottom":
        cy = y + h - ch
    else:
        cy = y + (h - ch) // 2
    left = (x, y, left_w, h)
    right = (x + left_w + cw, y, right_w, h)
    if work_rect:
        left, right = intersect(left, work_rect), intersect(right, work_rect)
    return {
        "left": left,
        "center": (x + left_w, cy, cw, ch),
        "right": right,
    }


def is_resize(start, end):
    """Did the user resize (drag an edge) rather than move the window?"""
    sx, sy, sw, sh = start
    ex, ey, ew, eh = end
    if (sw, sh) == (ew, eh):
        return False
    same_x_edge = sx == ex or sx + sw == ex + ew
    same_y_edge = sy == ey or sy + sh == ey + eh
    return same_x_edge and same_y_edge


def zone_at(zones, px, py):
    for name, (x, y, w, h) in zones.items():
        if w > 0 and h > 0 and x <= px < x + w and y <= py < y + h:
            return name
    return None


def rule_matches(rule, exe, title):
    if rule.get("exe"):
        return exe.lower() == rule["exe"].lower()
    return title == rule.get("title", "")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes.wintypes as wt

    # Use real pixels (not scaled) so 5120x1440 really is 5120x1440.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    shell32 = ctypes.WinDLL("shell32")

    def _fn(dll, name, restype, *argtypes):
        f = getattr(dll, name)
        f.restype = restype
        f.argtypes = list(argtypes)
        return f

    LONG_PTR = ctypes.c_ssize_t
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                    ("szDevice", wt.WCHAR * 32)]

    EnumWindows = _fn(user32, "EnumWindows", wt.BOOL, WNDENUMPROC, wt.LPARAM)
    IsWindow = _fn(user32, "IsWindow", wt.BOOL, wt.HWND)
    IsWindowVisible = _fn(user32, "IsWindowVisible", wt.BOOL, wt.HWND)
    IsIconic = _fn(user32, "IsIconic", wt.BOOL, wt.HWND)
    IsZoomed = _fn(user32, "IsZoomed", wt.BOOL, wt.HWND)
    GetWindow = _fn(user32, "GetWindow", wt.HWND, wt.HWND, wt.UINT)
    GetWindowTextLengthW = _fn(user32, "GetWindowTextLengthW", ctypes.c_int, wt.HWND)
    GetWindowTextW = _fn(user32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wt.DWORD,
                                   wt.HWND, ctypes.POINTER(wt.DWORD))
    GetWindowRect = _fn(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    SetWindowPos = _fn(user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
    GetWindowLongPtrW = _fn(user32, "GetWindowLongPtrW", LONG_PTR, wt.HWND, ctypes.c_int)
    SetWindowLongPtrW = _fn(user32, "SetWindowLongPtrW", LONG_PTR, wt.HWND, ctypes.c_int, LONG_PTR)
    ShowWindow = _fn(user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
    GetForegroundWindow = _fn(user32, "GetForegroundWindow", wt.HWND)
    RegisterHotKey = _fn(user32, "RegisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
    UnregisterHotKey = _fn(user32, "UnregisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int)
    GetMessageW = _fn(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
    PostThreadMessageW = _fn(user32, "PostThreadMessageW", wt.BOOL, wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
    ClipCursor = _fn(user32, "ClipCursor", wt.BOOL, ctypes.POINTER(wt.RECT))
    EnumDisplayMonitors = _fn(user32, "EnumDisplayMonitors", wt.BOOL, wt.HDC,
                              ctypes.POINTER(wt.RECT), MONITORENUMPROC, wt.LPARAM)
    GetMonitorInfoW = _fn(user32, "GetMonitorInfoW", wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW))
    WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG,
                                      wt.DWORD, wt.DWORD)
    SetWinEventHook = _fn(user32, "SetWinEventHook", wt.HANDLE, wt.DWORD, wt.DWORD, wt.HMODULE,
                          WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD)
    UnhookWinEvent = _fn(user32, "UnhookWinEvent", wt.BOOL, wt.HANDLE)
    GetCursorPos = _fn(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
    GetAsyncKeyState = _fn(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
    GetClassNameW = _fn(user32, "GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    SetForegroundWindow = _fn(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)

    OpenProcess = _fn(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    CloseHandle = _fn(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
    QueryFullProcessImageNameW = _fn(kernel32, "QueryFullProcessImageNameW", wt.BOOL,
                                     wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
    GetCurrentThreadId = _fn(kernel32, "GetCurrentThreadId", wt.DWORD)

    OpenProcessToken = _fn(advapi32, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
    GetTokenInformation = _fn(advapi32, "GetTokenInformation", wt.BOOL, wt.HANDLE, ctypes.c_int,
                              ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))

    DwmGetWindowAttribute = _fn(dwmapi, "DwmGetWindowAttribute", ctypes.c_long,
                                wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)
    DwmSetWindowAttribute = _fn(dwmapi, "DwmSetWindowAttribute", ctypes.c_long,
                                wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)
    IsUserAnAdmin = _fn(shell32, "IsUserAnAdmin", wt.BOOL)
    ShellExecuteW = _fn(shell32, "ShellExecuteW", ctypes.c_void_p, wt.HWND, wt.LPCWSTR,
                        wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int)

# Constants
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU = 0x00020000, 0x00010000, 0x00080000
WS_EX_DLGMODALFRAME, WS_EX_WINDOWEDGE = 0x1, 0x100
WS_EX_CLIENTEDGE, WS_EX_STATICEDGE, WS_EX_TOOLWINDOW = 0x200, 0x20000, 0x80
FRAME_STYLES = WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
FRAME_EXSTYLES = WS_EX_DLGMODALFRAME | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE | WS_EX_STATICEDGE
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x1, 0x2, 0x4
SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_NOOWNERZORDER = 0x10, 0x20, 0x200
SWP_ASYNCWINDOWPOS = 0x4000
MOVE_FLAGS = SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS
SWP_SHOWWINDOW = 0x40
SW_HIDE, SW_RESTORE = 0, 9
GW_OWNER, GW_HWNDPREV = 4, 3
HWND_TOPMOST, HWND_NOTOPMOST, HWND_TOP = -1, -2, 0
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x8, 0x20, 0x80000, 0x08000000
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MOVESIZEEND = 0x000A, 0x000B
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0, 0x2
OBJID_WINDOW = 0
VK_SHIFT, VK_CONTROL = 0x10, 0x11
TASKBAR_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")
# Windows-shell things that can be "focused" but aren't apps (taskbar, Start,
# search, Alt+Tab switcher, desktop...)
SHELL_CLASSES = TASKBAR_CLASSES + ("Progman", "WorkerW", "Windows.UI.Core.CoreWindow",
                                   "XamlExplorerHostIslandWindow", "MultitaskingViewFrame",
                                   "TaskSwitcherWnd", "ForegroundStaging", "NotifyIconOverflowWindow",
                                   "TopLevelWindowForOverflowXamlIsland")
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
PROCESS_QUERY_LIMITED_INFORMATION, TOKEN_QUERY, TOKEN_ELEVATION = 0x1000, 0x8, 20
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_L = 0x25, 0x26, 0x27, 0x28, 0x4C

HOTKEYS = {
    1: (MOD_CONTROL | MOD_ALT, VK_UP, "center", "Ctrl+Alt+Up"),
    2: (MOD_CONTROL | MOD_ALT, VK_LEFT, "left", "Ctrl+Alt+Left"),
    3: (MOD_CONTROL | MOD_ALT, VK_RIGHT, "right", "Ctrl+Alt+Right"),
    4: (MOD_CONTROL | MOD_ALT, VK_DOWN, "restore", "Ctrl+Alt+Down"),
    5: (MOD_CONTROL | MOD_ALT, VK_L, "lock", "Ctrl+Alt+L"),
    6: (MOD_CONTROL | MOD_ALT, 0x46, "fps", "Ctrl+Alt+F"),
}


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------
def get_monitors():
    mons = []

    def cb(hmon, hdc, lprect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if GetMonitorInfoW(hmon, ctypes.byref(info)):
            r, wk = info.rcMonitor, info.rcWork
            mons.append({
                "device": info.szDevice,
                "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
                "work": (wk.left, wk.top, wk.right - wk.left, wk.bottom - wk.top),
                "primary": bool(info.dwFlags & 1),
            })
        return True

    EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    mons.sort(key=lambda m: (m["rect"][0], m["rect"][1]))
    return mons


def window_title(hwnd):
    n = GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_pid(hwnd):
    pid = wt.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def exe_name(pid):
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        CloseHandle(h)


def process_elevated(pid):
    """True / False, or None if we can't tell."""
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = wt.HANDLE()
        if not OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            val, ret = wt.DWORD(), wt.DWORD()
            if GetTokenInformation(tok, TOKEN_ELEVATION, ctypes.byref(val), 4, ctypes.byref(ret)):
                return bool(val.value)
            return None
        finally:
            CloseHandle(tok)
    finally:
        CloseHandle(h)


def is_cloaked(hwnd):
    val = ctypes.c_int(0)
    DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return val.value != 0


def window_rect(hwnd):
    r = wt.RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def visible_rect(hwnd):
    """The rectangle you actually see (without Windows 10/11 invisible borders)."""
    r = wt.RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    return window_rect(hwnd)


class WinInfo:
    __slots__ = ("hwnd", "title", "exe", "pid")

    def __init__(self, hwnd, title, exe, pid):
        self.hwnd, self.title, self.exe, self.pid = hwnd, title, exe, pid


def list_windows(skip_pid):
    out = []

    def cb(hwnd, lparam):
        try:
            if not hwnd or not IsWindowVisible(hwnd) or GetWindow(hwnd, GW_OWNER):
                return True
            if GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            if is_cloaked(hwnd):
                return True
            title = window_title(hwnd)
            if not title:
                return True
            pid = window_pid(hwnd)
            if pid == skip_pid:
                return True
            exe = exe_name(pid)
            if title == "Program Manager" and exe.lower() == "explorer.exe":
                return True
            out.append(WinInfo(hwnd, title, exe, pid))
        except Exception:
            pass
        return True

    EnumWindows(WNDENUMPROC(cb), 0)
    return out


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def taskbars():
    out = []

    def cb(hwnd, lparam):
        try:
            if class_name(hwnd) in TASKBAR_CLASSES:
                out.append(hwnd)
        except Exception:
            pass
        return True

    EnumWindows(WNDENUMPROC(cb), 0)
    return out


def is_above(a, b):
    """True if window a is above window b in the Z-order."""
    h = GetWindow(b, GW_HWNDPREV)
    n = 0
    while h and n < 5000:
        if h == a:
            return True
        h = GetWindow(h, GW_HWNDPREV)
        n += 1
    return False


def cursor_pos():
    p = wt.POINT()
    GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def key_down(vk):
    return bool(GetAsyncKeyState(vk) & 0x8000)


class HotkeyThread(threading.Thread):
    """Registers global hotkeys + watches window drags. Pushes events into a queue."""

    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q
        self.tid = None
        self.failed = []
        self.ready = threading.Event()

    def on_win_event(self, hook, event, hwnd, id_object, id_child, thread, time_ms):
        try:
            if hwnd and id_object == OBJID_WINDOW and id_child == 0:
                if event == EVENT_SYSTEM_FOREGROUND:
                    self.q.put(("foreground", hwnd, None))
        except Exception:
            pass

    def run(self):
        self.tid = GetCurrentThreadId()
        for hk_id, (mods, vk, _action, name) in HOTKEYS.items():
            if not RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                self.failed.append(name)
        self._proc = WINEVENTPROC(self.on_win_event)   # keep a reference!
        # Only watch which window is focused (no snapping when you move real windows).
        hook = SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._proc,
                               0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        self.ready.set()
        msg = wt.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
                self.q.put(HOTKEYS[msg.wParam][2])
        if hook:
            UnhookWinEvent(hook)
        for hk_id in HOTKEYS:
            UnregisterHotKey(None, hk_id)

    def stop(self):
        if self.tid:
            PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


def startup_file():
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs\Startup", "G9 Splitter 2.cmd")


def pythonw_path():
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


# ---------------------------------------------------------------------------
# Start with Windows - fast. A Task Scheduler "at sign-in" task starts the app right
# away (programs in the Startup folder / Run list are held back by Windows for a while
# after you sign in). If Windows won't allow the task, the normal Run list is used.
# The app starts quietly in the tray (--tray).
# ---------------------------------------------------------------------------
STARTUP_NAME = os.path.splitext(os.path.basename(startup_file()))[0]     # "G9 Splitter 2" / "G9 Splitter"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_args():
    return f'"{os.path.abspath(__file__)}" --tray'


def run_elevated(exe, params, timeout=60):
    """Run a program with admin rights (Windows asks you first). Returns its exit code, or None
    if you said no / it couldn't start."""
    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                    ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                    ("dwHotKey", ctypes.c_uint32), ("hIconOrMonitor", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = ctypes.c_int
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = 0x40 | 0x8000                 # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", exe, params, 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        return None
    try:
        k32.WaitForSingleObject(info.hProcess, int(timeout * 1000))
        code = ctypes.c_uint32()
        k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        k32.CloseHandle(info.hProcess)


def _schtasks(args, elevated=False):
    import subprocess
    if elevated:
        return run_elevated("schtasks.exe", subprocess.list2cmdline(args)) == 0
    try:
        r = subprocess.run(["schtasks"] + args, capture_output=True, creationflags=0x08000000, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _task_xml():
    from xml.sax.saxutils import escape
    user = escape((os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")).strip("\\"))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts {escape(STARTUP_NAME)} in the tray when you sign in.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(pythonw_path())}</Command><Arguments>{escape(_startup_args())}</Arguments><WorkingDirectory>{escape(APP_DIR)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _run_key(value=None, delete=False):
    """Read (default), set or delete our entry in Windows' Run list."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if delete:
            try:
                winreg.DeleteValue(k, STARTUP_NAME)
            except OSError:
                pass
            return None
        if value is not None:
            winreg.SetValueEx(k, STARTUP_NAME, 0, winreg.REG_SZ, value)
            return value
        try:
            return winreg.QueryValueEx(k, STARTUP_NAME)[0]
        except OSError:
            return None


def startup_status():
    """'task' (fast), 'run' (Run list), 'legacy' (old Startup-folder file) or None (off)."""
    if _schtasks(["/Query", "/TN", STARTUP_NAME]):
        return "task"
    try:
        if _run_key():
            return "run"
    except Exception:
        pass
    return "legacy" if os.path.exists(startup_file()) else None


def enable_startup(allow_prompt=True):
    """Turn on 'start with Windows' the fast way. Returns 'task' or 'run' (or None if both failed)."""
    import tempfile
    try:
        if os.path.exists(startup_file()):
            os.remove(startup_file())              # the old, slow way
    except OSError:
        pass
    xml_path = os.path.join(tempfile.gettempdir(), "g9splitter_startup_task.xml")
    try:
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(_task_xml())
        args = ["/Create", "/TN", STARTUP_NAME, "/XML", xml_path, "/F"]
        if _schtasks(args) or (allow_prompt and _schtasks(args, elevated=True)):
            _run_key(delete=True)
            return "task"
    except Exception:
        log_error()
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass
    try:
        _run_key(f'"{pythonw_path()}" {_startup_args()}')
        return "run"
    except Exception:
        log_error()
        return None


def disable_startup():
    """Turn off 'start with Windows' (every way it could have been set up). True if it's off."""
    for fn in (lambda: os.path.exists(startup_file()) and os.remove(startup_file()),
               lambda: _run_key(delete=True)):
        try:
            fn()
        except Exception:
            log_error()
    if _schtasks(["/Query", "/TN", STARTUP_NAME]):
        args = ["/Delete", "/TN", STARTUP_NAME, "/F"]
        if not _schtasks(args) and not _schtasks(args, elevated=True):
            return False
    return True


# ---------------------------------------------------------------------------
# Tray icon (the little icons next to the clock). Plain Win32 - nothing to install.
# Click = open the app, right-click = menu (Open / Quit).
# ---------------------------------------------------------------------------
class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", ctypes.c_void_p), ("uID", ctypes.c_uint32),
                ("uFlags", ctypes.c_uint32), ("uCallbackMessage", ctypes.c_uint32), ("hIcon", ctypes.c_void_p),
                ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_uint32), ("dwStateMask", ctypes.c_uint32),
                ("szInfo", ctypes.c_wchar * 256), ("uTimeoutOrVersion", ctypes.c_uint32),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", ctypes.c_uint32),
                ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", ctypes.c_void_p)]


class TrayIcon:
    WM_TRAY = 0x8000 + 21                       # WM_APP + 21
    ID_OPEN, ID_QUIT = 1, 2

    def __init__(self, tip, icon_path, on_open, on_quit, open_label):
        self.tip, self.on_open, self.on_quit, self.open_label = tip, on_open, on_quit, open_label
        # Windows talks to the window while it's being created, so everything the
        # message handler looks at has to exist before that.
        self.ok = False
        self.hwnd = None
        self.hicon = None
        self.taskbar_created = 0
        u32 = self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        s32 = self.s32 = ctypes.WinDLL("shell32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]
        u32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u32.DefWindowProcW.restype = LRESULT
        u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u32.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        u32.CreateWindowExW.restype = ctypes.c_void_p
        u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        u32.LoadImageW.restype = ctypes.c_void_p
        u32.CreatePopupMenu.restype = ctypes.c_void_p
        u32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
        u32.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_void_p, ctypes.c_void_p]
        u32.DestroyMenu.argtypes = [ctypes.c_void_p]
        u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint32]
        u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u32.DestroyWindow.argtypes = [ctypes.c_void_p]
        u32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
        u32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u32.LoadIconW.restype = ctypes.c_void_p
        s32.Shell_NotifyIconW.argtypes = [ctypes.c_uint32, ctypes.POINTER(_NOTIFYICONDATAW)]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

        self._proc = self.WNDPROC(self._wndproc)     # keep a reference (or Windows calls freed memory)
        hinst = k32.GetModuleHandleW(None)
        cls = WNDCLASSW()
        cls.lpfnWndProc, cls.hInstance = self._proc, hinst
        cls.lpszClassName = tray_class(tip) + str(os.getpid())     # (another start finds us by this)
        if not u32.RegisterClassW(ctypes.byref(cls)):
            raise OSError("RegisterClassW failed")
        self._cls = cls
        self.hwnd = u32.CreateWindowExW(0, cls.lpszClassName, tip, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed")
        self.taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated") or 0
        cx, cy = u32.GetSystemMetrics(49), u32.GetSystemMetrics(50)     # small icon size
        self.hicon = (u32.LoadImageW(None, icon_path, 1, cx, cy, 0x10) if icon_path and os.path.exists(icon_path)
                      else None) or u32.LoadIconW(None, ctypes.c_void_p(32512))
        self.ok = self._add()

    def _data(self, flags):
        d = _NOTIFYICONDATAW()
        d.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        d.hWnd, d.uID, d.uFlags = self.hwnd, 1, flags
        d.uCallbackMessage, d.hIcon = self.WM_TRAY, self.hicon
        d.szTip = self.tip[:127]
        return d

    def _add(self):
        return bool(self.s32.Shell_NotifyIconW(0, ctypes.byref(self._data(0x1 | 0x2 | 0x4))))   # NIM_ADD

    def balloon(self, title, text):
        if not self.ok:
            return
        d = self._data(0x10)                                  # NIF_INFO
        d.szInfoTitle, d.szInfo, d.dwInfoFlags = title[:63], text[:255], 0x1
        self.s32.Shell_NotifyIconW(1, ctypes.byref(d))         # NIM_MODIFY

    def remove(self):
        try:
            if self.ok:
                self.s32.Shell_NotifyIconW(2, ctypes.byref(self._data(0)))   # NIM_DELETE
                self.ok = False
            if self.hwnd:
                self.u32.DestroyWindow(self.hwnd)
                self.hwnd = None
        except Exception:
            log_error()

    def _menu(self):
        u32 = self.u32
        menu = u32.CreatePopupMenu()
        u32.AppendMenuW(menu, 0, self.ID_OPEN, self.open_label)
        u32.AppendMenuW(menu, 0x800, 0, None)                  # separator
        u32.AppendMenuW(menu, 0, self.ID_QUIT, "Quit " + self.tip)
        pt = (ctypes.c_long * 2)()
        u32.GetCursorPos(pt)
        u32.SetForegroundWindow(self.hwnd)                     # so the menu closes when you click away
        cmd = u32.TrackPopupMenu(menu, 0x0100 | 0x0002, pt[0], pt[1], 0, self.hwnd, None)
        u32.PostMessageW(self.hwnd, 0, 0, 0)
        u32.DestroyMenu(menu)
        if cmd == self.ID_OPEN:
            self._open()
        elif cmd == self.ID_QUIT:
            self.on_quit()

    def _open(self):
        try:   # the click gives us the right to bring a window to the front - pass it on to the app window
            self.u32.AllowSetForegroundWindow(0xFFFFFFFF)
        except Exception:
            pass
        self.on_open()

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == self.WM_TRAY:
                ev = lp & 0xFFFF
                if ev in (0x0202, 0x0203, 0x0400):             # left click / double click / NIN_SELECT
                    self._open()
                elif ev in (0x0205, 0x007B):                   # right click / context menu
                    self._menu()
                return 0
            if msg == self.taskbar_created and self.taskbar_created:
                self.ok = self._add()                          # Explorer restarted: put the icon back
                return 0
        except Exception:
            log_error()
        return self.u32.DefWindowProcW(hwnd, msg, wp, lp)




# ---------------------------------------------------------------------------
# Version 2 engine: no visible Tk window. A hidden Tk root is only used as a
# timer loop and for the "show zones on my monitor" boxes.
# ---------------------------------------------------------------------------
WEB_DIR = os.path.join(APP_DIR, "web")
UI_HOST = os.path.join(APP_DIR, "ui_host.pyw")
LOCK_PATH = os.path.join(APP_DIR, ".running")
SHORTCUT_NAME = "G9 Splitter 2.lnk"
ZONE_CHOICES = [  # (label, zone, borderless)
    ("GAME", "center", True),
    ("Middle", "center", False),
    ("Left screen", "left", False),
    ("Right screen", "right", False),
    ("Ignored (full screen)", "ignore", False),   # not a spot: left completely alone
]


def zone_choice_label(zone, borderless):
    for label, z, b in ZONE_CHOICES:
        if z == zone and b == bool(borderless):
            return label
    return zone


# ---------------------------------------------------------------------------
# Program icons and live window previews (for the modern theme).
# Pure ctypes + zlib: no extra installs. Runs in the web-server threads so a
# slow program can never freeze the window logic.
# ---------------------------------------------------------------------------
import struct
import zlib


def png_encode(w, h, data, alpha=True):
    """Minimal PNG writer. data = RGBA (alpha=True) or RGB bytes, top row first."""
    bpp = 4 if alpha else 3
    stride = w * bpp
    raw = b"".join(b"\x00" + data[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if alpha else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))


def unblend(black, white):
    """Recover RGBA from the same icon drawn on black and on white (BGRA bytes)."""
    out = bytearray(len(black))
    for i in range(0, len(black), 4):
        a = 255 - (white[i + 1] - black[i + 1])
        a = 0 if a < 0 else (255 if a > 255 else a)
        if a:
            out[i] = min(255, black[i + 2] * 255 // a)
            out[i + 1] = min(255, black[i + 1] * 255 // a)
            out[i + 2] = min(255, black[i] * 255 // a)
            out[i + 3] = a
    return bytes(out)


def bgrx_to_rgb(buf):
    src = memoryview(buf)
    n = len(buf) // 4
    rgb = bytearray(n * 3)
    rgb[0::3] = src[2::4]
    rgb[1::3] = src[1::4]
    rgb[2::3] = src[0::4]
    return bytes(rgb)


if sys.platform == "win32":
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    GetDC = _fn(user32, "GetDC", wt.HDC, wt.HWND)
    ReleaseDC = _fn(user32, "ReleaseDC", ctypes.c_int, wt.HWND, wt.HDC)
    CreateCompatibleDC = _fn(gdi32, "CreateCompatibleDC", wt.HDC, wt.HDC)
    CreateCompatibleBitmap = _fn(gdi32, "CreateCompatibleBitmap", wt.HBITMAP, wt.HDC, ctypes.c_int, ctypes.c_int)
    CreateDIBSection = _fn(gdi32, "CreateDIBSection", wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                           ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
    SelectObject = _fn(gdi32, "SelectObject", wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
    DeleteObject = _fn(gdi32, "DeleteObject", wt.BOOL, wt.HGDIOBJ)
    DeleteDC = _fn(gdi32, "DeleteDC", wt.BOOL, wt.HDC)
    PatBlt = _fn(gdi32, "PatBlt", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD)
    StretchBlt = _fn(gdi32, "StretchBlt", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD)
    SetStretchBltMode = _fn(gdi32, "SetStretchBltMode", ctypes.c_int, wt.HDC, ctypes.c_int)
    SetBrushOrgEx = _fn(gdi32, "SetBrushOrgEx", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    GdiFlush = _fn(gdi32, "GdiFlush", wt.BOOL)
    DrawIconEx = _fn(user32, "DrawIconEx", wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, wt.HICON, ctypes.c_int,
                     ctypes.c_int, wt.UINT, wt.HBRUSH, wt.UINT)
    DestroyIcon = _fn(user32, "DestroyIcon", wt.BOOL, wt.HICON)
    PrintWindow = _fn(user32, "PrintWindow", wt.BOOL, wt.HWND, wt.HDC, wt.UINT)
    IsHungAppWindow = _fn(user32, "IsHungAppWindow", wt.BOOL, wt.HWND)
    SendMessageTimeoutW = _fn(user32, "SendMessageTimeoutW", LONG_PTR, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
                              wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t))
    GetClassLongPtrW = _fn(user32, "GetClassLongPtrW", ctypes.c_size_t, wt.HWND, ctypes.c_int)
    PrivateExtractIconsW = _fn(user32, "PrivateExtractIconsW", wt.UINT, wt.LPCWSTR, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.POINTER(wt.HICON), ctypes.POINTER(wt.UINT), wt.UINT, wt.UINT)

BLACKNESS, WHITENESS, SRCCOPY, HALFTONE = 0x00000042, 0x00FF0062, 0x00CC0020, 4


def exe_path(pid):
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        return buf.value if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        CloseHandle(h)


def _dib(hdc, w, h):
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h      # negative = top row first
    bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    hbmp = CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    return hbmp, bits


def icon_png(hicon, size=64):
    """Draw an icon on black and on white, work out the transparency, return PNG bytes."""
    screen = GetDC(None)
    mem = CreateCompatibleDC(screen)
    hbmp, bits = _dib(screen, size, size)
    if not hbmp or not bits.value:
        DeleteDC(mem)
        ReleaseDC(None, screen)
        return None
    old = SelectObject(mem, hbmp)
    try:
        shots = []
        for rop in (BLACKNESS, WHITENESS):
            PatBlt(mem, 0, 0, size, size, rop)
            DrawIconEx(mem, 0, 0, hicon, size, size, 0, None, 3)
            GdiFlush()
            shots.append(ctypes.string_at(bits, size * size * 4))
        return png_encode(size, size, unblend(shots[0], shots[1]))
    finally:
        SelectObject(mem, old)
        DeleteObject(hbmp)
        DeleteDC(mem)
        ReleaseDC(None, screen)


def window_icon_png(hwnd, path):
    """The program's icon: from its .exe (sharp, 64px), else from the window."""
    hicon, owned = None, False
    if path and not os.path.basename(path).lower() in ("applicationframehost.exe", "explorer.exe"):
        ico = wt.HICON()
        got = wt.UINT()
        if PrivateExtractIconsW(path, 0, 64, 64, ctypes.byref(ico), ctypes.byref(got), 1, 0) and ico.value:
            hicon, owned = ico.value, True
    if not hicon:
        res = ctypes.c_size_t()
        for which in (1, 2, 0):          # ICON_BIG, ICON_SMALL2, ICON_SMALL
            if SendMessageTimeoutW(hwnd, 0x7F, which, 0, 2, 150, ctypes.byref(res)) and res.value:
                hicon = res.value
                break
        if not hicon:
            hicon = GetClassLongPtrW(hwnd, -14) or GetClassLongPtrW(hwnd, -34)
    if not hicon:
        return None
    try:
        return icon_png(hicon, 64)
    finally:
        if owned:
            DestroyIcon(hicon)


def window_thumb_png(hwnd, max_w=400):
    """A small picture of a window (it asks the program to draw itself, so the
    window doesn't have to be visible)."""
    if not IsWindow(hwnd) or IsIconic(hwnd) or IsHungAppWindow(hwnd):
        return None
    x, y, w, h = window_rect(hwnd)
    if w < 20 or h < 20:
        return None
    tw = min(max_w, w)
    th = max(1, round(h * tw / w))
    screen = GetDC(None)
    big_dc = CreateCompatibleDC(screen)
    big = CreateCompatibleBitmap(screen, w, h)
    old_big = SelectObject(big_dc, big)
    small_dc = CreateCompatibleDC(screen)
    small, bits = _dib(screen, tw, th)
    old_small = SelectObject(small_dc, small)
    try:
        if not big or not small or not bits.value:
            return None
        if not PrintWindow(hwnd, big_dc, 2):          # PW_RENDERFULLCONTENT
            return None
        SetStretchBltMode(small_dc, HALFTONE)
        SetBrushOrgEx(small_dc, 0, 0, None)
        vx, vy, vw, vh = visible_rect(hwnd)            # crop the invisible borders
        sx, sy = max(0, vx - x), max(0, vy - y)
        sw, sh = min(vw, w - sx), min(vh, h - sy)
        StretchBlt(small_dc, 0, 0, tw, th, big_dc, sx, sy, sw, sh, SRCCOPY)
        GdiFlush()
        data = ctypes.string_at(bits, tw * th * 4)
        return png_encode(tw, th, bgrx_to_rgb(data), alpha=False)
    finally:
        SelectObject(big_dc, old_big)
        DeleteObject(big)
        DeleteDC(big_dc)
        SelectObject(small_dc, old_small)
        DeleteObject(small)
        DeleteDC(small_dc)
        ReleaseDC(None, screen)


class ImageCache:
    """Caches icons (per program) and previews (a few seconds) for the interface."""

    def __init__(self, engine):
        self.engine = engine
        self.icons = {}
        self.thumbs = {}
        self.lock = threading.Lock()

    def icon(self, hwnd):
        try:
            pid = window_pid(hwnd)
            path = exe_path(pid)
            key = path.lower() if path and not path.lower().endswith(("applicationframehost.exe", "explorer.exe")) \
                else f"hwnd:{hwnd}"
            with self.lock:
                if key in self.icons:
                    return self.icons[key]
            png = window_icon_png(hwnd, path)
            with self.lock:
                self.icons[key] = png
            return png
        except Exception:
            log_error()
            return None

    def thumb(self, hwnd):
        # Never ask the game to draw itself (it could cause a stutter) - games show their icon instead.
        if hwnd in self.engine.ontop or hwnd in self.engine.saved_styles:
            return None
        now = time.time()
        with self.lock:
            hit = self.thumbs.get(hwnd)
            if hit and now - hit[0] < 2.5:
                return hit[1]
        try:
            png = window_thumb_png(hwnd)
        except Exception:
            log_error()
            png = None
        with self.lock:
            self.thumbs[hwnd] = (now, png)
            if len(self.thumbs) > 80:
                for k in sorted(self.thumbs, key=lambda k: self.thumbs[k][0])[:40]:
                    self.thumbs.pop(k, None)
        return png


# ---------------------------------------------------------------------------
# Automatic game detection: any game you launch is recognised, put in the
# middle (no border) and remembered - no setup needed.
# ---------------------------------------------------------------------------
GAME_DIR_HINTS = ("\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\battle.net\\",
                  "\\ea games\\", "\\origin games\\", "\\ubisoft game launcher\\games\\", "\\ubisoft\\",
                  "\\gog galaxy\\games\\", "\\gog games\\", "\\xboxgames\\", "\\rockstar games\\",
                  "\\blizzard entertainment\\", "\\wargaming.net\\", "\\bethesda.net launcher\\games\\",
                  "\\itch\\apps\\", "\\amazon games\\library\\", "\\games\\")
GAME_WINDOW_CLASSES = {"unrealwindow", "unitywndclass", "sdl_app", "valve001", "cryengine", "glfw30",
                       "riotwindowclass", "launchunrealuwindowsclient", "ogred3d11wnd",
                       "frostbite"}
NOT_GAME_EXES = {
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "epicwebhelper.exe", "unrealcefsubprocess.exe",
    "battle.net.exe", "agent.exe", "riotclientservices.exe", "riot client.exe", "riotclientux.exe",
    "riotclientuxrender.exe", "leagueclient.exe", "leagueclientux.exe", "leagueclientuxrender.exe",
    "eadesktop.exe", "eabackgroundservice.exe", "origin.exe", "ubisoftconnect.exe", "upc.exe",
    "ubisoftgamelauncher.exe", "galaxyclient.exe", "rockstarlauncher.exe", "socialclubhelper.exe",
    "playnite.desktopapp.exe", "xboxpcapp.exe", "gamebar.exe", "explorer.exe", "applicationframehost.exe",
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "brave.exe", "discord.exe",
    "spotify.exe", "obs64.exe", "pythonw.exe", "python.exe", "presentmon.exe", "msedgewebview2.exe",
    "code.exe", "devenv.exe", "unrealeditor.exe", "unity.exe", "unity hub.exe", "blender.exe",
}
NOT_GAME_WORDS = ("launcher", "crashreport", "crashhandler", "crashpad", "setup", "install", "updater",
                  "easyanticheat", "beservice", "battleye", "redist", "uninst", "webhelper", "cefprocess",
                  "overlay", "bootstrap", "patcher", "helper", "editor")


def _exe_path(pid):
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        return buf.value if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        CloseHandle(h)


# ---------------------------------------------------------------------------
# Per-app mute. Uses Windows' own volume mixer (Core Audio) - the same switch as
# "Volume mixer" in Windows settings - with plain ctypes, no extra installs.
# A program is muted on every speaker/headset, including sound it starts later.
# ---------------------------------------------------------------------------
import uuid as _uuid


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                ("d4", ctypes.c_ubyte * 8)]


def _guid(s):
    return _GUID.from_buffer_copy(_uuid.UUID(s).bytes_le)


def _com(ptr, index, *argtypes):
    """Method number `index` of the COM object at `ptr` (returns an HRESULT)."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(ptr):
    if ptr:
        try:
            vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)
        except Exception:
            pass


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260)]


_names_cache = [0.0, {}]


def process_names():
    """{pid: exe name} for every running process, from Windows' process list (works even for
    processes that refuse to be opened). Cached for 2 seconds."""
    if time.time() - _names_cache[0] < 2:
        return _names_cache[1]
    names = {}
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snap_fn = k32.CreateToolhelp32Snapshot
        snap_fn.restype, snap_fn.argtypes = ctypes.c_void_p, [ctypes.c_uint32, ctypes.c_uint32]
        first, nxt = k32.Process32FirstW, k32.Process32NextW
        for f in (first, nxt):
            f.restype, f.argtypes = ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        close = k32.CloseHandle
        close.restype, close.argtypes = ctypes.c_int, [ctypes.c_void_p]
        snap = snap_fn(0x2, 0)                          # TH32CS_SNAPPROCESS
        if snap and snap != ctypes.c_void_p(-1).value:
            try:
                e = _PROCESSENTRY32W()
                e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                ok = first(snap, ctypes.byref(e))
                for _ in range(100000):
                    if ok != 1:
                        break
                    names[int(e.th32ProcessID)] = e.szExeFile
                    ok = nxt(snap, ctypes.byref(e))
            finally:
                close(snap)
    except Exception:
        log_error()
    _names_cache[0], _names_cache[1] = time.time(), names
    return names


class AudioMixer:
    CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    IID_SESSION_MANAGER2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
    IID_SESSION_CONTROL2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
    IID_SIMPLE_VOLUME = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"
    CLSCTX_ALL = 0x17
    CLSCTX_INPROC_SERVER = 0x1
    LOG = "g9_splitter_audio.log"

    def __init__(self):
        self.ok = None              # None = not tried yet, False = mixer unavailable
        self.enumerator = None
        self.muted = set()          # programs you muted here (lower-case exe names)
        self.seen = {}              # exe -> process ids we already muted (new ones get muted too)
        self.state = {}             # exe -> True muted / False has sound, not muted
        self.exe_cache = {}
        self.trace = []             # step-by-step record of the last read (written to the audio log)
        self.logged_start = False
        self._q = None              # all Windows sound work runs on one background thread (see _run)
        self._thread = None
        self.com_init = None

    # ----- one background thread owns everything Core Audio -----
    # Windows' audio API is meant to be used from a "multi-threaded" COM thread; the app's main
    # (window) thread isn't one, so the mixer gets its own.
    def _worker(self):
        try:
            self.com_init = ctypes.windll.ole32.CoInitializeEx(None, 0)     # COINIT_MULTITHREADED
        except Exception:
            log_error()
        while True:
            fn, box, done = self._q.get()
            try:
                box["result"] = fn()
            except Exception:
                log_error()
            done.set()

    def _run(self, fn, default=None):
        if self._thread is None:
            self._q = queue.Queue()
            self._thread = threading.Thread(target=self._worker, name="g9-audio", daemon=True)
            self._thread.start()
        box, done = {}, threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(5):
            return default
        return box.get("result", default)

    # ----- raw Core Audio (only ever called on the audio thread) -----
    def _hr(self, hr):
        return f"{hr & 0xFFFFFFFF:#010x}" if isinstance(hr, int) else str(hr)

    def _start(self):
        ole32 = ctypes.windll.ole32
        p = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(_guid(self.CLSID_ENUMERATOR)), None, self.CLSCTX_ALL,
                                    ctypes.byref(_guid(self.IID_ENUMERATOR)), ctypes.byref(p))
        self.trace.append(f"COM init {self._hr(self.com_init or 0)}, create device enumerator {self._hr(hr)}")
        if hr < 0 or not p.value:
            raise OSError(f"MMDeviceEnumerator failed: {self._hr(hr)}")
        self.enumerator = p.value

    def _qi(self, ptr, iid):
        out = ctypes.c_void_p()
        hr = _com(ptr, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            ptr, ctypes.byref(_guid(iid)), ctypes.byref(out))
        return (out.value if hr >= 0 else None), hr

    def exe_of(self, pid):
        exe = self.exe_cache.get(pid)
        if exe is None:
            if len(self.exe_cache) > 500:
                self.exe_cache.clear()
            exe = (exe_name(pid) or "").lower()
            if not exe:
                # Browsers (Opera, Chrome, Edge...) and some games play sound from a locked-down
                # helper process that can't be opened - Windows' process list still has its name.
                exe = process_names().get(pid, "").lower()
            self.exe_cache[pid] = exe
        return exe

    def _device_sessions(self, dev, label, out):
        c_void_p, byref, POINTER, T = ctypes.c_void_p, ctypes.byref, ctypes.POINTER, self.trace
        mgr = c_void_p()
        activate = _com(dev, 3, POINTER(_GUID), ctypes.c_uint32, c_void_p, POINTER(c_void_p))
        hr = activate(dev, byref(_guid(self.IID_SESSION_MANAGER2)), self.CLSCTX_ALL, None, byref(mgr))
        if hr < 0 or not mgr.value:
            T.append(f"  {label}: open session manager {self._hr(hr)} - trying again")
            hr = activate(dev, byref(_guid(self.IID_SESSION_MANAGER2)), self.CLSCTX_INPROC_SERVER, None, byref(mgr))
        if hr < 0 or not mgr.value:
            T.append(f"  {label}: open session manager FAILED {self._hr(hr)}")
            return
        try:
            sessions = c_void_p()
            hr = _com(mgr.value, 5, POINTER(c_void_p))(mgr.value, byref(sessions))
            if hr < 0 or not sessions.value:
                T.append(f"  {label}: list sound streams FAILED {self._hr(hr)}")
                return
            try:
                count = ctypes.c_int()
                hr = _com(sessions.value, 3, POINTER(ctypes.c_int))(sessions.value, byref(count))
                T.append(f"  {label}: {count.value} sound stream(s) (count {self._hr(hr)})")
                for j in range(count.value):
                    ctl = c_void_p()
                    hr = _com(sessions.value, 4, ctypes.c_int, POINTER(c_void_p))(sessions.value, j, byref(ctl))
                    if hr < 0 or not ctl.value:
                        T.append(f"    stream {j}: get FAILED {self._hr(hr)}")
                        continue
                    try:
                        ctl2, hr = self._qi(ctl.value, self.IID_SESSION_CONTROL2)
                        if not ctl2:
                            T.append(f"    stream {j}: details FAILED {self._hr(hr)}")
                            continue
                        pid = ctypes.c_uint32()
                        try:
                            hr = _com(ctl2, 14, POINTER(ctypes.c_uint32))(ctl2, byref(pid))
                        finally:
                            _release(ctl2)
                        if hr < 0 or not pid.value:      # 0 = Windows' own system sounds
                            T.append(f"    stream {j}: system sounds / no process (pid {pid.value}, {self._hr(hr)})")
                            continue
                        vol, hr = self._qi(ctl.value, self.IID_SIMPLE_VOLUME)
                        exe = self.exe_of(pid.value)
                        if not vol:
                            T.append(f"    stream {j}: {exe or '?'} pid {pid.value}: volume control FAILED {self._hr(hr)}")
                            continue
                        T.append(f"    stream {j}: {exe or '(unknown name)'} pid {pid.value}, muted={self._get_mute(vol)}")
                        out.append((exe, pid.value, vol))
                    finally:
                        _release(ctl.value)
            finally:
                _release(sessions.value)
        finally:
            _release(mgr.value)

    def _sessions(self):
        """[(exe, pid, ISimpleAudioVolume pointer)] for every sound stream on every output
        device. The caller must _release() the pointers."""
        c_void_p, byref, POINTER, T = ctypes.c_void_p, ctypes.byref, ctypes.POINTER, self.trace
        out = []
        coll = c_void_p()
        hr = _com(self.enumerator, 3, ctypes.c_int, ctypes.c_uint32, POINTER(c_void_p))(
            self.enumerator, 0, 1, byref(coll))           # eRender, DEVICE_STATE_ACTIVE
        if hr < 0 or not coll.value:
            raise OSError(f"EnumAudioEndpoints failed: {self._hr(hr)}")
        try:
            n = ctypes.c_uint32()
            hr = _com(coll.value, 3, POINTER(ctypes.c_uint32))(coll.value, byref(n))
            T.append(f"output devices: {n.value} ({self._hr(hr)})")
            for i in range(n.value):
                dev = c_void_p()
                hr = _com(coll.value, 4, ctypes.c_uint32, POINTER(c_void_p))(coll.value, i, byref(dev))
                if hr < 0 or not dev.value:
                    T.append(f"  device {i}: get FAILED {self._hr(hr)}")
                    continue
                try:
                    self._device_sessions(dev.value, f"device {i}", out)
                finally:
                    _release(dev.value)
        finally:
            _release(coll.value)
        if not out:
            # nothing found on the list of devices - also ask for the default speakers directly
            for role in (0, 1, 2):                       # eConsole, eMultimedia, eCommunications
                dev = c_void_p()
                hr = _com(self.enumerator, 4, ctypes.c_int, ctypes.c_int, POINTER(c_void_p))(
                    self.enumerator, 0, role, byref(dev))
                if hr < 0 or not dev.value:
                    T.append(f"  default device (role {role}): get FAILED {self._hr(hr)}")
                    continue
                try:
                    self._device_sessions(dev.value, f"default device (role {role})", out)
                finally:
                    _release(dev.value)
                if out:
                    break
        return out

    def _get_mute(self, vol):
        b = ctypes.c_int()
        _com(vol, 6, ctypes.POINTER(ctypes.c_int))(vol, ctypes.byref(b))
        return bool(b.value)

    def _set_mute(self, vol, mute):
        return _com(vol, 5, ctypes.c_int, ctypes.c_void_p)(vol, 1 if mute else 0, None)

    def _open(self):
        if self.ok is False:
            return None
        self.trace = []
        try:
            if self.enumerator is None:
                self._start()
            sess = self._sessions()
            self.ok = True
            return sess
        except Exception:
            log_error()
            self.trace.append("ERROR: " + traceback.format_exc().strip().splitlines()[-1])
            self._write_log("could not read Windows' volume mixer")
            self.ok = False                 # don't keep hammering a broken mixer
            return None

    def _write_log(self, headline):
        try:   # what Windows reported - shows exactly where muting goes wrong on a PC
            with open(os.path.join(APP_DIR, self.LOG), "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + headline + "\n" + "\n".join(self.trace) + "\n")
        except Exception:
            pass

    # ----- what the app uses (safe to call from any thread) -----
    def refresh(self):
        """Read every program's mute state, and mute new sound from programs you muted."""
        return self._run(self._refresh, self.state)

    def set_muted(self, exe, mute):
        """Mute / unmute a program everywhere. Returns how many of its sound streams were changed
        (0 = it isn't making sound right now), or None if Windows' mixer can't be reached."""
        return self._run(lambda: self._set_muted(exe, mute), None)

    def is_muted(self, exe):
        """True muted, False has sound, None no sound right now."""
        return self.state.get((exe or "").lower())

    # ----- the work itself (audio thread) -----
    def _refresh(self):
        sess = self._open()
        if sess is None:
            self.state = {}
            return self.state
        state = {}
        try:
            for exe, pid, vol in sess:
                if not exe:
                    continue
                if exe in self.muted and pid not in self.seen.setdefault(exe, set()):
                    self._set_mute(vol, True)           # it started a new sound stream - keep it quiet
                    self.seen[exe].add(pid)
                state[exe] = state.get(exe, True) and self._get_mute(vol)
        except Exception:
            log_error()
        finally:
            for _, _, vol in sess:
                _release(vol)
        if not self.logged_start:                       # one snapshot at start-up, for troubleshooting
            self.logged_start = True
            self._write_log(f"start-up check: {len(sess)} sound stream(s) found")
        for exe in list(self.muted):
            if state.get(exe) is False:     # you turned it back on in Windows' volume mixer
                self.muted.discard(exe)
                self.seen.pop(exe, None)
        for exe in self.muted:
            state.setdefault(exe, True)     # no sound yet - it'll be muted as soon as it makes some
        self.state = state
        return state

    def _set_muted(self, exe, mute):
        exe = (exe or "").lower()
        sess = self._open()
        if sess is None or not exe:
            return None
        changed = 0
        try:
            for e, pid, vol in sess:
                if e == exe:
                    self.trace.append(f"  .. {'muting' if mute else 'unmuting'} {e} pid {pid}")
                    self._write_log(f"{'mute' if mute else 'unmute'} {exe}: in progress")
                    hr = self._set_mute(vol, mute)
                    changed += 1
                    self.trace.append(f"  -> {'mute' if mute else 'unmute'} {e} pid {pid}: {self._hr(hr)}, "
                                      f"now muted={self._get_mute(vol)}")
                    if mute:
                        self.seen.setdefault(exe, set()).add(pid)
        finally:
            for _, _, vol in sess:
                _release(vol)
        self._write_log(f"{'mute' if mute else 'unmute'} {exe}: {changed} stream(s) changed")
        if mute:
            self.muted.add(exe)
        else:
            self.muted.discard(exe)
            self.seen.pop(exe, None)
        self._refresh()
        return changed


class GameDetector:
    """Decides whether a window is a game (all signals are read-only and harmless)."""

    def __init__(self):
        self._known = set()      # games Windows itself has recognised (Game Bar's list)
        self._known_at = 0
        self._cache = {}         # hwnd -> (time, exe, reason)

    def windows_known_games(self):
        now = time.time()
        if now - self._known_at > 60:
            self._known_at = now
            found = set()
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children") as root:
                    i = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(root, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(root, sub) as k:
                                path, _ = winreg.QueryValueEx(k, "MatchedExeFullPath")
                                if path:
                                    found.add(os.path.basename(str(path)).lower())
                        except OSError:
                            pass
            except Exception:
                pass
            self._known = found
        return self._known

    def why_game(self, w):
        """A short reason if this window looks like a game, else None (cached for 2 s)."""
        now = time.time()
        hit = self._cache.get(w.hwnd)
        if hit and now - hit[0] < 2 and hit[1] == w.exe:
            return hit[2]
        why = self._why_game(w)
        if len(self._cache) > 400:
            self._cache.clear()
        self._cache[w.hwnd] = (now, w.exe, why)
        return why

    def _why_game(self, w):
        exe = (w.exe or "").lower()
        if not exe or exe in NOT_GAME_EXES or any(word in exe for word in NOT_GAME_WORDS):
            return None
        try:
            x, y, ww, hh = window_rect(w.hwnd)
        except Exception:
            return None
        if ww < 640 or hh < 400:
            return None
        cls = class_name(w.hwnd).lower()
        if cls in GAME_WINDOW_CLASSES:
            return "game engine window"
        path = _exe_path(w.pid).lower()
        if any(d in path for d in GAME_DIR_HINTS):
            return "installed in a games folder"
        if exe in self.windows_known_games():
            return "Windows lists it as a game"
        return None


# ---------------------------------------------------------------------------
# FPS counter. Uses Intel PresentMon (free, open source) which reads Windows'
# own frame tracing - nothing is injected into the game, so it's safe with
# anti-cheat. Windows only lets admins (or "Performance Log Users") use it.
# ---------------------------------------------------------------------------
import collections

FPS_COLUMNS = ("MsBetweenPresents", "msBetweenPresents", "FrameTime", "MsBetweenDisplayChange",
               "msBetweenDisplayChange")


class FpsMonitor:
    TOOLS = os.path.join(APP_DIR, "tools")

    def __init__(self):
        self.proc = None
        self.pid = None
        self.frames = collections.deque(maxlen=3000)   # (arrival time, frame time in ms)
        self.lock = threading.Lock()
        self.message = None                            # for the status bar
        self.downloading = False
        self.download_failed_at = 0
        self.failed_at = {}
        self.last_lines = collections.deque(maxlen=8)

    def exe(self):
        p = os.path.join(self.TOOLS, "PresentMon.exe")
        return p if os.path.exists(p) else None

    def download(self):
        if self.downloading or time.time() - self.download_failed_at < 60:
            return
        self.downloading = True
        self.message = "Downloading the free FPS tool (Intel PresentMon) - one time only..."
        threading.Thread(target=self._download, daemon=True).start()

    def _download(self):
        import re
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/GameTechDev/PresentMon/releases?per_page=20",
                headers={"User-Agent": "G9Splitter", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                releases = json.load(r)
            url = None
            for prerelease_ok in (False, True):
                for rel in releases:
                    if rel.get("draft") or (rel.get("prerelease") and not prerelease_ok):
                        continue
                    for a in rel.get("assets", []):
                        n = a.get("name", "")
                        if (re.match(r"(?i)^presentmon.*x64\.exe$", n)
                                and not re.search(r"(?i)setup|install|arm|service|capture", n)):
                            url = a.get("browser_download_url")
                            break
                    if url:
                        break
                if url:
                    break
            if not url:
                raise RuntimeError("no PresentMon download found")
            os.makedirs(self.TOOLS, exist_ok=True)
            tmp = os.path.join(self.TOOLS, "PresentMon.download")
            req = urllib.request.Request(url, headers={"User-Agent": "G9Splitter"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, os.path.join(self.TOOLS, "PresentMon.exe"))
            self.message = "FPS tool ready."
        except Exception:
            log_error()
            self.download_failed_at = time.time()
            self.message = "Couldn't download the FPS tool - check your internet connection."
        finally:
            self.downloading = False

    def start(self, pid, force=False):
        self.stop()
        exe = self.exe()
        if not exe:
            self.download()
            return
        if not force and time.time() - self.failed_at.get(pid, 0) < 60:
            self.pid = pid
            return
        self.pid = pid
        with self.lock:
            self.frames.clear()
        threading.Thread(target=self._run, args=(exe, pid), daemon=True).start()

    def _run(self, exe, pid):
        import subprocess
        self.last_lines.clear()
        for dash in ("--", "-"):          # PresentMon 2.x uses --options, 1.x uses -options
            args = [exe, dash + "process_id", str(pid), dash + "output_stdout", dash + "stop_existing_session",
                    dash + "session_name", "G9SplitterFPS", dash + "terminate_on_proc_exit"]
            try:
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=0x08000000,
                                     text=True, encoding="utf-8", errors="replace")
            except Exception:
                log_error()
                break
            self.proc = p
            col, got = None, False
            for line in p.stdout:
                if self.proc is not p:
                    break
                line = line.strip()
                if not line:
                    continue
                if col is None:
                    parts = [x.strip() for x in line.split(",")]
                    if "ProcessID" in parts or "Application" in parts:
                        col = next((parts.index(c) for c in FPS_COLUMNS if c in parts), None)
                    if col is None:
                        self.last_lines.append(line[:200])
                    continue
                try:
                    ms = float(line.split(",")[col])
                except (ValueError, IndexError):
                    continue
                if ms > 0:
                    got = True
                    with self.lock:
                        self.frames.append((time.time(), ms))
            try:
                p.wait(5)
            except Exception:
                pass
            if self.proc is not p or got:
                return                        # stopped on purpose, or the game closed
            if col is None and dash == "--":
                continue                      # maybe an older PresentMon - try 1.x options
            break
        self.failed_at[pid] = time.time()
        txt = " ".join(self.last_lines).lower()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "PresentMon: " + " | ".join(self.last_lines) + "\n")
        except Exception:
            pass
        if any(k in txt for k in ("access", "admin", "privilege", "permission", "elevat")) or not txt:
            self.message = "The FPS counter needs admin rights - click 'Restart as admin'."
        else:
            self.message = "The FPS counter couldn't start: " + list(self.last_lines)[-1][:120]

    def stop(self):
        p, self.proc, self.pid = self.proc, None, None
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def current(self):
        """(fps, average frame time in ms) over about the last second, or None."""
        with self.lock:
            if not self.frames or time.time() - self.frames[-1][0] > 2.5:
                return None
            total, n = 0.0, 0
            for _, ms in reversed(self.frames):
                total += ms
                n += 1
                if total >= 1000:
                    break
        avg = total / n
        return 1000.0 / avg, avg


class Engine:
    def __init__(self, root):
        self.root = root
        root.withdraw()
        self.cfg = load_config()
        if self.cfg.get("theme") == "Destiny 2 style (modern)":   # now just "Destiny 2 style"
            self.cfg["theme"] = "Destiny 2 style"
            save_config(self.cfg)
        if self.cfg.get("theme") not in THEMES:
            self.cfg["theme"] = "Dark" if self.cfg.get("dark_mode") else "Light"
        self.pid = os.getpid()
        self.is_admin = bool(IsUserAnAdmin())
        self.monitors = get_monitors()
        self.mon_idx = 0
        self.saved_styles = {}
        self.paused = set()
        self.last_apply = {}
        self.rule_lock_hwnds = set()
        self.lock_hwnd = None
        self.clipping = False
        self.ontop = set()
        self.windows = {}
        self.zone_of = {}            # hwnd -> zone it currently fills
        self.ui_pids = set()         # our own interface window(s) - hidden from the list
        self.q = queue.Queue()
        self.status = "Ready. Drag a program from the list onto a box."
        self.status_id = 0
        self.snap_lock = threading.Lock()
        self.snapshot_json = "{}"
        self.overlay_hide_job = None
        self.closing = False
        self.on_quit = None
        self.fps = FpsMonitor()
        self.games = GameDetector()
        self.mixer = AudioMixer()
        self.tray = None
        self.ui = None

        # start with Windows: find out how it's set up (and move the old, slow way to the fast one)
        self.startup_on = os.path.exists(startup_file())

        def _startup_check():
            try:
                how = startup_status()
                if how == "legacy":
                    how = enable_startup(allow_prompt=False)
                self.startup_on = how is not None
            except Exception:
                log_error()
            self._startup_checked = True
        threading.Thread(target=_startup_check, daemon=True).start()
        self.images = ImageCache(self)

        self.pick_monitor()
        self.build_overlays()
        self.refresh_windows()
        self.update_zone_contents()

        self.hotkeys = HotkeyThread(self.q)
        self.hotkeys.start()
        self.hotkeys.ready.wait(2)
        if self.hotkeys.failed:
            self.set_status("These hotkeys are used by another program: " + ", ".join(self.hotkeys.failed))
        self.build_snapshot()

        root.after(25, self.poll_queue)
        root.after(1000, self.rules_tick)
        root.after(200, self.mouse_tick)
        root.after(200, self.ontop_tick)
        root.after(1000, self.windows_tick)
        root.after(700, self.fps_tick)

    # ---------------- status / snapshot for the interface ----------------
    def set_status(self, text):
        self.status = text
        self.status_id += 1

    @property
    def ontop_on(self):
        return bool(self.cfg.get("game_on_top", True))

    @property
    def auto_on(self):
        return bool(self.cfg.get("auto_apply", True))

    def build_snapshot(self):
        zones = self.zones()
        mon = self.monitors[self.mon_idx] if self.monitors else {"device": "", "rect": (0, 0, 1920, 1080),
                                                                  "work": (0, 0, 1920, 1080), "primary": True}
        cw, ch = zones["center"][2], zones["center"][3]
        preset = preset_name_for(self.cfg, cw, ch)
        ga = game_area_options(self.cfg)
        wins = []
        for w in self.windows.values():
            wins.append({"hwnd": str(w.hwnd), "title": w.title, "exe": w.exe or "",
                         "muted": self.mixer.is_muted(w.exe), "ignored": self.is_ignored(w.exe),
                         "zone": self.zone_of.get(w.hwnd),
                         "borderless": self.zone_of.get(w.hwnd) == "center" and self.is_game_window(w.hwnd)})
            if wins[-1]["zone"] == "center":     # is a game sitting in the middle? (labels it GAME vs Middle)
                wins[-1]["game"] = wins[-1]["borderless"] or self.looks_like_game(w)
        rules = []
        for i, r in enumerate(self.cfg["rules"]):
            rules.append({"index": i, "app": r.get("exe") or r.get("title", ""),
                          "where": zone_choice_label(r["zone"], r.get("borderless"))
                          + (" · auto-detected game" if r.get("auto") else ""),
                          "zone": r["zone"], "lock_mouse": bool(r.get("lock_mouse"))})
        snap = {
            "status": self.status, "status_id": self.status_id,
            "theme": self.cfg["theme"], "is_admin": self.is_admin,
            "startup": bool(self.startup_on),
            "monitors": [{"device": m["device"], "label": self.monitor_label(m)} for m in self.monitors],
            "monitor": mon["device"],
            "mon": list(mon["rect"]), "work": list(mon.get("work") or mon["rect"]),
            "zones": {k: list(v) for k, v in zones.items()},
            "presets": [p[0] for p in PRESETS], "preset": preset,
            "layout": {"height_pct": ga["height_pct"], "position": ga["position"], "valign": ga["valign"],
                       "shape": self.cfg.get("monitor_shape", "auto"),
                       "mon_aspect": aspect_name(mon["rect"][2], mon["rect"][3]),
                       "game_aspect": aspect_name(cw, ch),
                       "new_game_action": self.cfg.get("new_game_action", "game"),
                       "new_game_lock": bool(self.cfg.get("new_game_lock", True))},
            "layout_choices": {"heights": HEIGHT_CHOICES, "positions": POSITION_CHOICES,
                               "valigns": VALIGN_CHOICES, "new_game": NEW_GAME_CHOICES},
            "windows": wins, "rules": rules, "ignored": list(self.cfg.get("ignored", [])),
            "zone_choices": [c[0] for c in ZONE_CHOICES],
            "options": {"game_on_top": self.ontop_on, "auto_apply": self.auto_on,
                        "fps_overlay": bool(self.cfg.get("fps_overlay")),
                        "auto_detect_games": bool(self.cfg.get("auto_detect_games", True))},
            "hotkeys_failed": self.hotkeys.failed if hasattr(self, "hotkeys") else [],
        }
        with self.snap_lock:
            self.snapshot_json = json.dumps(snap)

    def is_borderless(self, hwnd):
        try:
            return not (GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION)
        except Exception:
            return False

    def get_snapshot(self):
        with self.snap_lock:
            return self.snapshot_json

    # ---------------- queue: hotkeys, focus changes, interface actions ----------------
    def call(self, action, args, timeout=6.0):
        """Called from the web-server thread. Runs the action on the engine thread."""
        box = {"result": None}
        ev = threading.Event()
        self.q.put(("api", action, args, box, ev))
        ev.wait(timeout)
        return box["result"] or {"ok": False, "error": "busy"}

    def poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                try:
                    if isinstance(item, tuple) and item[0] == "api":
                        _, action, args, box, ev = item
                        try:
                            ok = self.do_action(action, args or {})
                        except Exception:
                            log_error()
                            ok = False
                            self.set_status("Something went wrong - see g9_splitter_error.log.")
                        self.build_snapshot()
                        box["result"] = {"ok": ok is not False, "state": json.loads(self.get_snapshot())}
                        ev.set()
                    elif isinstance(item, tuple) and item[0] == "foreground":
                        self.update_ontop()
                    elif item == "quit":
                        self.on_close()
                        return
                    elif item == "ui_closed":
                        self.ui_closed()
                    elif isinstance(item, str):
                        self.handle_hotkey(item)
                        self.build_snapshot()
                except Exception:
                    log_error()
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(25, self.poll_queue)

    def do_action(self, action, a):
        hwnd = int(a["hwnd"]) if a.get("hwnd") else None
        if action == "place":
            borderless = a.get("borderless")
            if borderless == "auto":     # dropped in the middle: games -> GAME, everyday apps -> Middle
                borderless = a["zone"] == "center" and bool(hwnd) and IsWindow(hwnd) \
                    and self.looks_like_game(self.win_info(hwnd))
            else:
                borderless = bool(borderless)
                if a["zone"] == "center" and hwnd and IsWindow(hwnd):
                    self.mark_game(hwnd, borderless)     # you chose GAME / Middle yourself
            ok = self.place(hwnd, a["zone"], borderless, focus=False)
            if ok:
                self.pause_if_rule_differs(hwnd, a["zone"], borderless)
                self.keep_ui_on_top(hwnd)
                self.root.after(350, self.after_change)
            return ok
        if action == "restore":
            self.restore(hwnd)
            self.keep_ui_on_top(hwnd)
            self.root.after(350, self.after_change)
        elif action == "ignore":
            on = bool(a.get("on", True))
            w = self.windows.get(hwnd) if hwnd else None
            exe = (w.exe if w else "") or a.get("exe", "")
            if not exe:
                self.set_status("That window can't be ignored (it's closed or protected).")
                return False
            self.set_ignored(exe, on)
            name = exe[:-4] if exe.lower().endswith(".exe") else exe
            self.set_status(f"Ignoring {name} - G9 Splitter leaves it alone now, so it can run full screen."
                            if on else f"Stopped ignoring {name}.")
        elif action == "mute":
            w = self.windows.get(hwnd)
            if not w or not w.exe:
                self.set_status("That window can't be muted (it's closed or protected).")
                return False
            mute = bool(a.get("mute", True))
            name = w.exe[:-4] if w.exe.lower().endswith(".exe") else w.exe
            n = self.mixer.set_muted(w.exe, mute)
            if n is None:
                self.set_status("Couldn't reach Windows' volume mixer - see g9_splitter_error.log.")
                return False
            if not mute:
                self.set_status(f"Sound back on for {name}.")
            elif n:
                self.set_status(f"Muted {name}.")
            else:
                self.set_status(f"{name} isn't making sound right now - it'll be muted as soon as it does.")
        elif action == "set_monitor":
            for i, m in enumerate(self.monitors):
                if m["device"] == a["device"]:
                    self.mon_idx = i
                    self.cfg["monitor"] = m["device"]
                    save_config(self.cfg)
            self.last_apply.clear()
        elif action == "set_preset":
            ratio = dict(PRESETS).get(a["name"])
            if ratio:
                self.set_game_shape(ratio)
                cw, ch = self.zones()["center"][2:]
                self.set_status(f"Game area is now {cw} x {ch} ({aspect_name(cw, ch)}).")
        elif action == "set_size":
            self.set_game_size(int(a["width"]), int(a["height"]))
            cw, ch = self.zones()["center"][2:]
            self.set_status(f"Game area is now {cw} x {ch}.")
        elif action == "set_layout":
            if not self.set_layout_option(a.get("key"), a.get("value")):
                return False
            if a.get("key") in ("game_height_pct", "game_position", "game_valign"):
                cw, ch = self.zones()["center"][2:]
                self.set_status(f"Game area is now {cw} x {ch} ({aspect_name(cw, ch)}).")
        elif action == "set_option":
            key, val = a["key"], a["value"]
            if key == "fps_overlay":
                self.toggle_fps(bool(val))
                return True
            if key in ("game_on_top", "auto_apply", "auto_detect_games"):
                self.cfg[key] = bool(val)
            elif key == "theme" and val in THEMES:
                self.cfg["theme"] = val
            save_config(self.cfg)
            if key == "game_on_top":
                self.update_ontop()
        elif action == "add_rule":
            w = self.windows.get(hwnd)
            if not w:
                self.set_status("That window is closed.")
                return False
            label = a.get("where", ZONE_CHOICES[0][0])
            zone, borderless = next(((z, b) for l, z, b in ZONE_CHOICES if l == label), ("center", True))
            if zone == "ignore":
                if not w.exe:
                    self.set_status("That window can't be ignored (it's protected).")
                    return False
                self.set_ignored(w.exe, True)
                self.set_status(f"Remembered {w.exe} as ignored - G9 Splitter leaves it alone every time it opens, so it can run full screen.")
                return True
            rule = {"exe": w.exe, "title": "" if w.exe else w.title, "zone": zone,
                    "borderless": borderless, "lock_mouse": bool(a.get("lock_mouse", True))}
            self.cfg["rules"] = [r for r in self.cfg["rules"] if not rule_matches(r, w.exe, w.title)]
            self.cfg["rules"].append(rule)
            save_config(self.cfg)
            self.place(hwnd, zone, borderless, focus=False)
            self.keep_ui_on_top(hwnd)
            self.set_status(f"Remembered {w.exe or w.title} - it goes to {ZONE_NAMES[zone]} every time it opens.")
        elif action == "remove_rule":
            i = int(a["index"])
            if 0 <= i < len(self.cfg["rules"]):
                r = self.cfg["rules"].pop(i)
                if r.get("auto") and r.get("exe"):     # you said it isn't a game: never auto-pick it again
                    self.cfg["games"] = [g for g in self.cfg.get("games", []) if g.lower() != r["exe"].lower()]
                    self.cfg.setdefault("not_games", []).append(r["exe"])
                save_config(self.cfg)
                self.set_status(f"Forgot {r.get('exe') or r.get('title')}.")
        elif action == "toggle_rule_lock":
            i = int(a["index"])
            if 0 <= i < len(self.cfg["rules"]):
                self.cfg["rules"][i]["lock_mouse"] = not self.cfg["rules"][i].get("lock_mouse")
                save_config(self.cfg)
        elif action == "show_zones":
            self.show_overlay()
        elif action == "refresh":
            self.refresh_monitor_info()
            self.refresh_windows()
            self.update_zone_contents()
        elif action == "startup":
            self.set_startup(bool(a.get("on")))
        elif action == "desktop_shortcut":
            self.set_status("Creating desktop shortcut...")

            def work():
                ok = _quiet(make_shortcut, "desktop")
                self.root.after(0, lambda: (self.set_status(
                    "Added 'G9 Splitter 2' to your desktop." if ok else "Couldn't create the desktop shortcut."),
                    self.build_snapshot()))
            threading.Thread(target=work, daemon=True).start()
        elif action == "restart_admin":
            self.restart_as_admin()
        elif action == "status":
            self.set_status(str(a.get("text", ""))[:200])
        else:
            return False
        return True

    def after_change(self):
        try:
            self.refresh_windows()
            self.update_zone_contents()
            self.build_snapshot()
        except Exception:
            log_error()

    # ---------------- monitors / zones ----------------
    def monitor_label(self, m):
        x, y, w, h = m["rect"]
        name = m["device"].replace("\\\\.\\", "")
        tags = []
        if m["primary"]:
            tags.append("main")
        shape = aspect_name(w, h)
        if shape:
            tags.append(shape + (" super-ultrawide" if w / max(h, 1) > 3 else
                                 " ultrawide" if w / max(h, 1) > 2.2 else ""))
        return f"{name}  -  {w} x {h}" + (f"  ({', '.join(tags)})" if tags else "")

    def pick_monitor(self):
        idx = None
        for i, m in enumerate(self.monitors):
            if m["device"] == self.cfg.get("monitor"):
                idx = i
        if idx is None and self.monitors:
            idx = max(range(len(self.monitors)),
                      key=lambda i: (self.monitors[i]["rect"][2] / max(self.monitors[i]["rect"][3], 1),
                                     self.monitors[i]["rect"][2]))
        self.mon_idx = idx or 0

    def refresh_monitor_info(self):
        mons = get_monitors()
        if not mons:
            return
        if [m["device"] for m in mons] != [m["device"] for m in self.monitors]:
            self.monitors = mons
            self.pick_monitor()
        else:
            self.monitors = mons

    def mon_rect(self):
        if not self.monitors:
            return (0, 0, 1920, 1080)
        return self.monitors[self.mon_idx]["rect"]

    def zones(self):
        work = self.monitors[self.mon_idx].get("work") if self.monitors else None
        return compute_zones(self.mon_rect(), int(self.cfg.get("center_width", 0)),
                             int(self.cfg.get("center_height", 0)), work, **game_area_options(self.cfg))

    # ---------------- game-area & game options ----------------
    def set_game_shape(self, ratio):
        """Pick a shape preset: it's kept on any monitor."""
        self.cfg["game_ratio"] = ratio
        self.cfg["center_width"] = 0
        if not self.cfg.get("game_height_pct") and not self.cfg.get("center_height"):
            self.cfg["game_height_pct"] = 100
        save_config(self.cfg)
        self.last_apply.clear()

    def set_game_size(self, cw, ch):
        """Exact size in pixels (Custom)."""
        self.cfg["game_ratio"] = 0
        self.cfg["game_height_pct"] = 0
        self.cfg["center_width"] = cw
        self.cfg["center_height"] = 0 if ch >= self.mon_rect()[3] else ch
        save_config(self.cfg)
        self.last_apply.clear()

    def set_layout_option(self, key, value):
        """Height %, position, vertical position, new-game behaviour, mouse lock for new games."""
        if key == "game_height_pct":
            value = int(value)
            if value not in HEIGHT_CHOICES:
                return False
            if not self.cfg.get("game_ratio"):       # keep the current shape while changing the height
                cw, ch = self.zones()["center"][2:]
                self.cfg["game_ratio"] = round(cw / max(ch, 1), 4)
                self.cfg["center_width"] = 0
            self.cfg["center_height"] = 0
        elif key == "game_position" and value not in dict(POSITION_CHOICES):
            return False
        elif key == "game_valign" and value not in dict(VALIGN_CHOICES):
            return False
        elif key == "new_game_action" and value not in dict(NEW_GAME_CHOICES):
            return False
        elif key == "new_game_lock":
            value = bool(value)
        elif key == "monitor_shape" and value not in ("auto", "curved", "flat"):
            return False
        elif key not in ("game_height_pct", "game_position", "game_valign", "new_game_action",
                         "new_game_lock", "monitor_shape"):
            return False
        self.cfg[key] = value
        save_config(self.cfg)
        self.last_apply.clear()
        return True

    def target_rect(self, zone, borderless):
        """Where a window really goes. A borderless game in the middle covers the
        taskbar; a normal window in the middle stops just above it, like the sides."""
        z = self.zones()[zone]
        if zone == "center" and not borderless:
            work = self.monitors[self.mon_idx].get("work") if self.monitors else None
            if work:
                z = intersect(z, work)
        return z

    def move_visible(self, hwnd, x, y, w, h, sync=False):
        """Move a normal window so the part you SEE lines up exactly
        (Windows adds invisible resize borders around most windows)."""
        wx, wy, ww, wh = window_rect(hwnd)
        vx, vy, vw, vh = visible_rect(hwnd)
        dl, dt = vx - wx, vy - wy
        dr, db = (wx + ww) - (vx + vw), (wy + wh) - (vy + vh)
        flags = (MOVE_FLAGS & ~SWP_ASYNCWINDOWPOS) if sync else MOVE_FLAGS
        return SetWindowPos(hwnd, None, x - dl, y - dt, w + dl + dr, h + dt + db, flags)

    def verify_place(self, hwnd, target, borderless, tries, quiet=False):
        """Check the window really landed where it should; nudge it again if not
        (some programs resize themselves or change borders right after a move)."""
        try:
            if getattr(self, "closing", False) or not IsWindow(hwnd) or IsIconic(hwnd):
                return
            cur = visible_rect(hwnd)
            if not all(abs(a - b) <= 3 for a, b in zip(cur, target)):
                if tries > 0:
                    if IsZoomed(hwnd):
                        ShowWindow(hwnd, SW_RESTORE)
                    x, y, w, h = target
                    if borderless:
                        SetWindowPos(hwnd, None, x, y, w, h,
                                     (MOVE_FLAGS & ~SWP_ASYNCWINDOWPOS) | SWP_FRAMECHANGED)
                    else:
                        self.move_visible(hwnd, x, y, w, h, sync=True)
                    self.root.after(300, lambda: self.verify_place(hwnd, target, borderless, tries - 1, quiet))
                    return
                if not quiet:
                    self.set_status(f"\"{window_title(hwnd)[:40]}\" won't go to exactly {target[2]} x {target[3]} "
                                    "- the program limits its own size.")
        except Exception:
            log_error()
        cb = getattr(self, "after_change", None)
        if cb:
            cb()
        else:
            self.update_zone_contents()

    # ---------------- "show zones on my monitor" boxes ----------------
    def make_floating(self, alpha):
        t = tkinter.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-alpha", alpha)
        t.geometry("1x1+-32000+-32000")
        t.update_idletasks()
        t.update()
        try:
            hwnd = int(t.wm_frame(), 16)
        except Exception:
            hwnd = t.winfo_id()
        t.withdraw()
        try:
            ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT | WS_EX_LAYERED
                              | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        except Exception:
            log_error()
        return t, hwnd

    def build_overlays(self):
        self.overlays = {}
        for name in ("left", "center", "right"):
            t, hwnd = self.make_floating(0.55)
            lbl = tkinter.Label(t, justify="center", font=("Segoe UI", 26, "bold"))
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            self.overlays[name] = {"top": t, "label": lbl, "hwnd": hwnd}

    def show_overlay(self):
        if self.overlay_hide_job:
            self.root.after_cancel(self.overlay_hide_job)
        t = THEMES.get(self.cfg["theme"], THEMES["Dark"])
        for name, ov in self.overlays.items():
            x, y, w, h = self.zones()[name]
            if w <= 0 or h <= 0:
                ShowWindow(ov["hwnd"], SW_HIDE)
                continue
            color = t["zone_center"] if name == "center" else t["zone_side"]
            ov["top"].configure(bg=color, highlightthickness=6, highlightbackground=t["zone_outline"])
            ov["label"].configure(bg=color, fg=t["zone_text"], text=f"{ZONE_LABELS[name]}\n{w} x {h}")
            SetWindowPos(ov["hwnd"], HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.root.update_idletasks()
        self.overlay_hide_job = self.root.after(2500, self.overlay_hide)

    def overlay_hide(self):
        self.overlay_hide_job = None
        for ov in self.overlays.values():
            ShowWindow(ov["hwnd"], SW_HIDE)

    # ---------------- placing windows ----------------
    def focus_window(self, hwnd):
        """Bring a window to the front even though the click happened in our UI window."""
        try:
            if not SetForegroundWindow(hwnd):
                # Classic trick: a synthetic Alt tap lets us take the foreground.
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                SetForegroundWindow(hwnd)
        except Exception:
            pass

    def place(self, hwnd, zone, borderless, quiet=False, focus=True):
        if not hwnd or not IsWindow(hwnd):
            if not quiet:
                self.set_status("That window is closed.")
            return False
        pid = window_pid(hwnd)
        unignored = None
        known = self.windows.get(hwnd)
        exe_now = (known.exe if known else "") or exe_name(pid)
        if self.is_ignored(exe_now):
            if quiet:                   # automatic moves never touch an ignored program
                return False
            self.set_ignored(exe_now, False)       # you placed it yourself - stop ignoring it
            unignored = exe_now
        if not self.is_admin and process_elevated(pid):
            self.set_status("That program runs as administrator. Click 'Restart as admin' so "
                            "G9 Splitter is allowed to move it.")
            return False
        x, y, w, h = self.target_rect(zone, borderless)
        if w <= 0 or h <= 0:
            if not quiet:
                self.set_status("That zone has no space - make the game area narrower.")
            return False
        if IsZoomed(hwnd) or IsIconic(hwnd):
            ShowWindow(hwnd, SW_RESTORE)
        style = GetWindowLongPtrW(hwnd, GWL_STYLE)
        exstyle = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        ok = True
        if borderless:
            if hwnd not in self.saved_styles:
                self.saved_styles[hwnd] = (style, exstyle, window_rect(hwnd))
            new_style = style & ~FRAME_STYLES
            new_ex = exstyle & ~FRAME_EXSTYLES
            if new_style != style:
                SetWindowLongPtrW(hwnd, GWL_STYLE, new_style)
            if new_ex != exstyle:
                SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_ex)
            if zone == "center" and self.ontop_on:
                self.ontop.add(hwnd)
                if (not quiet and focus) or GetForegroundWindow() == hwnd:
                    ok = SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h,
                                      (MOVE_FLAGS & ~SWP_NOZORDER) | SWP_FRAMECHANGED)
                    if not quiet and focus:
                        self.focus_window(hwnd)   # jump straight into the game
                else:
                    ok = SetWindowPos(hwnd, None, x, y, w, h, MOVE_FLAGS | SWP_FRAMECHANGED)
            else:
                ok = SetWindowPos(hwnd, None, x, y, w, h, MOVE_FLAGS | SWP_FRAMECHANGED)
        if not (borderless and zone == "center" and self.ontop_on) and hwnd in self.ontop:
            self.ontop.discard(hwnd)
            self.set_topmost(hwnd, False)
        if not borderless:
            if hwnd in self.saved_styles and not (GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION):
                # We made it borderless earlier - give it its border back first.
                st, ex, _ = self.saved_styles.pop(hwnd)
                SetWindowLongPtrW(hwnd, GWL_STYLE, st)
                SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
                SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                             | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            ok = self.move_visible(hwnd, x, y, w, h)
            if not quiet and focus:
                self.focus_window(hwnd)   # bring it to the front so you see it land
        self.paused.discard(hwnd)
        self.root.after(300, lambda: self.verify_place(hwnd, (x, y, w, h), borderless, 2, quiet))
        if not quiet:
            title = window_title(hwnd)[:50]
            if ok:
                self.set_status(f"Moved \"{title}\" to {ZONE_NAMES[zone]} ({w} x {h})."
                                + (f" {unignored} is no longer ignored." if unignored else ""))
            else:
                self.set_status(f"Windows refused to move \"{title}\". Try 'Restart as admin'.")
        return bool(ok)

    def restore(self, hwnd):
        if not hwnd or not IsWindow(hwnd):
            self.set_status("That window is closed.")
            return
        self.paused.add(hwnd)
        if self.lock_hwnd == hwnd:
            self.lock_hwnd = None
        if hwnd in self.ontop:
            self.ontop.discard(hwnd)
            self.set_topmost(hwnd, False)
        if hwnd in self.saved_styles:
            style, exstyle, (x, y, w, h) = self.saved_styles.pop(hwnd)
            SetWindowLongPtrW(hwnd, GWL_STYLE, style)
            SetWindowLongPtrW(hwnd, GWL_EXSTYLE, exstyle)
            SetWindowPos(hwnd, None, x, y, w, h, MOVE_FLAGS | SWP_FRAMECHANGED)
        else:
            style = GetWindowLongPtrW(hwnd, GWL_STYLE)
            if not style & WS_CAPTION:
                SetWindowLongPtrW(hwnd, GWL_STYLE, style | FRAME_STYLES | WS_SYSMENU)
            x, y, w, h = self.zones()["center"]
            # put it back as a normal window in the middle, a bit smaller
            SetWindowPos(hwnd, None, x + w // 8, y + h // 8, w * 3 // 4, h * 3 // 4,
                         MOVE_FLAGS | SWP_FRAMECHANGED)
        self.set_status(f"Restored \"{window_title(hwnd)[:50]}\" to a normal window.")

    def pause_if_rule_differs(self, hwnd, zone, borderless):
        exe, title = exe_name(window_pid(hwnd)), window_title(hwnd)
        for rule in self.cfg["rules"]:
            if rule_matches(rule, exe, title) and (rule["zone"], bool(rule.get("borderless"))) != (zone, borderless):
                self.paused.add(hwnd)

    # ---------------- hotkeys ----------------
    def handle_hotkey(self, action):
        if action == "fps":
            self.toggle_fps()
            return
        hwnd = GetForegroundWindow()
        if not hwnd or window_pid(hwnd) == self.pid or window_pid(hwnd) in self.ui_pids:
            self.set_status("Hotkeys work on the window you're in - click your game or app first.")
            return
        if action == "center":
            self.place(hwnd, "center", self.looks_like_game(self.win_info(hwnd)))
        elif action in ("left", "right"):
            self.place(hwnd, action, False)
        elif action == "restore":
            self.restore(hwnd)
        elif action == "lock":
            if self.lock_hwnd == hwnd:
                self.lock_hwnd = None
                self.set_status("Mouse unlocked.")
            else:
                self.lock_hwnd = hwnd
                self.set_status(f"Mouse locked to \"{window_title(hwnd)[:50]}\" (Ctrl+Alt+L to unlock).")

    # ---------------- remembered apps ----------------
    def needs_apply(self, hwnd, target, borderless):
        if borderless and GetWindowLongPtrW(hwnd, GWL_STYLE) & (WS_CAPTION | WS_THICKFRAME):
            return True
        cur = visible_rect(hwnd)
        return any(abs(a - b) > 2 for a, b in zip(cur, target))

    def rules_tick(self):
        try:
            self.refresh_monitor_info()
            lock_set = set()
            self.detect_games(list(self.windows.values()))
            if self.cfg["rules"]:
                wins = list(self.windows.values())
                zones = self.zones()
                now = time.time()
                for rule in self.cfg["rules"]:
                    if self.is_ignored(rule.get("exe")):
                        continue
                    if rule.get("zone") in zones and (zones[rule["zone"]][2] <= 0 or zones[rule["zone"]][3] <= 0):
                        continue        # that screen has no room with the current layout - wait
                    cands = [w for w in wins if rule_matches(rule, w.exe, w.title)
                             and w.hwnd not in self.paused and IsWindow(w.hwnd)]
                    if not cands:
                        continue
                    w = max(cands, key=lambda c: window_rect(c.hwnd)[2] * window_rect(c.hwnd)[3])
                    if rule.get("lock_mouse"):
                        lock_set.add(w.hwnd)
                    if not self.auto_on or IsIconic(w.hwnd):
                        continue
                    if rule["zone"] == "center" and rule.get("borderless") and self.ontop_on:
                        self.ontop.add(w.hwnd)
                    if (self.needs_apply(w.hwnd, self.target_rect(rule["zone"], rule.get("borderless")),
                                         rule.get("borderless"))
                            and now - self.last_apply.get(w.hwnd, 0) > 3):
                        self.last_apply[w.hwnd] = now
                        self.auto_place(w.hwnd, rule, now)
            self.rule_lock_hwnds = lock_set
        except Exception:
            log_error()
        if not self.closing:
            self.root.after(1500, self.rules_tick)

    # ---------------- mouse lock ----------------
    def mouse_tick(self):
        try:
            targets = set(self.rule_lock_hwnds)
            if self.lock_hwnd:
                if IsWindow(self.lock_hwnd):
                    targets.add(self.lock_hwnd)
                else:
                    self.lock_hwnd = None
            fg = GetForegroundWindow()
            if fg and fg in targets and not IsIconic(fg):
                x, y, w, h = visible_rect(fg)
                ClipCursor(ctypes.byref(wt.RECT(x, y, x + w, y + h)))
                self.clipping = True
            elif self.clipping:
                ClipCursor(None)
                self.clipping = False
        except Exception:
            log_error()
        if not self.closing:
            self.root.after(200, self.mouse_tick)

    # ---------------- game above the taskbar (only while you're playing) ----------------
    def set_topmost(self, hwnd, on):
        SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS)

    def my_taskbars(self):
        now = time.time()
        if not hasattr(self, "_bars") or now - self._bars[0] > 5:
            self._bars = (now, [t for t in taskbars() if intersect(window_rect(t), self.mon_rect())[2] > 0])
        return self._bars[1]

    def update_ontop(self):
        if not self.ontop:
            return
        fg = GetForegroundWindow()
        fg_pid = window_pid(fg) if fg else 0
        fg_normal = bool(fg) and IsWindowVisible(fg) and class_name(fg) not in SHELL_CLASSES \
            and not (GetWindowLongPtrW(fg, GWL_EXSTYLE) & WS_EX_TOPMOST)
        for hwnd in list(self.ontop):
            if not IsWindow(hwnd):
                self.ontop.discard(hwnd)
                continue
            game_active = bool(fg) and (hwnd == fg or fg_pid == window_pid(hwnd))
            is_top = bool(GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
            if game_active:
                if self.ontop_on and not IsIconic(hwnd):
                    if not is_top or any(is_above(t, hwnd) for t in self.my_taskbars()):
                        self.set_topmost(hwnd, True)
                elif not self.ontop_on and is_top:
                    self.set_topmost(hwnd, False)
                continue
            if is_top:
                self.set_topmost(hwnd, False)
            if fg_normal and (is_top or not is_above(fg, hwnd)):
                SetWindowPos(hwnd, fg, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                             | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS)

    def ontop_tick(self):
        try:
            self.update_ontop()
        except Exception:
            log_error()
        if not self.closing:
            self.root.after(200, self.ontop_tick)

    # ---------------- window list ----------------
    def ui_hwnd(self):
        """Our own interface window (to keep it in front after you use it)."""
        now = time.time()
        cached = getattr(self, "_ui_hwnd", (0, None))
        if now - cached[0] < 5 and cached[1] and IsWindow(cached[1]):
            return cached[1]
        hwnd = None
        try:
            for w in list_windows(self.pid):
                if self.is_ui_window(w):
                    hwnd = w.hwnd
                    break
        except Exception:
            log_error()
        self._ui_hwnd = (now, hwnd)
        return hwnd

    def keep_ui_on_top(self, moved=None):
        """After you change something in the app, the app stays in front: the window
        you moved goes right behind it. Nothing is pinned 'always on top', so
        clicking another app still covers G9 Splitter as normal."""
        ui = self.ui_hwnd()
        if not ui:
            return
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS
        try:
            SetWindowPos(ui, HWND_TOP, 0, 0, 0, 0, flags)
            if moved and moved != ui and IsWindow(moved) \
                    and not (GetWindowLongPtrW(moved, GWL_EXSTYLE) & WS_EX_TOPMOST):
                SetWindowPos(moved, ui, 0, 0, 0, 0, flags)
            for delay in (150, 450):     # the moved program may raise itself after resizing
                self.root.after(delay, lambda: self._reassert_ui(ui, moved, flags))
        except Exception:
            log_error()

    def _reassert_ui(self, ui, moved, flags):
        try:
            if not IsWindow(ui) or GetForegroundWindow() not in (ui, None, 0):
                return                    # you've clicked something else - leave it alone
            if moved and IsWindow(moved) and is_above(moved, ui) \
                    and not (GetWindowLongPtrW(moved, GWL_EXSTYLE) & WS_EX_TOPMOST):
                SetWindowPos(moved, ui, 0, 0, 0, 0, flags)
        except Exception:
            log_error()

    def is_ui_window(self, w):
        return w.pid in self.ui_pids or (w.title == "G9 Splitter" and
                                          w.exe.lower() in ("msedge.exe", "pythonw.exe", "python.exe"))

    def refresh_windows(self):
        self.windows = {w.hwnd: w for w in list_windows(self.pid) if not self.is_ui_window(w)}

    def update_zone_contents(self):
        zones = self.zones()
        zone_of = {}
        for w in self.windows.values():
            try:
                if not IsWindow(w.hwnd) or IsIconic(w.hwnd):
                    continue
                r = visible_rect(w.hwnd)
            except Exception:
                continue
            for name, z in zones.items():
                fits = [z, self.target_rect(name, False)] if name == "center" else [z]
                if z[2] > 0 and any(all(abs(a - b) <= 6 for a, b in zip(r, c)) for c in fits):
                    zone_of[w.hwnd] = name
                    if name == "center" and self.is_game_window(w.hwnd):
                        self.ontop.add(w.hwnd)
                    break
        self.zone_of = zone_of

    def windows_tick(self):
        try:
            self.refresh_windows()
            self.update_zone_contents()
            if time.time() - getattr(self, "_mix_at", 0) > 2:     # who's muted (Windows' volume mixer)
                self._mix_at = time.time()
                self.mixer.refresh()
            self.build_snapshot()
        except Exception:
            log_error()
        if not self.closing:
            self.root.after(1000, self.windows_tick)

    # ---------------- misc ----------------
    def set_startup(self, on):
        """Start with Windows: the fast way (a sign-in task - Windows may ask once), straight into the tray."""
        self.set_status("Setting up start with Windows..." if on else "Turning off start with Windows...")

        def work():
            try:
                if on:
                    how = enable_startup()
                    self.startup_on = how is not None
                    self.set_status({"task": "G9 Splitter 2 will start in the tray as soon as you sign in.",
                                     "run": "G9 Splitter 2 will start in the tray when Windows starts (Windows didn't "
                                            "allow the fastest way)."}.get(
                        how, "Couldn't turn on start with Windows - see g9_splitter_error.log."))
                else:
                    off = disable_startup()
                    self.startup_on = not off
                    self.set_status("G9 Splitter 2 will no longer start with Windows." if off else
                                    "Couldn't turn off start with Windows (the admin question was declined).")
            except Exception:
                log_error()
        threading.Thread(target=work, daemon=True).start()

    def _startup_changed(self):
        pass                       # the interface picks up startup_on with the next refresh

    # ---------------- tray ----------------
    def open_ui(self):
        if self.ui:
            self.ui.start()

    def ui_closed(self):
        """You closed the window: keep running in the tray (once, say so)."""
        if self.tray and self.tray.ok and not self.cfg.get("tray_hint_shown"):
            self.cfg["tray_hint_shown"] = True
            save_config(self.cfg)
            self.tray.balloon("G9 Splitter 2 is still running",
                              "It's here in the tray. Click the icon to open it, right-click to quit.")

    def restart_as_admin(self):
        r = ShellExecuteW(None, "runas", pythonw_path(), f'"{os.path.abspath(__file__)}" --restarted',
                          APP_DIR, 1)
        if r and r > 32:
            self.root.after(100, self.on_close)
        else:
            self.set_status("Admin restart was cancelled.")

    # ---------------- automatic game detection ----------------
    # ---------------- game or everyday app? ----------------
    # ---------------- ignored programs ----------------
    # An ignored program is left completely alone: never placed, never recognised as a game,
    # never put on top - e.g. a game you want to play in real full screen.
    def is_ignored(self, exe):
        return bool(exe) and exe.lower() in {e.lower() for e in self.cfg.get("ignored", [])}

    def set_ignored(self, exe, on):
        if not exe:
            return
        low = exe.lower()
        ign = [e for e in self.cfg.get("ignored", []) if e.lower() != low]
        if on:
            ign.append(exe)
            self.cfg["rules"] = [r for r in self.cfg["rules"] if (r.get("exe") or "").lower() != low]
            self.cfg["games"] = [g for g in self.cfg.get("games", []) if g.lower() != low]
            for hwnd, w in list(self.windows.items()):
                if (w.exe or "").lower() == low:
                    if hwnd in self.saved_styles:      # we made it borderless - give it back as it was
                        self.restore(hwnd)
                    self.ontop.discard(hwnd)
                    if self.lock_hwnd == hwnd:
                        self.lock_hwnd = None
        self.cfg["ignored"] = ign
        save_config(self.cfg)
        if hasattr(self, "refresh_rules"):
            self.refresh_rules()

    def looks_like_game(self, w):
        """Games go in the middle with no border (GAME); everyday apps keep their border (Middle)."""
        exe = (w.exe or "").lower()
        if self.is_ignored(exe):
            return False
        if exe and exe in {e.lower() for e in self.cfg.get("not_games", [])}:
            return False
        if exe and exe in {e.lower() for e in self.cfg.get("games", [])}:
            return True
        if any(rule_matches(r, w.exe, w.title) and r.get("zone") == "center" and r.get("borderless")
               for r in self.cfg["rules"]):
            return True
        return bool(self.games.why_game(w))

    def win_info(self, hwnd):
        w = self.windows.get(hwnd)
        if w:
            return w
        pid = window_pid(hwnd)
        return WinInfo(hwnd, window_title(hwnd), exe_name(pid), pid)

    def is_game_window(self, hwnd):
        """A window without a title bar only counts as GAME if we made it borderless or it's a
        game. (Browsers like Opera GX draw their own title bar - that doesn't make them games.)"""
        try:
            if GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION:
                return False
        except Exception:
            return False
        return hwnd in self.saved_styles or self.looks_like_game(self.win_info(hwnd))

    def mark_game(self, hwnd, is_game):
        """You picked GAME or Middle yourself - remember that for next time."""
        exe = self.win_info(hwnd).exe or ""
        if not exe:
            return
        low = exe.lower()
        games = self.cfg.setdefault("games", [])
        in_games = low in [g.lower() for g in games]
        changed = False
        if is_game:
            if not in_games:
                games.append(exe)
                changed = True
            nots = self.cfg.setdefault("not_games", [])
            if low in [g.lower() for g in nots]:
                self.cfg["not_games"] = [g for g in nots if g.lower() != low]
                changed = True
        elif in_games:
            self.cfg["games"] = [g for g in games if g.lower() != low]
            changed = True
        if changed:
            save_config(self.cfg)

    def detect_games(self, wins):
        if not self.cfg.get("auto_detect_games", True):
            return
        now = time.time()
        seen = getattr(self, "_game_seen", {})
        self._game_seen = seen
        known = {(r.get("exe") or "").lower() for r in self.cfg["rules"]}
        ignored = {e.lower() for e in self.cfg.get("not_games", []) + self.cfg.get("ignored", [])}
        yours = {e.lower() for e in self.cfg.get("games", [])}
        for w in wins:
            exe = (w.exe or "").lower()
            if not exe or exe in known or exe in ignored or w.hwnd in self.paused:
                continue
            why = self.games.why_game(w) or ("you put it in GAME" if exe in yours else None)
            if not why:
                seen.pop(w.hwnd, None)
                continue
            first = seen.setdefault(w.hwnd, now)
            if now - first < 3:          # wait out splash screens / launch windows
                continue
            action = self.cfg.get("new_game_action", "game")
            known.add(exe)
            if action == "ignore":            # Settings: leave new games alone (e.g. play them full screen)
                self.set_ignored(w.exe, True)
                self.set_status(f"Game detected: {w.title[:40]} ({why}) - left alone (ignored) as set in "
                                "Settings. Remove it from the ignored list to place it.")
                continue
            rule = {"exe": w.exe, "title": "", "zone": "center", "borderless": action != "middle",
                    "lock_mouse": bool(self.cfg.get("new_game_lock", True)), "auto": True}
            self.cfg["rules"].append(rule)
            save_config(self.cfg)
            if hasattr(self, "refresh_rules"):
                self.refresh_rules()
            self.set_status(f"Game detected: {w.title[:40]} ({why}) - put in "
                            f"{'Middle' if action == 'middle' else 'GAME'} and remembered. "
                            "Not a game? Remove it from Remembered apps.")

    def auto_place(self, hwnd, rule, now):
        """Place a remembered app, but stop if the program keeps resizing itself."""
        hist = [t for t in getattr(self, "_apply_hist", {}).get(hwnd, []) if now - t < 45] + [now]
        self._apply_hist = getattr(self, "_apply_hist", {})
        self._apply_hist[hwnd] = hist
        if len(hist) > 4:
            self.paused.add(hwnd)
            cx, cy, cw, ch = self.zones()["center"]
            self.set_status(f"\"{window_title(hwnd)[:40]}\" keeps resizing itself. In its video settings choose "
                            f"Windowed at {cw} x {ch}, then drag it onto GAME again.")
            return
        self.place(hwnd, rule["zone"], rule.get("borderless"), quiet=True)

    # ---------------- FPS counter ----------------
    def toggle_fps(self, on=None):
        on = (not self.cfg.get("fps_overlay")) if on is None else bool(on)
        self.cfg["fps_overlay"] = on
        save_config(self.cfg)
        var = getattr(self, "fps_var", None)
        if var is not None:
            var.set(on)
        if on:
            self.fps.failed_at.clear()
            if self.fps.pid:
                self.fps.stop()
            msg = "FPS counter on - it shows on the borderless window in the middle while you're in it."
            if not self.fps.exe():
                self.fps.download()
                msg += " Downloading the FPS tool first..."
            elif not self.is_admin:
                msg += " If it stays at '-- FPS', click 'Restart as admin'."
            self.set_status(msg)
        else:
            self.fps.stop()
            self.fps_hide()
            self.set_status("FPS counter off.")

    def fps_target(self):
        """The borderless window filling the middle (your game), preferring the focused one."""
        cz = self.zones()["center"]
        fg = GetForegroundWindow()
        best = None
        for hwnd in list(self.ontop) + list(self.saved_styles):
            try:
                if not IsWindow(hwnd) or GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION:
                    continue
                if not all(abs(a - b) <= 6 for a, b in zip(visible_rect(hwnd), cz)):
                    continue
            except Exception:
                continue
            if hwnd == fg:
                return hwnd
            best = best or hwnd
        return best

    def fps_tick(self):
        try:
            if self.fps.message:
                self.set_status(self.fps.message)
                self.fps.message = None
            if self.cfg.get("fps_overlay"):
                hwnd = self.fps_target()
                if not hwnd:
                    if self.fps.pid:
                        self.fps.stop()
                    self.fps_hide()
                else:
                    pid = window_pid(hwnd)
                    if not self.fps.exe():
                        self.fps.download()
                    elif self.fps.pid != pid:
                        self.fps.start(pid)
                    fg = GetForegroundWindow()
                    if fg and window_pid(fg) == pid and not IsIconic(hwnd):
                        self.fps_show(hwnd)
                    else:
                        self.fps_hide()      # Alt+Tabbed away: hide the counter
            elif self.fps.pid:
                self.fps.stop()
                self.fps_hide()
        except Exception:
            log_error()
        if not getattr(self, "closing", False):
            self.root.after(500, self.fps_tick)

    def fps_show(self, hwnd):
        import tkinter as _tk
        if not getattr(self, "fps_ov", None):
            t, h = self.make_floating(0.9)
            lbl = _tk.Label(t, font=("Consolas", 14, "bold"), bg="#0b0c10", fg="#7CFC00", padx=10, pady=3)
            lbl.pack()
            self.fps_ov = {"top": t, "label": lbl, "hwnd": h, "shown": False}
        ov = self.fps_ov
        cur = self.fps.current()
        if cur:
            fps, ms = cur
            text = f"{fps:.0f} FPS   {ms:.1f} ms"
            color = "#7CFC00" if fps >= 100 else ("#ffd23f" if fps >= 50 else "#ff4d4d")
        else:
            text, color = "-- FPS", "#9aa0a6"
        if ov["label"].cget("text") != text or ov["label"].cget("fg") != color:
            ov["label"].configure(text=text, fg=color)
        ov["top"].update_idletasks()
        w, h = ov["label"].winfo_reqwidth(), ov["label"].winfo_reqheight()
        x, y, _, _ = visible_rect(hwnd)
        SetWindowPos(ov["hwnd"], HWND_TOPMOST, x + 12, y + 12, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        ov["shown"] = True

    def fps_hide(self):
        ov = getattr(self, "fps_ov", None)
        if ov and ov["shown"]:
            ShowWindow(ov["hwnd"], SW_HIDE)
            ov["shown"] = False

    def fps_shutdown(self):
        try:
            self.fps.stop()
        except Exception:
            pass

    def on_close(self):
        if self.closing:
            return
        self.closing = True
        run_log("closing: " + "".join(traceback.format_stack(limit=4)[:-1]).replace("\n", " | ")[-600:])
        try:
            self.fps_shutdown()
            ClipCursor(None)
            self.hotkeys.stop()
        except Exception:
            pass
        if self.on_quit:
            _quiet(self.on_quit)
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tiny local web server: serves the interface and lets it talk to the engine.
# Only listens on 127.0.0.1 and every API call needs a secret token.
# ---------------------------------------------------------------------------
STATIC = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js"}
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon"}


def make_server(engine, token):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json", cache="no-store"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            return self.headers.get("X-Token") == token

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in STATIC or path in ("/logo.png", "/favicon.ico"):
                if path == "/logo.png":
                    fp = ICON_PNG
                elif path == "/favicon.ico":
                    fp = ICON_ICO
                else:
                    fp = os.path.join(WEB_DIR, STATIC[path])
                try:
                    with open(fp, "rb") as f:
                        data = f.read()
                    self.send(200, data, MIME.get(os.path.splitext(fp)[1], "application/octet-stream"))
                except OSError:
                    self.send(404, "not found", "text/plain")
            elif path in ("/api/icon", "/api/thumb"):
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                if q.get("t", [""])[0] != token:
                    return self.send(403, "token", "text/plain")
                try:
                    hwnd = int(q.get("h", ["0"])[0])
                except ValueError:
                    hwnd = 0
                if not hwnd or not IsWindow(hwnd):
                    return self.send(404, "gone", "text/plain")
                png = engine.images.icon(hwnd) if path == "/api/icon" else engine.images.thumb(hwnd)
                if not png:
                    return self.send(204, b"", "image/png")
                self.send(200, png, "image/png", "max-age=900" if path == "/api/icon" else "no-store")
            elif path == "/api/state":
                if not self.authed():
                    return self.send(403, '{"error":"token"}')
                engine.last_poll = time.time()
                self.send(200, engine.get_snapshot())
            else:
                self.send(404, "not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?")[0]
            if not self.authed():
                return self.send(403, '{"error":"token"}')
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            if path == "/api/action":
                res = engine.call(body.get("action", ""), body.get("args") or {})
                self.send(200, json.dumps(res))
            elif path == "/api/bye":
                engine.bye_at = time.time()
                self.send(200, "{}")
            else:
                self.send(404, "{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    return srv


# ---------------------------------------------------------------------------
# Opening the interface window
# ---------------------------------------------------------------------------
def find_edge():
    import shutil
    for p in (shutil.which("msedge"),
              os.path.join(os.environ.get("ProgramFiles(x86)", ""), r"Microsoft\Edge\Application\msedge.exe"),
              os.path.join(os.environ.get("ProgramFiles", ""), r"Microsoft\Edge\Application\msedge.exe"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\Application\msedge.exe")):
        if p and os.path.exists(p):
            return p
    return None


class UILauncher:
    """Runs the interface window and quits the engine when you close it."""

    def __init__(self, engine, url):
        self.engine, self.url = engine, url
        self.proc = None
        self.busy = False

    def start(self):
        if self.busy:                 # already open: bring it to the front
            self.focus()
            return
        self.busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.run()
        finally:
            self.busy = False
            self.proc = None

    def is_open(self):
        return self.busy

    def focus(self):
        try:
            hwnd = self.engine.ui_hwnd()
            if hwnd:
                ShowWindow(hwnd, 9)               # SW_RESTORE
                SetForegroundWindow(hwnd)
        except Exception:
            log_error()

    def closed(self):
        """The window was closed: stay in the tray if there is one, otherwise quit."""
        tray = getattr(self.engine, "tray", None)
        self.engine.q.put("ui_closed" if tray and tray.ok else "quit")

    def run(self):
        import subprocess
        # 1) Preferred: our own app window (pywebview + Edge WebView2).
        try:
            started = time.time()
            self.proc = subprocess.Popen([pythonw_path(), UI_HOST, self.url + "&host=webview", ICON_ICO],
                                         cwd=APP_DIR)
            self.engine.ui_pids.add(self.proc.pid)
            code = self.proc.wait()
            if code == 0 or time.time() - started > 15:
                self.closed()
                return
        except Exception:
            log_error()
        # 2) Fallback: Microsoft Edge in "app" mode (no tabs / address bar).
        edge = find_edge()
        if edge:
            try:
                prof = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "G9Splitter2", "edge-profile")
                started = time.time()
                self.proc = subprocess.Popen([edge, "--app=" + self.url + "&host=edge", "--user-data-dir=" + prof,
                                              "--no-first-run", "--no-default-browser-check",
                                              "--window-size=1280,920"])
                self.engine.ui_pids.add(self.proc.pid)
                self.proc.wait()
                if time.time() - started > 5:
                    self.closed()
                    return
            except Exception:
                log_error()
        else:
            os.startfile(self.url + "&host=browser")
        # Edge handed the window to another process: quit when the page says goodbye.
        self.engine.bye_at = None
        while not self.engine.closing:
            time.sleep(1)
            bye = getattr(self.engine, "bye_at", None)
            if bye and time.time() - bye > 4 and getattr(self.engine, "last_poll", 0) < bye:
                self.closed()
                return

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass


def log_error():
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
    except Exception:
        pass


def _quiet(fn, *a):
    try:
        return fn(*a)
    except Exception:
        log_error()


def make_shortcut(where):
    import subprocess

    def q(x):
        return "'" + x.replace("'", "''") + "'"
    if where == "desktop":
        dest = f"(Join-Path ([Environment]::GetFolderPath('Desktop')) {q(SHORTCUT_NAME)})"
    else:
        dest = q(os.path.join(APP_DIR, SHORTCUT_NAME))
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({dest});"
          f"$s.TargetPath={q(pythonw_path())};"
          f"$s.Arguments={q(chr(34) + os.path.abspath(__file__) + chr(34))};"
          f"$s.WorkingDirectory={q(APP_DIR)};"
          f"$s.IconLocation={q(ICON_ICO + ',0')};"
          f"$s.Description='G9 Splitter 2 - Left | Game | Right';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, creationflags=0x08000000, timeout=30)
    return r.returncode == 0


class SelfUpdater:
    """Restarts the app by itself when its files are updated, so a new version takes effect
    right away - no need to close and reopen it. The new code is checked first: if it doesn't
    load, the running version simply keeps going (and the problem goes in the error log)."""

    def __init__(self, root, files, close_app, extra_args=None):
        self.root, self.files, self.close_app = root, [os.path.abspath(f) for f in files], close_app
        self.extra_args = extra_args      # e.g. come back in the tray if that's where the app was
        self.base = self.last = self._stamp()
        self.changed_at = 0
        root.after(3000, self.tick)

    def _stamp(self):
        out = {}
        for f in self.files:
            try:
                st = os.stat(f)
                out[f] = (st.st_mtime_ns, st.st_size)
            except OSError:
                out[f] = None
        return out

    def tick(self):
        try:
            cur = self._stamp()
            if cur != self.base:
                if cur != self.last:                       # still being copied in - wait until it settles
                    self.last, self.changed_at = cur, time.time()
                elif time.time() - self.changed_at >= 4:
                    self.restart(cur)
                    return
        except Exception:
            log_error()
        try:
            self.root.after(1500, self.tick)
        except Exception:
            pass

    def restart(self, cur):
        for f in self.files:
            if f.endswith((".py", ".pyw")) and os.path.exists(f):
                try:
                    with open(f, encoding="utf-8") as fh:
                        compile(fh.read(), f, "exec")
                except Exception:
                    log_error()                            # broken update: keep running this version
                    self.base = self.last = cur
                    self.root.after(1500, self.tick)
                    return
        import subprocess
        main_file = os.path.abspath(__file__)
        args = [a for a in sys.argv[1:] if a not in ("--restarted", "--tray", "--minimized", "--from-ui")]
        try:
            args += list(self.extra_args() if self.extra_args else [])
        except Exception:
            log_error()
        args += ["--restarted"]
        exe = sys.executable
        if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
            exe = exe[:-10] + "pythonw.exe"                # no console window
        try:
            self.close_app()                               # let go of hotkeys, mouse lock, FPS tool first
        except Exception:
            log_error()
        try:
            subprocess.Popen([exe, main_file] + args, cwd=APP_DIR, close_fds=True,
                             creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        except Exception:
            log_error()


def main():
    if sys.platform != "win32":
        print("G9 Splitter only runs on Windows.")
        return
    start_crash_log()
    run_log("start " + " ".join(sys.argv[1:]))
    import secrets
    mutex = single_instance("Local\\G9Splitter2_Engine", wait="--restarted" in sys.argv)
    if not mutex:
        run_log("not started: already running")
        if "--from-ui" not in sys.argv:     # (a window that lost the engine asked - nothing to say)
            if not wake_running_copy("G9 Splitter 2"):       # just open the one that's running
                ctypes.windll.user32.MessageBoxW(None, "G9 Splitter 2 is already running.", APP_NAME, 0x40)
        return
    report_last_crash()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OleJakob.G9Splitter2")
    except Exception:
        pass
    root = tkinter.Tk()
    try:
        engine = Engine(root)
        token = secrets.token_urlsafe(24)
        srv = make_server(engine, token)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/?t={token}"
        ui = UILauncher(engine, url)
        engine.ui = ui
        tray = None
        try:      # icon next to the clock: click = open, right-click = Open / Quit
            tray = TrayIcon("G9 Splitter 2", ICON_ICO, open_label="Open G9 Splitter 2",
                            on_open=lambda: root.after(0, engine.open_ui),
                            on_quit=lambda: root.after(0, engine.on_close))
        except Exception:
            log_error()
        engine.tray = tray
        engine.on_quit = lambda: (ui.stop(), srv.shutdown(), tray and tray.remove())
        if not (tray and tray.ok and "--tray" in sys.argv):   # started with Windows: stay in the tray
            ui.start()
        engine.updater = SelfUpdater(root, [__file__, UI_HOST] + [os.path.join(WEB_DIR, n) for n in
                                     ("index.html", "app.js", "style.css")], engine.on_close,
                                     extra_args=lambda: [] if ui.is_open() else ["--tray"])
        if os.path.exists(ICON_ICO) and not os.path.exists(os.path.join(APP_DIR, SHORTCUT_NAME)):
            threading.Thread(target=lambda: _quiet(make_shortcut, "folder"), daemon=True).start()
    except Exception:
        log_error()
        ctypes.windll.user32.MessageBoxW(None, "G9 Splitter 2 hit an error while starting.\n\n"
                                         + traceback.format_exc()[-1200:], APP_NAME, 0x10)
        return
    root.mainloop()


if __name__ == "__main__":
    main()
