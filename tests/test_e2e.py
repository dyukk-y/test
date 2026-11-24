"""Сквозные сценарии: апдейты Telegram → диспетчер → БД → ответы бота."""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timezone

from aiogram.types import CallbackQuery, Chat, Location, Message, Update, User

from dps_bot.__main__ import setup_dispatcher
from dps_bot.app import App
from dps_bot.config import Config
from dps_bot.db import Database
from dps_bot.events import EventService
from dps_bot.geo import Geo
from dps_bot.keyboards import EvCb, RepCb
from dps_bot.notify import Notifier

OWNER = 100
_ids = itertools.count(1)


def user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name=f"U{uid}", username=f"u{uid}")


def msg(uid: int, text: str | None = None, location: tuple | None = None, chat_id: int | None = None,
        chat_type: str = "private", live: int | None = None) -> Update:
    chat = Chat(id=chat_id or uid, type=chat_type, title="Водители" if chat_type != "private" else None)
    m = Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=chat, from_user=user(uid), text=text,
                location=Location(latitude=location[0], longitude=location[1], live_period=live) if location else None)
    return Update(update_id=next(_ids), message=m)


def cb(uid: int, data: str) -> Update:
    m = Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                from_user=user(1), text="x")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=user(uid), chat_instance="1", message=m, data=data))


async def make_app(tmp_path, bot):
    cfg = Config(bot_token="x", owner_ids=[OWNER], data_dir=str(tmp_path),
                 nominatim_url="http://127.0.0.1:9/none")  # геокодер «офлайн»
    db = Database(cfg.db_path)
    await db.connect()
    await db.set_setting("moderation", "off")
    geo = Geo(db, cfg)
    await geo.load_places()
    app = App(bot=bot, cfg=cfg, db=db, geo=geo, events=EventService(db), notifier=Notifier(bot, db, geo, cfg))
    return app


def test_full_flow(tmp_path, fake_bot):
    bot, s = fake_bot

    async def scenario():
        app = await make_app(tmp_path, bot)
        dp = setup_dispatcher(app, throttle=0)
        feed = lambda u: dp.feed_update(bot, u)  # noqa: E731

        # водитель 2 подписан на Бажинск, водитель 3 был рядом с Маслянино
        await feed(msg(2, "/start"))
        await feed(msg(3, "/start"))
        await app.db.run("INSERT INTO zone_subs VALUES (2, 'бажинск')")
        await feed(msg(3, location=(54.344, 84.214)))
        assert any("всё спокойно" in t for t in s.texts(3))

        # 1. текстовый отчёт со сленгом → превью → публикация
        await feed(msg(5, "гайцы у моста в бажинске, тормозят"))
        assert any("Вот что я понял" in t and "Бажинск" in t for t in s.texts(5))
        await feed(cb(5, RepCb(act="pub").pack()))
        e = (await app.events.active())[0]
        assert e["kind"] == "police" and e["place"] == "Бажинск" and e["geo_method"] == "village"
        assert any("В вашем селе" in t for t in s.texts(2)), "подписчик села получил уведомление"

        # 2. отчёт по GPS в два касания: точка → «Тут ДПС»
        await feed(msg(4, "/start"))
        await feed(msg(4, location=(54.3440, 84.2150)))
        await feed(cb(4, RepCb(act="here", val="radar").pack()))
        radar = [x for x in await app.events.active() if x["kind"] == "radar"][0]
        assert radar["geo_method"] == "gps"
        assert any("от вашей точки" in t for t in s.texts(3)), "водитель рядом получил уведомление"

        # 3. повторный отчёт рядом сливается, а не дублируется
        await feed(msg(3, location=(54.3442, 84.2152)))
        await feed(cb(3, RepCb(act="here", val="police").pack()))
        merged = await app.events.get(radar["id"])
        assert merged["reports"] == 2 and merged["confirms"] == 1
        assert len(await app.events.active()) == 2

        # 4. голосование: нельзя за свой, «уехали» закрывает
        await feed(cb(4, EvCb(act="here", id=radar["id"]).pack()))
        await feed(cb(2, EvCb(act="gone", id=radar["id"]).pack()))
        await feed(cb(6, EvCb(act="gone", id=radar["id"]).pack()))
        assert (await app.events.get(radar["id"]))["status"] == "closed"
        u4 = await app.db.get_user(4)
        assert u4["reputation"] > 0 and u4["reports_ok"] == 1, "автор подтверждённого отчёта получил репутацию"

        # 5. «отбой» текстом закрывает событие у моста в Бажинске
        await feed(msg(7, "в бажинске чисто, дпс уехали"))
        assert (await app.events.get(e["id"]))["status"] == "closed"

        # 6. вопрос → ответ по данным, а не создание отчёта
        await feed(msg(7, "есть дпс в маслянино?"))
        assert any("Около «Маслянино»" in t for t in s.texts(7))

        # 7. сообщение в группе водителей собирается автоматически
        await feed(msg(8, "ДПС на въезде в Егорьевское, всех тормозят", chat_id=-500, chat_type="supergroup"))
        g = [x for x in await app.events.active() if x["source"] == "group"]
        assert g and g[0]["kind"] == "raid"
        assert s.of("SetMessageReaction")

        # 8. модерация новичков
        await app.db.set_setting("moderation", "new")
        await feed(msg(9, "/start"))
        await feed(msg(9, "камера в Суенге"))
        await feed(cb(9, RepCb(act="pub").pack()))
        p = (await app.events.pending())[0]
        assert any("Новый отчёт на проверке" in t for t in s.texts(OWNER))
        await feed(cb(OWNER, f"adm:ev:ok:{p['id']}:0"))
        assert (await app.events.get(p["id"]))["status"] == "active"

        # 9. не-админ не попадает в админку
        before = len(s.calls)
        await feed(msg(9, "/admin"))
        assert not any("Панель администратора" in (getattr(m, "text", "") or "") for m in s.calls[before:])
        await feed(msg(OWNER, "/admin"))
        assert any("Панель администратора" in t for t in s.texts(OWNER))

        # 10. лента, прогноз, карта, настройки, профиль — без ошибок
        for t in ("📋 Сейчас на дорогах", "🔮 Прогноз", "⚙️ Настройки", "/me", "/help", "⭐ Премиум"):
            await feed(msg(3, t))
        await feed(cb(3, "nav:map_png:0"))
        await feed(cb(3, "nav:map_html:0"))
        assert s.of("SendPhoto") and s.of("SendDocument")
        await feed(cb(3, "pr:30"))
        assert s.of("SendInvoice")
        await feed(cb(OWNER, "adm:stats::0:0"))
        await feed(cb(OWNER, "adm:set:cyc:0:0"))
        assert app.db.setting("moderation") == "all"

        # 11. бан
        await feed(msg(OWNER, "/ban 9 спам"))
        before = len(s.calls)
        await feed(msg(9, "дпс у школы в маслянино"))
        assert any("заблокированы" in t for t in s.texts(9)[-1:])

        await app.db.close()

    asyncio.run(scenario())


def test_expire_and_fake(tmp_path, fake_bot):
    bot, s = fake_bot

    async def scenario():
        app = await make_app(tmp_path, bot)
        from dps_bot.events import NewEvent

        await app.db.upsert_user(10, "a", "A")
        e, _ = await app.events.create(NewEvent("police", 54.3, 84.1, 50, "gps", author_id=10))
        for uid in (11, 12, 13):
            await app.db.upsert_user(uid, None, None)
            await app.events.vote(e["id"], uid, "fake")
        e = await app.events.get(e["id"])
        assert e["status"] == "fake"
        assert (await app.db.get_user(10))["reputation"] < 0

        e2, _ = await app.events.create(NewEvent("camera", 54.35, 84.2, 50, "gps"))
        await app.db.run("UPDATE events SET expires_at='2000-01-01 00:00:00' WHERE id=?", (e2["id"],))
        assert await app.events.expire() == 1
        await app.db.close()

    asyncio.run(scenario())
