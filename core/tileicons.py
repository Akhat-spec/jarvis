"""Картинки для плиток: обложки игр Steam и значки программ/папок из Windows (256×256).

Файлы кладутся в hud/tile_img/, плитка получает поля img (путь для HUD) и kind ("cover" | "icon").
"""
import ctypes
import hashlib
import re
import shutil
import winreg
from ctypes import wintypes
from pathlib import Path

from .config import HUD_DIR

OUT = HUD_DIR / "tile_img"


# ── Steam ──
def _steam_root() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return None


def _steam_appid(target: str) -> str | None:
    p = Path(target)
    if p.suffix.lower() != ".url" or not p.exists():
        return None
    m = re.search(r"steam://rungameid/(\d+)", p.read_text(encoding="utf-8", errors="ignore"))
    return m.group(1) if m else None


def _steam_cover(appid: str) -> Path | None:
    root = _steam_root()
    if not root:
        return None
    d = root / "appcache" / "librarycache" / appid
    if not d.exists():
        return None
    for name in ("header.jpg", "library_header.jpg", "library_600x900.jpg", "capsule_616x353.jpg"):
        hits = sorted(d.rglob(name), key=lambda f: -f.stat().st_size)
        if hits:
            return hits[0]
    return None


# ── значок из оболочки Windows (как в Проводнике) ──
import threading

_com = threading.local()  # COM нужно инициализировать в КАЖДОМ потоке (картинки делаются в разных потоках)


def _shell_icon(path: str, size: int = 256):
    """IShellItemImageFactory::GetImage → PIL.Image (RGBA) или None."""
    import comtypes
    from comtypes import GUID, HRESULT, IUnknown, COMMETHOD
    from PIL import Image

    if not getattr(_com, "ready", False):
        comtypes.CoInitialize()
        _com.ready = True

    class IShellItemImageFactory(IUnknown):
        _iid_ = GUID("{bcc18b79-ba16-442f-80c4-8a59c30c463b}")
        _methods_ = [COMMETHOD([], HRESULT, "GetImage", (["in"], wintypes.SIZE, "size"),
                               (["in"], ctypes.c_int, "flags"), (["out"], ctypes.POINTER(wintypes.HBITMAP), "phbm"))]

    shell32 = ctypes.windll.shell32
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ptr = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(path, None, ctypes.byref(IShellItemImageFactory._iid_),
                                           ctypes.byref(ptr)) != 0 or not ptr.value:
        return None
    fac = ctypes.cast(ptr, ctypes.POINTER(IShellItemImageFactory))
    hbm = fac.GetImage(wintypes.SIZE(size, size), 0x1 | 0x4)  # SIIGBF_BIGGERSIZEOK | SIIGBF_ICONONLY
    gdi = ctypes.windll.gdi32

    class BITMAP(ctypes.Structure):
        _fields_ = [("t", ctypes.c_long), ("w", ctypes.c_long), ("h", ctypes.c_long), ("wb", ctypes.c_long),
                    ("pl", wintypes.WORD), ("bpp", wintypes.WORD), ("bits", ctypes.c_void_p)]

    bm = BITMAP()
    gdi.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
    w, h = bm.w, abs(bm.h)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi.GetBitmapBits.argtypes = [wintypes.HBITMAP, ctypes.c_long, ctypes.c_void_p]
    gdi.GetBitmapBits(hbm, len(buf), buf)
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteObject(hbm)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    # значок обычно меньше 256 и по центру — обрезаем пустые поля
    box = img.getchannel("A").getbbox()
    return img.crop(box) if box else img


def _builtin_path(target: str) -> str:
    """notepad / calc / explorer → путь к exe, чтобы взять его значок."""
    import shutil as sh
    return sh.which(target) or target


def add_images(tiles: dict) -> bool:
    """Дописывает img/kind плиткам без картинки. Возвращает True, если что-то изменилось."""
    OUT.mkdir(exist_ok=True)
    changed = False
    for items in tiles.values():
        for it in items:
            if it.get("img") and (HUD_DIR / it["img"]).exists():
                continue
            target = it.get("target", "")
            try:
                appid = _steam_appid(target)
                cover = _steam_cover(appid) if appid else None
                if cover:
                    name = f"steam_{appid}{cover.suffix}"
                    shutil.copyfile(cover, OUT / name)
                    it["img"], it["kind"] = f"tile_img/{name}", "cover"
                    changed = True
                    continue
                src = target if Path(target).exists() else _builtin_path(target)
                if src.lower().endswith(".url"):  # у интернет-ярлыка Windows рисует значок браузера —
                    m = re.search(r"^IconFile=(.+)$", Path(src).read_text(encoding="utf-8", errors="ignore"), re.M)
                    if m and Path(m.group(1).strip()).exists():  # берём настоящий из IconFile
                        src = m.group(1).strip()
                if not Path(src).exists():
                    continue
                img = _shell_icon(src)
                if img is None:
                    continue
                name = hashlib.sha1(src.lower().encode("utf-8")).hexdigest()[:16] + ".png"
                img.save(OUT / name)
                it["img"], it["kind"] = f"tile_img/{name}", "icon"
                changed = True
            except Exception as e:
                print(f"[tiles] нет картинки для «{it.get('name')}»: {type(e).__name__}: {e}")
    return changed
