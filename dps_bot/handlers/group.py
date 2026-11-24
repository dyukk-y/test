"""Работа в группах: бот читает местный чат водителей и сам собирает отчёты."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message, ReactionTypeEmoji

from ..app import App
from ..kinds import kind
from ..utils import esc, now_db

router = Router(name="group")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
log = logging.getLogger(__name__)


@router.my_chat_member()
async def added_to_group(upd: ChatMemberUpdated, app: App):
    if upd.chat.type not in ("group", "supergroup"):
        return
    if upd.new_chat_member.status in ("member", "administrator"):
        await app.db.run(
            "INSERT OR IGNORE INTO groups(chat_id, title, added_at) VALUES (?, ?, ?)",
            (upd.chat.id, upd.chat.title, now_db()),
        )
        try:
            await app.bot.send_message(
                upd.chat.id,
                "👋 Привет! Я буду собирать сообщения о ДПС, камерах и ДТП из этого чата на общую карту.\n"
                "Пишите как обычно: «гайцы у моста», «чисто на трассе».\n"
                "Команды: /dps — что сейчас на дорогах. Админ чата: /dps_off, /dps_on.",
            )
        except Exception:
            pass
    elif upd.new_chat_member.status in ("left", "kicked"):
        await app.db.run("DELETE FROM groups WHERE chat_id=?", (upd.chat.id,))


async def _group_admin(message: Message) -> bool:
    try:
        m = await message.chat.get_member(message.from_user.id)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


@router.message(Command("dps_off"))
async def dps_off(message: Message, app: App):
    if await _group_admin(message):
        await app.db.run("UPDATE groups SET enabled=0 WHERE chat_id=?", (message.chat.id,))
        await message.reply("⏸ Сбор отчётов из этого чата выключен.")


@router.message(Command("dps_on"))
async def dps_on(message: Message, app: App):
    if await _group_admin(message):
        await app.db.run(
            "INSERT INTO groups(chat_id, title, added_at, enabled) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(chat_id) DO UPDATE SET enabled=1", (message.chat.id, message.chat.title, now_db()))
        await message.reply("▶️ Сбор отчётов включён.")


@router.message(Command("dps"))
async def dps_now(message: Message, app: App):
    events = await app.events.active(15)
    if not events:
        await message.reply("✅ Сейчас активных событий нет.")
        return
    from ..utils import ago

    lines = ["📋 <b>Сейчас на дорогах:</b>"]
    for e in events:
        k = kind(e["kind"])
        lines.append(f"{k.emoji} {esc(e['place'] or k.title)} — {ago(e['created_at'])}")
    me = await app.bot.me()
    lines.append(f"\nПодробнее и карта — в @{me.username}")
    await message.reply("\n".join(lines))


@router.message(F.text | F.caption)
async def group_text(message: Message, app: App):
    if app.db.setting("group_parsing") != "1" or not message.from_user or message.from_user.is_bot:
        return
    g = await app.db.one("SELECT enabled FROM groups WHERE chat_id=?", (message.chat.id,))
    if g is None:
        await app.db.run("INSERT OR IGNORE INTO groups(chat_id, title, added_at) VALUES (?, ?, ?)",
                         (message.chat.id, message.chat.title, now_db()))
    elif not g["enabled"]:
        return
    text = message.text or message.caption or ""
    res, e = await app.ingest_text(text, "group", source_ref=f"{message.chat.id}:{message.message_id}",
                                   author_id=message.from_user.id, created_at=message.date)
    if res in ("created", "merged", "cleared"):
        await app.db.run("UPDATE groups SET found=found+1 WHERE chat_id=?", (message.chat.id,))
        emoji = "👀" if res != "cleared" else "👌"
        try:
            await message.react([ReactionTypeEmoji(emoji=emoji)])
        except Exception:
            pass
