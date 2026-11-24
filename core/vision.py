"""Зрение: снимок экрана или окна → JPEG для Claude + привязка координат.

На снимок по краям наносится лёгкая линейка (деления каждые 100 пикселей снимка), чтобы модель
точнее называла координаты. Клики по снимку переводятся в экранные координаты через ShotMap.
"""
import base64
import io
from dataclasses import dataclass

import mss
from PIL import Image, ImageDraw

MAX_SIDE = 1280      # длинная сторона снимка (баланс между детальностью и стоимостью)
JPEG_QUALITY = 72


@dataclass
class ShotMap:
    left: int
    top: int
    width: int       # размер области на экране, пиксели
    height: int
    scale: float     # пикселей снимка на пиксель экрана

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        sx = self.left + x / self.scale
        sy = self.top + y / self.scale
        return int(round(sx)), int(round(sy))

    def describe(self) -> str:
        w, h = int(self.width * self.scale), int(self.height * self.scale)
        return (f"Снимок {w}×{h} px. Область экрана: левый верхний угол ({self.left}, {self.top}), "
                f"размер {self.width}×{self.height}. Для клика по снимку передавай координаты снимка "
                f"и space=\"screenshot\" — система сама переведёт их в экранные.")


def _ruler(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    w, h = img.size
    for x in range(100, w, 100):
        d.line([(x, 0), (x, 8)], fill=(255, 0, 180), width=2)
        d.text((x + 2, 9), str(x), fill=(255, 0, 180))
    for y in range(100, h, 100):
        d.line([(0, y), (8, y)], fill=(255, 0, 180), width=2)
        d.text((10, y - 5), str(y), fill=(255, 0, 180))


def capture(region: tuple[int, int, int, int] | None = None) -> tuple[str, ShotMap]:
    """region = (left, top, right, bottom) на экране; None — все мониторы. → (base64 JPEG, ShotMap)."""
    with mss.mss() as sct:
        if region:
            l, t, r, b = region
            mon = {"left": l, "top": t, "width": max(1, r - l), "height": max(1, b - t)}
        else:
            mon = sct.monitors[0]  # виртуальный экран — все мониторы
        raw = sct.grab(mon)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    scale = min(1.0, MAX_SIDE / max(img.size))
    if scale < 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    _ruler(img)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    shot = ShotMap(mon["left"], mon["top"], mon["width"], mon["height"], scale)
    return base64.b64encode(buf.getvalue()).decode("ascii"), shot


def clamp_to_screen(rect: list[int]) -> tuple[int, int, int, int]:
    """Окно может частично выходить за экран — обрезаем по виртуальному экрану."""
    with mss.mss() as sct:
        v = sct.monitors[0]
    l, t, r, b = rect
    return (max(l, v["left"]), max(t, v["top"]),
            min(r, v["left"] + v["width"]), min(b, v["top"] + v["height"]))
