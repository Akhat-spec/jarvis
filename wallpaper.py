"""
J.A.R.V.I.S. — живые обои рабочего стола.

Страница wallpaper/index.html открывается в отдельном окне Edge, которое встраивается за значки
рабочего стола (как это делают Lively Wallpaper и Wallpaper Engine).

    pythonw wallpaper.py          включить
    pythonw wallpaper.py --stop   выключить (вернуть обычные обои)
    pythonw wallpaper.py --auto   включить, только если обои не выключены пользователем

Когда рабочий стол закрыт развёрнутым окном, анимация замедляется до 2 кадров/с.
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE_DIR = ROOT / "wallpaper"
PORT = int(os.getenv("JARVIS_WALL_PORT", "8768"))
LOCK_PORT = PORT + 1
SETTINGS = ROOT / "wallpaper.json"
TITLE = "JARVIS Wallpaper"

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

for name, res, args in [
    ("FindWindowW", wintypes.HWND, [wintypes.LPCWSTR, wintypes.LPCWSTR]),
    ("FindWindowExW", wintypes.HWND, [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]),
    ("SetParent", wintypes.HWND, [wintypes.HWND, wintypes.HWND]),
    ("GetParent", wintypes.HWND, [wintypes.HWND]),
    ("IsWindow", wintypes.BOOL, [wintypes.HWND]),
    ("GetForegroundWindow", wintypes.HWND, []),
    ("IsZoomed", wintypes.BOOL, [wintypes.HWND]),
    ("GetWindowRect", wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
    ("GetClassNameW", ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
    ("MonitorFromWindow", wintypes.HANDLE, [wintypes.HWND, wintypes.DWORD]),
    ("ScreenToClient", wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]),
    ("SetWindowPos", wintypes.BOOL, [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.UINT]),
    ("GetWindowLongPtrW", ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int]),
    ("SetWindowLongPtrW", ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]),
    ("SendMessageTimeoutW", ctypes.c_ssize_t, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                               wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]),
    ("GetWindowThreadProcessId", wintypes.DWORD, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]),
]:
    fn = getattr(user32, name)
    fn.restype, fn.argtypes = res, args

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CHILD, WS_VISIBLE, WS_POPUP = 0x40000000, 0x10000000, 0x80000000
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU, WS_MINMAX = 0xC00000, 0x40000, 0x80000, 0x30000
WS_EX_APPWINDOW, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x40000, 0x80, 0x08000000
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x4, 0x10, 0x20, 0x40


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


# ── настройки ──
def enabled() -> bool:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8")).get("enabled", True)
    except Exception:
        return True


def set_enabled(on: bool) -> None:
    SETTINGS.write_text(json.dumps({"enabled": on}), encoding="utf-8")


# ── рабочий стол ──
def wallpaper_host() -> int | None:
    """Окно, в которое Windows рисует обои (под значками рабочего стола)."""
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None
    res = ctypes.c_size_t()
    user32.SendMessageTimeoutW(progman, 0x052C, 0xD, 0x1, 0, 1000, ctypes.byref(res))  # создать WorkerW
    # Windows 11 24H2 и новее: WorkerW — дочернее окно Progman
    w = user32.FindWindowExW(progman, None, "WorkerW", None)
    if w:
        return w
    # Старые сборки: WorkerW верхнего уровня, следующий за тем, где лежат значки (SHELLDLL_DefView)
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            nxt = user32.FindWindowExW(None, hwnd, "WorkerW", None)
            if nxt:
                found.append(nxt)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else progman


def primary_rect():
    mon = user32.MonitorFromWindow(user32.FindWindowW("Progman", None), 1)  # MONITOR_DEFAULTTOPRIMARY
    mi = MONITORINFO(ctypes.sizeof(MONITORINFO))
    ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcMonitor
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def work_insets() -> dict:
    mon = user32.MonitorFromWindow(user32.FindWindowW("Progman", None), 1)
    mi = MONITORINFO(ctypes.sizeof(MONITORINFO))
    ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    m, w = mi.rcMonitor, mi.rcWork
    return {"left": w.left - m.left, "top": w.top - m.top, "right": m.right - w.right, "bottom": m.bottom - w.bottom}


SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "TopLevelWindowForOverflowXamlIsland",
                 "XamlExplorerHostIslandWindow", "Windows.UI.Core.CoreWindow"}


def _cloaked(hwnd) -> bool:
    v = ctypes.c_int()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(v), 4)  # DWMWA_CLOAKED
    return bool(v.value)


def desktop_covered() -> bool:
    """Рабочий стол закрыт: есть видимое (не свёрнутое, не скрытое системой) развёрнутое или
    полноэкранное окно. Смотрим все окна, а не только активное: после «свернуть всё» активным
    остаётся свёрнутое окно, а развёрнутое неактивное окно тоже закрывает рабочий стол."""
    x, y, w, h = primary_rect()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if (not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or _cloaked(hwnd)
                or not user32.GetWindowTextLengthW(hwnd) or _class(hwnd) in SHELL_CLASSES):
            return True
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if ex & (WS_EX_TOOLWINDOW | 0x20):  # служебные и «прозрачные для мыши» окна (оверлеи) не в счёт
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        on_primary = r.left < x + w and r.right > x and r.top < y + h and r.bottom > y
        if on_primary and (user32.IsZoomed(hwnd) or
                           (r.left <= x and r.top <= y and r.right >= x + w and r.bottom >= y + h)):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(cb, 0)
    return bool(found)


# ── сервер страницы ──
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path == "/covered":  # JARVIS_WALL_ALWAYS_VISIBLE=1 — для проверки: не экономить
            return self._json({"covered": desktop_covered() and not os.getenv("JARVIS_WALL_ALWAYS_VISIBLE")})
        if self.path == "/insets":  # сколько места по краям занимает панель задач (физические пиксели)
            return self._json(work_insets())
        if self.path == "/events":
            return self._events()
        if self.path == "/quit":
            self._json({"ok": True})
            STOP.set()
            return
        super().do_GET()

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        seq = EVENTS.seq
        try:
            while not STOP.is_set():
                batch = EVENTS.wait_after(seq, 15)
                if not batch:
                    self.wfile.write(b": ping\n\n")
                for seq, ev in batch:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


STOP = threading.Event()


class EventHub:
    """События для страницы обоев (Server-Sent Events на /events): сейчас — прокрутка колесом."""

    def __init__(self):
        self.cond = threading.Condition()
        self.items: list[tuple[int, dict]] = []
        self.seq = 0

    def push(self, ev: dict) -> None:
        with self.cond:
            self.seq += 1
            self.items = (self.items + [(self.seq, ev)])[-50:]
            self.cond.notify_all()

    def wait_after(self, seq: int, timeout: float) -> list[tuple[int, dict]]:
        with self.cond:
            self.cond.wait_for(lambda: self.seq > seq or STOP.is_set(), timeout)
            return [it for it in self.items if it[0] > seq]


EVENTS = EventHub()


# ── окно Edge ──
def find_edge_window(pid_hint=None) -> int | None:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(TITLE) and _class(hwnd) == "Chrome_WidgetWin_1":
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def launch_edge(rect) -> subprocess.Popen | None:
    x, y, w, h = rect
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        edge = Path(base) / "Microsoft/Edge/Application/msedge.exe"
        if edge.exists():
            return subprocess.Popen([
                str(edge), f"--app=http://127.0.0.1:{PORT}/", f"--user-data-dir={ROOT / '.wall_profile'}",
                f"--window-position={x},{y}", f"--window-size={w},{h}", "--start-fullscreen", "--no-first-run",
                "--disable-features=Translate,CalculateNativeWinOcclusion",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--autoplay-policy=no-user-gesture-required",
                "--disable-extensions", "--disable-sync", "--disable-component-update",
                "--no-default-browser-check", "--disable-default-apps",
                *([f"--remote-debugging-port={os.getenv('JARVIS_WALL_DEBUG')}"] if os.getenv("JARVIS_WALL_DEBUG") else []),
            ])
    return None


def attach(edge_hwnd: int, host: int) -> None:
    style = user32.GetWindowLongPtrW(edge_hwnd, GWL_STYLE)
    style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINMAX)
    user32.SetWindowLongPtrW(edge_hwnd, GWL_STYLE, style | WS_CHILD | WS_VISIBLE)
    ex = user32.GetWindowLongPtrW(edge_hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(edge_hwnd, GWL_EXSTYLE, (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    user32.SetParent(edge_hwnd, host)
    x, y, w, h = primary_rect()
    pt = wintypes.POINT(x, y)
    user32.ScreenToClient(host, ctypes.byref(pt))
    user32.SetWindowPos(edge_hwnd, None, pt.x, pt.y, w, h,
                        SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW)


# ── ввод: клики и клавиши с рабочего стола → интерфейс на обоях ──
# Обои лежат под значками, и Windows отдаёт клики окну значков. Низкоуровневые перехватчики
# пересылают мышь (когда курсор над открытым рабочим столом) и клавиатуру (после клика по обоям,
# пока активен рабочий стол) окну Edge с обоями. Правый клик остаётся рабочему столу (его меню).
DESKTOP_CLASSES = {"Progman", "WorkerW", "SHELLDLL_DefView", "SysListView32"}
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEWHEEL = 0x200, 0x201, 0x202, 0x20A
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x100, 0x101, 0x102
# клавиши, которые пересылаются как нажатия (не как символы)
NAV_KEYS = {0x08, 0x09, 0x0D, 0x1B, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E}


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
for name, res, args in [
    ("SetWindowsHookExW", wintypes.HHOOK, [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]),
    ("CallNextHookEx", ctypes.c_ssize_t, [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]),
    ("WindowFromPoint", wintypes.HWND, [wintypes.POINT]),
    ("PostMessageW", wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
    ("GetKeyState", ctypes.c_short, [ctypes.c_int]),
    ("GetKeyboardLayout", wintypes.HKL, [wintypes.DWORD]),
    ("ToUnicodeEx", ctypes.c_int, [wintypes.UINT, wintypes.UINT, ctypes.c_char_p, wintypes.LPWSTR, ctypes.c_int,
                                   wintypes.UINT, wintypes.HKL]),
    ("GetMessageW", wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]),
]:
    fn = getattr(user32, name)
    fn.restype, fn.argtypes = res, args


def _lparam_xy(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


class InputBridge:
    def __init__(self):
        self.edge = None      # окно Edge с обоями
        self.target = None    # Chrome_RenderWidgetHostHWND внутри него — принимает ввод
        self.focused = False  # последний клик был по обоям — клавиатура идёт им
        self.lbutton = False
        self.shift = False
        threading.Thread(target=self._loop, daemon=True, name="input-bridge").start()

    def set_edge(self, edge_hwnd):
        if edge_hwnd == self.edge and self.target and self.target != edge_hwnd and user32.IsWindow(self.target):
            return  # окно ввода уже найдено и живо
        self.edge = edge_hwnd
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _):
            if _class(h) == "Chrome_RenderWidgetHostHWND":
                found.append(h)
                return False
            return True

        user32.EnumChildWindows(edge_hwnd, cb, 0)
        self.target = found[0] if found else edge_hwnd

    def _over_desktop(self, pt) -> bool:
        h = user32.WindowFromPoint(pt)
        return bool(h) and (_class(h) in DESKTOP_CLASSES or h in (self.edge, self.target))

    @staticmethod
    def _desktop_active() -> bool:
        fg = user32.GetForegroundWindow()
        return bool(fg) and _class(fg) in DESKTOP_CLASSES

    def _client(self, pt):
        p = wintypes.POINT(pt.x, pt.y)
        user32.ScreenToClient(self.target, ctypes.byref(p))
        return p

    def _mouse(self, n, w, l):
        if n == 0 and self.target and user32.IsWindow(self.target):
            m = ctypes.cast(l, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            over = self._over_desktop(m.pt)
            if w == WM_LBUTTONDOWN:
                self.focused = over
            if over or (self.lbutton and w in (WM_MOUSEMOVE, WM_LBUTTONUP)):
                c = self._client(m.pt)
                if w == WM_MOUSEMOVE:
                    user32.PostMessageW(self.target, WM_MOUSEMOVE, 0x1 if self.lbutton else 0, _lparam_xy(c.x, c.y))
                elif w == WM_LBUTTONDOWN:
                    self.lbutton = True
                    user32.PostMessageW(self.target, WM_LBUTTONDOWN, 0x1, _lparam_xy(c.x, c.y))
                elif w == WM_LBUTTONUP:
                    self.lbutton = False
                    user32.PostMessageW(self.target, WM_LBUTTONUP, 0, _lparam_xy(c.x, c.y))
                elif w == WM_MOUSEWHEEL:
                    # Сообщение колеса Chrome перенаправляет окну под курсором (а это значки рабочего
                    # стола), поэтому колесо идёт странице обоев отдельным каналом (/events), и
                    # интерфейс сам прокручивает элемент под курсором.
                    delta = ctypes.c_short((m.mouseData >> 16) & 0xFFFF).value
                    EVENTS.push({"type": "wheel", "x": c.x, "y": c.y, "delta": delta})
            elif w == WM_LBUTTONUP:
                self.lbutton = False
        return user32.CallNextHookEx(None, n, w, l)

    def _key(self, n, w, l):
        if n != 0 or not self.target:
            return user32.CallNextHookEx(None, n, w, l)
        k = ctypes.cast(l, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        down = w in (WM_KEYDOWN, 0x104)
        if k.vk in (0xA0, 0xA1, 0x10):  # Shift — запоминаем сами, своему потоку Edge его не видно
            self.shift = down
            return user32.CallNextHookEx(None, n, w, l)
        if not self.focused or not self._desktop_active():
            self.focused = self.focused and self._desktop_active()
            return user32.CallNextHookEx(None, n, w, l)
        # сочетания с Win / Alt / Ctrl и F-клавиши — системе, не обоям
        if any(user32.GetKeyState(v) & 0x8000 for v in (0x5B, 0x5C, 0x12, 0x11)) or 0x70 <= k.vk <= 0x87:
            return user32.CallNextHookEx(None, n, w, l)
        lp = 1 | (k.scan & 0xFF) << 16 | (0x1000000 if k.flags & 1 else 0)
        if k.vk in NAV_KEYS:
            user32.PostMessageW(self.target, WM_KEYDOWN if down else WM_KEYUP, k.vk,
                                lp if down else lp | 0xC0000000)
            if down and k.vk in (0x08, 0x0D, 0x20):  # Backspace, Enter, пробел — ещё и символ
                user32.PostMessageW(self.target, WM_CHAR, {0x08: 8, 0x0D: 13, 0x20: 32}[k.vk], lp)
            return 1
        if down:  # символ в раскладке, активной на рабочем столе (русские буквы тоже)
            state = (ctypes.c_ubyte * 256)()
            if self.shift:
                state[0x10] = 0x80
            if user32.GetKeyState(0x14) & 1:
                state[0x14] = 0x01
            buf = ctypes.create_unicode_buffer(8)
            tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            got = user32.ToUnicodeEx(k.vk, k.scan, ctypes.cast(state, ctypes.c_char_p), buf, 8, 0x4,
                                     user32.GetKeyboardLayout(tid))
            for ch in buf.value[:max(0, got)]:
                user32.PostMessageW(self.target, WM_CHAR, ord(ch), lp)
            if got <= 0:
                return user32.CallNextHookEx(None, n, w, l)
        return 1

    def _loop(self):
        self._mp, self._kp = HOOKPROC(self._mouse), HOOKPROC(self._key)  # держим ссылки, иначе их соберёт GC
        user32.SetWindowsHookExW(14, self._mp, None, 0)   # WH_MOUSE_LL
        user32.SetWindowsHookExW(13, self._kp, None, 0)   # WH_KEYBOARD_LL
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass


def refresh_wallpaper() -> None:
    """Перерисовать обычные обои после выключения живых."""
    buf = ctypes.create_unicode_buffer(520)
    ctypes.windll.user32.SystemParametersInfoW(0x73, 520, buf, 0)        # SPI_GETDESKWALLPAPER
    ctypes.windll.user32.SystemParametersInfoW(0x14, 0, buf, 0x2)        # SPI_SETDESKWALLPAPER + SENDCHANGE


def kill_tree(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


def stop_running() -> None:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/quit", timeout=3).read()
    except Exception:
        pass


def main() -> None:
    if "--stop" in sys.argv:
        set_enabled(False)
        stop_running()
        return
    if "--auto" in sys.argv and not enabled():
        return
    set_enabled(True)

    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return  # обои уже работают

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(PAGE_DIR)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    proc, edge_hwnd, host, fails, last_rect = None, None, None, 0, None
    bridge = InputBridge()
    try:
        while not STOP.is_set():
            if proc is None or proc.poll() is not None or not (edge_hwnd and user32.IsWindow(edge_hwnd)):
                if fails > 5:
                    print("[wallpaper] Edge не запускается — выхожу")
                    break
                kill_tree(proc)
                proc, edge_hwnd = launch_edge(primary_rect()), None
                if not proc:
                    print("[wallpaper] Microsoft Edge не найден")
                    break
                for _ in range(100):  # ждём окно до 20 с
                    edge_hwnd = find_edge_window()
                    if edge_hwnd:
                        break
                    time.sleep(0.2)
                if not edge_hwnd:
                    fails += 1
                    continue
                host = None
            # проводник мог перезапуститься или поменяться разрешение — тогда встраиваемся заново
            rect = primary_rect()
            if (not host or not user32.IsWindow(host) or user32.GetParent(edge_hwnd) != host
                    or rect != last_rect):
                host = wallpaper_host()
                if host:
                    attach(edge_hwnd, host)
                    last_rect, fails = rect, 0
            # Окно ввода Edge (Chrome_RenderWidgetHostHWND) появляется не сразу после окна Edge и может
            # пересоздаваться — перепроверяем его каждый круг. Иначе клики уходили бы в главное окно,
            # которое их игнорирует (вкладки на обоях «не нажимались»).
            bridge.set_edge(edge_hwnd)
            STOP.wait(3)
    finally:
        kill_tree(proc)
        srv.shutdown()
        refresh_wallpaper()


if __name__ == "__main__":
    main()
