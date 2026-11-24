"""Бизнес-логика событий: создание, слияние дублей, голосование, истечение, репутация."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta

import aiosqlite

from .db import Database
from .kinds import CLEARABLE, kind as get_kind
from .utils import from_db, haversine, now_db, to_db, utcnow

log = logging.getLogger(__name__)

# Группы типов, которые считаются одним и тем же нарядом при слиянии
MERGE_GROUP = {"police": "enf", "raid": "enf", "radar": "enf",
               "camera": "camera", "accident": "accident", "hazard": "hazard"}


@dataclass
class NewEvent:
    kind: str
    lat: float | None
    lon: float | None
    accuracy_m: int | None = None
    geo_method: str | None = None
    place: str | None = None
    direction: str | None = None
    plate: str | None = None
    text: str | None = None
    photo_id: str | None = None
    source: str = "user"
    source_ref: str | None = None
    author_id: int | None = None
    created_at: str | None = None


def text_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()


class EventService:
    def __init__(self, db: Database):
        self.db = db

    def ttl(self, kind_code: str) -> timedelta:
        return timedelta(minutes=get_kind(kind_code).ttl_min * max(self.db.setting_float("ttl_factor"), 0.1))

    # --------------------------------------------------------------- чтение
    async def get(self, event_id: int) -> aiosqlite.Row | None:
        return await self.db.one("SELECT * FROM events WHERE id=?", (event_id,))

    async def active(self, limit: int = 100) -> list[aiosqlite.Row]:
        return await self.db.all(
            "SELECT * FROM events WHERE status='active' AND expires_at > ? ORDER BY updated_at DESC LIMIT ?",
            (now_db(), limit),
        )

    async def active_near(self, lat: float, lon: float, radius_km: float) -> list[tuple[aiosqlite.Row, float]]:
        out = []
        for e in await self.active(500):
            if e["lat"] is None:
                continue
            d = haversine(lat, lon, e["lat"], e["lon"])
            if d <= radius_km:
                out.append((e, d))
        return sorted(out, key=lambda x: x[1])

    async def pending(self) -> list[aiosqlite.Row]:
        return await self.db.all("SELECT * FROM events WHERE status='pending' ORDER BY created_at")

    # --------------------------------------------------------------- политика
    async def rate_limited(self, user_id: int) -> bool:
        limit = int(self.db.setting_float("max_reports_hour") or 6)
        cnt = await self.db.val(
            "SELECT COUNT(*) FROM events WHERE author_id=? AND source='user' AND created_at > ?",
            (user_id, now_db(timedelta(hours=-1))), 0,
        )
        return cnt >= limit

    def initial_status(self, user: aiosqlite.Row | None, is_admin: bool) -> str:
        mode = self.db.setting("moderation")
        if is_admin or mode == "off" or user is None:
            return "active"
        if user["trusted"]:
            return "active"
        if mode == "all":
            return "pending"
        # mode == "new": новичков без подтверждённых отчётов — на проверку
        if user["reports_ok"] == 0 and user["reputation"] <= 0:
            return "pending"
        return "active" if user["reputation"] > -3 else "pending"

    # --------------------------------------------------------------- создание
    async def find_similar(self, ev: NewEvent) -> aiosqlite.Row | None:
        if ev.lat is None:
            return None
        group = MERGE_GROUP.get(ev.kind)
        rows = await self.db.all(
            "SELECT * FROM events WHERE status IN ('active','pending') AND expires_at > ? AND lat IS NOT NULL",
            (now_db(),),
        )
        best, best_d = None, 1e9
        for r in rows:
            if MERGE_GROUP.get(r["kind"]) != group:
                continue
            coarse = (r["accuracy_m"] or 0) > 1000 or (ev.accuracy_m or 0) > 1000
            radius = 1.5 if coarse else 0.4
            d = haversine(ev.lat, ev.lon, r["lat"], r["lon"])
            if d <= radius and d < best_d:
                best, best_d = r, d
        return best

    async def create(self, ev: NewEvent, status: str = "active") -> tuple[aiosqlite.Row, bool]:
        """Создаёт событие или сливает его с уже существующим поблизости.

        Возвращает (событие, merged)."""
        h = text_hash(ev.text)
        if h and ev.source in ("channel", "group"):
            dup = await self.db.one(
                "SELECT * FROM events WHERE text_hash=? AND created_at > ?", (h, now_db(timedelta(hours=-6)))
            )
            if dup:
                return dup, True

        similar = await self.find_similar(ev)
        if similar:
            return await self._merge(similar, ev), True

        now = now_db()
        created = ev.created_at or now
        expires = to_db((from_db(created) or utcnow()) + self.ttl(ev.kind))
        eid = await self.db.insert(
            """INSERT INTO events(kind, status, lat, lon, accuracy_m, geo_method, place, direction, plate, text,
                   photo_id, source, source_ref, author_id, created_at, updated_at, expires_at, text_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev.kind, status, ev.lat, ev.lon, ev.accuracy_m, ev.geo_method, ev.place, ev.direction, ev.plate,
             ev.text, ev.photo_id, ev.source, ev.source_ref, ev.author_id, created, now, expires, h),
        )
        if ev.author_id and ev.source == "user":
            await self.db.run("UPDATE users SET reports=reports+1 WHERE user_id=?", (ev.author_id,))
        return await self.get(eid), False

    async def _merge(self, e: aiosqlite.Row, ev: NewEvent) -> aiosqlite.Row:
        new_exp = max(e["expires_at"], now_db(self.ttl(e["kind"])))
        better = ev.lat is not None and (ev.accuracy_m or 9999) < (e["accuracy_m"] or 9999)
        status = e["status"]
        if status == "pending" and ev.author_id != e["author_id"]:
            status = "active"  # независимое подтверждение — публикуем
        kind_code = e["kind"]
        if ev.kind in ("raid", "radar") and kind_code == "police":
            kind_code = ev.kind  # уточняем тип
        await self.db.run(
            """UPDATE events SET reports=reports+1, updated_at=?, expires_at=?, status=?, kind=?,
                   lat=?, lon=?, accuracy_m=?, geo_method=?, plate=COALESCE(plate, ?),
                   direction=COALESCE(?, direction), photo_id=COALESCE(photo_id, ?) WHERE id=?""",
            (now_db(), new_exp, status, kind_code,
             ev.lat if better else e["lat"], ev.lon if better else e["lon"],
             ev.accuracy_m if better else e["accuracy_m"], ev.geo_method if better else e["geo_method"],
             ev.plate, ev.direction, ev.photo_id, e["id"]),
        )
        if ev.author_id and ev.author_id != e["author_id"]:
            inserted = await self.db.run(
                "INSERT OR IGNORE INTO votes(event_id, user_id, value, created_at) VALUES (?, ?, 'here', ?)",
                (e["id"], ev.author_id, now_db()),
            )
            if inserted:
                await self.db.run("UPDATE events SET confirms=confirms+1 WHERE id=?", (e["id"],))
        return await self.get(e["id"])

    # --------------------------------------------------------------- голоса
    async def vote(self, event_id: int, user_id: int, value: str, is_admin: bool = False) -> tuple[str, aiosqlite.Row | None]:
        e = await self.get(event_id)
        if not e:
            return "Событие не найдено", None
        if e["status"] not in ("active", "pending"):
            return "Событие уже неактуально", e
        if e["author_id"] == user_id and value != "gone" and not is_admin:
            return "Это ваш отчёт — за него голосуют другие 🙂", e

        col = {"here": "confirms", "gone": "gone", "fake": "denies"}[value]
        old = await self.db.one("SELECT value FROM votes WHERE event_id=? AND user_id=?", (event_id, user_id))
        if old and old["value"] == value:
            return "Вы уже голосовали так", e
        if old:
            old_col = {"here": "confirms", "gone": "gone", "fake": "denies"}[old["value"]]
            await self.db.run(f"UPDATE events SET {old_col}=MAX({old_col}-1, 0) WHERE id=?", (event_id,))
        else:
            await self.db.run("UPDATE users SET votes=votes+1 WHERE user_id=?", (user_id,))
        await self.db.run(
            "INSERT OR REPLACE INTO votes(event_id, user_id, value, created_at) VALUES (?, ?, ?, ?)",
            (event_id, user_id, value, now_db()),
        )
        await self.db.run(f"UPDATE events SET {col}={col}+1, updated_at=? WHERE id=?", (now_db(), event_id))

        e = await self.get(event_id)
        msg = {"here": "👍 Спасибо! Событие продлено", "gone": "👋 Спасибо, учли", "fake": "🚫 Жалоба принята"}[value]
        if value == "here":
            ext = now_db(min(self.ttl(e["kind"]), timedelta(minutes=45)))
            status = "active" if e["status"] == "pending" and e["confirms"] >= 2 else e["status"]
            await self.db.run("UPDATE events SET expires_at=MAX(expires_at, ?), status=? WHERE id=?",
                              (ext, status, event_id))
        elif value == "gone":
            if is_admin or e["gone"] >= 2 or (e["gone"] >= 1 and e["confirms"] == 0) or e["author_id"] == user_id:
                await self.close(event_id, "closed")
                msg = "👋 Отмечено: уехали. Событие закрыто"
        elif value == "fake":
            if is_admin or (e["denies"] >= 3 and e["denies"] > e["confirms"] * 2):
                await self.close(event_id, "fake")
                msg = "🗑 Событие удалено как фейк"
        return msg, await self.get(event_id)

    # --------------------------------------------------------------- закрытие
    async def close(self, event_id: int, status: str = "closed") -> None:
        await self.db.run(
            "UPDATE events SET status=?, closed_at=?, expires_at=MIN(expires_at, ?) WHERE id=? AND status IN ('active','pending')",
            (status, now_db(), now_db(), event_id),
        )
        await self._score(event_id)

    async def close_near(self, lat: float, lon: float, radius_km: float = 0.6, kinds=CLEARABLE) -> int:
        n = 0
        for e, _ in await self.active_near(lat, lon, radius_km):
            if e["kind"] in kinds:
                await self.close(e["id"], "closed")
                n += 1
        return n

    async def moderate(self, event_id: int, approve: bool) -> aiosqlite.Row | None:
        e = await self.get(event_id)
        if not e:
            return None
        if approve:
            exp = max(e["expires_at"], now_db(self.ttl(e["kind"])))
            await self.db.run("UPDATE events SET status='active', expires_at=? WHERE id=?", (exp, event_id))
        else:
            await self.db.run("UPDATE events SET status='rejected', closed_at=? WHERE id=?", (now_db(), event_id))
        return await self.get(event_id)

    async def expire(self) -> int:
        rows = await self.db.all(
            "SELECT id FROM events WHERE status='active' AND expires_at <= ?", (now_db(),)
        )
        for r in rows:
            await self.close(r["id"], "closed")
        # Непроверенные отчёты старше 3 часов неактуальны
        await self.db.run(
            "UPDATE events SET status='rejected', closed_at=? WHERE status='pending' AND created_at < ?",
            (now_db(), now_db(timedelta(hours=-3))),
        )
        return len(rows)

    # --------------------------------------------------------------- репутация
    async def _score(self, event_id: int) -> None:
        e = await self.get(event_id)
        if not e or e["scored"] or not e["author_id"] or e["source"] != "user":
            return
        await self.db.run("UPDATE events SET scored=1 WHERE id=?", (event_id,))
        uid = e["author_id"]
        if e["status"] == "fake":
            await self.db.run(
                "UPDATE users SET reports_fake=reports_fake+1, reputation=reputation-3, trusted=0 WHERE user_id=?",
                (uid,),
            )
        elif e["confirms"] >= 1 or e["reports"] >= 2:
            bonus = min(1 + 0.5 * e["confirms"], 3)
            await self.db.run(
                "UPDATE users SET reports_ok=reports_ok+1, reputation=reputation+? WHERE user_id=?", (bonus, uid)
            )
        threshold = self.db.setting_float("trust_threshold") or 5
        await self.db.run("UPDATE users SET trusted=1 WHERE user_id=? AND reputation>=? AND banned=0",
                          (uid, threshold))
        await self.db.run(
            "UPDATE users SET banned=1, ban_reason='Автоблокировка: много фейков' WHERE user_id=? AND reputation<=-12",
            (uid,),
        )
