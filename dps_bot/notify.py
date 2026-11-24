"""Уведомления: подписки на сёла, радиус вокруг пользователя, режим «в дороге»."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from .config import Config
from .db import Database
from .geo import Geo
from .keyboards import event_kb
from .kinds import kind
from .texts import event_card
from .utils import angle_diff, arrow, bearing, dist_str, esc, haversine, now_db, utcnow

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bot: Bot, db: Database, geo: Geo, cfg: Config):
        self.bot, self.db, self.geo, self.cfg = bot, db, geo, cfg

    # ---------------------------------------------------------------- helpers
    def premium_on(self) -> bool:
        return self.cfg.premium_enabled

    def _is_premium(self, u) -> bool:
        if not self.premium_on():
            return True
        return bool(u["premium_until"] and u["premium_until"] > now_db())

    def _quiet(self, u) -> bool:
        if u["quiet_from"] is None or u["quiet_to"] is None:
            return False
        h = utcnow().astimezone(self.cfg.tz).hour
        f, t = u["quiet_from"], u["quiet_to"]
        return f <= h < t if f < t else (h >= f or h < t)

    def _wants(self, u, kind_code: str) -> bool:
        return kind_code in (u["alert_kinds"] or "").split(",")

    async def send(self, uid: int, text: str, kb=None, silent: bool = False) -> bool:
        for _ in range(2):
            try:
                await self.bot.send_message(uid, text, reply_markup=kb, disable_notification=silent)
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await self.db.run("UPDATE users SET blocked_bot=1 WHERE user_id=?", (uid,))
                return False
            except TelegramBadRequest as e:
                log.warning("send %s: %s", uid, e)
                return False
        return False

    async def _mark(self, event_id: int, uid: int) -> bool:
        """True, если пользователю ещё не отправляли это событие."""
        return bool(await self.db.run(
            "INSERT OR IGNORE INTO alerts_sent(event_id, user_id, sent_at) VALUES (?, ?, ?)",
            (event_id, uid, now_db()),
        ))

    # ---------------------------------------------------------------- новое событие
    async def on_event(self, e) -> int:
        """Разослать уведомления о новом активном событии."""
        if e["status"] != "active" or e["lat"] is None:
            return 0
        k = kind(e["kind"])
        users = await self.db.all(
            "SELECT * FROM users WHERE alerts=1 AND banned=0 AND blocked_bot=0 AND user_id IS NOT ?",
            (e["author_id"],),
        )
        subs = {}
        for r in await self.db.all("SELECT user_id, place_key FROM zone_subs"):
            subs.setdefault(r["user_id"], set()).add(r["place_key"])

        fresh = now_db(timedelta(hours=-3))
        sent = 0
        for u in users:
            if not self._wants(u, e["kind"]) or self._quiet(u):
                continue
            reason = None
            # 1) режим «в дороге»
            if u["live_until"] and u["live_until"] > now_db() and u["lat"] is not None and self._is_premium(u):
                d = haversine(u["lat"], u["lon"], e["lat"], e["lon"])
                if d <= max(u["radius_km"], 3):
                    ahead = u["heading"] is None or angle_diff(u["heading"], bearing(u["lat"], u["lon"], e["lat"], e["lon"])) <= 75
                    if ahead:
                        reason = f"🛰 Впереди по курсу: {arrow(bearing(u['lat'], u['lon'], e['lat'], e['lon']))} {dist_str(d)}"
            # 2) радиус вокруг последней точки
            if not reason and u["lat"] is not None and u["loc_time"] and u["loc_time"] > fresh:
                d = haversine(u["lat"], u["lon"], e["lat"], e["lon"])
                radius = u["radius_km"] if self._is_premium(u) else min(u["radius_km"], 3)
                if d <= radius:
                    reason = f"📍 {dist_str(d)} от вашей точки"
            # 3) подписка на село
            if not reason and u["user_id"] in subs:
                for key in subs[u["user_id"]]:
                    p = self.geo.place_by_key(key)
                    if p and haversine(p.lat, p.lon, e["lat"], e["lon"]) <= 3:
                        reason = f"🏘 В вашем селе: {esc(p.name)}"
                        break
            if not reason or not await self._mark(e["id"], u["user_id"]):
                continue
            text = f"🔔 <b>{k.emoji} {k.title}!</b> {reason}\n\n" + event_card(e, self.cfg.tz, u["lat"], u["lon"])
            if await self.send(u["user_id"], text, event_kb(e)):
                sent += 1
            await asyncio.sleep(0.04)  # ≤ 25 сообщений/сек
        if sent:
            log.info("🔔 Событие #%s: уведомлено %s", e["id"], sent)
        return sent

    # ---------------------------------------------------------------- в дороге
    async def on_live_location(self, u, lat: float, lon: float, heading: float | None) -> None:
        if not u["alerts"] or self._quiet(u) or not self._is_premium(u):
            return
        from .events import EventService

        radius = max(u["radius_km"], 3)
        for e, d in await EventService(self.db).active_near(lat, lon, radius):
            if not self._wants(u, e["kind"]) or e["author_id"] == u["user_id"]:
                continue
            b = bearing(lat, lon, e["lat"], e["lon"])
            if heading is not None and d > 0.4 and angle_diff(heading, b) > 75:
                continue  # позади или сбоку
            if not await self._mark(e["id"], u["user_id"]):
                continue
            k = kind(e["kind"])
            text = (f"⚠️ <b>Впереди {dist_str(d)}: {k.emoji} {k.title}</b> {arrow(b)}\n"
                    f"{esc(e['place'] or '')}\n\n" + event_card(e, self.cfg.tz, lat, lon))
            await self.send(u["user_id"], text, event_kb(e))
