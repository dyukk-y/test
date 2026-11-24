"""Общие утилиты: время, расстояния, форматирование."""
from __future__ import annotations

import html
import math
from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%d %H:%M:%S"  # тот же формат, что у SQLite datetime('now') — сравнения строк корректны


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_db(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime(FMT)


def now_db(delta: timedelta | None = None) -> str:
    return to_db(utcnow() + (delta or timedelta()))


def from_db(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s[:19].replace("T", " "), FMT).replace(tzinfo=timezone.utc)


def ago(s: str | None) -> str:
    dt = from_db(s)
    if not dt:
        return "—"
    minutes = int((utcnow() - dt).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч {minutes % 60} мин назад"
    return f"{hours // 24} дн назад"


def local(s: str | None, tz, fmt: str = "%d.%m %H:%M") -> str:
    dt = from_db(s)
    return dt.astimezone(tz).strftime(fmt) if dt else "—"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние в километрах."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Азимут из точки 1 в точку 2, градусы 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


ARROWS = ["⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️"]
SIDES = ["север", "северо-восток", "восток", "юго-восток", "юг", "юго-запад", "запад", "северо-запад"]


def arrow(deg: float) -> str:
    return ARROWS[int(((deg + 22.5) % 360) // 45)]


def side(deg: float) -> str:
    return SIDES[int(((deg + 22.5) % 360) // 45)]


def dist_str(km: float) -> str:
    return f"{int(km * 1000 // 10 * 10)} м" if km < 1 else f"{km:.1f} км"


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def plural(n: int, one: str, few: str, many: str) -> str:
    n100, n10 = abs(n) % 100, abs(n) % 10
    if 11 <= n100 <= 19:
        return many
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


def maps_links(lat: float, lon: float) -> tuple[str, str]:
    yandex = f"https://yandex.ru/maps/?pt={lon:.6f},{lat:.6f}&z=16&l=map"
    google = f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"
    return yandex, google
