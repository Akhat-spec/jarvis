"""Низкоуровневое управление Windows через WinAPI (ctypes): окна, мышь, клавиатура.

Никаких решений о безопасности здесь нет — только «руки». Правила — в safety.py,
решения о подтверждении — в run.py (Hub). Каждое длительное действие проверяет check_abort():
аварийный стоп (Ctrl+Alt+X / кнопка СТОП) и «защитный угол» — увести курсор в левый верхний угол экрана.
"""
import ctypes
import math
import os
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND

ABORT = threading.Event()


class AbortError(Exception):
    pass


# ───────────────────────── общие ─────────────────────────
def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def screen_info() -> dict:
    return {
        "primary": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)],
        "virtual": [user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                    user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)],  # x, y, ширина, высота
        "cursor": list(cursor_pos()),
    }


def check_abort() -> None:
    if ABORT.is_set():
        raise AbortError("Аварийная остановка")
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    x, y = cursor_pos()
    if x <= vx + 2 and y <= vy + 2:
        ABORT.set()
        raise AbortError("Курсор в левом верхнем углу — защитная остановка")


# ───────────────────────── окна ─────────────────────────
def _title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _process_name(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _rect(hwnd) -> list[int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def _cloaked(hwnd) -> bool:
    val = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
    return val.value != 0


def window_info(hwnd) -> dict:
    return {
        "id": int(hwnd), "title": _title(hwnd), "process": _process_name(hwnd), "rect": _rect(hwnd),
        "minimized": bool(user32.IsIconic(hwnd)), "active": int(hwnd) == int(user32.GetForegroundWindow() or 0),
    }


def list_windows() -> list[dict]:
    result = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            return True
        exstyle = user32.GetWindowLongW(hwnd, -20)
        if exstyle & 0x80 or _cloaked(hwnd):  # WS_EX_TOOLWINDOW / скрытые UWP-окна
            return True
        info = window_info(hwnd)
        if info["title"] in ("Program Manager",):
            return True
        result.append(info)
        return True

    user32.EnumWindows(cb, 0)
    return result


def find_window(query: str) -> dict | None:
    q = query.lower().strip()
    wins = [w for w in list_windows() if not w["title"].startswith("J.A.R.V.I.S")]
    if q.isdigit():
        return next((w for w in wins if w["id"] == int(q)), None)
    for key in ("title", "process"):
        for w in wins:
            if q in w[key].lower():
                return w
    return None


def active_window() -> dict | None:
    h = user32.GetForegroundWindow()
    return window_info(h) if h else None


def window_at(x: int, y: int) -> dict | None:
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not h:
        return None
    root = user32.GetAncestor(h, 2)  # GA_ROOT
    return window_info(root or h)


def focus_window(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows не даёт фоновому процессу забирать фокус — нажатие Alt снимает это ограничение
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.15)


def window_command(hwnd: int, action: str) -> None:
    if action == "minimize":
        user32.ShowWindow(hwnd, 6)
    elif action == "maximize":
        user32.ShowWindow(hwnd, 3)
    elif action == "restore":
        user32.ShowWindow(hwnd, 9)
    elif action == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE — программа сама спросит про несохранённое
    else:
        raise ValueError(f"Неизвестное действие с окном: {action}")


# ───────────────────────── ввод (SendInput) ─────────────────────────
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


def _send(*inputs) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    return _INPUT(type=1, u=_UNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)))


def _mouse(flags: int, data: int = 0) -> _INPUT:
    return _INPUT(type=0, u=_UNION(mi=_MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)))


# ───────────────────────── мышь ─────────────────────────
def move_mouse(x: int, y: int, smooth: bool = True) -> None:
    check_abort()
    sx, sy = cursor_pos()
    if smooth:
        dist = math.hypot(x - sx, y - sy)
        steps = max(8, min(45, int(dist / 25)))
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)  # плавный разгон и торможение
            user32.SetCursorPos(int(sx + (x - sx) * e), int(sy + (y - sy) * e))
            time.sleep(0.012)
            if i < steps:
                check_abort()
    user32.SetCursorPos(int(x), int(y))


_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    move_mouse(x, y)
    check_abort()
    down, up = _BUTTONS[button]
    for _ in range(2 if double else 1):
        _send(_mouse(down), _mouse(up))
        time.sleep(0.06)


def scroll(amount: int) -> None:
    """amount > 0 — вверх, < 0 — вниз (в «щелчках» колеса)."""
    check_abort()
    _send(_mouse(0x0800, int(amount) * 120))


# ───────────────────────── клавиатура ─────────────────────────
VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D}


def parse_hotkey(combo: str) -> list[int]:
    codes = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and (part.isalnum()):
            codes.append(ord(part.upper()))
        else:
            raise ValueError(f"Неизвестная клавиша «{part}»")
    return codes


def press_hotkey(combo: str) -> None:
    check_abort()
    codes = parse_hotkey(combo)
    for c in codes:
        _send(_key(c, 0, 1 if c in _EXTENDED else 0))
        time.sleep(0.02)
    for c in reversed(codes):
        _send(_key(c, 0, (1 if c in _EXTENDED else 0) | 0x0002))
        time.sleep(0.02)


def type_text(text: str, delay: float = 0.012) -> None:
    """Печатает любой текст (кириллица, эмодзи) независимо от раскладки — через KEYEVENTF_UNICODE."""
    for ch in text:
        check_abort()
        if ch == "\n":
            press_hotkey("enter")
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            _send(_key(0, unit, 0x0004), _key(0, unit, 0x0004 | 0x0002))
        time.sleep(delay)


# ───────────────────────── файлы ─────────────────────────
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def recycle(path: str) -> None:
    """Удаление в Корзину (можно восстановить), а не безвозвратно."""
    op = _SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None,
                          0x0040 | 0x0010 | 0x0400 | 0x0004, False, None, None)  # ALLOWUNDO|NOCONFIRM|NOERRORUI|SILENT
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Не удалось переместить в Корзину (код {rc})")
