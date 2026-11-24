"""Прогноз: где наряды обычно стоят в это время (по истории отчётов)."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import Database
from .geo import Geo
from .kinds import ENFORCEMENT
from .utils import from_db, now_db

CELL_LAT, CELL_LON = 0.004, 0.0065  # ≈ 450 × 420 м
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


@dataclass
class Hotspot:
    lat: float
    lon: float
    score: float
    slot_hits: int     # в этот день недели ±1 час
    hour_hits: int     # в этот час в любой день
    total: int         # всего за период
    label: str


async def history_points(db: Database, days: int = 90, kinds=ENFORCEMENT) -> list:
    marks = ",".join("?" * len(kinds))
    return await db.all(
        f"""SELECT lat, lon, kind, created_at, place FROM events
            WHERE lat IS NOT NULL AND status NOT IN ('fake','rejected') AND kind IN ({marks})
            AND created_at > ?""",
        (*kinds, now_db(timedelta(days=-days))),
    )


async def hotspots(db: Database, geo: Geo, tz, at: datetime | None = None, days: int = 90,
                   top: int = 7) -> tuple[list[Hotspot], int, Counter]:
    at = (at or datetime.now(tz)).astimezone(tz)
    rows = await history_points(db, days)
    cells: dict[tuple[int, int], list] = defaultdict(list)
    hours = Counter()
    for r in rows:
        dt = from_db(r["created_at"]).astimezone(tz)
        hours[dt.hour] += 1
        cells[(round(r["lat"] / CELL_LAT), round(r["lon"] / CELL_LON))].append((r, dt))

    result = []
    for items in cells.values():
        slot = hour = 0
        for _, dt in items:
            dh = min(abs(dt.hour - at.hour), 24 - abs(dt.hour - at.hour))
            if dh <= 1:
                hour += 1
                if dt.weekday() == at.weekday():
                    slot += 1
        score = slot * 3 + hour + len(items) * 0.1
        if slot == 0 and hour == 0:
            continue
        lat = sum(r["lat"] for r, _ in items) / len(items)
        lon = sum(r["lon"] for r, _ in items) / len(items)
        names = Counter(r["place"] for r, _ in items if r["place"])
        label = names.most_common(1)[0][0] if names else geo.describe(lat, lon)
        result.append(Hotspot(lat, lon, score, slot, hour, len(items), label))
    result.sort(key=lambda h: -h.score)
    return result[:top], len(rows), hours


def hours_chart(hours: Counter) -> str:
    if not hours:
        return ""
    peak = max(hours.values())
    lines = []
    for start in range(0, 24, 3):
        n = sum(hours[h] for h in range(start, start + 3))
        bar = "█" * max(1 if n else 0, round(n / max(peak * 3, 1) * 12))
        lines.append(f"<code>{start:02d}–{start + 3:02d}</code> {bar} {n}")
    return "\n".join(lines)
