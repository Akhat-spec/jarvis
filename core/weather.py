"""Погода для HUD — Open-Meteo (бесплатно, без ключа). Город задаётся в настройках HUD."""
import json
import threading
import time
import urllib.parse
import urllib.request

REFRESH_SEC = 15 * 60

# код погоды WMO → (описание, значок для HUD)
CODES = {
    0: ("Ясно", "sun"), 1: ("Преимущественно ясно", "sun"), 2: ("Переменная облачность", "cloud-sun"),
    3: ("Пасмурно", "cloud"), 45: ("Туман", "fog"), 48: ("Изморозь, туман", "fog"),
    51: ("Слабая морось", "rain"), 53: ("Морось", "rain"), 55: ("Сильная морось", "rain"),
    56: ("Ледяная морось", "rain"), 57: ("Ледяная морось", "rain"),
    61: ("Небольшой дождь", "rain"), 63: ("Дождь", "rain"), 65: ("Сильный дождь", "rain"),
    66: ("Ледяной дождь", "rain"), 67: ("Ледяной дождь", "rain"),
    71: ("Небольшой снег", "snow"), 73: ("Снег", "snow"), 75: ("Сильный снег", "snow"), 77: ("Снежные зёрна", "snow"),
    80: ("Ливень", "rain"), 81: ("Ливни", "rain"), 82: ("Сильный ливень", "rain"),
    85: ("Снегопад", "snow"), 86: ("Сильный снегопад", "snow"),
    95: ("Гроза", "storm"), 96: ("Гроза с градом", "storm"), 99: ("Гроза с градом", "storm"),
}
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-hud"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(city: str) -> dict:
    geo = _get("https://geocoding-api.open-meteo.com/v1/search?" +
               urllib.parse.urlencode({"name": city, "count": 1, "language": "ru"}))
    if not geo.get("results"):
        return {"type": "weather", "error": f"Город «{city}» не найден"}
    g = geo["results"][0]
    f = _get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto", "forecast_days": 4,
        "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
    }))
    cur, daily = f["current"], f["daily"]
    desc, icon = CODES.get(cur["weather_code"], ("—", "cloud"))
    days = []
    for i in range(1, min(4, len(daily["time"]))):  # три следующих дня
        y, m, d = (int(x) for x in daily["time"][i].split("-"))
        import datetime
        days.append({"day": DAYS[datetime.date(y, m, d).weekday()],
                     "max": round(daily["temperature_2m_max"][i]), "min": round(daily["temperature_2m_min"][i])})
    return {"type": "weather", "city": g.get("name", city), "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]), "wind": round(cur["wind_speed_10m"]),
            "humidity": round(cur["relative_humidity_2m"]), "desc": desc, "icon": icon, "days": days,
            "updated": time.strftime("%H:%M")}


class Weather:
    def __init__(self, emit, get_city):
        self.emit, self.get_city = emit, get_city
        self.last: dict | None = None
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="weather").start()

    def refresh(self):
        self._wake.set()

    def _loop(self):
        while True:
            city = (self.get_city() or "").strip()
            if not city:
                self.last = {"type": "weather", "error": "Укажите город в настройках"}
            else:
                try:
                    self.last = fetch(city)
                except Exception as e:
                    print(f"[weather] {type(e).__name__}: {e}")
                    if not self.last or self.last.get("city") != city:
                        self.last = {"type": "weather", "error": "Нет связи с сервисом погоды"}
            self.emit(self.last)
            self._wake.wait(REFRESH_SEC)
            self._wake.clear()
