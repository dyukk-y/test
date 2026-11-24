"""Пользовательская часть: отчёты, геопозиция, лента, карта, прогноз, голосование."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..app import App
from ..events import NewEvent
from ..forecast import WEEKDAYS, history_points, hotspots, hours_chart
from ..keyboards import (BTN_FEED, BTN_FORECAST, BTN_HELP, BTN_MAP, BTN_REPORT, EvCb, NavCb, RepCb,
                         event_kb, feed_kb, home_kb, kinds_kb, location_request, main_menu, map_kb, nearby_kb,
                         with_home,
                         report_preview_kb, villages_kb)
from ..kinds import kind
from ..maps import render_html, render_png
from ..kinds import KINDS
from ..texts import HELP, WELCOME, analysis_preview, event_card, event_line
from ..webapp import build_url, parse_start
from ..utils import ago, esc, from_db, haversine, now_db, plural, to_db, utcnow

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


async def map_url(app: App, user=None, focus: int = 0, tab: str | None = None) -> str | None:
    """Ссылка на мини-апп со свежим снимком событий (или None, если мини-апп не настроен)."""
    if not app.cfg.webapp_url:
        return None
    me = await app.bot.me()
    events = await app.events.active(60)
    return build_url(app.cfg.webapp_url, me.username, events, fresh_location(user) if user else None, focus, tab)


async def home_view(app: App, user) -> tuple[str, object]:
    events = await app.events.active(300)
    head = f"🚔 <b>DPS Guardian</b> · {esc(app.cfg.region_name)}\n\n"
    if not events:
        body = "🟢 <b>На дорогах спокойно</b> — активных событий нет."
    else:
        counts: dict[str, int] = {}
        for e in events:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        by_kind = "  ".join(f"{KINDS[k].emoji} {n}" for k, n in counts.items() if k in KINDS)
        last = events[0]
        body = (f"🔴 <b>Сейчас на дорогах: {len(events)}</b> "
                f"{plural(len(events), 'событие', 'события', 'событий')}\n{by_kind}\n\n"
                f"🕐 Последнее: <b>{esc(last['place'] or '')}</b>, {ago(last['created_at'])}")
    tail = ("\n\n✍️ <i>Видите ДПС? Просто напишите мне, где — или отметьте на карте.</i>")
    return head + body + tail, home_kb(await map_url(app, user))


async def update_menu_button(app: App, chat_id: int, user) -> None:
    """Кнопка «Карта» слева от поля ввода — со свежим снимком событий."""
    url = await map_url(app, user)
    if not url:
        return
    from aiogram.types import MenuButtonWebApp, WebAppInfo
    try:
        await app.bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonWebApp(
            text="Карта", web_app=WebAppInfo(url=url)))
    except Exception:
        pass


async def send_home(target, app: App, user, edit: bool = False) -> None:
    text, kb = await home_view(app, user)
    chat_id = (target.message if isinstance(target, CallbackQuery) else target).chat.id
    await update_menu_button(app, chat_id, user)
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(text, reply_markup=kb)


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
        await target.answer("⏳ Слишком много отчётов за час. Попробуйте позже.", reply_markup=with_home())
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
    url = await map_url(app, user, focus=e["id"])
    await target.answer(head + "\n\n" + event_card(e, app.cfg.tz), reply_markup=with_home(event_kb(e, map_url=url)))


# ====================================================================== старт/помощь
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, app: App, user, is_admin: bool):
    await state.clear()
    deep = parse_start(command.args)
    if deep and deep[0] == "report":  # отчёт из мини-аппа
        _, kind_code, lat, lon = deep
        if not app.geo.in_bounds(lat, lon):
            await message.answer("🤔 Точка за пределами района.", reply_markup=with_home())
            return
        d = await build_draft(app, None)
        d.update(kind=kind_code, lat=lat, lon=lon, acc=60, method="gps", label=app.geo.describe(lat, lon))
        await publish(app, message, user, is_admin, d, state)
        return
    if deep and deep[0] == "vote":
        _, eid, value = deep
        msg, e = await app.events.vote(eid, message.from_user.id, value, is_admin)
        if e is not None:
            await message.answer(f"{msg}\n\n" + event_card(e, app.cfg.tz),
                                 reply_markup=with_home(event_kb(e) if e["status"] in ("active", "pending") else None))
        else:
            await message.answer(msg, reply_markup=with_home())
        return
    is_new = user["created_at"] >= now_db(timedelta(minutes=-2)) and not user["reports"]
    if is_new:
        # новичку — знакомство, старую клавиатуру снизу убираем
        await message.answer(WELCOME.format(region=esc(app.cfg.region_name)), reply_markup=main_menu())
    else:
        tmp = await message.answer("⏳", reply_markup=main_menu())
        try:
            await tmp.delete()
        except Exception:
            pass
    await send_home(message, app, user)
    if is_admin and not command.args:
        await message.answer("👑 Вы администратор — /admin открывает панель управления.")


@router.callback_query(NavCb.filter(F.to == "home"))
async def cb_home(cb: CallbackQuery, state: FSMContext, app: App, user):
    await state.clear()
    await send_home(cb, app, user, edit=True)
    await cb.answer()


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=with_home())


@router.callback_query(NavCb.filter(F.to == "help"))
async def cb_help(cb: CallbackQuery):
    try:
        await cb.message.edit_text(HELP, reply_markup=with_home())
    except Exception:
        await cb.message.answer(HELP, reply_markup=with_home())
    await cb.answer()


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext, app: App):
    if app.parser:
        await app.parser.login_cancel(message.from_user.id)
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())
    await send_home(message, app, await app.db.get_user(message.from_user.id))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ====================================================================== сообщить
REPORT_TEXT = (
    "🚨 <b>Сообщить о событии</b>\n\n"
    "📍 <b>Вы на месте?</b> Нажмите кнопку «Отправить мою точку» внизу — метка будет точной до метров.\n\n"
    "✍️ <b>Или напишите</b>, что и где:\n"
    "<blockquote>ДПС у моста в Бажинске, тормозят всех\n"
    "тренога на выезде из Маслянино в сторону Черепаново</blockquote>\n"
    "📷 Можно прислать фото с подписью."
)


@router.message(Command("report"))
@router.message(F.text == BTN_REPORT)
async def report_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Report.text)
    await message.answer(REPORT_TEXT, reply_markup=location_request())


@router.callback_query(NavCb.filter(F.to == "report"))
async def cb_report_start(cb: CallbackQuery, state: FSMContext, app: App, user):
    await state.clear()
    await state.set_state(Report.text)
    await cb.message.answer(REPORT_TEXT, reply_markup=location_request())
    url = await map_url(app, user, tab="report")
    if url:
        from ..webapp import button
        from aiogram.types import InlineKeyboardMarkup
        await cb.message.answer("…или отметьте точку на карте:", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[button("🗺 Отметить на карте", url)]]))
    await cb.answer()


@router.callback_query(RepCb.filter(F.act == "cancel"))
async def report_cancel(cb: CallbackQuery, state: FSMContext, app: App):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.answer()
    await send_home(cb, app, await app.db.get_user(cb.from_user.id))


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
        await message.answer(f"📍 Точка получена: <b>{esc(app.geo.describe(loc.latitude, loc.longitude))}</b>",
                             reply_markup=main_menu())
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
        text = f"🟢 <b>В радиусе {radius:g} км всё спокойно</b>\n📍 {esc(where)}"
    else:
        lines = [f"📍 <b>Рядом с вами</b> ({esc(where)}) — "
                 f"{len(near)} {plural(len(near), 'событие', 'события', 'событий')}:\n"]
        for e, _ in near[:10]:
            lines.append(event_line(e, app.cfg.tz, lat, lon))
        text = "\n".join(lines)
    text += "\n\n<b>Вы видите что-то здесь?</b> Отметьте в одно касание:"
    await message.answer(text, reply_markup=nearby_kb())


NEAR_ASK = ("📍 <b>Что рядом?</b>\n\nНажмите кнопку «Отправить мою точку» внизу "
            "(или 📎 → Геопозиция) — покажу события вокруг и дам отметить новое в одно касание.")


@router.message(Command("near"))
async def cmd_near(message: Message, app: App, user):
    loc = fresh_location(user, 30)
    if loc:
        await show_nearby(message, app, *loc)
    else:
        await message.answer(NEAR_ASK, reply_markup=location_request())


@router.callback_query(NavCb.filter(F.to == "near"))
async def cb_near(cb: CallbackQuery, app: App, user):
    loc = fresh_location(user, 30)
    if loc:
        await show_nearby(cb.message, app, *loc)
    else:
        await cb.message.answer(NEAR_ASK, reply_markup=location_request())
    await cb.answer()


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
    kb = with_home(event_kb(e, show_card_btn=True, is_admin=is_admin, map_url=await map_url(app, user, focus=e["id"])))
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
        return ("🟢 <b>Сейчас на дорогах спокойно</b>\n\nАктивных событий нет. "
                "Увидели ДПС — просто напишите мне!"), feed_kb([], 0, 1, await map_url(app, user))
    pages = (len(events) + FEED_PAGE - 1) // FEED_PAGE
    page = max(0, min(page, pages - 1))
    chunk = events[page * FEED_PAGE:(page + 1) * FEED_PAGE]
    head = f"📋 <b>Сейчас на дорогах: {len(events)}</b> {plural(len(events), 'событие', 'события', 'событий')}"
    if loc:
        head += "\n<i>ближайшие к вам — сверху</i>"
    body = "\n".join(event_line(e, app.cfg.tz, *(loc or (None, None))) for e in chunk)
    return (f"{head}\n\n{body}\n\n<i>Нажмите номер, чтобы открыть карточку.</i>",
            feed_kb(chunk, page, pages, await map_url(app, user)))


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
async def cmd_map(message: Message, app: App, user):
    url = await map_url(app, user)
    kb = map_kb()
    if url:
        from ..webapp import button
        kb.inline_keyboard.insert(0, [button("🗺 Открыть интерактивную карту", url)])
    await message.answer("🗺 <b>Карта</b>\n\nВ мини-приложении — живая карта со списком и отметкой событий. "
                         "Ещё можно получить картинку или HTML-файл.", reply_markup=kb)


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

    from ..keyboards import HOME_BTN

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
    b.row(HOME_BTN)
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
@router.callback_query(NavCb.filter(F.to == "me"))
async def cb_me(cb: CallbackQuery, app: App, user):
    await cb.answer()
    await cmd_me(cb.message, app, user, edit=True)


@router.message(Command("me"))
async def cmd_me(message: Message, app: App, user, edit: bool = False):
    prem = "не активен"
    if user["premium_until"] and user["premium_until"] > now_db():
        prem = "до " + from_db(user["premium_until"]).astimezone(app.cfg.tz).strftime("%d.%m.%Y")
    if user["trusted"]:
        status = "🛡 доверенный — публикуете без проверки"
    elif user["reputation"] < 0:
        status = "⚠️ отчёты проверяет модератор"
    else:
        status = "🙂 обычный"
    threshold = app.db.setting_float("trust_threshold") or 5
    progress = ""
    if not user["trusted"] and user["reputation"] >= 0:
        filled = int(min(user["reputation"] / threshold, 1) * 10)
        progress = f"\n<code>{'▰' * filled}{'▱' * (10 - filled)}</code> до доверенного: {user['reputation']:g}/{threshold:g}"
    text = (
        f"👤 <b>{esc(user['full_name'] or 'Ваш профиль')}</b>\n\n"
        f"⭐ Репутация: <b>{user['reputation']:g}</b>{progress}\n"
        f"Статус: {status}\n\n"
        f"📝 Отчётов: <b>{user['reports']}</b> · ✅ подтверждено: {user['reports_ok']} · 🚫 фейков: {user['reports_fake']}\n"
        f"🗳 Голосов: <b>{user['votes']}</b>\n"
        f"💎 Премиум: {prem}\n\n"
        "<i>Репутация растёт, когда ваши отчёты подтверждают другие водители.</i>"
    )
    if edit:
        try:
            await message.edit_text(text, reply_markup=with_home())
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=with_home())


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
