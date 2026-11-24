"""Фейковый Telegram API: бот работает по-настоящему, но запросы никуда не уходят."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, MessageId



class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        ret = getattr(method, "__returning__", None)
        name = getattr(ret, "__name__", str(ret))
        if name == "Message":
            chat_id = getattr(method, "chat_id", 0) or 0
            return Message(message_id=next(self._ids), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else 0, type="private"),
                           text=getattr(method, "text", None) or getattr(method, "caption", None) or "")
        if name in ("User", "BotFullInfo"):
            return ret.model_construct(id=1, is_bot=True, first_name="DPS", username="dps_test_bot")
        if name == "MessageId":
            return MessageId(message_id=next(self._ids))
        if name == "bool":
            return True
        return None

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    # удобные выборки
    def texts(self, chat_id=None) -> list[str]:
        out = []
        for m in self.calls:
            t = getattr(m, "text", None) or getattr(m, "caption", None)
            if t and (chat_id is None or getattr(m, "chat_id", None) == chat_id):
                out.append(t)
        return out

    def of(self, name: str) -> list:
        return [m for m in self.calls if type(m).__name__ == name]


@pytest.fixture
def fake_bot():
    session = FakeSession()
    bot = Bot("123456:TEST", session=session)
    return bot, session
