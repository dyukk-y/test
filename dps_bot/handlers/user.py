"""Пользовательская часть: отчёты, геопозиция, лента, карта, прогноз, голосование."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..app import App
from ..events import NewEvent
from ..forecast import WEEKDAYS, history_points, hotspots, hours_chart
from ..keyboards import (BTN_FEED, BTN_FORECAST, BTN_HELP, BTN_MAP, BTN_REPORT, EvCb, NavCb, RepCb,
                         event_kb, feed_kb, kinds_kb, location_request, main_menu, map_kb, nearby_kb,
                         report_preview_kb, villages_kb)
from ..kinds import kind
from ..maps import render_html, render_png
from ..texts import HELP, START, analysis_preview, event_card, event_line
from ..utils import esc, from_db, haversine, now_db, plural, to_db, utcnow

router = Router(name="user")
router.message.filter(F.chat.type == "private")

FEED_PAGE = 8


class Report(StatesGroup):
    text = State()
    location = State()


# ====================================================================== утилиты
def fresh_location(user, minutes: int = 180) -> tuple[float, float] | None:
    if user and user["lat"] is not None and user["loc_time"] and \
            user["loc_time"] > now_db(timedelta(minutes=-minutes)):
        return user["lat"], user["lon"]
    return None


async def build_draft(app: App, text: str | None, photo_id: str | None = None) -> dict:
    d: dict = {"text": text, "photo_id": photo_id, "kind": None, "lat": None, "lon": None,
               "acc": None, "method": None, "label": None, "direction": None, "plate": None}
    if text:
        a = app.analyze(text)
        d.update(kind=a.kind, direction=a.direction, plate=a.plate)
        g = await app.geo.resolve(a)
        if g:
            d.update(lat=g.lat, lon=g.lon, acc=g.accuracy_m, method=g.method, label=g.label)
    return d


async def show_preview(message: Message, app: App, d: dict, state: FSMContext) -> None:
    await state.update_data(draft=d)
    if not d.get("kind"):
        await message.answer("Что произошло? Выберите тип события:", reply_markup=kinds_kb())
        return
    a = app.analyze(d["text"] or "")
    await message.answer(analysis_preview(a, d["label"], d["method"], d["acc"], d["kind"]),
                         reply_markup=report_preview_kb(d["lat"] is not None))


async def publish(app: App, target: Message, user, is_admin: bool, d: dict, state: FSMContext) -> None:
    uid = user["user_id"]
    if d.get("lat") is None:
        await target.answer("📍 Не хватает места. Отправьте геопозицию или выберите село.",
                            reply_markup=report_preview_kb(False))
        return
    if not is_admin and await app.events.rate_limited(uid):
        await state.clear()
        await target.answer("⏳ Слишком много отчётов за час. Попробуйте позже.", reply_markup=main_menu())
        return
    status = app.events.initial_status(user, is_admin)
    ev = NewEvent(kind=d["kind"] or "police", lat=d["lat"], lon=d["lon"], accuracy_m=d["acc"],
                  geo_method=d["method"], place=d.get("label"), direction=d.get("direction"),
                  plate=d.get("plate"), text=d.get("text"), photo_id=d.get("photo_id"),
                  source="admin" if is_admin else "user", author_id=uid)
    a = app.analyze(d["text"]) if d.get("text") else None
    e, merged = await app.submit(ev, status, a, gps=d.get("method") == "gps")
    await state.clear()
    if merged:
        head = "🤝 <b>Такое событие уже есть рядом — засчитали как подтверждение!</b>"
    elif e["status"] == "pending":
        head = ("📩 <b>Отчёт отправлен на проверку.</b>\n"
                "Появится на карте после модерации или когда его подтвердит ещё кто-то. "
                "Подтверждённые отчёты повышают репутацию — потом проверка не понадобится.")
    else:
        head = "✅ <b>Опубликовано! Спасибо, вы помогли водителям.</b>"
    await target.answer(head, reply_markup=main_menu())
    await target.answer(event_card(e, app.cfg.tz), reply_markup=event_kb(e))


# ====================================================================== старт/помощь
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, app: App, is_admin: bool):
    await state.clear()
    await message.answer(START.format(region=esc(app.cfg.region_name), loc="📍 Что рядом"),
                         reply_markup=main_menu())
    if is_admin:
        await message.answer("👑 Вы администратор: /admin — панель управления.")


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=main_menu())


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext, app: App):
    if app.parser:
        await app.parser.login_cancel(message.from_user.id)
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ====================================================================== сообщить
@router.message(Command("report"))
@router.message(F.text == BTN_REPORT)
async def report_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Report.text)
    await message.answer(
        "🚨 <b>Что и где?</b>\n\n"
        "• Самое точное — нажмите <b>📍 Отправить мою точку</b> (если вы на месте)\n"
        "• Или напишите текстом: <i>«ДПС у моста в Бажинске, тормозят всех»</i>, "
        "<i>«тренога на выезде из Маслянино в сторону Черепаново»</i>\n"
        "• Можно прислать фото с подписью",
        reply_markup=location_request(),
    )


@router.callback_query(RepCb.filter(F.act == "cancel"))
async def report_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Главное меню", reply_markup=main_menu())
    await cb.answer()


@router.callback_query(RepCb.filter(F.act.in_({"kind", "retype"})))
async def report_kind(cb: CallbackQuery, callback_data: RepCb, state: FSMContext, app: App, user, is_admin: bool):
    data = await state.get_data()
    d = data.get("draft")
    if d is None:
        await cb.answer("Черновик устарел — начните заново", show_alert=True)
        return
    if callback_data.act == "retype":
        await cb.message.edit_text("Выберите тип события:", reply_markup=kinds_kb())
        await cb.answer()
        return
    d["kind"] = callback_data.val
    await cb.answer(kind(d["kind"]).title)
    await cb.message.delete()
    if d.get("method") == "gps" and not d.get("text"):
        await publish(app, cb.message, user, is_admin, d, state)  # точка + тип = сразу публикуем
    else:
        await show_preview(cb.message, app, d, state)


@router.callback_query(RepCb.filter(F.act == "needloc"))
async def report_needloc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Report.location)
    await cb.message.answer("📍 Нажмите кнопку ниже, чтобы отправить текущую геопозицию.\n"
                            "Или прикрепите точку на карте: 📎 → Геопозиция → выбрать место.",
                            reply_markup=location_request())
    await cb.answer()


@router.callback_query(RepCb.filter(F.act == "village"))
async def report_village(cb: CallbackQuery, callback_data: RepCb, app: App):
    await cb.message.edit_text("🏘 Выберите населённый пункт:",
                               reply_markup=villages_kb(app.geo.villages(), callback_data.page))
    await cb.answer()


@router.callback_query(RepCb.filter(F.act == "pickv"))
async def report_pick_village(cb: CallbackQuery, callback_data: RepCb, state: FSMContext, app: App):
    d = (await state.get_data()).get("draft")
    if d is None:
        await cb.answer("Черновик устарел", show_alert=True)
        return
    p = app.geo.villages()[int(callback_data.val)]
    d.update(lat=p.lat, lon=p.lon, acc=1500, method="village", label=p.name)
    await cb.message.delete()
    await show_preview(cb.message, app, d, state)
    await cb.answer()


@router.callback_query(RepCb.filter(F.act == "pub"))
async def report_publish(cb: CallbackQuery, state: FSMContext, app: App, user, is_admin: bool):
    d = (await state.get_data()).get("draft")
    if d is None:
        await cb.answer("Черновик устарел — начните заново", show_alert=True)
        return
    if d.get("lat") is None:
        await cb.answer("Укажите место: геопозиция или выбор села", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await publish(app, cb.message, user, is_admin, d, state)


@router.callback_query(RepCb.filter(F.act == "here"))
async def report_here(cb: CallbackQuery, callback_data: RepCb, state: FSMContext, app: App, user, is_admin: bool):
    loc = fresh_location(user, minutes=20)
    if not loc:
        await cb.answer("Геопозиция устарела — отправьте точку ещё раз", show_alert=True)
        return
    d = await build_draft(app, None)
    d.update(kind=callback_data.val, lat=loc[0], lon=loc[1], acc=50, method="gps",
             label=app.geo.describe(*loc))
    await cb.answer()
    await publish(app, cb.message, user, is_admin, d, state)


# ====================================================================== геопозиция
@router.message(F.location, StateFilter(Report.text, Report.location))
async def report_location(message: Message, state: FSMContext, app: App):
    loc = message.location
    if not app.geo.in_bounds(loc.latitude, loc.longitude):
        await message.answer("🤔 Эта точка далеко от района, который обслуживает бот.", reply_markup=main_menu())
        await state.clear()
        return
    await app.db.set_location(message.from_user.id, loc.latitude, loc.longitude, loc.heading)
    d = (await state.get_data()).get("draft") or await build_draft(app, None)
    d.update(lat=loc.latitude, lon=loc.longitude, acc=int(loc.horizontal_accuracy or 30), method="gps",
             label=app.geo.describe(loc.latitude, loc.longitude))
    await state.set_state(None)
    await message.answer("📍 Точка получена.", reply_markup=main_menu())
    await show_preview(message, app, d, state)


@router.message(F.location)
async def any_location(message: Message, app: App, state: FSMContext):
    loc = message.location
    uid = message.from_user.id
    if loc.live_period:
        until = to_db(utcnow() + timedelta(seconds=min(loc.live_period, 86400)))
        await app.db.set_location(uid, loc.latitude, loc.longitude, loc.heading, live_until=until)
        premium = await app.db.is_premium(uid) or not app.cfg.premium_enabled
        txt = ("🛰 <b>Режим «в дороге» включён</b>\nПредупрежу о ДПС, камерах и ДТП <b>впереди по курсу</b>. "
               "Чтобы выключить — остановите трансляцию.")
        if not premium:
            txt += "\n\n⭐ Предупреждения в дороге — функция Премиум. Сейчас покажу только то, что рядом."
        await message.answer(txt)
        user = await app.db.get_user(uid)
        await app.notifier.on_live_location(user, loc.latitude, loc.longitude, loc.heading)
    else:
        await app.db.set_location(uid, loc.latitude, loc.longitude, loc.heading)
    await show_nearby(message, app, loc.latitude, loc.longitude)


@router.edited_message(F.location, F.chat.type == "private")
async def live_update(message: Message, app: App):
    loc = message.location
    user = await app.db.get_user(message.from_user.id)
    if not user:
        return
    await app.db.set_location(user["user_id"], loc.latitude, loc.longitude, loc.heading)
    await app.notifier.on_live_location(user, loc.latitude, loc.longitude, loc.heading)


async def show_nearby(message: Message, app: App, lat: float, lon: float, radius: float = 10) -> None:
    near = await app.events.active_near(lat, lon, radius)
    where = app.geo.describe(lat, lon)
    if not near:
        text = f"✅ <b>В радиусе {radius:g} км всё спокойно</b>\n📍 {esc(where)}"
    else:
        lines = [f"📍 <b>Рядом с вами</b> ({esc(where)}) — "
                 f"{len(near)} {plural(len(near), 'событие', 'события', 'событий')}:\n"]
        for e, _ in near[:10]:
            lines.append(event_line(e, app.cfg.tz, lat, lon))
        text = "\n".join(lines)
    text += "\n\n<b>Вы видите что-то здесь?</b> Отметьте в одно касание:"
    await message.answer(text, reply_markup=nearby_kb())


@router.message(Command("near"))
async def cmd_near(message: Message, app: App, user):
    loc = fresh_location(user, 30)
    if loc:
        await show_nearby(message, app, *loc)
    else:
        await message.answer("Отправьте геопозицию кнопкой «📍 Что рядом».", reply_markup=main_menu())


# ====================================================================== голосование
@router.callback_query(EvCb.filter(F.act.in_({"here", "gone", "fake"})))
async def ev_vote(cb: CallbackQuery, callback_data: EvCb, app: App, is_admin: bool):
    msg, e = await app.events.vote(callback_data.id, cb.from_user.id, callback_data.act, is_admin)
    await cb.answer(msg, show_alert=callback_data.act == "fake")
    if e is not None:
        try:
            await cb.message.edit_reply_markup(reply_markup=event_kb(e) if e["status"] in ("active", "pending") else None)
        except Exception:
            pass


@router.callback_query(EvCb.filter(F.act == "card"))
async def ev_card(cb: CallbackQuery, callback_data: EvCb, app: App, user, is_admin: bool):
    e = await app.events.get(callback_data.id)
    if not e:
        await cb.answer("Не найдено", show_alert=True)
        return
    loc = fresh_location(user)
    text = event_card(e, app.cfg.tz, *(loc or (None, None)))
    kb = event_kb(e, show_card_btn=True, is_admin=is_admin)
    if e["photo_id"] and len(text) <= 1024:
        await cb.message.answer_photo(e["photo_id"], caption=text, reply_markup=kb)
    else:
        if e["photo_id"]:
            await cb.message.answer_photo(e["photo_id"])
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(EvCb.filter(F.act == "loc"))
async def ev_location(cb: CallbackQuery, callback_data: EvCb, app: App):
    e = await app.events.get(callback_data.id)
    if not e or e["lat"] is None:
        await cb.answer("Нет координат", show_alert=True)
        return
    k = kind(e["kind"])
    await cb.message.answer_venue(latitude=e["lat"], longitude=e["lon"],
                                  title=f"{k.emoji} {k.title} #{e['id']}", address=e["place"] or "")
    await cb.answer()


# ====================================================================== лента
async def render_feed(app: App, user, page: int) -> tuple[str, object]:
    events = await app.events.active(300)
    loc = fresh_location(user)
    if loc:
        events.sort(key=lambda e: haversine(loc[0], loc[1], e["lat"], e["lon"]) if e["lat"] is not None else 999)
    if not events:
        return ("✅ <b>Сейчас на дорогах спокойно</b>\nАктивных событий нет. "
                "Увидели ДПС — просто напишите мне!"), feed_kb([], 0, 1)
    pages = (len(events) + FEED_PAGE - 1) // FEED_PAGE
    page = max(0, min(page, pages - 1))
    chunk = events[page * FEED_PAGE:(page + 1) * FEED_PAGE]
    head = f"📋 <b>Сейчас на дорогах</b> — {len(events)} {plural(len(events), 'событие', 'события', 'событий')}"
    if loc:
        head += " (ближайшие сверху)"
    body = "\n".join(event_line(e, app.cfg.tz, *(loc or (None, None))) for e in chunk)
    return f"{head}\n\n{body}\n\nНажмите номер, чтобы открыть карточку:", feed_kb(chunk, page, pages)


@router.message(Command("feed"))
@router.message(F.text == BTN_FEED)
async def cmd_feed(message: Message, app: App, user):
    text, kb = await render_feed(app, user, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(NavCb.filter(F.to == "feed"))
async def cb_feed(cb: CallbackQuery, callback_data: NavCb, app: App, user):
    text, kb = await render_feed(app, user, callback_data.page)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await cb.answer("Обновлено")


# ====================================================================== карты
@router.message(Command("map"))
@router.message(F.text == BTN_MAP)
async def cmd_map(message: Message):
    await message.answer("🗺 Какую карту показать?", reply_markup=map_kb())


@router.callback_query(NavCb.filter(F.to.in_({"map_html", "map_png", "map_heat"})))
async def cb_map(cb: CallbackQuery, callback_data: NavCb, app: App, user):
    await cb.answer("Рисую карту…")
    await cb.message.bot.send_chat_action(cb.message.chat.id, "upload_document")
    events = [dict(e) for e in await app.events.active(300)]
    loc = fresh_location(user)
    villages = app.geo.villages()
    title = f"ДПС — {app.cfg.region_name}"
    if callback_data.to == "map_html":
        history = [dict(h) for h in await history_points(app.db, 30)]
        data = await asyncio.to_thread(render_html, events, villages, history, app.cfg.center, app.cfg.tz, title, loc)
        await cb.message.answer_document(
            BufferedInputFile(data, filename=f"dps_map_{datetime.now(app.cfg.tz):%d%m_%H%M}.html"),
            caption="🗺 <b>Интерактивная карта</b>\nОткройте файл в браузере: слои по типам, "
                    "точность меток, «горячие точки» за 30 дней (в меню слоёв).")
    else:
        history = [dict(h) for h in await history_points(app.db, 30)] if callback_data.to == "map_heat" else None
        if callback_data.to == "map_heat":
            title = f"Горячие точки за 30 дней — {app.cfg.region_name}"
        data = await asyncio.to_thread(render_png, events, villages, app.cfg.bounds, app.cfg.tz, title, history, loc)
        await cb.message.answer_photo(BufferedInputFile(data, filename="map.png"),
                                      caption=f"🖼 {esc(title)} · активных событий: {len(events)}")


# ====================================================================== прогноз
async def render_forecast(app: App, offset_h: int) -> tuple[str, object]:
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    at = datetime.now(app.cfg.tz) + timedelta(hours=offset_h)
    spots, total, hours = await hotspots(app.db, app.geo, app.cfg.tz, at)
    head = f"🔮 <b>Прогноз на {WEEKDAYS[at.weekday()]}, {at:%H}:00</b>\n"
    if total < 5:
        text = head + ("\nСтатистики пока мало (нужно хотя бы несколько десятков отчётов). "
                       "Каждый ваш отчёт делает прогноз точнее! 📈")
    elif not spots:
        text = head + "\nВ это время наряды обычно не отмечались. Но будьте внимательны 🙂"
    else:
        lines = [head, f"По {total} отчётам за 90 дней наряды чаще всего стоят:\n"]
        for i, s in enumerate(spots, 1):
            slot = f", в этот день недели — {s.slot_hits}" if s.slot_hits else ""
            lines.append(f"{i}. <b>{esc(s.label)}</b> — в это время {s.hour_hits} раз{slot}")
        lines.append("\n<b>Активность по часам:</b>\n" + hours_chart(hours))
        text = "\n".join(lines)
    b = InlineKeyboardBuilder()
    for h, title in ((0, "Сейчас"), (1, "+1 ч"), (3, "+3 ч"), (12, "+12 ч")):
        b.button(text=("• " if h == offset_h else "") + title, callback_data=NavCb(to="fc", page=h))
    b.adjust(4)
    return text, b.as_markup()


@router.message(Command("forecast"))
@router.message(F.text == BTN_FORECAST)
async def cmd_forecast(message: Message, app: App):
    text, kb = await render_forecast(app, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(NavCb.filter(F.to == "fc"))
async def cb_forecast(cb: CallbackQuery, callback_data: NavCb, app: App):
    text, kb = await render_forecast(app, callback_data.page)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await cb.answer()


# ====================================================================== профиль
@router.message(Command("me"))
async def cmd_me(message: Message, app: App, user):
    prem = "нет"
    if user["premium_until"] and user["premium_until"] > now_db():
        prem = "до " + from_db(user["premium_until"]).astimezone(app.cfg.tz).strftime("%d.%m.%Y")
    status = "🛡 доверенный (публикация без проверки)" if user["trusted"] else "обычный"
    await message.answer(
        "👤 <b>Ваш профиль</b>\n\n"
        f"⭐ Репутация: <b>{user['reputation']:g}</b> · статус: {status}\n"
        f"📝 Отчётов: {user['reports']} · подтверждено: {user['reports_ok']} · фейков: {user['reports_fake']}\n"
        f"🗳 Голосов: {user['votes']}\n"
        f"💎 Премиум: {prem}\n\n"
        "Репутация растёт, когда ваши отчёты подтверждают другие водители."
    )


# ====================================================================== свободный текст
@router.message(F.photo)
async def photo_msg(message: Message, state: FSMContext, app: App):
    photo_id = message.photo[-1].file_id
    d = await build_draft(app, message.caption, photo_id)
    await show_preview(message, app, d, state)


@router.message(F.text)
async def free_text(message: Message, state: FSMContext, app: App, user):
    text = message.text.strip()
    if text.startswith("/"):
        await message.answer("Не знаю такую команду. /help — справка.")
        return
    a = app.analyze(text)

    if a.is_clear:
        g = await app.geo.resolve(a)
        if g:
            n = await app.events.close_near(g.lat, g.lon, 0.8 if g.accuracy_m < 1000 else 2.5)
            await message.answer(f"👋 Спасибо! Отметил «отбой» у {esc(g.label)}: закрыто событий — {n}."
                                 if n else f"Спасибо! Активных событий у {esc(g.label)} не было.")
        else:
            await message.answer("👋 Понял, что там «чисто», но не понял где. Отправьте точку или назовите место.")
        return

    if a.is_question:
        g = await app.geo.resolve(a)
        if g:
            near = await app.events.active_near(g.lat, g.lon, 5 if g.accuracy_m < 1000 else 7)
            if near:
                lines = [f"🔎 Около «{esc(g.label)}» сейчас:\n"]
                lines += [event_line(e, app.cfg.tz, g.lat, g.lon) for e, _ in near[:8]]
                await message.answer("\n".join(lines))
            else:
                await message.answer(f"✅ Около «{esc(g.label)}» активных событий нет.")
        else:
            text_, kb = await render_feed(app, user, 0)
            await message.answer(text_, reply_markup=kb)
        return

    if a.is_report or (a.kind and a.confidence >= 0.5) or await state.get_state() == Report.text:
        d = await build_draft(app, text)
        await state.set_state(None)
        await show_preview(message, app, d, state)
        return

    await message.answer(
        "🤔 Не похоже на сообщение о ДПС.\n\n"
        "Напишите, например: <i>«ДПС у заправки в Маслянино»</i>, <i>«камера на 45 км»</i>, "
        "<i>«чисто у моста»</i> — или нажмите «🚨 Сообщить».",
        reply_markup=main_menu(),
    )
