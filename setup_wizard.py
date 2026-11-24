"""Мастер установки J.A.R.V.I.S.

  pythonw setup_wizard.py              — установка (из распакованного установщика)
  pythonw setup_wizard.py --keys       — изменить ключи API уже установленного Джарвиса
  pythonw setup_wizard.py --uninstall  — удалить Джарвиса

Ключи сохраняются только на этом компьютере, в файле .env в папке программы.
"""
import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
import winreg
from pathlib import Path
from tkinter import filedialog, messagebox

APP = "J.A.R.V.I.S."
VERSION = "2.0"
HERE = Path(__file__).resolve().parent
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "JARVIS"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\JARVIS"
NO_WINDOW = 0x08000000
# что не копируем при установке/обновлении (личные данные и служебные папки)
SKIP = {".env", ".ws_token", "google_token.json", "google_client_secret.json", "memory.json",
        "hud_settings.json", "wallpaper.json", "logs", ".hud_profile", ".wall_profile", "tile_img",
        "__pycache__", ".venv", "tests", "we_backup", "lhm_autostart.log", "jarvis.py", "Jarvis.bat",
        "autostart.ps1", "Включить температуру CPU.bat"}

BG, PANEL, FIELD, LINE = "#0a1017", "#101a24", "#0d151e", "#1f2d3a"
ACCENT, ACCENT_DK, TEXT, MUTED, ERR, OK = "#2ee6c8", "#1fb39b", "#dbe7ef", "#7f909d", "#ff6b6b", "#5be08f"
FONT = "Segoe UI"


# ════════════ вспомогательное ════════════
def ps(script: str, wait=True):
    """PowerShell без окна; скрипт передаём в UTF-16 Base64 — пути с кириллицей и кавычками не ломаются."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
    if wait:
        return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW,
                              encoding="utf-8", errors="replace")
    return subprocess.Popen(args, creationflags=NO_WINDOW)


def q(s) -> str:
    """Строка для PowerShell в одинарных кавычках."""
    return "'" + str(s).replace("'", "''") + "'"


def find_we() -> Path | None:
    """Папка Wallpaper Engine во всех библиотеках Steam (или None)."""
    libs = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            libs.insert(0, Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        vdf = (libs[0] / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="ignore")
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    for lib in libs:
        we = lib / "steamapps" / "common" / "wallpaper_engine"
        if (we / "wallpaper64.exe").exists():
            return we
    return None


def read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"')
    except OSError:
        pass
    return out


def write_env(path: Path, values: dict) -> None:
    """Обновляет ключи в .env, сохраняя остальные строки и комментарии."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else [
        "# Настройки J.A.R.V.I.S. Изменить ключи: Пуск → J.A.R.V.I.S. → «Ключи API J.A.R.V.I.S.»"]
    left = dict(values)
    for i, line in enumerate(lines):
        m = re.match(r"\s*#?\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in left:
            lines[i] = f"{m.group(1)}={left.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in left.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_anthropic(key: str) -> tuple[bool | None, str]:
    """True — ключ рабочий, False — неверный, None — проверить не вышло (нет сети и т. п.)."""
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=12):
            return True, "ключ работает"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "Anthropic не принял ключ — проверьте, что он скопирован целиком."
        return None, f"сервер ответил {e.code}"
    except OSError as e:
        return None, f"нет связи с сервером ({e.__class__.__name__})"


def check_elevenlabs(key: str) -> tuple[bool | None, str]:
    req = urllib.request.Request("https://api.elevenlabs.io/v1/models", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=12):
            return True, "ключ работает"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "ElevenLabs не принял ключ."
        return None, f"сервер ответил {e.code}"
    except OSError as e:
        return None, f"нет связи с сервером ({e.__class__.__name__})"


def stop_jarvis(folder: Path) -> None:
    """Останавливает запущенного из этой папки Джарвиса (перед обновлением или удалением)."""
    ps(f"""$d = {q(str(folder).lower())}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object {{
  $_.CommandLine -and $_.CommandLine.ToLower().Contains($d) -and $_.CommandLine -match '(run|wallpaper)\\.py'
}} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}""")


def base_python() -> Path:
    """python.exe (не pythonw и не из виртуального окружения) — для создания .venv."""
    exe = Path(getattr(sys, "_base_executable", "") or sys.executable)
    py = exe.with_name("python.exe")
    return py if py.exists() else exe


def fix_clipboard_keys(root: tk.Misc) -> None:
    """Ctrl+V/C/X/A работают и в русской раскладке (tkinter по умолчанию понимает только латиницу)."""
    if getattr(fix_clipboard_keys, "done", False):
        return
    fix_clipboard_keys.done = True
    events = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>"}

    def on_key(e):
        if e.keycode in events and e.keysym.lower() not in ("v", "c", "x"):
            e.widget.event_generate(events[e.keycode])
            return "break"
        if e.keycode == 65 and isinstance(e.widget, tk.Entry):
            e.widget.select_range(0, "end")
            return "break"

    root.bind_class("Entry", "<Control-KeyPress>", on_key, add="+")


# ════════════ оформление ════════════
def btn(parent, text, cmd, primary=False, width=None):
    b = tk.Button(parent, text=text, command=cmd, font=(FONT, 10, "bold" if primary else "normal"),
                  bg=ACCENT if primary else PANEL, fg="#04221d" if primary else TEXT,
                  activebackground=ACCENT_DK if primary else LINE, activeforeground="#04221d" if primary else TEXT,
                  relief="flat", bd=0, padx=18, pady=8, cursor="hand2",
                  highlightthickness=1, highlightbackground=ACCENT if primary else LINE)
    if width:
        b.configure(width=width)
    return b


def link(parent, text, url):
    lb = tk.Label(parent, text=text, font=(FONT, 9, "underline"), fg=ACCENT, bg=parent["bg"], cursor="hand2")
    lb.bind("<Button-1>", lambda e: webbrowser.open(url))
    return lb


def entry(parent, var, secret=False, width=60):
    e = tk.Entry(parent, textvariable=var, font=("Consolas", 10), width=width, show="•" if secret else "",
                 bg=FIELD, fg=TEXT, insertbackground=ACCENT, relief="flat",
                 highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
    menu = tk.Menu(e, tearoff=0, bg=PANEL, fg=TEXT, activebackground=LINE, activeforeground=TEXT)
    menu.add_command(label="Вставить", command=lambda: e.event_generate("<<Paste>>"))
    menu.add_command(label="Очистить", command=lambda: var.set(""))
    e.bind("<Button-3>", lambda ev: menu.tk_popup(ev.x_root, ev.y_root))
    return e


def check(parent, text, var, state="normal"):
    return tk.Checkbutton(parent, text=text, variable=var, state=state, font=(FONT, 10), bg=parent["bg"],
                          fg=TEXT, selectcolor=FIELD, activebackground=parent["bg"], activeforeground=TEXT,
                          disabledforeground=MUTED, anchor="w", justify="left", wraplength=580, cursor="hand2")


def radio(parent, text, var, value, state="normal"):
    return tk.Radiobutton(parent, text=text, variable=var, value=value, state=state, font=(FONT, 10),
                          bg=parent["bg"], fg=TEXT, selectcolor=FIELD, activebackground=parent["bg"],
                          activeforeground=TEXT, disabledforeground=MUTED, anchor="w", cursor="hand2")


def header(parent, title, sub):
    top = tk.Frame(parent, bg=BG)
    top.pack(fill="x", padx=28, pady=(22, 6))
    tk.Label(top, text="●", font=(FONT, 13), fg=ACCENT, bg=BG).pack(side="left", padx=(0, 8))
    tk.Label(top, text=title, font=(FONT, 16, "bold"), fg=TEXT, bg=BG).pack(side="left")
    if sub:
        tk.Label(parent, text=sub, font=(FONT, 10), fg=MUTED, bg=BG, justify="left",
                 wraplength=600).pack(anchor="w", padx=28, pady=(0, 10))


def set_icon(win):
    for ico in (HERE / "jarvis.ico",):
        if ico.exists():
            try:
                win.iconbitmap(str(ico))
            except tk.TclError:
                pass


def center(win, w=None, h=None):
    """Размер по содержимому (не меньше w×h) и по центру экрана."""
    win.geometry("")
    win.update_idletasks()
    w = max(w or 0, win.winfo_reqwidth())
    h = max(h or 0, win.winfo_reqheight())
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"{w}x{h}+{x}+{y}")


# ════════════ окно «Ключи API» ════════════
class KeysWindow:
    """Отдельное окно для ключей. on_done(values) вызывается после «Сохранить»."""

    def __init__(self, master, env: dict, on_done, standalone=False, on_cancel=None):
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.win = tk.Toplevel(master) if master else tk.Tk()
        w = self.win
        w.title(f"{APP} — ключи API")
        w.configure(bg=BG)
        w.resizable(False, False)
        set_icon(w)
        if master and not standalone:
            w.transient(master)
            w.grab_set()
        fix_clipboard_keys(w)
        header(w, "Ключи API", "Ключи хранятся только на этом компьютере — в файле .env в папке Джарвиса. "
                               "Их можно поменять позже: Пуск → J.A.R.V.I.S. → «Ключи API J.A.R.V.I.S.».")

        box = tk.Frame(w, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill="x", padx=28, pady=4)
        self.v_ant = tk.StringVar(value=env.get("ANTHROPIC_API_KEY", "").replace("sk-ant-...", ""))
        self.v_el = tk.StringVar(value=env.get("ELEVENLABS_API_KEY", ""))
        self.v_title = tk.StringVar(value=env.get("USER_TITLE", "сэр"))
        self.v_google = tk.StringVar(value="")
        self.google_path: Path | None = None

        def row(r, label, hint, var, secret, url_text=None, url=None):
            tk.Label(box, text=label, font=(FONT, 10, "bold"), fg=TEXT, bg=PANEL).grid(
                row=r, column=0, sticky="w", padx=16, pady=(14, 2))
            if url:
                link(box, url_text, url).grid(row=r, column=1, sticky="e", padx=16, pady=(14, 2))
            e = entry(box, var, secret)
            e.grid(row=r + 1, column=0, columnspan=2, sticky="we", padx=16, ipady=5)
            tk.Label(box, text=hint, font=(FONT, 9), fg=MUTED, bg=PANEL, justify="left", wraplength=560).grid(
                row=r + 2, column=0, columnspan=2, sticky="w", padx=16, pady=(3, 0))
            return e

        self.e_ant = row(0, "Anthropic API — мозг Джарвиса (обязательно)",
                         "Ключ начинается с sk-ant-. Создаётся в консоли Anthropic → API Keys.",
                         self.v_ant, True, "Получить ключ ↗", "https://console.anthropic.com/settings/keys")
        row(3, "ElevenLabs API — голос как в фильме (необязательно)",
            "Без ключа Джарвис говорит бесплатным голосом Microsoft (Дмитрий).",
            self.v_el, True, "Получить ключ ↗", "https://elevenlabs.io/app/settings/api-keys")
        row(6, "Как Джарвис обращается к вам", "Например: сэр, мэм, босс или ваше имя.", self.v_title, False)

        tk.Label(box, text="Google: почта, календарь, карты (необязательно)", font=(FONT, 10, "bold"),
                 fg=TEXT, bg=PANEL).grid(row=9, column=0, sticky="w", padx=16, pady=(14, 2))
        link(box, "Инструкция ↗", "https://console.cloud.google.com/apis/credentials").grid(
            row=9, column=1, sticky="e", padx=16, pady=(14, 2))
        gl = tk.Frame(box, bg=PANEL)
        gl.grid(row=10, column=0, columnspan=2, sticky="we", padx=16)
        btn(gl, "Выбрать client_secret.json…", self.pick_google).pack(side="left")
        tk.Label(gl, textvariable=self.v_google, font=(FONT, 9), fg=MUTED, bg=PANEL).pack(side="left", padx=10)
        tk.Label(box, text="Файл OAuth-клиента Google («Desktop app»). Можно пропустить и подключить позже "
                           "в настройках интерфейса.", font=(FONT, 9), fg=MUTED, bg=PANEL, justify="left",
                 wraplength=560).grid(row=11, column=0, columnspan=2, sticky="w", padx=16, pady=(3, 16))
        box.columnconfigure(0, weight=1)

        self.v_status = tk.StringVar(value="")
        self.status = tk.Label(w, textvariable=self.v_status, font=(FONT, 9), fg=MUTED, bg=BG,
                               wraplength=600, justify="left")
        self.status.pack(anchor="w", padx=28, pady=(8, 0))
        bar = tk.Frame(w, bg=BG)
        bar.pack(fill="x", padx=28, pady=(8, 20))
        self.b_save = btn(bar, "Проверить и сохранить", self.save, primary=True)
        self.b_save.pack(side="right")
        btn(bar, "Отмена", self.cancel).pack(side="right", padx=8)
        w.protocol("WM_DELETE_WINDOW", self.cancel)
        w.bind("<Return>", lambda e: self.save())
        center(w, 680)
        self.e_ant.focus_set()

    def cancel(self):
        (self.on_cancel or self.win.destroy)()

    def pick_google(self):
        f = filedialog.askopenfilename(parent=self.win, title="Файл client_secret от Google",
                                       filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")])
        if not f:
            return
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
            assert "installed" in data or "web" in data
        except Exception:
            messagebox.showerror(APP, "Это не файл OAuth-клиента Google (client_secret*.json).", parent=self.win)
            return
        self.google_path = Path(f)
        self.v_google.set(Path(f).name)

    def say(self, text, color=MUTED):
        self.v_status.set(text)
        self.status.configure(fg=color)

    def save(self):
        ant = re.sub(r"\s+", "", self.v_ant.get())
        el = re.sub(r"\s+", "", self.v_el.get())
        title = self.v_title.get().strip() or "сэр"
        if not ant:
            self.say("Вставьте ключ Anthropic — без него Джарвис не сможет отвечать.", ERR)
            return
        if not ant.startswith("sk-ant-"):
            self.say("Ключ Anthropic должен начинаться с sk-ant-.", ERR)
            return
        self.b_save.configure(state="disabled")
        self.say("Проверяю ключи…")

        def work():
            res_a = check_anthropic(ant)
            res_e = check_elevenlabs(el) if el else (True, "")
            self.win.after(0, lambda: self._checked(ant, el, title, res_a, res_e))

        threading.Thread(target=work, daemon=True).start()

    def _checked(self, ant, el, title, res_a, res_e):
        self.b_save.configure(state="normal")
        if res_a[0] is False:
            self.say(res_a[1], ERR)
            return
        if res_e[0] is False:
            self.say(res_e[1] + " Исправьте или очистите поле — тогда будет бесплатный голос.", ERR)
            return
        if res_a[0] is None or res_e[0] is None:
            why = res_a[1] if res_a[0] is None else res_e[1]
            if not messagebox.askyesno(APP, f"Не удалось проверить ключ: {why}.\nСохранить как есть?",
                                       parent=self.win):
                self.say("")
                return
        values = {"ANTHROPIC_API_KEY": ant, "ELEVENLABS_API_KEY": el, "USER_TITLE": title}
        self.win.grab_release()
        self.win.withdraw()
        self.on_done(values, self.google_path)
        try:
            self.win.destroy()
        except tk.TclError:
            pass


# ════════════ установка ════════════
class Installer:
    def __init__(self):
        self.root = tk.Tk()
        r = self.root
        r.title(f"Установка {APP}")
        r.configure(bg=BG)
        r.resizable(False, False)
        set_icon(r)
        fix_clipboard_keys(r)
        self.we = find_we()
        self.v_dir = tk.StringVar(value=str(DEFAULT_DIR))
        self.v_mode = tk.StringVar(value="we" if self.we else "live")
        self.v_desk = tk.BooleanVar(value=True)
        self.v_auto = tk.BooleanVar(value=True)
        self.v_temp = tk.BooleanVar(value=False)
        self.keys = None
        self.google = None
        self.page = tk.Frame(r, bg=BG)
        self.page.pack(fill="both", expand=True)
        self.page_options()

    def clear(self):
        for c in self.page.winfo_children():
            c.destroy()

    # ── страница 1: параметры ──
    def page_options(self):
        self.clear()
        p = self.page
        header(p, f"Установка {APP}", "Голосовой помощник для Windows: открывает программы и файлы, меняет "
               "громкость и яркость, ищет в интернете, ведёт заметки, работает с почтой и календарём. "
               "Интерфейс живёт прямо на рабочем столе.")
        box = tk.Frame(p, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill="x", padx=28, pady=4)
        tk.Label(box, text="Папка установки", font=(FONT, 10, "bold"), fg=TEXT, bg=PANEL).pack(
            anchor="w", padx=16, pady=(14, 4))
        row = tk.Frame(box, bg=PANEL)
        row.pack(fill="x", padx=16)
        entry(row, self.v_dir, width=50).pack(side="left", fill="x", expand=True, ipady=5)
        btn(row, "Обзор…", self.pick_dir).pack(side="left", padx=(8, 0))

        tk.Label(box, text="Где показывать интерфейс", font=(FONT, 10, "bold"), fg=TEXT, bg=PANEL).pack(
            anchor="w", padx=16, pady=(16, 2))
        radio(box, "Обои Wallpaper Engine" + ("" if self.we else " — не найден (нужен Wallpaper Engine из Steam)"),
              self.v_mode, "we", "normal" if self.we else "disabled").pack(anchor="w", padx=12)
        radio(box, "Живые обои без Wallpaper Engine (через Microsoft Edge)", self.v_mode, "live").pack(
            anchor="w", padx=12)

        tk.Label(box, text="Дополнительно", font=(FONT, 10, "bold"), fg=TEXT, bg=PANEL).pack(
            anchor="w", padx=16, pady=(16, 2))
        check(box, "Ярлык на рабочем столе", self.v_desk).pack(anchor="w", padx=12)
        check(box, "Запускать вместе с Windows", self.v_auto).pack(anchor="w", padx=12)
        check(box, "Показывать температуру процессора (LibreHardwareMonitor, Windows спросит права "
                   "администратора)", self.v_temp).pack(anchor="w", padx=12, pady=(0, 14))

        tk.Label(p, text="Нужен интернет: установщик скачает библиотеки Python (≈150 МБ).",
                 font=(FONT, 9), fg=MUTED, bg=BG).pack(anchor="w", padx=28, pady=(10, 0))
        bar = tk.Frame(p, bg=BG)
        bar.pack(side="bottom", fill="x", padx=28, pady=20)
        btn(bar, "Далее →", self.open_keys, primary=True).pack(side="right")
        btn(bar, "Отмена", self.root.destroy).pack(side="right", padx=8)
        center(self.root, 680)

    def pick_dir(self):
        d = filedialog.askdirectory(parent=self.root, title="Папка для J.A.R.V.I.S.")
        if d:
            self.v_dir.set(str(Path(d) / "JARVIS") if Path(d).name.lower() != "jarvis" else d)

    def target(self) -> Path:
        return Path(self.v_dir.get().strip().strip('"'))

    def open_keys(self):
        t = self.target()
        if not t.is_absolute():
            messagebox.showerror(APP, "Укажите полный путь к папке установки.", parent=self.root)
            return
        try:
            t.mkdir(parents=True, exist_ok=True)
            (t / ".write_test").write_text("1")
            (t / ".write_test").unlink()
        except OSError:
            messagebox.showerror(APP, f"Нет доступа к папке:\n{t}\n\nВыберите другую — например, "
                                      f"{DEFAULT_DIR}", parent=self.root)
            return
        KeysWindow(self.root, read_env(t / ".env"), self.keys_saved)

    def keys_saved(self, values, google):
        self.keys, self.google = values, google
        self.page_progress()
        threading.Thread(target=self.install, daemon=True).start()

    # ── страница 2: ход установки ──
    def page_progress(self):
        self.clear()
        p = self.page
        header(p, "Устанавливаю…", "Это займёт несколько минут. Окно можно не трогать.")
        self.steps = ["Копирую файлы", "Сохраняю ключи", "Создаю окружение Python",
                      "Скачиваю библиотеки", "Создаю ярлыки", "Датчики температуры", "Запускаю Джарвиса"]
        self.step_labels = []
        box = tk.Frame(p, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill="x", padx=28, pady=4)
        for s in self.steps:
            lb = tk.Label(box, text="○  " + s, font=(FONT, 10), fg=MUTED, bg=PANEL, anchor="w")
            lb.pack(fill="x", padx=16, pady=3)
            self.step_labels.append(lb)
        tk.Frame(box, bg=PANEL, height=8).pack()
        self.log = tk.Text(p, height=9, font=("Consolas", 8), bg=FIELD, fg=MUTED, relief="flat",
                           highlightthickness=1, highlightbackground=LINE, wrap="word")
        self.log.pack(fill="both", expand=True, padx=28, pady=(10, 0))
        self.bar = tk.Frame(p, bg=BG, height=56)
        self.bar.pack(side="bottom", fill="x", padx=28, pady=16)
        center(self.root, 680)

    def ui(self, fn, *a):
        self.root.after(0, fn, *a)

    def mark(self, i, state):
        icon, color = {"run": ("◌", ACCENT), "ok": ("✓", OK), "err": ("✕", ERR), "skip": ("–", MUTED)}[state]
        self.ui(lambda: self.step_labels[i].configure(text=f"{icon}  {self.steps[i]}", fg=color))

    def write_log(self, text):
        def f():
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
        self.ui(f)

    def run_logged(self, args, cwd=None) -> int:
        proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        for line in proc.stdout:
            if line.strip():
                self.write_log(line)
        return proc.wait()

    def install(self):
        t = self.target()
        step = 0
        try:
            # 1. файлы
            self.mark(step, "run")
            t.mkdir(parents=True, exist_ok=True)
            stop_jarvis(t)
            if t.resolve() != HERE.resolve():
                for item in HERE.iterdir():
                    if item.name in SKIP:
                        continue
                    dst = t / item.name
                    if item.is_dir():
                        shutil.copytree(item, dst, dirs_exist_ok=True,
                                        ignore=lambda d, names: [n for n in names if n in SKIP])
                    else:
                        shutil.copy2(item, dst)
            self.write_log(f"Файлы скопированы в {t}")
            self.mark(step, "ok"); step += 1

            # 2. ключи
            self.mark(step, "run")
            env = dict(self.keys)
            env["JARVIS_WE"] = "1" if self.v_mode.get() == "we" else "0"
            write_env(t / ".env", env)
            if self.google:
                shutil.copy2(self.google, t / "google_client_secret.json")
            (t / "wallpaper.json").write_text(json.dumps({"enabled": self.v_mode.get() != "we"}), encoding="utf-8")
            self.write_log("Ключи сохранены в .env")
            self.mark(step, "ok"); step += 1

            # 3. окружение
            self.mark(step, "run")
            venv = t / ".venv"
            vpy = venv / "Scripts" / "python.exe"
            if not vpy.exists():
                if self.run_logged([str(base_python()), "-m", "venv", str(venv)]) != 0:
                    raise RuntimeError("не удалось создать окружение Python")
            self.mark(step, "ok"); step += 1

            # 4. библиотеки
            self.mark(step, "run")
            code = self.run_logged([str(vpy), "-m", "pip", "install", "--disable-pip-version-check",
                                    "--no-input", "-r", str(t / "requirements.txt")], cwd=str(t))
            if code != 0:
                raise RuntimeError("pip не смог установить библиотеки — проверьте интернет и нажмите «Повторить»")
            self.mark(step, "ok"); step += 1

            # 5. ярлыки и запись в «Приложения»
            self.mark(step, "run")
            self.make_shortcuts(t)
            self.register(t)
            self.mark(step, "ok"); step += 1

            # 6. обои и датчики
            if self.v_temp.get():
                self.mark(step, "run")
                self.write_log("Включаю датчики температуры — подтвердите запрос Windows…")
                ps(f"& {q(t / 'tools' / 'lhm_autostart.ps1')}")
                self.mark(step, "ok")
            else:
                self.mark(step, "skip")
            step += 1

            # 7. запуск
            self.mark(step, "run")
            vpyw = venv / "Scripts" / "pythonw.exe"
            subprocess.Popen([str(vpyw), str(t / "run.py")], cwd=str(t),
                             creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            if self.v_mode.get() == "we" and self.we:
                self.apply_we()
            self.mark(step, "ok")
            self.ui(self.page_done)
        except Exception as e:
            self.mark(step, "err")
            self.write_log(f"Ошибка: {e}")
            self.ui(self.show_retry)

    def make_shortcuts(self, t: Path):
        pyw = t / ".venv" / "Scripts" / "pythonw.exe"
        ico = t / "jarvis.ico"
        wiz = t / "setup_wizard.py"
        r = ps(f"""$ErrorActionPreference = 'Stop'
$sh = New-Object -ComObject WScript.Shell
function New-Link($path, $args_, $desc) {{
  $l = $sh.CreateShortcut($path); $l.TargetPath = {q(pyw)}; $l.Arguments = $args_
  $l.WorkingDirectory = {q(t)}; $l.IconLocation = {q(ico)}; $l.Description = $desc; $l.Save()
}}
$run = '"' + {q(t / 'run.py')} + '"'
$wiz = '"' + {q(wiz)} + '"'
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'J.A.R.V.I.S'
New-Item -ItemType Directory -Force $menu | Out-Null
New-Link (Join-Path $menu 'J.A.R.V.I.S..lnk') $run 'Голосовой помощник J.A.R.V.I.S.'
New-Link (Join-Path $menu 'Окно J.A.R.V.I.S..lnk') ($run + ' --window') 'Открыть интерфейс в окне'
New-Link (Join-Path $menu 'Ключи API J.A.R.V.I.S..lnk') ($wiz + ' --keys') 'Изменить ключи API'
New-Link (Join-Path $menu 'Удалить J.A.R.V.I.S..lnk') ($wiz + ' --uninstall') 'Удалить J.A.R.V.I.S.'
$desk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'J.A.R.V.I.S..lnk'
$start = Join-Path ([Environment]::GetFolderPath('Startup')) 'J.A.R.V.I.S..lnk'
if ({'$true' if self.v_desk.get() else '$false'}) {{ New-Link $desk $run 'Голосовой помощник J.A.R.V.I.S.' }} elseif (Test-Path $desk) {{ Remove-Item $desk }}
if ({'$true' if self.v_auto.get() else '$false'}) {{ New-Link $start $run 'Автозапуск J.A.R.V.I.S.' }} elseif (Test-Path $start) {{ Remove-Item $start }}
""")
        if r.returncode != 0:
            raise RuntimeError("ярлыки не созданы: " + (r.stderr or r.stdout).strip()[-300:])
        self.write_log("Ярлыки созданы: меню «Пуск»" + (", рабочий стол" if self.v_desk.get() else "")
                       + (", автозапуск" if self.v_auto.get() else ""))

    def register(self, t: Path):
        pyw = t / ".venv" / "Scripts" / "pythonw.exe"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            for name, val in {"DisplayName": APP, "DisplayVersion": VERSION, "Publisher": "J.A.R.V.I.S.",
                              "InstallLocation": str(t), "DisplayIcon": str(t / "jarvis.ico"),
                              "UninstallString": f'"{pyw}" "{t / "setup_wizard.py"}" --uninstall'}.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)

    def apply_we(self):
        """Ждём, пока Джарвис соберёт проект обоев, и ставим его в Wallpaper Engine."""
        proj = self.we / "projects" / "myprojects" / "jarvis"
        for _ in range(60):
            if (proj / "we_config.js").exists() and (proj / "hud" / "app.js").exists():
                break
            time.sleep(0.5)
        else:
            self.write_log("Проект обоев не появился — выберите «J.A.R.V.I.S.» в Wallpaper Engine вручную.")
            return
        subprocess.Popen([str(self.we / "wallpaper64.exe"), "-control", "openWallpaper",
                          "-file", str(proj / "project.json"), "-monitor", "0"], creationflags=NO_WINDOW)
        self.write_log("Обои J.A.R.V.I.S. включены в Wallpaper Engine")

    def show_retry(self):
        for c in self.bar.winfo_children():
            c.destroy()
        btn(self.bar, "Повторить", self.retry, primary=True).pack(side="right")
        btn(self.bar, "Закрыть", self.root.destroy).pack(side="right", padx=8)
        center(self.root, 680)

    def retry(self):
        self.page_progress()
        threading.Thread(target=self.install, daemon=True).start()

    # ── страница 3: готово ──
    def page_done(self):
        self.clear()
        p = self.page
        header(p, "Готово!", None)
        box = tk.Frame(p, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill="x", padx=28, pady=4)
        where = ("на обоях Wallpaper Engine" if self.v_mode.get() == "we"
                 else "на рабочем столе (сверните окна, чтобы его увидеть)")
        text = (f"Джарвис запущен — интерфейс {where}.\n\n"
                "• Скажите «Джарвис» и команду: «Джарвис, открой браузер», «Джарвис, сделай громкость 30».\n"
                "• Открыть интерфейс в окне: Пуск → J.A.R.V.I.S. → «Окно J.A.R.V.I.S.».\n"
                "• Поменять ключи API: Пуск → J.A.R.V.I.S. → «Ключи API J.A.R.V.I.S.».\n"
                "• Удалить: Параметры → Приложения или Пуск → J.A.R.V.I.S. → «Удалить J.A.R.V.I.S.».\n\n"
                "Если Windows спросит доступ к микрофону — разрешите.")
        tk.Label(box, text=text, font=(FONT, 10), fg=TEXT, bg=PANEL, justify="left", wraplength=590).pack(
            anchor="w", padx=16, pady=16)
        bar = tk.Frame(p, bg=BG)
        bar.pack(side="bottom", fill="x", padx=28, pady=20)
        btn(bar, "Закрыть", self.root.destroy, primary=True).pack(side="right")
        center(self.root, 680)


# ════════════ изменение ключей ════════════
def keys_mode():
    def saved(values, google):
        write_env(HERE / ".env", values)
        if google:
            shutil.copy2(google, HERE / "google_client_secret.json")
        if messagebox.askyesno(APP, "Ключи сохранены. Перезапустить Джарвиса, чтобы они заработали?"):
            stop_jarvis(HERE)
            pyw = Path(sys.executable).with_name("pythonw.exe")
            subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(HERE / "run.py")], cwd=str(HERE),
                             creationflags=0x00000008 | 0x00000200)
        root.destroy()

    root = tk.Tk()
    root.withdraw()
    set_icon(root)
    KeysWindow(root, read_env(HERE / ".env"), saved, standalone=True, on_cancel=root.destroy)
    root.mainloop()


# ════════════ удаление ════════════
def uninstall_mode():
    root = tk.Tk()
    root.withdraw()
    set_icon(root)
    if not messagebox.askyesno(APP, f"Удалить {APP} с этого компьютера?\n\nБудут удалены программа, ярлыки, "
                                    f"его память и настройки:\n{HERE}"):
        return
    stop_jarvis(HERE)
    task = subprocess.run(["schtasks", "/query", "/tn", "LibreHardwareMonitor (Jarvis)"], capture_output=True,
                          creationflags=NO_WINDOW)
    if task.returncode == 0 and (HERE / "tools" / "lhm_autostart.ps1").exists():
        ps(f"& {q(HERE / 'tools' / 'lhm_autostart.ps1')} -Remove")
    ps("""foreach ($p in @(
  (Join-Path ([Environment]::GetFolderPath('Programs')) 'J.A.R.V.I.S'),
  (Join-Path ([Environment]::GetFolderPath('Desktop')) 'J.A.R.V.I.S..lnk'),
  (Join-Path ([Environment]::GetFolderPath('Startup')) 'J.A.R.V.I.S..lnk'))) {
  if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}""")
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    we = find_we()
    if we:
        shutil.rmtree(we / "projects" / "myprojects" / "jarvis", ignore_errors=True)
    messagebox.showinfo(APP, f"{APP} удалён. Обои Wallpaper Engine можно сменить в самом Wallpaper Engine.")
    # папку удаляем после выхода: сейчас из неё запущен этот мастер
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 4 >nul & rmdir /s /q "{HERE}"', creationflags=NO_WINDOW,
                     cwd=os.environ.get("TEMP", "C:\\"))


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if "--keys" in sys.argv:
        keys_mode()
    elif "--uninstall" in sys.argv:
        uninstall_mode()
    else:
        Installer().root.mainloop()


if __name__ == "__main__":
    main()
