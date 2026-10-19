"""
G9 Splitter - split one super-ultrawide monitor into Left | Game | Right.

Built for a 49" Samsung Odyssey G9 (5120x1440): the middle 2560x1440 is
exactly the size of a 27" 1440p monitor, with a 1280x1440 strip each side.

Only uses Python's standard library (tkinter + ctypes). Windows only.
"""

import ctypes
import json
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
                if event == EVENT_SYSTEM_MOVESIZESTART:
                    self.q.put(("movestart", hwnd, window_rect(hwnd)))
                elif event == EVENT_SYSTEM_MOVESIZEEND:
                    self.q.put(("moveend", hwnd, window_rect(hwnd)))
                elif event == EVENT_SYSTEM_FOREGROUND:
                    self.q.put(("foreground", hwnd, None))
        except Exception:
            pass

    def run(self):
        self.tid = GetCurrentThreadId()
        for hk_id, (mods, vk, _action, name) in HOTKEYS.items():
            if not RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                self.failed.append(name)
        self._proc = WINEVENTPROC(self.on_win_event)   # keep a reference!
        hook = SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MOVESIZEEND, None, self._proc,
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
                        r"Microsoft\Windows\Start Menu\Programs\Startup", "G9 Splitter.cmd")


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
        cls.lpszClassName = "G9SplitterTray_" + str(os.getpid())
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
# The app
# ---------------------------------------------------------------------------
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


class App:
    def __init__(self, root, start_minimized=False):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.cfg = load_config()
        self.pid = os.getpid()
        self.is_admin = bool(IsUserAnAdmin())
        self.monitors = get_monitors()
        self.saved_styles = {}      # hwnd -> (style, exstyle, rect) for restoring
        self.paused = set()         # hwnds the user restored manually (rules leave them alone)
        self.last_apply = {}        # hwnd -> time of last automatic apply
        self.rule_lock_hwnds = set()
        self.lock_hwnd = None       # window locked with Ctrl+Alt+L
        self.clipping = False
        self.hk_queue = queue.Queue()
        self.mon_idx = 0
        self.windows = {}
        self.ontop = set()          # game windows kept above the taskbar
        self.drag = None            # (hwnd, start_rect) while a window is being dragged
        self.drag_zone = None
        self.drag_released_since = None
        self.overlay_hide_job = None
        self.list_drag = None       # dragging a row out of the window list
        self.fps = FpsMonitor()
        self.games = GameDetector()
        self.mixer = AudioMixer()

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

        self.build_ui()
        self.pick_initial_monitor()
        self.build_overlays()
        self.refresh_windows()
        self.update_zone_contents()
        self.refresh_rules()

        self.hotkeys = HotkeyThread(self.hk_queue)
        self.hotkeys.start()
        self.hotkeys.ready.wait(2)
        if self.hotkeys.failed:
            self.set_status("These hotkeys are used by another program: " + ", ".join(self.hotkeys.failed))
        else:
            self.set_status("Ready. Pick a window and a spot, or use the hotkeys while in-game.")

        # icon next to the clock: click = open, right-click = Open / Quit; closing the window hides it there
        self.tray = None
        try:
            self.tray = TrayIcon(APP_NAME, ICON_ICO, open_label="Open " + APP_NAME,
                                 on_open=lambda: root.after(0, self.show_from_tray),
                                 on_quit=lambda: root.after(0, self.on_close))
        except Exception:
            log_error()
        root.protocol("WM_DELETE_WINDOW", self.close_to_tray)

        def _startup_poll():
            if getattr(self, "_startup_checked", False):
                self._startup_changed()
            else:
                root.after(500, _startup_poll)
        root.after(500, _startup_poll)
        root.after(50, self.poll_hotkeys)
        root.after(1000, self.rules_tick)
        root.after(200, self.mouse_tick)
        root.after(500, self.ontop_tick)
        root.after(2000, self.windows_tick)
        root.after(700, self.fps_tick)
        if start_minimized:       # started with Windows: wait in the tray
            root.after(100, self.hide_to_tray if self.tray and self.tray.ok else root.iconify)

    # ---------------- UI ----------------
    def build_ui(self):
        tk, ttk = self.tk, self.ttk
        root = self.root
        root.title(APP_NAME + (" (admin)" if self.is_admin else ""))
        root.minsize(900, 860)
        root.geometry("980x1010")
        pad = {"padx": 8, "pady": 4}
        self.status_var = tk.StringVar()
        ttk.Label(root, textvariable=self.status_var, style="Status.TLabel", anchor="w", padding=(8, 3)
                  ).pack(fill="x", side="bottom")
        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # --- Header: themed title banner + theme picker
        header = ttk.Frame(main)
        header.pack(fill="x", padx=8, pady=(0, 2))
        tp = ttk.Frame(header)
        tp.pack(side="right", anchor="n")
        ttk.Label(tp, text="Theme:").pack(side="left", padx=(0, 6))
        if self.cfg.get("theme") not in THEMES:
            self.cfg["theme"] = "Dark" if self.cfg.get("dark_mode") else "Light"
        self.theme_var = tk.StringVar(value=self.cfg["theme"])
        tcb = ttk.Combobox(tp, textvariable=self.theme_var, state="readonly", width=22, values=list(THEMES))
        tcb.pack(side="left")
        tcb.bind("<<ComboboxSelected>>", lambda e: self.on_theme())
        self.banner = tk.Canvas(header, height=46, highlightthickness=0)
        self.banner.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.banner.bind("<Configure>", lambda e: self.draw_banner())

        # --- Layout section
        lf = ttk.LabelFrame(main, text=" 1. Screen layout ", padding=8)
        lf.pack(fill="x", **pad)

        ttk.Label(lf, text="Monitor:").grid(row=0, column=0, sticky="w")
        self.mon_var = tk.StringVar()
        self.mon_combo = ttk.Combobox(lf, textvariable=self.mon_var, state="readonly", width=48)
        self.mon_combo.grid(row=0, column=1, columnspan=4, sticky="w", padx=6)
        self.mon_combo.bind("<<ComboboxSelected>>", lambda e: self.on_monitor_change())
        ttk.Button(lf, text="Re-scan monitors", command=self.rescan_monitors).grid(row=0, column=5, sticky="e")

        ttk.Label(lf, text="Game area shape:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.preset_var = tk.StringVar(value=PRESETS[0][0])
        preset = ttk.Combobox(lf, textvariable=self.preset_var, state="readonly", width=34,
                              values=[p[0] for p in PRESETS])
        preset.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        preset.bind("<<ComboboxSelected>>", lambda e: self.on_preset())

        self.cw_var = tk.IntVar(value=0)
        self.ch_var = tk.IntVar(value=0)
        ttk.Label(lf, text="Width:").grid(row=1, column=2, sticky="e", pady=(6, 0))
        ttk.Spinbox(lf, from_=200, to=16000, increment=10, textvariable=self.cw_var, width=7
                    ).grid(row=1, column=3, sticky="w", padx=4, pady=(6, 0))
        ttk.Label(lf, text="Height:").grid(row=1, column=4, sticky="e", pady=(6, 0))
        hrow = ttk.Frame(lf)
        hrow.grid(row=1, column=5, sticky="w", pady=(6, 0))
        ttk.Spinbox(hrow, from_=200, to=8000, increment=10, textvariable=self.ch_var, width=7
                    ).pack(side="left", padx=4)
        ttk.Button(hrow, text="Apply size", command=self.on_size_apply).pack(side="left", padx=4)

        self.preview = tk.Canvas(lf, height=200, bg="#1a1a1a", highlightthickness=0)
        # game-area height / position (works on any monitor size or shape)
        lrow = ttk.Frame(lf)
        lrow.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Label(lrow, text="Height:").pack(side="left")
        self.height_var = tk.StringVar()
        self.height_combo = ttk.Combobox(lrow, textvariable=self.height_var, state="readonly", width=26)
        self.height_combo.pack(side="left", padx=(4, 12))
        self.height_combo.bind("<<ComboboxSelected>>", lambda e: self.on_layout_pick("game_height_pct"))
        ttk.Label(lrow, text="Position:").pack(side="left")
        self.pos_var = tk.StringVar()
        pos = ttk.Combobox(lrow, textvariable=self.pos_var, state="readonly", width=18,
                           values=[l for _, l in POSITION_CHOICES])
        pos.pack(side="left", padx=(4, 12))
        pos.bind("<<ComboboxSelected>>", lambda e: self.on_layout_pick("game_position"))
        ttk.Label(lrow, text="If shorter than the monitor:").pack(side="left")
        self.valign_var = tk.StringVar()
        val = ttk.Combobox(lrow, textvariable=self.valign_var, state="readonly", width=12,
                           values=[l for _, l in VALIGN_CHOICES])
        val.pack(side="left", padx=4)
        val.bind("<<ComboboxSelected>>", lambda e: self.on_layout_pick("game_valign"))

        self.preview.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(10, 4))
        self.preview.bind("<Configure>", lambda e: self.draw_preview())
        self.preview_boxes = {}
        self.preview_hot = None
        self.zone_contents = {"left": [], "center": [], "right": []}
        lf.columnconfigure(1, weight=1)
        ttk.Button(lf, text="Show zones on my monitor", command=self.show_overlay
                   ).grid(row=4, column=0, sticky="w")
        # Moving real windows around never snaps them - drag & drop only works inside the app.
        self.drag_var = tk.BooleanVar(value=False)
        self.ontop_var = tk.BooleanVar(value=bool(self.cfg.get("game_on_top", True)))
        ttk.Checkbutton(lf, text="Game covers the taskbar while it's focused", variable=self.ontop_var,
                        command=self.on_options).grid(row=4, column=1, columnspan=3, sticky="w", padx=6)
        self.fps_var = tk.BooleanVar(value=bool(self.cfg.get("fps_overlay")))
        ttk.Checkbutton(lf, text="FPS counter on the game (Ctrl+Alt+F)", variable=self.fps_var,
                        command=lambda: self.toggle_fps(self.fps_var.get())).grid(row=4, column=4, columnspan=2,
                                                                                  sticky="w")

        # --- Windows section
        wf = ttk.LabelFrame(main, text=" 2. Open windows - drag one onto a box above, "
                                       "or pick one and click a button ", padding=8)
        wf.pack(fill="both", expand=True, **pad)
        tv_frame = ttk.Frame(wf)
        tv_frame.pack(fill="both", expand=True)
        self.win_tree = ttk.Treeview(tv_frame, columns=("title", "exe", "sound"), show="headings", height=6,
                                     selectmode="browse")
        self.win_tree.heading("title", text="Window")
        self.win_tree.heading("exe", text="Program")
        self.win_tree.heading("sound", text="Sound (click)")
        self.win_tree.column("title", width=460)
        self.win_tree.column("exe", width=180)
        self.win_tree.column("sound", width=100, anchor="center")
        sb = ttk.Scrollbar(tv_frame, orient="vertical", command=self.win_tree.yview)
        self.win_tree.configure(yscrollcommand=sb.set)
        self.win_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.win_tree.bind("<ButtonPress-1>", self.list_drag_press, add="+")
        self.win_tree.bind("<B1-Motion>", self.list_drag_motion)
        self.win_tree.bind("<ButtonRelease-1>", self.list_drag_release, add="+")

        br = ttk.Frame(wf)
        br.pack(fill="x", pady=(8, 0))
        ttk.Button(br, text="<  Left screen", command=lambda: self.on_place("left", False)).pack(side="left", padx=3)
        ttk.Button(br, text="GAME", command=lambda: self.on_place("center", True)).pack(side="left", padx=3)
        ttk.Button(br, text="Middle", command=lambda: self.on_place("center", False)).pack(side="left", padx=3)
        ttk.Button(br, text="Right screen  >", command=lambda: self.on_place("right", False)).pack(side="left", padx=3)
        ttk.Button(br, text="Restore window", command=self.on_restore).pack(side="left", padx=3)
        ttk.Button(br, text="Mute / unmute", command=self.on_mute).pack(side="left", padx=3)
        ttk.Button(br, text="Ignore / stop ignoring", command=self.on_ignore).pack(side="left", padx=3)
        ttk.Button(br, text="Refresh list", command=self.refresh_windows).pack(side="right", padx=3)

        # --- Rules section
        rf = ttk.LabelFrame(main, text=" 3. Remember apps - placed automatically every time they open ", padding=8)
        rf.pack(fill="x", **pad)
        self.rule_tree = ttk.Treeview(rf, columns=("app", "where", "mouse"), show="headings", height=3,
                                      selectmode="browse")
        self.rule_tree.heading("app", text="Program")
        self.rule_tree.heading("where", text="Goes to")
        self.rule_tree.heading("mouse", text="Mouse locked to it")
        self.rule_tree.column("app", width=300)
        self.rule_tree.column("where", width=220)
        self.rule_tree.column("mouse", width=160)
        self.rule_tree.pack(fill="x")

        ar = ttk.Frame(rf)
        ar.pack(fill="x", pady=(8, 0))
        ttk.Label(ar, text="Remember the selected window in:").pack(side="left")
        self.rule_zone_var = tk.StringVar(value="GAME")
        ttk.Combobox(ar, textvariable=self.rule_zone_var, state="readonly", width=26,
                     values=["GAME", "Middle", "Left screen", "Right screen", "Ignored (full screen)"]
                     ).pack(side="left", padx=6)
        self.rule_lock_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ar, text="Lock mouse to it", variable=self.rule_lock_var).pack(side="left", padx=6)
        ttk.Button(ar, text="Remember", command=self.add_rule).pack(side="left", padx=6)
        ttk.Button(ar, text="Forget selected", command=self.remove_rule).pack(side="right")

        self.detect_var = tk.BooleanVar(value=bool(self.cfg.get("auto_detect_games", True)))
        ttk.Checkbutton(rf, text="Recognise games automatically (put them in the middle and remember them)",
                        variable=self.detect_var, command=self.on_detect_toggle).pack(anchor="w", pady=(6, 0))
        grow = ttk.Frame(rf)
        grow.pack(anchor="w", pady=(4, 0))
        ttk.Label(grow, text="When a new game is recognised:").pack(side="left")
        self.newgame_var = tk.StringVar(value=dict(NEW_GAME_CHOICES).get(self.cfg.get("new_game_action", "game")))
        ng = ttk.Combobox(grow, textvariable=self.newgame_var, state="readonly", width=30,
                          values=[l for _, l in NEW_GAME_CHOICES])
        ng.pack(side="left", padx=6)
        ng.bind("<<ComboboxSelected>>", lambda e: self.on_layout_pick("new_game_action"))
        self.newlock_var = tk.BooleanVar(value=bool(self.cfg.get("new_game_lock", True)))
        ttk.Checkbutton(grow, text="Lock the mouse to new games", variable=self.newlock_var,
                        command=lambda: self.on_layout_pick("new_game_lock")).pack(side="left", padx=6)
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_apply", True)))
        ttk.Checkbutton(rf, text="Auto-place remembered apps (keeps them in place if the game moves itself)",
                        variable=self.auto_var, command=self.on_auto_toggle).pack(anchor="w", pady=(6, 0))

        # --- Bottom
        bf = ttk.Frame(main)
        bf.pack(fill="x", **pad)
        ttk.Label(bf, style="Hint.TLabel", justify="left", text=(
            "Hotkeys (work inside games):   Ctrl+Alt+Up = game to middle     Ctrl+Alt+Left = left screen     "
            "Ctrl+Alt+Right = right screen\n"
            "                                              Ctrl+Alt+Down = back to normal     "
            "Ctrl+Alt+L = lock/unlock mouse to the window     Ctrl+Alt+F = FPS counter on/off\n"
            "Drag & drop:  drag a row from the list onto a box in the layout above.  "
            "Hold Ctrl when dropping in the middle = keep its border.\n"
            "Alt+Tab or the Windows key = normal desktop (the game stops covering things). "
            "Click the game to go back.")).pack(anchor="w")
        opt = ttk.Frame(main)
        opt.pack(fill="x", **pad)
        self.startup_var = tk.BooleanVar(value=bool(getattr(self, "startup_on", False)))
        ttk.Checkbutton(opt, text="Start with Windows", variable=self.startup_var,
                        command=self.on_startup_toggle).pack(side="left")
        if not self.is_admin:
            ttk.Button(opt, text="Restart as admin (needed for some games)",
                       command=self.restart_as_admin).pack(side="right")
        ttk.Button(opt, text="Add desktop shortcut", command=self.on_desktop_shortcut
                   ).pack(side="right", padx=6)

        self.style = ttk.Style(root)
        self.light_theme = self.style.theme_use()
        self.apply_theme()

    # ---------------- themes ----------------
    def on_theme(self):
        self.cfg["theme"] = self.theme_var.get()
        save_config(self.cfg)
        self.apply_theme()

    def theme(self):
        return THEMES.get(self.cfg.get("theme"), THEMES["Light"])

    def pick_font(self, names):
        if not hasattr(self, "_families"):
            from tkinter import font as tkfont
            self._families = {f.lower() for f in tkfont.families(self.root)}
        for n in names:
            if n.lower() in self._families:
                return n
        return "Segoe UI" if "segoe ui" in self._families else "TkDefaultFont"

    def apply_theme(self):
        t = self.theme()
        s = self.style
        head = self.pick_font(t["head"])
        body = self.pick_font(t["body"])
        hs = t.get("head_scale", 1.0)
        self.fonts = {"head": head, "body": body, "hs": hs, "slant": t.get("slant", "roman")}
        hw = t.get("head_weight", "bold")
        if t.get("native"):
            s.theme_use(self.light_theme)
            s.configure("Hint.TLabel", foreground=t["hint"])
            s.configure("Status.TLabel", relief="sunken")
            s.configure("TLabelframe.Label", font=(head, 9, "bold"))
            s.configure("Treeview", rowheight=20)
            list_bg, list_fg = "white", "black"
            bg = s.lookup("TFrame", "background") or "#f0f0f0"
        else:
            bg = t["bg"]
            s.theme_use("clam")
            s.configure(".", background=bg, foreground=t["fg"], fieldbackground=t["field"],
                        bordercolor=t["border"], lightcolor=bg, darkcolor=bg,
                        troughcolor=t["field"], focuscolor=t["accent"], selectbackground=t["accent"],
                        selectforeground=t["accent_fg"], insertcolor=t["field_fg"], arrowcolor=t["fg"],
                        font=(body, 9))
            s.configure("TButton", background=t["button"], foreground=t["button_fg"], bordercolor=t["border"],
                        padding=(10, 4), font=(body, 9, "bold"))
            s.map("TButton", background=[("pressed", t["accent"]), ("active", t["hover"])],
                  foreground=[("pressed", t["accent_fg"])])
            s.configure("Treeview", background=t["field"], fieldbackground=t["field"],
                        foreground=t["field_fg"], rowheight=22, font=(body, 9))
            s.configure("Treeview.Heading", background=t["button"], foreground=t["heading"],
                        font=(head, max(9, int(9 * hs)), hw), relief="flat")
            s.map("Treeview", background=[("selected", t["accent"])],
                  foreground=[("selected", t["accent_fg"])])
            s.map("Treeview.Heading", background=[("active", t["hover"])])
            for w in ("TCombobox", "TSpinbox", "TEntry"):
                s.configure(w, background=t["button"], foreground=t["field_fg"], fieldbackground=t["field"],
                            arrowcolor=t["field_fg"])
            s.map("TCombobox", fieldbackground=[("readonly", t["field"])],
                  foreground=[("readonly", t["field_fg"])],
                  selectbackground=[("readonly", t["field"])], selectforeground=[("readonly", t["field_fg"])],
                  background=[("active", t["hover"])])
            s.configure("TCheckbutton", indicatorbackground=t["field"], indicatorforeground=t["accent_fg"])
            s.map("TCheckbutton", background=[("active", bg)],
                  indicatorbackground=[("selected", t["accent"])])
            s.configure("TScrollbar", background=t["button"], arrowcolor=t["fg"])
            s.map("TScrollbar", background=[("active", t["hover"])])
            s.configure("TLabelframe", background=bg, bordercolor=t["border"])
            s.configure("TLabelframe.Label", background=bg, foreground=t["heading"],
                        font=(head, max(10, int(10 * hs)), hw))
            s.configure("Hint.TLabel", foreground=t["hint"])
            s.configure("Status.TLabel", background=t["button"], foreground=t["fg"], relief="flat")
            list_bg, list_fg = t["field"], t["field_fg"]
        self.root.configure(bg=bg)
        self.theme_bg = bg
        # Dropdown lists of comboboxes are classic Tk widgets - colour them by hand.
        self.root.option_add("*TCombobox*Listbox.background", list_bg)
        self.root.option_add("*TCombobox*Listbox.foreground", list_fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", t.get("accent_fg", "white"))
        for cb in self.all_widgets(self.root):
            if cb.winfo_class() == "TCombobox":
                try:
                    pop = cb.tk.eval(f"ttk::combobox::PopdownWindow {cb}")
                    cb.tk.call(f"{pop}.f.l", "configure", "-background", list_bg, "-foreground", list_fg,
                               "-selectbackground", t["accent"], "-selectforeground", t.get("accent_fg", "white"))
                except Exception:
                    pass
        self.set_dark_titlebar(not t.get("native"))
        self.draw_banner()
        self.draw_preview()
        if hasattr(self, "overlays"):
            for ov in self.overlays.values():
                ov["label"].configure(font=(head, int(22 * hs), hw))
        if hasattr(self, "ghost"):
            self.ghost["label"].configure(bg=t["accent"], fg=t.get("accent_fg", "white"),
                                          font=(body, 10, "bold"))

    def all_widgets(self, w):
        out = [w]
        for c in w.winfo_children():
            out.extend(self.all_widgets(c))
        return out

    def set_dark_titlebar(self, dark):
        try:
            self.root.update_idletasks()
            hwnd = int(self.root.wm_frame(), 16)
            val = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old Windows 10 builds)
                if DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
            SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception:
            pass

    # ---------------- the title banner (each theme draws its own) ----------------
    def draw_banner(self):
        if not hasattr(self, "fonts"):
            return
        if not hasattr(self, "logo_small"):
            self.logo_small = None
            try:
                if os.path.exists(ICON_PNG):
                    self.logo_small = self.tk.PhotoImage(file=ICON_PNG).subsample(7)
            except Exception:
                pass
        c, t = self.banner, self.theme()
        c.delete("all")
        c.configure(bg=self.theme_bg)
        W, H = max(c.winfo_width(), 300), 46
        head, hs, slant = self.fonts["head"], self.fonts["hs"], self.fonts["slant"]
        hw = t.get("head_weight", "bold")
        title = APP_NAME.upper() if t.get("upper") else APP_NAME
        sp = t.get("spacing", 0)
        if sp:
            title = (" " * (1 if sp < 4 else 2)).join(title.replace(" ", " "))
        font = (head, int(18 * hs), hw, slant)
        style = t.get("banner", "plain")
        acc, bord, headc = t["accent"], t["border"], t["heading"]

        def text(x, color, dx=0, dy=0):
            return c.create_text(x + dx, H // 2 + dy, text=title, anchor="w", fill=color, font=font)

        if style == "overwatch":
            for i in range(3):
                x = 4 + i * 11
                c.create_polygon(x + 8, 8, x + 14, 8, x + 6, H - 8, x, H - 8, fill=acc, outline="")
            tid = text(48, headc)
            x0, _, x1, _ = c.bbox(tid)
            c.create_rectangle(x0, H - 7, x1, H - 4, fill=acc, outline="")
            c.create_line(x1 + 14, H - 6, W - 4, H - 6, fill=bord, width=2)
        elif style == "destiny":
            c.create_rectangle(0, H // 2 - 3, 6, H // 2 + 3, fill=headc, outline="")
            tid = text(16, headc)
            x1 = c.bbox(tid)[2] + 18
            c.create_line(x1, H // 2, W - 10, H // 2, fill=bord)
            for i in range(4):
                x = W - 10 - i * 8
                c.create_line(x, H // 2 - 4, x, H // 2 + 4, fill=t["hint"])
        elif style == "nms":
            tid = text(4, headc)
            x1 = c.bbox(tid)[2] + 18
            for i, col in enumerate(t.get("blocks", [acc])):
                c.create_rectangle(x1 + i * 22, H // 2 - 7, x1 + i * 22 + 16, H // 2 + 7, fill=col, outline="")
            lx = x1 + len(t.get("blocks", [acc])) * 22 + 10
            c.create_line(lx, H // 2, W - 6, H // 2, fill=bord, width=2)
            for x in range(lx, W - 6, 18):
                c.create_line(x, H // 2 - 4, x, H // 2 + 4, fill=bord)
        elif style == "cyber":
            c.create_polygon(0, 6, 8, 6, 8, H - 6, 0, H - 14, fill=acc, outline="")
            text(18, t.get("glitch1", "#00f0ff"), 2, 1)
            text(18, t.get("glitch2", "#ff003c"), -2, -1)
            tid = text(18, headc)
            x1 = c.bbox(tid)[2] + 16
            c.create_polygon(x1, H // 2 - 2, W - 120, H // 2 - 2, W - 112, H // 2 + 6, x1, H // 2 + 6,
                             fill=acc, outline="")
            for i in range(3):
                c.create_rectangle(W - 100 + i * 9, H // 2 - 2, W - 94 + i * 9, H // 2 + 6,
                                   fill=t.get("glitch2", "#ff003c"), outline="")
            c.create_text(W - 4, H - 4, text="// LINK_ESTABLISHED", anchor="se",
                          fill=t.get("glitch1", "#00f0ff"), font=("Consolas", 8))
        else:
            x = 2
            logo = getattr(self, "logo_small", None)
            if logo:
                c.create_image(0, H // 2, image=logo, anchor="w")
                x = logo.width() + 10
            tid = text(x, headc)
            x0, _, x1, _ = c.bbox(tid)
            c.create_rectangle(x0, H - 6, x0 + 40, H - 3, fill=acc, outline="")

    def set_status(self, text):
        self.status_var.set(text)

    # ---------------- monitors / layout ----------------
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
        return f"{name}  -  {w} x {h}" + (f"   ({', '.join(tags)})" if tags else "")

    def pick_initial_monitor(self):
        self.mon_combo["values"] = [self.monitor_label(m) for m in self.monitors]
        idx = None
        for i, m in enumerate(self.monitors):
            if m["device"] == self.cfg.get("monitor"):
                idx = i
        if idx is None and self.monitors:
            idx = max(range(len(self.monitors)),
                      key=lambda i: (self.monitors[i]["rect"][2] / max(self.monitors[i]["rect"][3], 1),
                                     self.monitors[i]["rect"][2]))
        self.mon_idx = idx or 0
        if self.monitors:
            self.mon_combo.current(self.mon_idx)
        self.sync_size_fields()

    def rescan_monitors(self):
        self.monitors = get_monitors()
        self.pick_initial_monitor()
        self.draw_preview()
        self.set_status(f"Found {len(self.monitors)} monitor(s).")

    def on_monitor_change(self):
        self.mon_idx = self.mon_combo.current()
        self.cfg["monitor"] = self.monitors[self.mon_idx]["device"]
        save_config(self.cfg)
        self.sync_size_fields()
        self.draw_preview()

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

    def refresh_monitor_info(self):
        """Pick up taskbar moves / resolution changes without losing the chosen monitor."""
        mons = get_monitors()
        if not mons:
            return
        old_devices = [m["device"] for m in self.monitors]
        old_zones = self.zones()
        if [m["device"] for m in mons] != old_devices:
            self.monitors = mons
            self.pick_initial_monitor()
        else:
            self.monitors = mons
        if self.zones() != old_zones:
            self.draw_preview()

    def on_options(self):
        self.cfg["game_on_top"] = bool(self.ontop_var.get())
        save_config(self.cfg)
        cz = self.zones()["center"]
        for hwnd in list(self.saved_styles):
            if IsWindow(hwnd) and visible_rect(hwnd) == cz:
                self.ontop.add(hwnd)
        self.update_ontop()

    def sync_size_fields(self):
        _, _, cw, ch = self.zones()["center"]
        self.cw_var.set(cw)
        self.ch_var.set(ch)
        if not hasattr(self, "height_var"):
            return
        self.preset_var.set(preset_name_for(self.cfg, cw, ch))
        ga = game_area_options(self.cfg)
        heights = [("Full height (100%)" if p == 100 else f"{p}% of the monitor height") for p in HEIGHT_CHOICES]
        if not ga["height_pct"]:
            heights = ["Custom (exact pixels)"] + heights
        self.height_combo["values"] = heights
        self.height_var.set("Custom (exact pixels)" if not ga["height_pct"] else
                            ("Full height (100%)" if ga["height_pct"] == 100 else f"{ga['height_pct']}% of the monitor height"))
        self.pos_var.set(dict(POSITION_CHOICES)[ga["position"]])
        self.valign_var.set(dict(VALIGN_CHOICES)[ga["valign"]])

    def on_layout_pick(self, key):
        if key == "game_height_pct":
            text = self.height_var.get()
            if text.startswith("Custom"):
                return
            value = 100 if text.startswith("Full") else int(text.split("%")[0])
        elif key == "game_position":
            value = {l: v for v, l in POSITION_CHOICES}[self.pos_var.get()]
        elif key == "game_valign":
            value = {l: v for v, l in VALIGN_CHOICES}[self.valign_var.get()]
        elif key == "new_game_action":
            value = {l: v for v, l in NEW_GAME_CHOICES}[self.newgame_var.get()]
        else:
            value = bool(self.newlock_var.get())
        self.set_layout_option(key, value)
        if key.startswith("game_"):
            self.sync_size_fields()
            self.draw_preview()
            self.reapply_rules_now()
            cw, ch = self.zones()["center"][2:]
            self.set_status(f"Game area is now {cw} x {ch} ({aspect_name(cw, ch)}).")

    def on_preset(self):
        ratio = dict(PRESETS)[self.preset_var.get()]
        if ratio is None:
            return
        self.set_game_shape(ratio)
        self.sync_size_fields()
        self.draw_preview()
        self.reapply_rules_now()

    def on_size_apply(self):
        try:
            cw, ch = int(self.cw_var.get()), int(self.ch_var.get())
        except Exception:
            self.set_status("Width and height must be numbers.")
            return
        self.set_game_size(cw, ch)
        self.preset_var.set("Custom")
        self.sync_size_fields()
        self.draw_preview()
        self.reapply_rules_now()

    def hot_text(self):
        t = self.theme()
        return "#ffffff" if t["zone_hot"] == "#3b82f6" else t.get("bg", "#000000")

    def draw_preview(self):
        """The layout picture. It's also where you drop apps dragged from the list."""
        c = self.preview
        c.delete("all")
        t = self.theme()
        c.configure(bg=t["preview_bg"])
        cw, chh = c.winfo_width(), c.winfo_height()
        self.preview_boxes = {}
        if cw < 10:
            return
        fonts = getattr(self, "fonts", {"head": "Segoe UI", "body": "Segoe UI", "hs": 1.0})
        head, body, hs = fonts["head"], fonts["body"], fonts["hs"]
        hw = t.get("head_weight", "bold")
        mx, my, mw, mh = self.mon_rect()
        want = int(min(320, max(150, (cw - 20) * mh / mw * 0.9 + 16)))   # the shape of your monitor
        if abs(want - chh) > 3:
            c.configure(height=want)          # redraws itself at the new height
            return
        scale = min((cw - 20) / mw, (chh - 16) / mh)
        ox = (cw - mw * scale) / 2
        oy = (chh - mh * scale) / 2
        # the taskbar strip (whatever isn't the work area)
        work = self.monitors[self.mon_idx].get("work") if self.monitors else None
        if work and work[3] < mh:
            ty = oy + (work[1] + work[3] - my) * scale if work[1] == my else oy
            th = (mh - work[3]) * scale
            c.create_rectangle(ox, ty, ox + mw * scale, ty + th, fill="#3a3a3a", outline="")
        c.create_rectangle(ox, oy, ox + mw * scale, oy + mh * scale, outline=t["zone_outline"], dash=(3, 3))
        from tkinter import font as tkfont
        name_font = tkfont.Font(root=self.root, family=body, size=8)
        for name, (x, y, w, h) in self.zones().items():
            if w <= 0:
                continue
            x0, y0 = ox + (x - mx) * scale, oy + (y - my) * scale
            x1, y1 = x0 + w * scale, y0 + h * scale
            hot = name == self.preview_hot
            fill = t["zone_hot"] if hot else (t["zone_center"] if name == "center" else t["zone_side"])
            txt = self.hot_text() if hot else t["zone_text"]
            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=t["zone_outline"], width=3 if hot else 2)
            self.preview_boxes[name] = (x0, y0, x1, y1)
            c.create_text((x0 + x1) / 2, y0 + 16, fill=txt, text=ZONE_LABELS[name],
                          font=(head, max(9, int(10 * hs)), hw))
            c.create_text((x0 + x1) / 2, y0 + 34, fill=txt, text=f"{w} x {h}", font=(body, 8))
            apps = self.zone_contents.get(name, [])
            maxw = (x1 - x0) - 12
            yy = y0 + 56
            if hot:
                c.create_text((x0 + x1) / 2, (y0 + y1) / 2 + 10, fill=txt, text="DROP HERE",
                              font=(head, max(10, int(12 * hs)), hw))
            else:
                for title in apps[:4]:
                    label = title
                    while len(label) > 3 and name_font.measure(label + "...") > maxw:
                        label = label[:-1]
                    if label != title:
                        label += "..."
                    c.create_text((x0 + x1) / 2, yy, fill=txt, text=label, font=name_font)
                    yy += 15
                if len(apps) > 4:
                    c.create_text((x0 + x1) / 2, yy, fill=txt, text=f"+{len(apps) - 4} more", font=name_font)
                if not apps and name == "center":
                    c.create_text((x0 + x1) / 2, (y0 + y1) / 2 + 14, fill=txt, font=(body, 8),
                                  text="drag your game here")

    # ---------------- floating helper windows ----------------
    def make_floating(self, alpha):
        """A borderless, click-through, always-on-top window shown with raw Win32
        calls, so it never steals focus (from your game or from a drag)."""
        t = self.tk.Toplevel(self.root)
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
        tk = self.tk
        self.overlays = {}
        for name in ("left", "center", "right"):
            t, hwnd = self.make_floating(0.5)
            lbl = tk.Label(t, justify="center")
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            self.overlays[name] = {"top": t, "label": lbl, "hwnd": hwnd}
        g, ghwnd = self.make_floating(0.92)
        glbl = tk.Label(g, padx=10, pady=5)
        glbl.pack()
        self.ghost = {"top": g, "label": glbl, "hwnd": ghwnd}
        self.apply_theme()
        self.root.focus_force()

    def ghost_show(self, text, x, y):
        g = self.ghost
        if g["label"].cget("text") != text:
            g["label"].configure(text=text)
        g["top"].update_idletasks()
        w, h = g["label"].winfo_reqwidth(), g["label"].winfo_reqheight()
        SetWindowPos(g["hwnd"], HWND_TOPMOST, x + 18, y + 14, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def ghost_hide(self):
        ShowWindow(self.ghost["hwnd"], SW_HIDE)

    def overlay_show(self, hot=None, dragging=False):
        if self.overlay_hide_job:
            self.root.after_cancel(self.overlay_hide_job)
            self.overlay_hide_job = None
        t = self.theme()
        zones = self.zones()
        for name, ov in self.overlays.items():
            x, y, w, h = zones[name]
            if w <= 0 or h <= 0:
                ShowWindow(ov["hwnd"], SW_HIDE)
                continue
            is_hot = name == hot
            color = t["zone_hot"] if is_hot else (t["zone_center"] if name == "center" else t["zone_side"])
            fg = self.hot_text() if is_hot else t["zone_text"]
            text = f"{ZONE_LABELS[name]}\n{w} x {h}"
            if dragging and name == "center":
                text += "\n\ngames: no border, covers the taskbar\napps: keep their border (hold Ctrl = always)"
            if dragging:
                text += "\n\nhold Shift = don't snap"
            ov["top"].configure(bg=color, highlightthickness=6,
                                highlightbackground=t["zone_outline"] if not is_hot else t["accent"])
            ov["label"].configure(bg=color, fg=fg, text=text)
            SetWindowPos(ov["hwnd"], HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.root.update_idletasks()

    def overlay_hide(self):
        for ov in self.overlays.values():
            ShowWindow(ov["hwnd"], SW_HIDE)

    def show_overlay(self):
        self.overlay_show()
        self.overlay_hide_job = self.root.after(2500, self.overlay_hide)

    # ---------------- drag & drop snapping (windows dragged by their title bar) ----------------
    def on_drag_event(self, kind, hwnd, rect):
        if kind == "foreground":
            self.update_ontop()
            return
        if kind == "movestart":
            if not self.drag_var.get() or IsIconic(hwnd):
                return
            self.drag = (hwnd, rect, bool(IsZoomed(hwnd)))
            self.drag_zone = None
            self.drag_released_since = None
            self.overlay_show(None, dragging=True)
            self.root.after(30, self.drag_tick)
        elif kind == "moveend":
            if not self.drag or self.drag[0] != hwnd:
                return
            _, start, was_zoomed = self.drag
            self.drag = None
            self.overlay_hide()
            if not was_zoomed and is_resize(start, rect):
                return
            if key_down(VK_SHIFT):
                self.pause_if_rule_differs(hwnd, None, False)
                self.set_status("Moved freely (Shift was held).")
                return
            zone = zone_at(self.zones(), *cursor_pos())
            if not zone:
                return
            borderless = (zone == "center" and not key_down(VK_CONTROL)
                          and self.looks_like_game(self.win_info(hwnd)))
            # tiny delay so Windows finishes its own drop handling first
            self.root.after(60, lambda: self.drop_into(hwnd, zone, borderless))

    def drag_tick(self):
        if not self.drag:
            return
        try:
            # Safety net: if the mouse button is up for a while and we never got
            # the "drag ended" message, hide the boxes.
            if not key_down(0x01):
                self.drag_released_since = self.drag_released_since or time.time()
                if time.time() - self.drag_released_since > 0.5:
                    self.drag = None
                    self.overlay_hide()
                    return
            else:
                self.drag_released_since = None
            zone = None if key_down(VK_SHIFT) else zone_at(self.zones(), *cursor_pos())
            if zone != self.drag_zone:
                self.drag_zone = zone
                self.overlay_show(zone, dragging=True)
        except Exception:
            log_error()
        self.root.after(30, self.drag_tick)

    # ---------------- drag a row from the list onto a box in the layout picture ----------------
    def list_drag_press(self, event):
        row = self.win_tree.identify_row(event.y)
        self.list_drag = {"hwnd": int(row), "x": event.x_root, "y": event.y_root,
                          "active": False, "zone": None} if row else None

    def preview_zone_at(self, x_root, y_root):
        cx = x_root - self.preview.winfo_rootx()
        cy = y_root - self.preview.winfo_rooty()
        for name, (x0, y0, x1, y1) in self.preview_boxes.items():
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return name
        return None

    def list_drag_motion(self, event):
        d = self.list_drag
        if not d:
            return
        if not d["active"]:
            if abs(event.x_root - d["x"]) + abs(event.y_root - d["y"]) < 10:
                return
            d["active"] = True
            w = self.windows.get(d["hwnd"])
            d["title"] = (w.title if w else "window")[:40]
            self.win_tree.configure(cursor="fleur")
            self.set_status(f"Dragging \"{d['title']}\" - drop it on a box in the layout above.")
        self.ghost_show(d["title"], event.x_root, event.y_root)
        zone = None if key_down(VK_SHIFT) else self.preview_zone_at(event.x_root, event.y_root)
        if zone != d["zone"]:
            d["zone"] = zone
            self.preview_hot = zone
            self.draw_preview()
        return "break"

    def list_drag_release(self, event):
        d = self.list_drag
        self.list_drag = None
        if d and not d["active"] and self.win_tree.identify_column(event.x) == "#3":
            self.toggle_mute(d["hwnd"])       # clicked the Sound column
            return
        if not d or not d["active"]:
            return
        self.win_tree.configure(cursor="")
        self.ghost_hide()
        self.preview_hot = None
        self.draw_preview()
        zone = None if key_down(VK_SHIFT) else self.preview_zone_at(event.x_root, event.y_root)
        if not zone:
            self.set_status("Drag cancelled - drop it on one of the boxes in the layout above.")
            return "break"
        borderless = (zone == "center" and not key_down(VK_CONTROL)
                      and self.looks_like_game(self.win_info(d["hwnd"])))
        self.drop_into(d["hwnd"], zone, borderless)
        self.root.after(400, self.update_zone_contents)
        return "break"

    def drop_into(self, hwnd, zone, borderless):
        if self.place(hwnd, zone, borderless, focus=False):
            self.keep_ui_on_top(hwnd)
            self.pause_if_rule_differs(hwnd, zone, borderless)

    def pause_if_rule_differs(self, hwnd, zone, borderless):
        """If you drag a remembered app somewhere else, don't yank it back."""
        exe, title = exe_name(window_pid(hwnd)), window_title(hwnd)
        for rule in self.cfg["rules"]:
            if rule_matches(rule, exe, title) and (rule["zone"], bool(rule.get("borderless"))) != (zone, borderless):
                self.paused.add(hwnd)

    # ---------------- game above the taskbar (only while you're playing) ----------------
    def set_topmost(self, hwnd, on):
        SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS)

    def my_taskbars(self):
        now = time.time()
        if not hasattr(self, "_bars") or now - self._bars[0] > 5:
            bars = [t for t in taskbars() if intersect(window_rect(t), self.mon_rect())[2] > 0]
            self._bars = (now, bars)
        return self._bars[1]

    def update_ontop(self):
        """The game covers the taskbar only while it's the window you're using.
        Alt+Tab / Windows key / clicking another app = normal Windows until you
        click the game again."""
        if not self.ontop or self.drag or self.list_drag:
            return
        fg = GetForegroundWindow()
        fg_pid = window_pid(fg) if fg else 0
        # A "normal" app window you switched to (not the taskbar, Start, Alt+Tab
        # switcher or desktop). The game gets tucked in right behind it.
        fg_normal = bool(fg) and IsWindowVisible(fg) and class_name(fg) not in SHELL_CLASSES \
            and not (GetWindowLongPtrW(fg, GWL_EXSTYLE) & WS_EX_TOPMOST)
        for hwnd in list(self.ontop):
            if not IsWindow(hwnd):
                self.ontop.discard(hwnd)
                continue
            game_active = bool(fg) and (hwnd == fg or fg_pid == window_pid(hwnd))
            is_top = bool(GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
            if game_active:
                if self.ontop_var.get() and not IsIconic(hwnd):
                    if not is_top or any(is_above(t, hwnd) for t in self.my_taskbars()):
                        self.set_topmost(hwnd, True)
                elif not self.ontop_var.get() and is_top:
                    self.set_topmost(hwnd, False)
                continue
            # You're using something else: the game acts like a normal window.
            if is_top:
                self.set_topmost(hwnd, False)
            if fg_normal and (is_top or not is_above(fg, hwnd)):
                # HWND_NOTOPMOST alone would leave the game ABOVE your browser,
                # so put it directly behind the window you switched to.
                SetWindowPos(hwnd, fg, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                             | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS)

    def ontop_tick(self):
        try:
            self.update_ontop()
        except Exception:
            log_error()
        self.root.after(200, self.ontop_tick)

    # ---------------- window list ----------------
    def refresh_windows(self):
        """Update the list in place (keeps your selection and scroll position)."""
        new = list_windows(self.pid)
        newmap = {w.hwnd: w for w in new}
        existing = set(self.win_tree.get_children())
        for iid in existing:
            if int(iid) not in newmap:
                self.win_tree.delete(iid)
        for w in new:
            prog = (w.exe or "(protected)") + ("  - ignored" if self.is_ignored(w.exe) else "")
            iid, vals = str(w.hwnd), (w.title, prog, self.sound_text(w.exe))
            if iid in existing:
                if tuple(str(v) for v in self.win_tree.item(iid, "values")) != vals:
                    self.win_tree.item(iid, values=vals)
            else:
                self.win_tree.insert("", "end", iid=iid, values=vals)
        self.windows = newmap

    def update_zone_contents(self):
        zones = self.zones()
        new = {k: [] for k in zones}
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
                    new[name].append(w.title)
                    # A borderless game sitting in the middle (e.g. placed before the app
                    # was restarted) gets the same focus-aware on-top handling.
                    if name == "center" and self.is_game_window(w.hwnd):
                        self.ontop.add(w.hwnd)
                    break
        if new != self.zone_contents:
            self.zone_contents = new
            if not self.list_drag:
                self.draw_preview()

    def windows_tick(self):
        try:
            if not self.list_drag:
                self.mixer.refresh()        # who's muted (Windows' volume mixer)
                self.refresh_windows()
                self.update_zone_contents()
        except Exception:
            log_error()
        self.root.after(2000, self.windows_tick)

    def selected_hwnd(self):
        sel = self.win_tree.selection()
        return int(sel[0]) if sel else None

    # ---------------- placing windows ----------------
    def place(self, hwnd, zone, borderless, quiet=False, focus=True):
        if not hwnd or not IsWindow(hwnd):
            if not quiet:
                self.set_status("That window is closed. Click 'Refresh list'.")
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
        if borderless:
            if hwnd not in self.saved_styles:
                self.saved_styles[hwnd] = (style, exstyle, window_rect(hwnd))
            new_style = style & ~FRAME_STYLES
            new_ex = exstyle & ~FRAME_EXSTYLES
            if new_style != style:
                SetWindowLongPtrW(hwnd, GWL_STYLE, new_style)
            if new_ex != exstyle:
                SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_ex)
            if zone == "center" and self.ontop_var.get():
                # Topmost = drawn above the taskbar - but only while it's the window
                # you're using (update_ontop handles Alt+Tab etc.).
                self.ontop.add(hwnd)
                if (not quiet and focus) or GetForegroundWindow() == hwnd:
                    ok = SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h,
                                      (MOVE_FLAGS & ~SWP_NOZORDER) | SWP_FRAMECHANGED)
                    if not quiet and focus:
                        SetForegroundWindow(hwnd)   # jump straight into the game
                else:
                    ok = SetWindowPos(hwnd, None, x, y, w, h, MOVE_FLAGS | SWP_FRAMECHANGED)
            else:
                ok = SetWindowPos(hwnd, None, x, y, w, h, MOVE_FLAGS | SWP_FRAMECHANGED)
        if not (borderless and zone == "center" and self.ontop_var.get()) and hwnd in self.ontop:
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
                SetForegroundWindow(hwnd)   # bring it to the front so you see it land
        self.paused.discard(hwnd)
        self.root.after(300, lambda: self.verify_place(hwnd, (x, y, w, h), borderless, 2, quiet))
        if not quiet:
            title = window_title(hwnd)[:50]
            if ok:
                self.set_status(f"Moved \"{title}\" to {ZONE_NAMES[zone]} ({w} x {h})."
                                + (f" {unignored} is no longer ignored." if unignored else ""))
            else:
                self.set_status(f"Windows refused to move \"{title}\" (error {ctypes.get_last_error()}). "
                                "Try 'Restart as admin'.")
        return bool(ok)

    def ui_hwnd(self):
        try:
            return int(self.root.wm_frame(), 16)
        except Exception:
            return None

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
            SetWindowLongPtrW(hwnd, GWL_STYLE, style | FRAME_STYLES | WS_SYSMENU)
            SetWindowPos(hwnd, None, 0, 0, 0, 0,
                         MOVE_FLAGS | SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED)
        self.set_status(f"Restored \"{window_title(hwnd)[:50]}\". Remembered-app rules will leave it alone "
                        "until you place it again.")

    def on_place(self, zone, borderless):
        hwnd = self.selected_hwnd()
        if not hwnd:
            self.set_status("First click a window in the list.")
            return
        if zone == "center" and IsWindow(hwnd):
            self.mark_game(hwnd, borderless)     # you chose GAME / Middle yourself
        if self.place(hwnd, zone, borderless, focus=False):
            self.keep_ui_on_top(hwnd)

    # ---------------- per-app mute ----------------
    def sound_text(self, exe):
        m = self.mixer.is_muted(exe) if exe else None
        return "MUTED" if m else ("On" if m is False else "silent")

    def toggle_mute(self, hwnd):
        w = self.windows.get(hwnd)
        if not w or not w.exe:
            self.set_status("That window can't be muted (it's closed or protected).")
            return
        mute = not self.mixer.is_muted(w.exe)
        name = w.exe[:-4] if w.exe.lower().endswith(".exe") else w.exe
        n = self.mixer.set_muted(w.exe, mute)
        if n is None:
            self.set_status("Couldn't reach Windows' volume mixer - see g9_splitter_error.log.")
            return
        if not mute:
            self.set_status(f"Sound back on for {name}.")
        elif n:
            self.set_status(f"Muted {name}.")
        else:
            self.set_status(f"{name} isn't making sound right now - it'll be muted as soon as it does.")
        self.refresh_windows()

    def on_ignore(self):
        hwnd = self.selected_hwnd()
        w = self.windows.get(hwnd) if hwnd else None
        if not w or not w.exe:
            self.set_status("First click a window in the list.")
            return
        on = not self.is_ignored(w.exe)
        self.set_ignored(w.exe, on)
        self.set_status(f"Ignoring {w.exe} - G9 Splitter leaves it alone now, so it can run full screen."
                        if on else f"Stopped ignoring {w.exe}.")
        self.refresh_windows()

    def on_mute(self):
        hwnd = self.selected_hwnd()
        if not hwnd:
            self.set_status("First click a window in the list.")
            return
        self.toggle_mute(hwnd)

    def on_restore(self):
        hwnd = self.selected_hwnd()
        if not hwnd:
            self.set_status("First click a window in the list.")
            return
        self.restore(hwnd)
        self.keep_ui_on_top(hwnd)

    # ---------------- hotkeys ----------------
    def poll_hotkeys(self):
        try:
            while True:
                item = self.hk_queue.get_nowait()
                try:
                    if isinstance(item, tuple):
                        self.on_drag_event(*item)
                    else:
                        self.handle_hotkey(item)
                except Exception:
                    log_error()
        except queue.Empty:
            pass
        self.root.after(25, self.poll_hotkeys)

    def handle_hotkey(self, action):
        if action == "fps":
            self.toggle_fps()
            return
        hwnd = GetForegroundWindow()
        if not hwnd or window_pid(hwnd) == self.pid:
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
                self.set_status(f"Mouse locked to \"{window_title(hwnd)[:50]}\" (Ctrl+Alt+L to unlock, "
                                "Alt+Tab also frees it).")

    # ---------------- rules ----------------
    ZONE_CHOICES = {
        "GAME": ("center", True),
        "Middle": ("center", False),
        "Left screen": ("left", False),
        "Right screen": ("right", False),
        "Ignored (full screen)": ("ignore", False),   # not a spot: left completely alone
    }

    def zone_text(self, rule):
        for text, (z, b) in self.ZONE_CHOICES.items():
            if z == rule["zone"] and b == bool(rule.get("borderless")):
                return text
        return rule["zone"]

    def refresh_rules(self):
        self.rule_tree.delete(*self.rule_tree.get_children())
        for i, r in enumerate(self.cfg["rules"]):
            app = r.get("exe") or f"Window: {r.get('title', '')}"
            where = self.zone_text(r) + ("  (auto-detected game)" if r.get("auto") else "")
            self.rule_tree.insert("", "end", iid=str(i), values=(
                app, where, "Yes" if r.get("lock_mouse") else "No"))
        for i, exe in enumerate(self.cfg.get("ignored", [])):
            self.rule_tree.insert("", "end", iid=f"ign:{i}", values=(
                exe, "Ignored - left alone (full screen OK)", "-"))

    def add_rule(self):
        hwnd = self.selected_hwnd()
        if not hwnd or hwnd not in self.windows:
            self.set_status("First click your game/app in the 'Open windows' list.")
            return
        w = self.windows[hwnd]
        zone, borderless = self.ZONE_CHOICES[self.rule_zone_var.get()]
        if zone == "ignore":
            if not w.exe:
                self.set_status("That window can't be ignored (it's protected).")
                return
            self.set_ignored(w.exe, True)
            self.set_status(f"Remembered {w.exe} as ignored - G9 Splitter leaves it alone every time it opens, so it can run full screen.")
            self.refresh_windows()
            return
        rule = {"exe": w.exe, "title": "" if w.exe else w.title, "zone": zone,
                "borderless": borderless, "lock_mouse": bool(self.rule_lock_var.get())}
        self.cfg["rules"] = [r for r in self.cfg["rules"] if not rule_matches(r, w.exe, w.title)]
        self.cfg["rules"].append(rule)
        save_config(self.cfg)
        self.refresh_rules()
        if self.place(hwnd, zone, borderless, focus=False):
            self.keep_ui_on_top(hwnd)
        self.set_status(f"Remembered {w.exe or w.title}. It will go to the "
                        f"{ZONE_NAMES[zone]} every time it opens.")

    def remove_rule(self):
        sel = self.rule_tree.selection()
        if not sel:
            self.set_status("Click a remembered app first.")
            return
        if sel[0].startswith("ign:"):          # an ignored program: stop ignoring it
            ign = self.cfg.get("ignored", [])
            i = int(sel[0][4:])
            if 0 <= i < len(ign):
                exe = ign[i]
                self.set_ignored(exe, False)
                self.set_status(f"Stopped ignoring {exe}.")
                self.refresh_windows()
            return
        r = self.cfg["rules"].pop(int(sel[0]))
        if r.get("auto") and r.get("exe"):     # you said it isn't a game: never auto-pick it again
            self.cfg["games"] = [g for g in self.cfg.get("games", []) if g.lower() != r["exe"].lower()]
            self.cfg.setdefault("not_games", []).append(r["exe"])
        save_config(self.cfg)
        self.refresh_rules()

    def on_detect_toggle(self):
        self.cfg["auto_detect_games"] = bool(self.detect_var.get())
        save_config(self.cfg)

    def on_auto_toggle(self):
        self.cfg["auto_apply"] = bool(self.auto_var.get())
        save_config(self.cfg)

    def reapply_rules_now(self):
        self.last_apply.clear()

    def needs_apply(self, hwnd, target, borderless):
        if borderless and GetWindowLongPtrW(hwnd, GWL_STYLE) & (WS_CAPTION | WS_THICKFRAME):
            return True
        cur = visible_rect(hwnd)
        return any(abs(a - b) > 2 for a, b in zip(cur, target))

    def rules_tick(self):
        try:
            self.refresh_monitor_info()
            lock_set = set()
            if not self.drag:
                self.detect_games(list_windows(self.pid))
            if self.cfg["rules"] and not self.drag:
                wins = list_windows(self.pid)
                zones = self.zones()
                now = time.time()
                for rule in self.cfg["rules"]:
                    if self.is_ignored(rule.get("exe")):
                        continue
                    if rule.get("zone") in zones and (zones[rule["zone"]][2] <= 0 or zones[rule["zone"]][3] <= 0):
                        continue        # that screen has no room with the current layout - wait
                    cands = [w for w in wins if rule_matches(rule, w.exe, w.title)
                             and w.hwnd not in self.paused]
                    if not cands:
                        continue
                    w = max(cands, key=lambda c: window_rect(c.hwnd)[2] * window_rect(c.hwnd)[3])
                    if rule.get("lock_mouse"):
                        lock_set.add(w.hwnd)
                    if not self.auto_var.get() or IsIconic(w.hwnd):
                        continue
                    if (rule["zone"] == "center" and rule.get("borderless") and self.ontop_var.get()
                            and w.hwnd not in self.ontop):
                        self.ontop.add(w.hwnd)
                    if (self.needs_apply(w.hwnd, self.target_rect(rule["zone"], rule.get("borderless")),
                                         rule.get("borderless"))
                            and now - self.last_apply.get(w.hwnd, 0) > 3):
                        self.last_apply[w.hwnd] = now
                        self.auto_place(w.hwnd, rule, now)
            self.rule_lock_hwnds = lock_set
        except Exception:
            log_error()
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
                r = wt.RECT(x, y, x + w, y + h)
                ClipCursor(ctypes.byref(r))
                self.clipping = True
            elif self.clipping:
                ClipCursor(None)
                self.clipping = False
        except Exception:
            log_error()
        self.root.after(200, self.mouse_tick)

    # ---------------- misc ----------------
    def on_startup_toggle(self):
        """Start with Windows: the fast way (a sign-in task - Windows may ask once), straight into the tray."""
        on = bool(self.startup_var.get())
        self.set_status("Setting up start with Windows..." if on else "Turning off start with Windows...")

        def work():
            msg = None
            try:
                if on:
                    how = enable_startup()
                    self.startup_on = how is not None
                    msg = {"task": "G9 Splitter will start in the tray as soon as you sign in.",
                           "run": "G9 Splitter will start in the tray when Windows starts (Windows didn't allow "
                                  "the fastest way)."}.get(how, "Couldn't turn on start with Windows - see "
                                                                "g9_splitter_error.log.")
                else:
                    off = disable_startup()
                    self.startup_on = not off
                    msg = ("G9 Splitter will no longer start with Windows." if off else
                           "Couldn't turn off start with Windows (the admin question was declined).")
            except Exception:
                log_error()
            self.root.after(0, lambda: (self._startup_changed(), msg and self.set_status(msg)))
        threading.Thread(target=work, daemon=True).start()

    def _startup_changed(self):
        try:
            if hasattr(self, "startup_var") and bool(self.startup_var.get()) != bool(self.startup_on):
                self.startup_var.set(bool(self.startup_on))
        except Exception:
            pass

    def on_desktop_shortcut(self):
        self.set_status("Creating desktop shortcut...")

        def work():
            ok = _quiet(make_shortcut, "desktop")
            self.root.after(0, lambda: self.set_status(
                "Added 'G9 Splitter' to your desktop." if ok else "Couldn't create the desktop shortcut."))
        threading.Thread(target=work, daemon=True).start()

    def restart_as_admin(self):
        script = os.path.abspath(__file__)
        r = ShellExecuteW(None, "runas", pythonw_path(), f'"{script}"', APP_DIR, 1)
        if r and r > 32:
            self.on_close()
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

    # ---------------- tray ----------------
    def show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def hide_to_tray(self):
        self.root.withdraw()

    def close_to_tray(self):
        """Closing the window keeps G9 Splitter running in the tray (right-click the icon to quit)."""
        if not (self.tray and self.tray.ok):
            self.on_close()
            return
        self.hide_to_tray()
        if not self.cfg.get("tray_hint_shown"):
            self.cfg["tray_hint_shown"] = True
            save_config(self.cfg)
            self.tray.balloon(APP_NAME + " is still running",
                              "It's here in the tray. Click the icon to open it, right-click to quit.")

    def on_close(self):
        run_log("closing: " + "".join(traceback.format_stack(limit=4)[:-1]).replace("\n", " | ")[-600:])
        try:
            self.fps_shutdown()
            ClipCursor(None)
            self.hotkeys.stop()
        except Exception:
            pass
        if getattr(self, "tray", None):
            self.tray.remove()
        self.root.destroy()


def log_error():
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
    except Exception:
        pass


def make_shortcut(where):
    """Create a 'G9 Splitter' shortcut with the app logo. where = 'folder' or 'desktop'."""
    import subprocess

    def q(x):
        return "'" + x.replace("'", "''") + "'"
    if where == "desktop":
        dest = "(Join-Path ([Environment]::GetFolderPath('Desktop')) 'G9 Splitter.lnk')"
    else:
        dest = q(os.path.join(APP_DIR, "G9 Splitter.lnk"))
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({dest});"
          f"$s.TargetPath={q(pythonw_path())};"
          f"$s.Arguments={q(chr(34) + os.path.abspath(__file__) + chr(34))};"
          f"$s.WorkingDirectory={q(APP_DIR)};"
          f"$s.IconLocation={q(ICON_ICO + ',0')};"
          f"$s.Description='G9 Splitter - Left | Game | Right';$s.Save()")
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
        args = [a for a in sys.argv[1:] if a not in ("--restarted", "--tray", "--minimized")]
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
    # Own taskbar identity, so Windows shows our logo instead of Python's.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OleJakob.G9Splitter")
    except Exception:
        pass
    import tkinter as tk
    root = tk.Tk()
    try:
        if os.path.exists(ICON_ICO):
            root.iconbitmap(default=ICON_ICO)
        if os.path.exists(ICON_PNG):
            root._logo = tk.PhotoImage(file=ICON_PNG)
            root.iconphoto(True, root._logo)
    except Exception:
        log_error()
    if os.path.exists(ICON_ICO) and not os.path.exists(os.path.join(APP_DIR, "G9 Splitter.lnk")):
        threading.Thread(target=lambda: _quiet(make_shortcut, "folder"), daemon=True).start()
    try:
        app = App(root, start_minimized="--minimized" in sys.argv or "--tray" in sys.argv)
        app.updater = SelfUpdater(root, [__file__], app.on_close,
                                  extra_args=lambda: ["--tray"] if root.state() == "withdrawn" else [])
    except Exception:
        log_error()
        from tkinter import messagebox
        messagebox.showerror(APP_NAME, "G9 Splitter hit an error while starting.\n\n"
                             + traceback.format_exc()[-1500:])
        root.destroy()
        return
    root.mainloop()


def _quiet(fn, *a):
    try:
        return fn(*a)
    except Exception:
        log_error()


if __name__ == "__main__":
    main()
