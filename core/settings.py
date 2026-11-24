"""Настройки интерфейса и плитки вкладок «Программы / Игры / Файлы» (hud_settings.json)."""
import json
import os
import re
from pathlib import Path

from .config import ROOT

FILE = ROOT / "hud_settings.json"
TABS = ("apps", "games", "files")
_SCRIPT_EXT = (".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".reg", ".scr", ".hta", ".wsf")
_SAFE_NAME = re.compile(r"^[\w .+\-]{1,60}$")

DEFAULTS = {
    "wake": True, "barge": True, "screen": True, "autonomy": "auto",
    "voice": True, "memory": True, "log": True, "hints": False,
    "city": "",
}

START_MENU = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
              Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]

# (название на плитке, подсказка, ключевые слова ярлыка в меню Пуск | встроенная команда)
APP_CANDIDATES = [
    ("VS Code", "редактор кода", ["visual studio code"]),
    ("Chrome", "браузер", ["google chrome"]),
    ("Edge", "браузер", ["microsoft edge"]),
    ("Telegram", "мессенджер", ["telegram"]),
    ("Discord", "голосовой чат", ["discord"]),
    ("Spotify", "музыка", ["spotify"]),
    ("OBS Studio", "запись экрана", ["obs studio"]),
    ("Блокнот", "заметки", "notepad"),
    ("Калькулятор", "вычисления", "calc"),
    ("Проводник", "файлы", "explorer"),
]
LAUNCHERS = [("Steam", "библиотека игр", ["steam"]), ("Epic Games", "лаунчер", ["epic games launcher"]),
             ("Battle.net", "лаунчер", ["battle.net"]), ("Riot Client", "лаунчер", ["riot client"])]


def _shortcuts():
    for d in START_MENU:
        if d.exists():
            yield from (p for p in d.rglob("*") if p.suffix.lower() in (".lnk", ".url"))


def _find(keys):
    """Ярлык программы: сначала точное имя, потом начало имени; .lnk важнее .url
    (иначе «steam» находил «Steam Support Center.url» — ссылку на сайт поддержки)."""
    found = [p for p in _shortcuts() if "uninstall" not in p.stem.lower() and "удал" not in p.stem.lower()]
    for exact in (True, False):
        for ext in (".lnk", ".url"):
            for p in found:
                stem = p.stem.lower()
                if p.suffix.lower() == ext and any(k == stem if exact else stem.startswith(k) for k in keys):
                    return str(p)
    return None


def _known_folder(csidl: int, fallback: Path) -> Path:
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
        return Path(buf.value)
    return fallback


def discover_tiles() -> dict:
    """Плитки по умолчанию — только то, что реально есть на этом компьютере."""
    apps = []
    for name, hint, how in APP_CANDIDATES:
        target = how if isinstance(how, str) else _find(how)
        if target:
            apps.append({"name": name, "hint": hint, "target": target})
    games = []
    for name, hint, keys in LAUNCHERS:
        target = _find(keys)
        if target:
            games.append({"name": name, "hint": hint, "target": target})
    for d in START_MENU:  # игры Steam кладут ярлыки .url в папку «Steam» меню Пуск
        steam = d / "Steam"
        if steam.exists():
            for p in sorted(steam.glob("*.url")):
                if p.stem.lower() != "steam" and not any(g["target"] == str(p) for g in games):
                    games.append({"name": p.stem, "hint": "игра Steam", "target": str(p)})
    home = Path.home()
    folders = [("Рабочий стол", _known_folder(0x10, home / "Desktop")),
               ("Документы", _known_folder(0x05, home / "Documents")),
               ("Загрузки", home / "Downloads"),
               ("Изображения", _known_folder(0x27, home / "Pictures")),
               ("Музыка", _known_folder(0x0D, home / "Music")),
               ("Видео", _known_folder(0x0E, home / "Videos"))]
    files = [{"name": n, "hint": _short(p), "target": str(p)} for n, p in folders if p.exists()]
    return {"apps": apps, "games": games[:24], "files": files}


def _short(p: Path) -> str:
    s = str(p)
    h = str(Path.home())
    return "~" + s[len(h):] if s.lower().startswith(h.lower()) else s


def load() -> dict:
    data = {}
    if FILE.exists():
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    for k, v in DEFAULTS.items():
        data.setdefault(k, v)
    if not isinstance(data.get("tiles"), dict) or any(t not in data["tiles"] for t in TABS):
        data["tiles"] = discover_tiles()
        save(data)
    return data


def save(data: dict) -> None:
    try:
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[!] настройки не сохранились: {e}")


def check_target(target: str) -> str | None:
    """Что можно запускать с плитки: существующий файл/папку/ярлык или имя программы. Скрипты — нет."""
    target = target.strip().strip('"')
    if not target:
        return "Укажите путь или имя программы"
    if target.lower().endswith(_SCRIPT_EXT):
        return "Скрипты (.bat, .ps1 и т. п.) с плиток не запускаются — попросите Джарвиса голосом"
    if Path(target).exists() or _SAFE_NAME.match(target):
        return None
    return "Такого файла или папки нет"


def pick_path(tab: str, kind: str) -> str | None:
    """Обычное окно выбора файла/папки Windows (поверх всех окон). Возвращает путь или None."""
    import tkinter as tk
    from tkinter import filedialog
    home = Path.home()
    start = {"apps": START_MENU[0], "games": START_MENU[1] / "Steam" if (START_MENU[1] / "Steam").exists() else START_MENU[1],
             "files": _known_folder(0x10, home / "Desktop")}.get(tab, home)
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    try:
        if kind == "folder":
            path = filedialog.askdirectory(parent=root, initialdir=str(start), title="Выберите папку для плитки")
        else:
            types = [("Все файлы", "*.*")] if tab == "files" else \
                [("Программы и ярлыки", "*.exe *.lnk *.url"), ("Все файлы", "*.*")]
            path = filedialog.askopenfilename(parent=root, initialdir=str(start), filetypes=types,
                                              title="Выберите программу, ярлык или файл для плитки")
    finally:
        root.destroy()
    return str(Path(path)) if path else None


def tile_from_path(tab: str, path: str) -> dict:
    p = Path(path)
    name = p.name if p.is_dir() else p.stem
    if p.is_dir():
        hint = _short(p)
    elif tab == "files":
        hint = p.suffix.lstrip(".").upper() + " · " + _short(p.parent)
    else:
        hint = "добавлено вами"
    return {"name": name[:40] or p.name, "hint": hint, "target": str(p)}


def launch(target: str) -> None:
    err = check_target(target)
    if err:
        raise ValueError(err)
    target = target.strip().strip('"')
    try:
        os.startfile(target)  # ShellExecute: ярлыки, папки, программы — без командной строки
    except OSError:
        if not _SAFE_NAME.match(target):
            raise
        import subprocess
        subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
