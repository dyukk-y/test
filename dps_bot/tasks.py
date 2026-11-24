"""Фоновые задачи."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram.types import FSInputFile

from .app import App
from .utils import now_db

log = logging.getLogger(__name__)


async def _loop(name: str, interval: float, fn, *args, delay: float = 0) -> None:
    await asyncio.sleep(delay)
    while True:
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Фоновая задача %s упала", name)
        await asyncio.sleep(interval)


async def expire(app: App) -> None:
    n = await app.events.expire()
    if n:
        log.info("⌛ Закрыто по времени: %s", n)


async def cleanup(app: App) -> None:
    db = app.db
    await db.run("DELETE FROM events WHERE status IN ('closed','fake','rejected') AND created_at < ?",
                 (now_db(timedelta(days=-180)),))
    await db.run("DELETE FROM votes WHERE event_id NOT IN (SELECT id FROM events)")
    await db.run("DELETE FROM alerts_sent WHERE sent_at < ?", (now_db(timedelta(days=-3)),))
    await db.run("DELETE FROM geocache WHERE created_at < ?", (now_db(timedelta(days=-30)),))
    await db.run("DELETE FROM geo_memory WHERE created_at < ?", (now_db(timedelta(days=-365)),))


async def premium_reminders(app: App) -> None:
    if not app.cfg.premium_enabled:
        return
    rows = await app.db.all(
        "SELECT user_id FROM users WHERE premium_warned=0 AND premium_until > ? AND premium_until < ?",
        (now_db(), now_db(timedelta(days=1))),
    )
    for r in rows:
        await app.notifier.send(r["user_id"], "⭐ Премиум заканчивается меньше чем через сутки. "
                                              "Продлить: /premium")
        await app.db.run("UPDATE users SET premium_warned=1 WHERE user_id=?", (r["user_id"],))


async def daily_backup(app: App) -> None:
    from .handlers.admin import make_backup

    while True:
        now = datetime.now(app.cfg.tz)
        nxt = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            path = await make_backup(app)
            await cleanup(app)
            if app.cfg.backup_chat_id:
                await app.bot.send_document(app.cfg.backup_chat_id, FSInputFile(path),
                                            caption=f"📦 Бэкап {now:%d.%m.%Y}")
            log.info("📦 Ежедневный бэкап: %s", path)
        except Exception:
            log.exception("Ошибка бэкапа")


async def parser_catch_up(app: App) -> None:
    if app.parser and app.parser.clients:
        await app.parser.catch_up()


def start_all(app: App) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_loop("expire", 60, expire, app)),
        asyncio.create_task(_loop("cleanup", 6 * 3600, cleanup, app, delay=60)),
        asyncio.create_task(_loop("premium", 3600, premium_reminders, app, delay=120)),
        asyncio.create_task(_loop("parser", 600, parser_catch_up, app, delay=30)),
        asyncio.create_task(_loop("refine", 7 * 24 * 3600, app.geo.refine_villages, delay=5)),
        asyncio.create_task(daily_backup(app)),
    ]
