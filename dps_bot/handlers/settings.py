"""Настройки уведомлений и премиум (Telegram Stars)."""
from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from ..app import App
from ..keyboards import (BTN_PREMIUM, BTN_SETTINGS, NavCb, PremCb, SetCb, ZoneCb, alert_kinds_kb,
                         back_kb, premium_kb, quiet_kb, radius_kb, settings_kb, zones_kb)
from ..utils import from_db, now_db, to_db, utcnow

router = Router(name="settings")

SETTINGS_TEXT = (
    "🔔 <b>Уведомления</b>\n\n"
    "Я пришлю сообщение, когда появится событие:\n"
    "🏘 в сёлах, на которые вы подписаны\n"
    "📍 рядом с вашей последней точкой <i>(3 часа после отправки)</i>\n"
    "🛰 впереди по курсу, пока вы транслируете геопозицию\n\n"
    "<i>Настройте под себя:</i>"
)


async def _premium(app: App, uid: int) -> bool:
    return not app.cfg.premium_enabled or await app.db.is_premium(uid)


async def _settings_view(app: App, uid: int):
    u = await app.db.get_user(uid)
    zones = await app.db.val("SELECT COUNT(*) FROM zone_subs WHERE user_id=?", (uid,), 0)
    return SETTINGS_TEXT, settings_kb(u, zones)


async def _edit(cb: CallbackQuery, text: str, kb) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS, F.chat.type == "private")
async def cmd_settings(message: Message, app: App):
    text, kb = await _settings_view(app, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(NavCb.filter(F.to == "settings"))
async def cb_settings(cb: CallbackQuery, app: App):
    text, kb = await _settings_view(app, cb.from_user.id)
    await _edit(cb, text, kb)
    await cb.answer()


@router.callback_query(SetCb.filter())
async def cb_set(cb: CallbackQuery, callback_data: SetCb, app: App):
    uid = cb.from_user.id
    u = await app.db.get_user(uid)
    key, val = callback_data.key, callback_data.val

    if key == "alerts":
        await app.db.run("UPDATE users SET alerts=1-alerts WHERE user_id=?", (uid,))
    elif key == "radius":
        if not val:
            await _edit(cb, "📏 <b>Радиус уведомлений</b>\nБольше 3 км — с Премиум.",
                        radius_kb(u["radius_km"], await _premium(app, uid)))
            await cb.answer()
            return
        r = float(val)
        if r > 3 and not await _premium(app, uid):
            await cb.answer("⭐ Радиус больше 3 км доступен с Премиум", show_alert=True)
            return
        await app.db.run("UPDATE users SET radius_km=? WHERE user_id=?", (r, uid))
    elif key == "quiet":
        if not val:
            await _edit(cb, "🌙 <b>Тихие часы</b>\nВ это время уведомления не приходят.", quiet_kb())
            await cb.answer()
            return
        if val == "off":
            await app.db.run("UPDATE users SET quiet_from=NULL, quiet_to=NULL WHERE user_id=?", (uid,))
        else:
            f, t = map(int, val.split("-"))
            await app.db.run("UPDATE users SET quiet_from=?, quiet_to=? WHERE user_id=?", (f, t, uid))
    elif key in ("kinds", "kind"):
        enabled = set(filter(None, (u["alert_kinds"] or "").split(",")))
        if key == "kind":
            enabled ^= {val}
            await app.db.run("UPDATE users SET alert_kinds=? WHERE user_id=?", (",".join(sorted(enabled)), uid))
        await _edit(cb, "🎚 <b>О чём уведомлять</b>", alert_kinds_kb(enabled))
        await cb.answer()
        return
    elif key == "live":
        await _edit(cb, (
            "🛰 <b>Режим «в дороге»</b>\n\n"
            "1. Нажмите 📎 (скрепка) в этом чате\n"
            "2. Выберите «Геопозиция» → «Транслировать геопозицию»\n"
            "3. Укажите время (например, 1 час)\n\n"
            "Пока трансляция идёт, бот следит за направлением движения и предупреждает о ДПС, камерах и ДТП "
            "<b>впереди по курсу</b> в радиусе ваших настроек (минимум 3 км).\n\n"
            + ("" if await _premium(app, uid) else "⭐ Это функция Премиум.")),
            back_kb("settings"))
        await cb.answer()
        return

    text, kb = await _settings_view(app, uid)
    await _edit(cb, text, kb)
    await cb.answer("Сохранено")


@router.callback_query(ZoneCb.filter())
async def cb_zone(cb: CallbackQuery, callback_data: ZoneCb, app: App):
    uid = cb.from_user.id
    villages = app.geo.villages()
    subs = {r["place_key"] for r in await app.db.all("SELECT place_key FROM zone_subs WHERE user_id=?", (uid,))}
    if callback_data.idx >= 0:
        p = villages[callback_data.idx]
        if p.key in subs:
            await app.db.run("DELETE FROM zone_subs WHERE user_id=? AND place_key=?", (uid, p.key))
            subs.discard(p.key)
            await cb.answer(f"Отписались: {p.name}")
        else:
            limit = int(app.db.setting_float("free_zones") or 2)
            if len(subs) >= limit and not await _premium(app, uid):
                await cb.answer(f"Бесплатно — до {limit} сёл. ⭐ С Премиум — без ограничений.", show_alert=True)
                return
            await app.db.run("INSERT OR IGNORE INTO zone_subs(user_id, place_key) VALUES (?, ?)", (uid, p.key))
            subs.add(p.key)
            await cb.answer(f"Подписка: {p.name} ✅")
    else:
        await cb.answer()
    await _edit(cb, "🏘 <b>Мои сёла</b>\nОтметьте сёла, о событиях в которых нужно сообщать "
                    "(в радиусе 3 км от центра):", zones_kb(villages, subs, callback_data.page))


# ====================================================================== премиум
PREMIUM_TEXT = (
    "⭐ <b>Премиум</b>\n"
    "{status}\n\n"
    "🛰 <b>Предупреждения впереди по курсу</b>\n"
    "<i>транслируете геопозицию — я слежу за дорогой перед вами</i>\n\n"
    "📏 <b>Радиус уведомлений до 10 км</b>\n"
    "🏘 <b>Сколько угодно сёл в подписке</b>\n"
    "❤️ <b>Поддержка развития бота</b>\n\n"
    "Оплата звёздами Telegram — в пару касаний, без карт и переводов."
)


@router.message(Command("premium"), F.chat.type == "private")
@router.message(F.text == BTN_PREMIUM, F.chat.type == "private")
async def cmd_premium(message: Message, app: App, user):
    if not app.cfg.premium_enabled:
        from ..keyboards import with_home
        await message.answer("🎁 Сейчас все функции бота бесплатны для всех!", reply_markup=with_home())
        return
    if user["premium_until"] and user["premium_until"] > now_db():
        until = from_db(user["premium_until"]).astimezone(app.cfg.tz).strftime("%d.%m.%Y")
        status = f"✅ Активен до <b>{until}</b> — можно продлить:"
    else:
        status = "Сейчас не активен."
    await message.answer(PREMIUM_TEXT.format(status=status), reply_markup=premium_kb(app.cfg.premium_plans))


@router.callback_query(NavCb.filter(F.to == "premium"))
async def cb_premium(cb: CallbackQuery, app: App, user):
    await cb.answer()
    await cmd_premium(cb.message, app, user)


@router.callback_query(PremCb.filter())
async def cb_buy(cb: CallbackQuery, callback_data: PremCb, app: App):
    plan = next((p for p in app.cfg.premium_plans if p[0] == callback_data.days), None)
    if not plan:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    days, stars = plan
    await cb.message.answer_invoice(
        title=f"Премиум на {days} дн.",
        description="Предупреждения в дороге, радиус до 10 км, безлимит подписок на сёла.",
        payload=f"premium:{days}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Премиум {days} дн.", amount=stars)],
    )
    await cb.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery, app: App):
    ok = q.invoice_payload.startswith("premium:") and any(
        f"premium:{d}" == q.invoice_payload and s == q.total_amount for d, s in app.cfg.premium_plans)
    await q.answer(ok=ok, error_message=None if ok else "Тариф изменился, попробуйте снова")


@router.message(F.successful_payment)
async def paid(message: Message, app: App):
    sp = message.successful_payment
    days = int(sp.invoice_payload.split(":")[1])
    uid = message.from_user.id
    u = await app.db.get_user(uid)
    base = max(utcnow(), from_db(u["premium_until"]) or utcnow())
    until = to_db(base + timedelta(days=days))  # продлеваем, а не перезаписываем
    await app.db.run("UPDATE users SET premium_until=?, premium_warned=0 WHERE user_id=?", (until, uid))
    await app.db.insert(
        "INSERT INTO payments(user_id, amount, currency, days, charge_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (uid, sp.total_amount, sp.currency, days, sp.telegram_payment_charge_id, now_db()),
    )
    await message.answer(f"🎉 <b>Спасибо! Премиум активен до "
                         f"{from_db(until).astimezone(app.cfg.tz):%d.%m.%Y}</b>\n"
                         "Включите 🛰 трансляцию геопозиции в дороге — и я предупрежу о ДПС впереди.")
    for aid in await app.admin_ids():
        await app.notifier.send(aid, f"💰 Оплата: {sp.total_amount} ⭐ от <code>{uid}</code> ({days} дн.)",
                                silent=True)
