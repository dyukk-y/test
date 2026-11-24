"""Точка входа: python -m dps_bot"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from .app import App
from .config import load_config
from .db import Database
from .events import EventService
from .geo import Geo
from .handlers import admin, group, settings, user
from .middlewares import ThrottleMiddleware, UserMiddleware
from .notify import Notifier
from .parser import ChannelParser
from .tasks import start_all


def make_session() -> AiohttpSession | None:
    """Прокси для Telegram API (TELEGRAM_PROXY или HTTPS_PROXY): http://, socks5://.

    Пригодится, если api.telegram.org недоступен напрямую. Нужен пакет aiohttp-socks."""
    proxy = os.environ.get("TELEGRAM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return None
    session = AiohttpSession(proxy=proxy)
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile:  # корпоративный/прокси-сертификат
        session._connector_init["ssl"] = ssl.create_default_context(cafile=cafile)
    return session


def setup_dispatcher(app: App, throttle: float = 0.3) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), app=app)
    for observer in (dp.message, dp.callback_query, dp.edited_message):
        observer.outer_middleware(UserMiddleware())
    if throttle:
        dp.message.outer_middleware(ThrottleMiddleware(throttle))
        dp.callback_query.outer_middleware(ThrottleMiddleware(throttle))
    # порядок важен: user.router последний — в нём «свободный текст»
    dp.include_routers(admin.router, settings.router, group.router, user.router)
    return dp


def setup_logging(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = RotatingFileHandler(os.path.join(data_dir, "bot.log"), maxBytes=5_000_000, backupCount=3,
                                 encoding="utf-8")
    stream_h = logging.StreamHandler(sys.stdout)
    for h in (file_h, stream_h):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_h, stream_h])
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.data_dir)
    log = logging.getLogger("dps_bot")

    db = Database(cfg.db_path)
    await db.connect()
    geo = Geo(db, cfg)
    await geo.load_places()

    bot = Bot(cfg.bot_token, session=make_session(), default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                          link_preview_is_disabled=True))
    app = App(bot=bot, cfg=cfg, db=db, geo=geo, events=EventService(db), notifier=Notifier(bot, db, geo, cfg))
    app.parser = ChannelParser(app)

    dp = setup_dispatcher(app)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="near", description="Что рядом со мной"),
        BotCommand(command="feed", description="Сейчас на дорогах"),
        BotCommand(command="report", description="Сообщить о ДПС"),
        BotCommand(command="map", description="Карта"),
        BotCommand(command="forecast", description="Прогноз: где обычно стоят"),
        BotCommand(command="settings", description="Уведомления"),
        BotCommand(command="me", description="Моя репутация"),
        BotCommand(command="premium", description="Премиум"),
        BotCommand(command="help", description="Справка"),
    ], scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands([BotCommand(command="dps", description="Что сейчас на дорогах")],
                              scope=BotCommandScopeAllGroupChats())

    await app.parser.start()
    tasks = start_all(app)
    me = await bot.me()
    log.info("🚀 @%s запущен (%s)", me.username, cfg.region_name)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False)
    finally:
        for t in tasks:
            t.cancel()
        await app.parser.stop()
        await geo.close()
        await db.close()
        await bot.session.close()


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code not in (None, 0):
            raise


if __name__ == "__main__":
    run()
