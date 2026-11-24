"""Контейнер сервисов + общие сценарии (приём отчёта из любого источника)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from aiogram import Bot

from .config import Config
from .db import Database
from .events import EventService, NewEvent
from .geo import Geo
from .nlp import Analysis, analyze
from .notify import Notifier
from .texts import event_card
from .utils import from_db, now_db, to_db, utcnow

log = logging.getLogger(__name__)


@dataclass
class App:
    bot: Bot
    cfg: Config
    db: Database
    geo: Geo
    events: EventService
    notifier: Notifier
    parser: object | None = None
    started_at: str = field(default_factory=now_db)

    # ------------------------------------------------------------- админы
    async def admin_ids(self) -> list[int]:
        rows = await self.db.all("SELECT user_id FROM admins")
        return list(dict.fromkeys(self.cfg.owner_ids + [r["user_id"] for r in rows]))

    async def is_admin(self, uid: int) -> bool:
        return await self.db.is_admin(uid, self.cfg.owner_ids)

    def analyze(self, text: str) -> Analysis:
        return analyze(text, self.geo.places)

    # ------------------------------------------------------------- публикация
    async def submit(self, ev: NewEvent, status: str, analysis: Analysis | None = None,
                     gps: bool = False) -> tuple[object, bool]:
        """Сохранить событие, обучить геокодер, разослать уведомления."""
        if ev.lat is not None and not ev.place:
            ev.place = self.geo.describe(ev.lat, ev.lon)
        e, merged = await self.events.create(ev, status)
        if gps and analysis and ev.lat is not None:
            await self.geo.remember(analysis, ev.lat, ev.lon, ev.author_id)
        if e["status"] == "active":
            # повторно никому не придёт: alerts_sent хранит, кому уже отправляли
            await self.notifier.on_event(e)
        elif e["status"] == "pending" and not merged:
            await self.notify_admins_pending(e)
        return e, merged

    async def notify_admins_pending(self, e) -> None:
        from .keyboards import AdmCb
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        b = InlineKeyboardBuilder()
        b.button(text="✅ Одобрить", callback_data=AdmCb(sec="ev", act="ok", id=e["id"]))
        b.button(text="❌ Отклонить", callback_data=AdmCb(sec="ev", act="no", id=e["id"]))
        b.button(text="🚫 Бан автора", callback_data=AdmCb(sec="ev", act="ban", id=e["id"]))
        b.adjust(2, 1)
        author = await self.db.get_user(e["author_id"]) if e["author_id"] else None
        who = ""
        if author:
            who = (f"\n👤 <code>{author['user_id']}</code> @{author['username'] or '—'} · "
                   f"репутация {author['reputation']:g}, отчётов {author['reports']}")
        text = "📩 <b>Новый отчёт на проверке</b>" + who + "\n\n" + event_card(e, self.cfg.tz)
        for aid in await self.admin_ids():
            await self.notifier.send(aid, text, b.as_markup(), silent=True)

    # ------------------------------------------------------------- автосбор
    async def ingest_text(self, text: str, source: str, source_ref: str | None = None,
                          author_id: int | None = None, created_at=None,
                          min_conf: float | None = None) -> tuple[str, object | None]:
        """Обработать сообщение из канала/чата. Возвращает (результат, событие)."""
        if not text or len(text) > 1500:
            return "skip", None
        if created_at is not None:
            created = to_db(created_at)
            if from_db(created) < utcnow() - timedelta(hours=3):
                return "old", None
        else:
            created = now_db()
        a = self.analyze(text)
        min_conf = self.db.setting_float("channel_min_conf") if min_conf is None else min_conf

        if a.is_clear:
            g = await self.geo.resolve(a)
            if g:
                n = await self.events.close_near(g.lat, g.lon, radius_km=0.8 if g.accuracy_m < 1000 else 2.5)
                return ("cleared" if n else "skip"), None
            return "skip", None

        if not a.is_report or a.confidence < min_conf:
            return "skip", None
        g = await self.geo.resolve(a)
        if not g:
            return "noplace", None
        ev = NewEvent(kind=a.kind, lat=g.lat, lon=g.lon, accuracy_m=g.accuracy_m, geo_method=g.method,
                      place=g.label, direction=a.direction, plate=a.plate, text=text, source=source,
                      source_ref=source_ref, author_id=author_id, created_at=created)
        status = "active"
        if author_id:
            user = await self.db.get_user(author_id)
            if user and user["banned"]:
                return "banned", None
        e, merged = await self.submit(ev, status, a)
        return ("merged" if merged else "created"), e
