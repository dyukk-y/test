"""Администрирование."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..app import App
from ..keyboards import AdmCb, event_kb, main_menu
from ..texts import event_card
from ..utils import esc, from_db, local, now_db, to_db, utcnow

router = Router(name="admin")
log = logging.getLogger(__name__)
PAGE = 6


class IsAdmin(Filter):
    async def __call__(self, event, is_admin: bool = False) -> bool:
        return is_admin


router.message.filter(IsAdmin(), F.chat.type == "private")
router.callback_query.filter(IsAdmin())


class AdmSt(StatesGroup):
    src_link = State()
    acc_phone = State()
    acc_code = State()
    acc_pwd = State()
    bcast = State()
    add_admin = State()
    place_loc = State()


def _btn(text: str, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=AdmCb(**kw).pack())


def _back(sec: str = "menu") -> list[InlineKeyboardButton]:
    return [_btn("◀️ Назад", sec=sec)]


def _pager(sec: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(_btn("◀️", sec=sec, page=page - 1))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(_btn("▶️", sec=sec, page=page + 1))
    return row


async def _show(target: Message | CallbackQuery, text: str, kb) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


# ====================================================================== меню
async def admin_menu(app: App):
    pending = await app.db.val("SELECT COUNT(*) FROM events WHERE status='pending'", (), 0)
    active = await app.db.val("SELECT COUNT(*) FROM events WHERE status='active' AND expires_at>?", (now_db(),), 0)
    users = await app.db.val("SELECT COUNT(*) FROM users", (), 0)
    b = InlineKeyboardBuilder()
    b.row(_btn(f"📩 Модерация ({pending})", sec="pend"), _btn("📊 Статистика", sec="stats"))
    b.row(_btn("📡 Источники", sec="src"), _btn("👥 Аккаунты-парсеры", sec="acc"))
    b.row(_btn("📍 Места", sec="places"), _btn("🚫 Баны", sec="bans"))
    b.row(_btn("⚙️ Настройки", sec="set"), _btn("👑 Админы", sec="admins"))
    b.row(_btn("📢 Рассылка", sec="bcast"), _btn("📦 Бэкап", sec="backup"))
    text = (f"👑 <b>Панель администратора</b>\n\n"
            f"🚔 Активных событий: <b>{active}</b> · на проверке: <b>{pending}</b>\n"
            f"👥 Пользователей: <b>{users}</b>\n\n"
            "Команды: /user ID · /ban ID причина · /unban ID · /trust ID · /untrust ID\n"
            "/givepremium ID дней · /refund ID charge_id · /setplace Название; алиасы · "
            "/setvillage Название · /delplace Название")
    return text, b.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, app: App):
    await state.clear()
    text, kb = await admin_menu(app)
    await message.answer(text, reply_markup=kb)


@router.callback_query(AdmCb.filter(F.sec == "menu"))
async def cb_menu(cb: CallbackQuery, state: FSMContext, app: App):
    await state.clear()
    text, kb = await admin_menu(app)
    await _show(cb, text, kb)


# ====================================================================== модерация
@router.callback_query(AdmCb.filter(F.sec == "pend"))
async def cb_pending(cb: CallbackQuery, callback_data: AdmCb, app: App):
    rows = await app.events.pending()
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(callback_data.page, pages - 1))
    b = InlineKeyboardBuilder()
    for e in rows[page * PAGE:(page + 1) * PAGE]:
        short = (e["text"] or e["place"] or "")[:30].replace("\n", " ")
        b.row(_btn(f"#{e['id']} {short}", sec="ev", id=e["id"]))
    if pages > 1:
        b.row(*_pager("pend", page, pages))
    b.row(*_back())
    await _show(cb, f"📩 <b>На проверке: {len(rows)}</b>", b.as_markup())


@router.callback_query(AdmCb.filter(F.sec == "ev"))
async def cb_event(cb: CallbackQuery, callback_data: AdmCb, app: App):
    e = await app.events.get(callback_data.id)
    if not e:
        await cb.answer("Не найдено", show_alert=True)
        return
    act = callback_data.act
    if act == "ok":
        e = await app.events.moderate(e["id"], True)
        await app.notifier.on_event(e)
        if e["author_id"]:
            await app.notifier.send(e["author_id"], f"✅ Ваш отчёт #{e['id']} опубликован. Спасибо!", event_kb(e))
        await cb.answer("Одобрено")
    elif act == "no":
        e = await app.events.moderate(e["id"], False)
        if e["author_id"]:
            await app.notifier.send(e["author_id"], f"❌ Отчёт #{e['id']} отклонён модератором.")
        await cb.answer("Отклонено")
    elif act == "ban":
        if e["author_id"] and e["author_id"] not in app.cfg.owner_ids:
            await app.db.run("UPDATE users SET banned=1, ban_reason='Ложные отчёты' WHERE user_id=?", (e["author_id"],))
        await app.events.moderate(e["id"], False)
        await cb.answer("Автор заблокирован, отчёт отклонён", show_alert=True)
    elif act == "close":
        await app.events.close(e["id"], "closed")
        await cb.answer("Закрыто")
    elif act == "fake":
        await app.events.close(e["id"], "fake")
        await cb.answer("Помечено фейком (автору −репутация)")
    e = await app.events.get(callback_data.id)
    b = InlineKeyboardBuilder()
    if e["status"] == "pending":
        b.row(_btn("✅ Одобрить", sec="ev", act="ok", id=e["id"]), _btn("❌ Отклонить", sec="ev", act="no", id=e["id"]))
    if e["status"] == "active":
        b.row(_btn("✔️ Закрыть", sec="ev", act="close", id=e["id"]), _btn("🚫 Фейк", sec="ev", act="fake", id=e["id"]))
    if e["status"] in ("pending", "active") and e["author_id"]:
        b.row(_btn("🔨 Забанить автора", sec="ev", act="ban", id=e["id"]))
    b.row(*_back("pend"))
    author = f"\n👤 автор: <code>{e['author_id']}</code>" if e["author_id"] else ""
    await _show(cb, event_card(e, app.cfg.tz) + author, b.as_markup())


# ====================================================================== статистика
@router.callback_query(AdmCb.filter(F.sec == "stats"))
async def cb_stats(cb: CallbackQuery, app: App):
    db = app.db
    day = now_db(timedelta(days=-1))
    week = now_db(timedelta(days=-7))
    q = db.val
    n = {
        "users": await q("SELECT COUNT(*) FROM users", (), 0),
        "u_day": await q("SELECT COUNT(*) FROM users WHERE last_seen>?", (day,), 0),
        "u_week": await q("SELECT COUNT(*) FROM users WHERE last_seen>?", (week,), 0),
        "prem": await q("SELECT COUNT(*) FROM users WHERE premium_until>?", (now_db(),), 0),
        "trusted": await q("SELECT COUNT(*) FROM users WHERE trusted=1", (), 0),
        "banned": await q("SELECT COUNT(*) FROM users WHERE banned=1", (), 0),
        "zones": await q("SELECT COUNT(*) FROM zone_subs", (), 0),
        "e_day": await q("SELECT COUNT(*) FROM events WHERE created_at>?", (day,), 0),
        "e_week": await q("SELECT COUNT(*) FROM events WHERE created_at>?", (week,), 0),
        "e_all": await q("SELECT COUNT(*) FROM events", (), 0),
        "fake": await q("SELECT COUNT(*) FROM events WHERE status='fake' AND created_at>?", (week,), 0),
        "alerts": await q("SELECT COUNT(*) FROM alerts_sent WHERE sent_at>?", (day,), 0),
        "memory": await q("SELECT COUNT(*) FROM geo_memory", (), 0),
        "groups": await q("SELECT COUNT(*) FROM groups WHERE enabled=1", (), 0),
        "stars": await q("SELECT SUM(amount) FROM payments WHERE currency='XTR' AND status='paid'", (), 0),
        "stars_w": await q("SELECT SUM(amount) FROM payments WHERE currency='XTR' AND status='paid' AND created_at>?",
                           (week,), 0),
    }
    by_src = {r["source"]: r["c"] for r in await db.all(
        "SELECT source, COUNT(*) AS c FROM events WHERE created_at>? GROUP BY source", (week,))}
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{n['users']}</b> · активных за сутки: {n['u_day']} · за неделю: {n['u_week']}\n"
        f"⭐ Премиум: {n['prem']} · 🛡 доверенных: {n['trusted']} · 🚫 в бане: {n['banned']}\n"
        f"🔔 Подписок на сёла: {n['zones']}\n\n"
        f"🚔 Событий за сутки: <b>{n['e_day']}</b> · за неделю: {n['e_week']} · всего: {n['e_all']}\n"
        f"   за неделю — от водителей: {by_src.get('user', 0) + by_src.get('admin', 0)} · "
        f"из каналов: {by_src.get('channel', 0)} · из чатов: {by_src.get('group', 0)} · фейков: {n['fake']}\n"
        f"🔔 Уведомлений за сутки: {n['alerts']}\n"
        f"🧠 Выучено точек ориентиров: {n['memory']}\n"
        f"💬 Чатов с ботом: {n['groups']}\n\n"
        f"💰 Stars всего: {n['stars']} ⭐ (за неделю: {n['stars_w']})\n"
        f"🌐 Геокодер OSM: {'доступен' if app.geo.online else 'недоступен'}"
    )
    top = await db.all("SELECT user_id, username, reputation, reports_ok FROM users ORDER BY reputation DESC LIMIT 5")
    if top:
        text += "\n\n🏆 <b>Топ помощников:</b>\n" + "\n".join(
            f"{i}. {('@' + esc(u['username'])) if u['username'] else u['user_id']} — {u['reputation']:g} "
            f"({u['reports_ok']} подтв.)" for i, u in enumerate(top, 1))
    b = InlineKeyboardBuilder()
    b.row(*_back())
    await _show(cb, text, b.as_markup())


# ====================================================================== источники
@router.callback_query(AdmCb.filter(F.sec == "src"))
async def cb_sources(cb: CallbackQuery, callback_data: AdmCb, state: FSMContext, app: App):
    p = app.parser
    act = callback_data.act
    if act == "add":
        if not p or not p.available:
            await cb.answer(p.why_unavailable() if p else "Парсер выключен", show_alert=True)
            return
        await state.set_state(AdmSt.src_link)
        await _show(cb, "📡 Пришлите ссылку на канал/группу: <code>@name</code>, <code>https://t.me/name</code> "
                        "или пригласительную ссылку.", InlineKeyboardBuilder().row(*_back("src")).as_markup())
        return
    if act == "tog":
        await app.db.run("UPDATE sources SET is_active=1-is_active WHERE id=?", (callback_data.id,))
    elif act == "del":
        await app.db.run("DELETE FROM sources WHERE id=?", (callback_data.id,))
    elif act == "parse":
        if not p or not p.clients:
            await cb.answer("Нет подключённых аккаунтов-парсеров", show_alert=True)
            return
        await cb.answer("Собираю…")
        stats = await p.catch_up()
        lines = [f"• {esc(k)}: +{v}" for k, v in stats.items()] or ["нет активных источников"]
        await cb.message.answer("📡 <b>Сбор завершён</b>\n" + "\n".join(lines))
    if p:
        await p.refresh()

    rows = await app.db.all("SELECT * FROM sources ORDER BY id DESC")
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(callback_data.page, pages - 1))
    b = InlineKeyboardBuilder()
    b.row(_btn("➕ Добавить", sec="src", act="add"), _btn("🔄 Собрать сейчас", sec="src", act="parse"))
    lines = []
    for s in rows[page * PAGE:(page + 1) * PAGE]:
        icon = "✅" if s["is_active"] else "⏸"
        name = s["title"] or s["link"]
        lines.append(f"{icon} <b>#{s['id']} {esc(name)}</b> — найдено {s['found']}, "
                     f"посл.: {local(s['last_parse'], app.cfg.tz)}"
                     + (f"\n   ⚠️ {esc(s['last_error'][:80])}" if s["last_error"] else ""))
        b.row(_btn(f"{icon} #{s['id']} {name[:18]}", sec="src", act="tog", id=s["id"]),
              _btn("🗑", sec="src", act="del", id=s["id"]))
    if pages > 1:
        b.row(*_pager("src", page, pages))
    b.row(*_back())
    status = "" if p and p.available else f"\n\n⚠️ Парсер недоступен: {esc(p.why_unavailable() if p else '')}"
    await _show(cb, "📡 <b>Источники (каналы и группы)</b>\n\n" + ("\n".join(lines) or "Пока пусто.") + status,
                b.as_markup())


@router.message(AdmSt.src_link, F.text)
async def src_link(message: Message, state: FSMContext, app: App):
    link = message.text.strip()
    try:
        chat_id, title = await app.parser.resolve_source(link)
    except Exception as e:
        await message.answer(f"❌ Не получилось: {esc(e)}\nПопробуйте другую ссылку или /cancel")
        return
    await app.db.insert(
        """INSERT INTO sources(link, title, chat_id, added_by, added_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(link) DO UPDATE SET title=excluded.title, chat_id=excluded.chat_id, is_active=1""",
        (link, title, chat_id, message.from_user.id, now_db()),
    )
    await app.parser.refresh()
    await state.clear()
    b = InlineKeyboardBuilder().row(_btn("📡 К источникам", sec="src"))
    await message.answer(f"✅ Источник добавлен: <b>{esc(title)}</b>\nНовые сообщения обрабатываются в реальном времени.",
                         reply_markup=b.as_markup())


# ====================================================================== аккаунты-парсеры
@router.callback_query(AdmCb.filter(F.sec == "acc"))
async def cb_accounts(cb: CallbackQuery, callback_data: AdmCb, state: FSMContext, app: App):
    p = app.parser
    if callback_data.act == "add":
        if not p or not p.available:
            await cb.answer(p.why_unavailable() if p else "Парсер выключен", show_alert=True)
            return
        await state.set_state(AdmSt.acc_phone)
        await _show(cb, "👥 Введите номер телефона аккаунта (например <code>+79001234567</code>).\n\n"
                        "Используйте отдельный аккаунт, не основной: он будет читать каналы.",
                    InlineKeyboardBuilder().row(*_back("acc")).as_markup())
        return
    if callback_data.act == "del" and p:
        await p.remove_account(callback_data.id)
        await cb.answer("Аккаунт удалён")
    rows = await app.db.all("SELECT * FROM parser_accounts ORDER BY id")
    b = InlineKeyboardBuilder()
    b.row(_btn("➕ Добавить аккаунт", sec="acc", act="add"))
    lines = []
    for a in rows:
        online = "🟢" if p and a["id"] in p.clients else "🔴"
        lines.append(f"{online} #{a['id']} {esc(a['phone'])} — {a['status']}"
                     + (f"\n   ⚠️ {esc(a['last_error'][:80])}" if a["last_error"] else ""))
        b.row(_btn(f"🗑 #{a['id']} {a['phone']}", sec="acc", act="del", id=a["id"]))
    b.row(*_back())
    await _show(cb, "👥 <b>Аккаунты-парсеры</b>\n\n" + ("\n".join(lines) or "Нет аккаунтов."), b.as_markup())


@router.message(AdmSt.acc_phone, F.text)
async def acc_phone(message: Message, state: FSMContext, app: App):
    phone = "+" + "".join(ch for ch in message.text if ch.isdigit())
    if not 11 <= len(phone) <= 16:
        await message.answer("❌ Неверный номер. Пример: +79001234567")
        return
    try:
        await app.parser.login_start(message.from_user.id, phone)
    except Exception as e:
        await message.answer(f"❌ Telegram не отправил код: {esc(e)}")
        await state.clear()
        return
    await state.set_state(AdmSt.acc_code)
    await message.answer("📨 Код отправлен в Telegram этого аккаунта.\n\n"
                         "⚠️ Введите его <b>с пробелами или дефисами</b>, например <code>1 2 3 4 5</code> — "
                         "иначе Telegram посчитает код «пересланным» и аннулирует его.")


@router.message(AdmSt.acc_code, F.text)
async def acc_code(message: Message, state: FSMContext, app: App):
    try:
        res = await app.parser.login_code(message.from_user.id, message.text)
    except Exception as e:
        await message.answer(f"❌ Ошибка входа: {esc(e)}\nНачните заново: /admin → Аккаунты")
        await app.parser.login_cancel(message.from_user.id)
        await state.clear()
        return
    if res == "password":
        await state.set_state(AdmSt.acc_pwd)
        await message.answer("🔐 Включена двухэтапная проверка. Введите облачный пароль "
                             "(сообщение будет сразу удалено):")
        return
    await state.clear()
    await message.answer("✅ Аккаунт подключён и читает источники.")


@router.message(AdmSt.acc_pwd, F.text)
async def acc_pwd(message: Message, state: FSMContext, app: App):
    pwd = message.text
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await app.parser.login_password(message.from_user.id, pwd)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {esc(e)}")
        await app.parser.login_cancel(message.from_user.id)
        await state.clear()
        return
    await state.clear()
    await message.answer("✅ Аккаунт подключён и читает источники.")


# ====================================================================== места
@router.callback_query(AdmCb.filter(F.sec == "places"))
async def cb_places(cb: CallbackQuery, callback_data: AdmCb, app: App):
    rows = await app.db.all("SELECT rowid AS id, * FROM places ORDER BY kind, name")
    if callback_data.act == "del":
        await app.db.run("DELETE FROM places WHERE rowid=?", (callback_data.id,))
        await app.geo.load_places()
        rows = await app.db.all("SELECT rowid AS id, * FROM places ORDER BY kind, name")
    marks = {0: "≈", 1: "🗺", 2: "✅"}
    lines = [f"{marks.get(r['verified'], '')} {'🏘' if r['kind'] == 'village' else '📌'} {esc(r['name'])}"
             f"{' (' + esc(r['aliases'].replace(';', ', ')) + ')' if r['aliases'] else ''}" for r in rows[:40]]
    approx = [p.name for p in app.geo.villages() if not p.verified]
    b = InlineKeyboardBuilder()
    for r in rows:
        if r["kind"] != "village":
            b.row(_btn(f"🗑 {r['name'][:30]}", sec="places", act="del", id=r["id"]))
    b.row(*_back())
    text = ("📍 <b>Места и ориентиры</b>\n✅ задано админом · 🗺 из OSM · ≈ примерно\n\n"
            + ("\n".join(lines) or "Своих ориентиров пока нет.")
            + "\n\n<b>Добавить ориентир:</b> <code>/setplace ТЦ Радуга; радуга, у радуги</code> → отправить точку\n"
              "<b>Уточнить или добавить село:</b> <code>/setvillage Бажинск</code> → отправить точку")
    if approx:
        text += f"\n\n≈ Приблизительные координаты у: {esc(', '.join(approx[:15]))}{'…' if len(approx) > 15 else ''}"
    await _show(cb, text, b.as_markup())


@router.message(Command("setplace", "setvillage"))
async def cmd_setplace(message: Message, command: CommandObject, state: FSMContext, app: App):
    if not command.args:
        await message.answer("Формат: <code>/setplace Название; алиас1, алиас2</code> или "
                             "<code>/setvillage Название</code>")
        return
    name, _, aliases = command.args.partition(";")
    name = name.strip()
    is_village = command.command == "setvillage"
    if is_village:
        p = next((v for v in app.geo.villages() if v.name.lower().replace("ё", "е") == name.lower().replace("ё", "е")), None)
        if p:
            name = p.name  # уточняем существующее, иначе добавляем новое село
    await state.set_state(AdmSt.place_loc)
    await state.update_data(place_name=name, place_aliases=[a.strip() for a in aliases.split(",") if a.strip()],
                            place_kind="village" if is_village else "landmark")
    await message.answer(f"📍 Отправьте точку для «{esc(name)}» (📎 → Геопозиция → выбрать место на карте).")


@router.message(AdmSt.place_loc, F.location)
async def place_loc(message: Message, state: FSMContext, app: App):
    d = await state.get_data()
    loc = message.location
    p = await app.geo.save_place(d["place_name"], loc.latitude, loc.longitude, d["place_kind"], d["place_aliases"])
    await state.clear()
    await message.answer(f"✅ Сохранено: <b>{esc(p.name)}</b> ({loc.latitude:.5f}, {loc.longitude:.5f})\n"
                         "Теперь бот будет узнавать это место в сообщениях.", reply_markup=main_menu())


@router.message(Command("delplace"))
async def cmd_delplace(message: Message, command: CommandObject, app: App):
    key = (command.args or "").strip().lower().replace("ё", "е").replace("-", " ").replace(".", " ")
    n = await app.db.run("DELETE FROM places WHERE key=?", (key,))
    await app.geo.load_places()
    await message.answer("🗑 Удалено" if n else "Не найдено")


# ====================================================================== пользователи
@router.callback_query(AdmCb.filter(F.sec == "bans"))
async def cb_bans(cb: CallbackQuery, callback_data: AdmCb, app: App):
    if callback_data.act == "unban":
        await app.db.run("UPDATE users SET banned=0, ban_reason=NULL, reputation=MAX(reputation, 0) WHERE user_id=?",
                         (callback_data.id,))
        await cb.answer("Разблокирован")
    rows = await app.db.all("SELECT user_id, username, ban_reason FROM users WHERE banned=1 ORDER BY last_seen DESC")
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(callback_data.page, pages - 1))
    b = InlineKeyboardBuilder()
    for u in rows[page * PAGE:(page + 1) * PAGE]:
        b.row(_btn(f"✅ Разбанить {u['username'] or u['user_id']}", sec="bans", act="unban", id=u["user_id"]))
    if pages > 1:
        b.row(*_pager("bans", page, pages))
    b.row(*_back())
    lines = [f"🚫 <code>{u['user_id']}</code> @{esc(u['username'] or '—')} — {esc(u['ban_reason'] or '')}"
             for u in rows[page * PAGE:(page + 1) * PAGE]]
    await _show(cb, f"🚫 <b>Заблокированные: {len(rows)}</b>\n\n" + "\n".join(lines) +
                "\n\nЗабанить: <code>/ban ID причина</code>", b.as_markup())


def _uid(command: CommandObject) -> tuple[int | None, str]:
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        return None, ""
    return int(parts[0]), parts[1] if len(parts) > 1 else ""


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, app: App):
    uid, _ = _uid(command)
    u = await app.db.get_user(uid) if uid else None
    if not u:
        await message.answer("Формат: /user ID (пользователь должен был запускать бота)")
        return
    await message.answer(
        f"👤 <code>{u['user_id']}</code> @{esc(u['username'] or '—')} {esc(u['full_name'] or '')}\n"
        f"С нами с {local(u['created_at'], app.cfg.tz, '%d.%m.%Y')}, был {local(u['last_seen'], app.cfg.tz)}\n"
        f"Репутация {u['reputation']:g} · отчётов {u['reports']} (ок {u['reports_ok']}, фейк {u['reports_fake']})\n"
        f"Голосов {u['votes']} · доверенный: {'да' if u['trusted'] else 'нет'} · бан: {'да' if u['banned'] else 'нет'}\n"
        f"Премиум до: {local(u['premium_until'], app.cfg.tz, '%d.%m.%Y') if u['premium_until'] else '—'}"
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, app: App):
    uid, reason = _uid(command)
    if not uid:
        await message.answer("Формат: /ban ID причина")
        return
    if uid in app.cfg.owner_ids:
        await message.answer("Нельзя забанить владельца")
        return
    await app.db.run(
        "INSERT INTO users(user_id, created_at, banned, ban_reason) VALUES (?, ?, 1, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET banned=1, ban_reason=excluded.ban_reason",
        (uid, now_db(), reason or "Нарушение правил"))
    await message.answer(f"🚫 <code>{uid}</code> заблокирован")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, app: App):
    uid, _ = _uid(command)
    n = await app.db.run("UPDATE users SET banned=0, ban_reason=NULL WHERE user_id=?", (uid,)) if uid else 0
    await message.answer("✅ Разблокирован" if n else "Не найден")


@router.message(Command("trust", "untrust"))
async def cmd_trust(message: Message, command: CommandObject, app: App):
    uid, _ = _uid(command)
    val = 1 if command.command == "trust" else 0
    n = await app.db.run("UPDATE users SET trusted=? WHERE user_id=?", (val, uid)) if uid else 0
    await message.answer(("🛡 Теперь доверенный" if val else "Доверие снято") if n else "Не найден")


@router.message(Command("givepremium"))
async def cmd_givepremium(message: Message, command: CommandObject, app: App):
    uid, rest = _uid(command)
    if not uid or not rest.strip().isdigit():
        await message.answer("Формат: /givepremium ID дней")
        return
    days = int(rest.strip())
    u = await app.db.get_user(uid)
    if not u:
        await message.answer("Пользователь ещё не запускал бота")
        return
    base = max(utcnow(), from_db(u["premium_until"]) or utcnow())
    until = to_db(base + timedelta(days=days))
    await app.db.run("UPDATE users SET premium_until=?, premium_warned=0 WHERE user_id=?", (until, uid))
    await app.db.insert("INSERT INTO payments(user_id, amount, currency, days, status, created_at) "
                        "VALUES (?, 0, 'GIFT', ?, 'gift', ?)", (uid, days, now_db()))
    await app.notifier.send(uid, f"🎁 Вам подарен Премиум на {days} дн.!")
    await message.answer(f"✅ Премиум до {local(until, app.cfg.tz, '%d.%m.%Y')}")


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, app: App):
    uid, charge = _uid(command)
    if not uid or not charge:
        last = await app.db.all("SELECT * FROM payments WHERE currency='XTR' ORDER BY id DESC LIMIT 5")
        lines = [f"{p['user_id']} {p['amount']}⭐ <code>{p['charge_id']}</code> {p['status']}" for p in last]
        await message.answer("Формат: /refund ID charge_id\n\nПоследние платежи:\n" + "\n".join(lines))
        return
    try:
        await app.bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge.strip())
    except Exception as e:
        await message.answer(f"❌ {esc(e)}")
        return
    p = await app.db.one("SELECT days FROM payments WHERE charge_id=?", (charge.strip(),))
    await app.db.run("UPDATE payments SET status='refunded' WHERE charge_id=?", (charge.strip(),))
    if p:
        u = await app.db.get_user(uid)
        if u and u["premium_until"]:
            until = to_db(from_db(u["premium_until"]) - timedelta(days=p["days"]))
            await app.db.run("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))
    await message.answer("✅ Звёзды возвращены")


# ====================================================================== настройки
SETTING_CYCLES = {
    "moderation": (["off", "new", "all"], "Модерация", {"off": "выкл", "new": "новички", "all": "все"}),
    "ttl_factor": (["0.5", "1.0", "1.5", "2.0"], "Время жизни событий ×", {}),
    "max_reports_hour": (["3", "6", "10", "20"], "Отчётов в час на человека", {}),
    "trust_threshold": (["3", "5", "10", "20"], "Репутация для доверия", {}),
    "channel_min_conf": (["0.6", "0.7", "0.8", "0.9"], "Порог уверенности для каналов/чатов", {}),
    "group_parsing": (["1", "0"], "Сбор из чатов", {"1": "вкл", "0": "выкл"}),
    "free_zones": (["1", "2", "3", "5", "100"], "Бесплатных подписок на сёла", {}),
}


@router.callback_query(AdmCb.filter(F.sec == "set"))
async def cb_settings(cb: CallbackQuery, callback_data: AdmCb, app: App):
    keys = list(SETTING_CYCLES)
    if callback_data.act == "cyc":
        key = keys[callback_data.id]
        values = SETTING_CYCLES[key][0]
        cur = app.db.setting(key)
        nxt = values[(values.index(cur) + 1) % len(values)] if cur in values else values[0]
        await app.db.set_setting(key, nxt)
    b = InlineKeyboardBuilder()
    for i, key in enumerate(keys):
        values, title, labels = SETTING_CYCLES[key]
        cur = app.db.setting(key)
        b.row(_btn(f"{title}: {labels.get(cur, cur)}", sec="set", act="cyc", id=i))
    b.row(*_back())
    await _show(cb, "⚙️ <b>Настройки бота</b>\nНажмите, чтобы переключить значение.\n\n"
                    "<b>Модерация:</b> «новички» — отчёты пользователей без подтверждённой истории идут на проверку; "
                    "отчёт публикуется сам, если его независимо подтвердит другой водитель.", b.as_markup())


# ====================================================================== админы
@router.callback_query(AdmCb.filter(F.sec == "admins"))
async def cb_admins(cb: CallbackQuery, callback_data: AdmCb, state: FSMContext, app: App):
    owner = cb.from_user.id in app.cfg.owner_ids
    if callback_data.act in ("add", "rm") and not owner:
        await cb.answer("Только владелец бота", show_alert=True)
        return
    if callback_data.act == "add":
        await state.set_state(AdmSt.add_admin)
        await _show(cb, "Пришлите Telegram ID нового администратора (его можно узнать, переслав его сообщение "
                        "боту @userinfobot).", InlineKeyboardBuilder().row(*_back("admins")).as_markup())
        return
    if callback_data.act == "rm":
        await app.db.run("DELETE FROM admins WHERE user_id=?", (callback_data.id,))
        await cb.answer("Удалён")
    rows = await app.db.all("SELECT a.user_id, u.username FROM admins a LEFT JOIN users u USING(user_id)")
    b = InlineKeyboardBuilder()
    lines = [f"👑 <code>{o}</code> (владелец)" for o in app.cfg.owner_ids]
    for r in rows:
        lines.append(f"🔰 <code>{r['user_id']}</code> @{esc(r['username'] or '—')}")
        if owner:
            b.row(_btn(f"➖ Убрать {r['username'] or r['user_id']}", sec="admins", act="rm", id=r["user_id"]))
    if owner:
        b.row(_btn("➕ Добавить админа", sec="admins", act="add"))
    b.row(*_back())
    await _show(cb, "👑 <b>Администраторы</b>\n\n" + "\n".join(lines), b.as_markup())


@router.message(AdmSt.add_admin, F.text)
async def add_admin(message: Message, state: FSMContext, app: App):
    if message.from_user.id not in app.cfg.owner_ids:
        return
    if not message.text.strip().isdigit():
        await message.answer("Нужен числовой ID")
        return
    uid = int(message.text.strip())
    await app.db.run("INSERT OR IGNORE INTO admins(user_id, added_by, added_at) VALUES (?, ?, ?)",
                     (uid, message.from_user.id, now_db()))
    await state.clear()
    await app.notifier.send(uid, "🔰 Вам выданы права администратора. /admin — панель.")
    await message.answer(f"✅ <code>{uid}</code> теперь администратор")


# ====================================================================== рассылка
@router.callback_query(AdmCb.filter(F.sec == "bcast"))
async def cb_bcast(cb: CallbackQuery, callback_data: AdmCb, state: FSMContext, app: App):
    if callback_data.act == "go":
        data = await state.get_data()
        src = data.get("bcast")
        await state.clear()
        if not src:
            await cb.answer("Сообщение не найдено", show_alert=True)
            return
        await cb.message.edit_text("📢 Рассылка запущена…")
        asyncio.create_task(_broadcast(app, cb.message, src))
        await cb.answer()
        return
    await state.set_state(AdmSt.bcast)
    await _show(cb, "📢 Пришлите сообщение для рассылки (текст, фото, видео — как есть, с форматированием).",
                InlineKeyboardBuilder().row(*_back()).as_markup())


@router.message(AdmSt.bcast)
async def bcast_msg(message: Message, state: FSMContext):
    await state.update_data(bcast=[message.chat.id, message.message_id])
    b = InlineKeyboardBuilder()
    b.row(_btn("📢 Отправить всем", sec="bcast", act="go"), _btn("❌ Отмена", sec="menu"))
    await message.answer("👆 Так будет выглядеть сообщение. Отправляем?", reply_markup=b.as_markup())


async def _broadcast(app: App, status_msg: Message, src: list[int]) -> None:
    users = await app.db.all("SELECT user_id FROM users WHERE banned=0 AND blocked_bot=0")
    sent = failed = 0
    for i, u in enumerate(users, 1):
        try:
            await app.bot.copy_message(u["user_id"], src[0], src[1])
            sent += 1
        except Exception as e:
            failed += 1
            if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                await app.db.run("UPDATE users SET blocked_bot=1 WHERE user_id=?", (u["user_id"],))
            if "retry after" in str(e).lower():
                await asyncio.sleep(5)
        await asyncio.sleep(0.05)
        if i % 100 == 0:
            try:
                await status_msg.edit_text(f"📢 Рассылка: {i}/{len(users)}…")
            except Exception:
                pass
    await status_msg.edit_text(f"✅ <b>Рассылка завершена</b>\nДоставлено: {sent} · ошибок: {failed}")


# ====================================================================== бэкап
async def make_backup(app: App) -> str:
    d = os.path.join(app.cfg.data_dir, "backups")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"dps_{datetime.now(app.cfg.tz):%Y%m%d_%H%M%S}.db")
    await app.db.backup(path)  # консистентная копия через SQLite backup API
    files = sorted(f for f in os.listdir(d) if f.endswith(".db"))
    for f in files[:-14]:
        os.remove(os.path.join(d, f))
    return path


@router.callback_query(AdmCb.filter(F.sec == "backup"))
async def cb_backup(cb: CallbackQuery, app: App):
    await cb.answer("Создаю бэкап…")
    path = await make_backup(app)
    await cb.message.answer_document(FSInputFile(path), caption="📦 Бэкап базы данных")
