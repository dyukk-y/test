"""Сбор отчётов из Telegram-каналов и групп через пользовательские аккаунты (Telethon).

Работает в реальном времени (events.NewMessage) + периодически «догоняет»
пропущенное по last_msg_id. Авторизация аккаунтов — прямо в боте
(телефон → код → пароль 2FA), без консоли.

Telethon — необязательная зависимость: без него и без TG_API_ID/TG_API_HASH
бот работает, просто этот раздел отключён.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING

from .utils import now_db

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, events, errors
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    HAS_TELETHON = True
except ImportError:  # pragma: no cover
    HAS_TELETHON = False


def parse_link(link: str) -> tuple[str, str]:
    """→ (тип, значение): ('invite', hash) | ('public', username)."""
    link = link.strip()
    m = re.search(r"(?:t\.me/|telegram\.me/)(?:joinchat/|\+)([\w-]+)", link)
    if m:
        return "invite", m.group(1)
    m = re.search(r"(?:t\.me/|telegram\.me/|@)([A-Za-z0-9_]{4,})", link)
    if m:
        return "public", m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", link):
        return "public", link
    raise ValueError("Не понимаю ссылку. Пример: @channel или https://t.me/channel")


class ChannelParser:
    def __init__(self, app: "App"):
        self.app = app
        self.clients: dict[int, "TelegramClient"] = {}
        self.pending_logins: dict[int, dict] = {}   # admin_id → {client, phone, hash, account_id}
        self._source_ids: dict[int, int] = {}

    @property
    def available(self) -> bool:
        return HAS_TELETHON and bool(self.app.cfg.api_id and self.app.cfg.api_hash)

    def why_unavailable(self) -> str:
        if not HAS_TELETHON:
            return "Не установлен пакет telethon (pip install telethon)."
        return "Не заданы TG_API_ID и TG_API_HASH в .env (получить на my.telegram.org)."

    def _session_path(self, name: str) -> str:
        d = os.path.join(self.app.cfg.data_dir, "sessions")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    def _client(self, session: str) -> "TelegramClient":
        return TelegramClient(self._session_path(session), self.app.cfg.api_id, self.app.cfg.api_hash,
                              device_model="DPS Guardian", system_version="bot", app_version="3.0")

    # ---------------------------------------------------------------- жизненный цикл
    async def start(self) -> None:
        if not self.available:
            log.info("📡 Парсер каналов отключён: %s", self.why_unavailable())
            return
        for acc in await self.app.db.all("SELECT * FROM parser_accounts WHERE status='active'"):
            await self._connect(acc)
        await self.refresh()

    async def _connect(self, acc) -> bool:
        client = self._client(acc["session"])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await self.app.db.run("UPDATE parser_accounts SET status='error', last_error=? WHERE id=?",
                                      ("Сессия не авторизована — добавьте аккаунт заново", acc["id"]))
                await client.disconnect()
                return False
            self.clients[acc["id"]] = client
            client.add_event_handler(self._on_new_message, events.NewMessage())
            log.info("📡 Аккаунт-парсер #%s подключён", acc["id"])
            return True
        except Exception as e:
            await self.app.db.run("UPDATE parser_accounts SET status='error', last_error=? WHERE id=?",
                                  (str(e)[:200], acc["id"]))
            return False

    async def stop(self) -> None:
        for c in list(self.clients.values()):
            try:
                await c.disconnect()
            except Exception:
                pass
        self.clients.clear()

    async def refresh(self) -> None:
        """Запомнить id чатов источников (нужно для фильтра новых сообщений)."""
        self._source_ids = {}
        for s in await self.app.db.all("SELECT * FROM sources WHERE is_active=1 AND chat_id IS NOT NULL"):
            self._source_ids[s["chat_id"]] = s["id"]

    def any_client(self):
        return next(iter(self.clients.values()), None)

    # ---------------------------------------------------------------- приём сообщений
    async def _on_new_message(self, event) -> None:
        sid = self._source_ids.get(event.chat_id)
        if not sid:
            return
        await self._process(sid, event.message)

    async def _process(self, source_id: int, msg) -> str:
        text = getattr(msg, "message", None) or ""
        res, _ = await self.app.ingest_text(text, "channel", source_ref=f"{source_id}:{msg.id}",
                                            created_at=msg.date)
        await self.app.db.run(
            "UPDATE sources SET last_msg_id=MAX(last_msg_id, ?), last_parse=?, found=found+? WHERE id=?",
            (msg.id, now_db(), 1 if res in ("created", "merged", "cleared") else 0, source_id),
        )
        return res

    async def catch_up(self) -> dict[str, int]:
        """Догнать пропущенные сообщения во всех источниках."""
        stats: dict[str, int] = {}
        client = self.any_client()
        if not client:
            return stats
        for s in await self.app.db.all("SELECT * FROM sources WHERE is_active=1 AND chat_id IS NOT NULL"):
            found = 0
            try:
                msgs = await client.get_messages(s["chat_id"], limit=40, min_id=s["last_msg_id"] or 0)
                for m in reversed(msgs):
                    if await self._process(s["id"], m) in ("created", "merged", "cleared"):
                        found += 1
                await self.app.db.run("UPDATE sources SET last_error=NULL WHERE id=?", (s["id"],))
            except Exception as e:
                await self.app.db.run("UPDATE sources SET last_error=? WHERE id=?", (str(e)[:200], s["id"]))
            stats[s["title"] or s["link"]] = found
            await asyncio.sleep(1)
        return stats

    # ---------------------------------------------------------------- источники
    async def resolve_source(self, link: str) -> tuple[int, str]:
        client = self.any_client()
        if not client:
            raise RuntimeError("Нет подключённых аккаунтов-парсеров. Сначала добавьте аккаунт.")
        kind, value = parse_link(link)
        if kind == "invite":
            try:
                upd = await client(ImportChatInviteRequest(value))
                entity = upd.chats[0]
            except errors.UserAlreadyParticipantError:
                info = await client.get_entity(link)
                entity = info
        else:
            entity = await client.get_entity(value)
            try:
                await client(JoinChannelRequest(entity))  # чтобы получать новые сообщения в реальном времени
            except Exception:
                pass
        from telethon import utils as tutils

        return tutils.get_peer_id(entity), getattr(entity, "title", None) or value

    # ---------------------------------------------------------------- авторизация аккаунтов
    async def login_start(self, admin_id: int, phone: str) -> None:
        session = "acc_" + re.sub(r"\D", "", phone)
        client = self._client(session)
        await client.connect()
        sent = await client.send_code_request(phone)
        self.pending_logins[admin_id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash,
                                         "session": session}

    async def login_code(self, admin_id: int, code: str) -> str:
        st = self.pending_logins[admin_id]
        try:
            await st["client"].sign_in(st["phone"], re.sub(r"\D", "", code), phone_code_hash=st["hash"])
        except errors.SessionPasswordNeededError:
            return "password"
        return await self._login_done(admin_id)

    async def login_password(self, admin_id: int, password: str) -> str:
        st = self.pending_logins[admin_id]
        await st["client"].sign_in(password=password)
        return await self._login_done(admin_id)

    async def _login_done(self, admin_id: int) -> str:
        st = self.pending_logins.pop(admin_id)
        await st["client"].disconnect()
        await self.app.db.run(
            """INSERT INTO parser_accounts(phone, session, status, added_by, added_at) VALUES (?, ?, 'active', ?, ?)
               ON CONFLICT(phone) DO UPDATE SET status='active', session=excluded.session, last_error=NULL""",
            (st["phone"], st["session"], admin_id, now_db()),
        )
        acc = await self.app.db.one("SELECT * FROM parser_accounts WHERE phone=?", (st["phone"],))
        await self._connect(acc)
        return "ok"

    async def login_cancel(self, admin_id: int) -> None:
        st = self.pending_logins.pop(admin_id, None)
        if st:
            try:
                await st["client"].disconnect()
            except Exception:
                pass

    async def remove_account(self, acc_id: int) -> None:
        c = self.clients.pop(acc_id, None)
        if c:
            try:
                await c.log_out()
            except Exception:
                pass
        await self.app.db.run("DELETE FROM parser_accounts WHERE id=?", (acc_id,))
