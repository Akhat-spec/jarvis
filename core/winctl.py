"""Управление окном HUD (поверх всех окон, компактный режим) и глобальная горячая клавиша стопа."""
import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32

try:  # реальные пиксели экрана при масштабировании 125–200 %
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x2, 0x1, 0x40
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000

HUD_TITLE = "J.A.R.V.I.S"


def find_hud() -> int | None:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(HUD_TITLE):
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def set_topmost(on: bool) -> bool:
    hwnd = find_hud()
    if not hwnd:
        return False
    user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    return True


def set_compact(on: bool) -> bool:
    hwnd = find_hud()
    if not hwnd:
        return False
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    scale = sh / 1080
    if on:
        w, h = int(440 * scale), int(580 * scale)  # уже ~500 px Edge окно не сжимает
        x, y = sw - w - int(24 * scale), sh - h - int(72 * scale)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_SHOWWINDOW)
    else:
        w, h = int(1280 * scale), int(800 * scale)
        x, y = (sw - w) // 2, (sh - h) // 2
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, x, y, w, h, SWP_SHOWWINDOW)
    return True


def start_hotkey(callback, mods: str = "ctrl+alt", key: str = "X") -> None:
    """Глобальная горячая клавиша в отдельном потоке (RegisterHotKey + цикл сообщений)."""
    flags = MOD_NOREPEAT
    flags |= MOD_CONTROL if "ctrl" in mods else 0
    flags |= MOD_ALT if "alt" in mods else 0
    flags |= MOD_SHIFT if "shift" in mods else 0

    def loop():
        if not user32.RegisterHotKey(None, 1, flags, ord(key.upper())):
            print(f"[!] Не удалось занять горячую клавишу {mods}+{key}")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                callback()

    threading.Thread(target=loop, daemon=True, name="hotkey").start()
