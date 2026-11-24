"""Колесо мыши над рабочим столом → интерфейс на обоях (Wallpaper Engine передаёт обоям клики, но не колесо).

Низкоуровневый перехватчик видит прокрутку; если курсор над открытым рабочим столом (под ним окно
значков), событие уходит в HUD ({"type": "wheel"}), и интерфейс на обоях сам прокручивает элемент
под курсором. Само колесо не задерживается и не отменяется — его по-прежнему получает система.
"""
import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
DESKTOP_CLASSES = {"Progman", "WorkerW", "SHELLDLL_DefView", "SysListView32"}
WM_MOUSEWHEEL, WM_TIMER = 0x20A, 0x0113
REHOOK_MS = 60_000  # Windows молча снимает «медленный» перехватчик — раз в минуту ставим заново


class _MSLL(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


_PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
for _n, _r, _a in [
    ("SetWindowsHookExW", wintypes.HHOOK, [ctypes.c_int, _PROC, wintypes.HINSTANCE, wintypes.DWORD]),
    ("UnhookWindowsHookEx", wintypes.BOOL, [wintypes.HHOOK]),
    ("CallNextHookEx", ctypes.c_ssize_t, [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]),
    ("WindowFromPoint", wintypes.HWND, [wintypes.POINT]),
    ("GetClassNameW", ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
    ("SetTimer", ctypes.c_size_t, [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]),
    ("GetMessageW", wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]),
]:
    _f = getattr(user32, _n)
    _f.restype, _f.argtypes = _r, _a


def _over_desktop(pt) -> bool:
    h = user32.WindowFromPoint(pt)
    if not h:
        return False
    buf = ctypes.create_unicode_buffer(32)
    user32.GetClassNameW(h, buf, 32)
    return buf.value in DESKTOP_CLASSES


class DesktopWheel(threading.Thread):
    def __init__(self, emit):
        super().__init__(daemon=True, name="desktop-wheel")
        self.emit = emit
        self._hook = None
        self._proc = _PROC(self._on_mouse)  # ссылка обязательна, иначе обработчик соберёт GC

    def _on_mouse(self, n, w, l):
        if n == 0 and w == WM_MOUSEWHEEL:
            m = ctypes.cast(l, ctypes.POINTER(_MSLL)).contents
            if _over_desktop(m.pt):
                delta = ctypes.c_short((m.mouseData >> 16) & 0xFFFF).value
                self.emit({"type": "wheel", "x": m.pt.x, "y": m.pt.y, "delta": delta})
        return user32.CallNextHookEx(None, n, w, l)

    def _rehook(self):
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
        self._hook = user32.SetWindowsHookExW(14, self._proc, None, 0)  # WH_MOUSE_LL

    def run(self):
        self._rehook()
        user32.SetTimer(None, 0, REHOOK_MS, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_TIMER:
                self._rehook()
