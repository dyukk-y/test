"""Мидлвари: регистрация пользователя, баны, антифлуд."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class UserMiddleware(BaseMiddleware):
    """Создаёт/обновляет пользователя, прокидывает `user` и `is_admin`, блокирует забаненных."""

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        app = data["app"]
        from_user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        private = chat is None or chat.type == "private"
        if from_user is None or from_user.is_bot:
            return await handler(event, data)

        if private:
            user = await app.db.upsert_user(from_user.id, from_user.username, from_user.full_name)
        else:
            user = await app.db.get_user(from_user.id)
        is_admin = await app.is_admin(from_user.id)
        if user is not None and user["banned"] and not is_admin:
            if private and isinstance(event, Message):
                await event.answer("🚫 Вы заблокированы в этом боте." +
                                   (f"\nПричина: {user['ban_reason']}" if user["ban_reason"] else ""))
            elif isinstance(event, CallbackQuery):
                await event.answer("🚫 Вы заблокированы", show_alert=True)
            return None
        data["user"] = user
        data["is_admin"] = is_admin
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Не больше N событий в секунду от одного пользователя (защита от флуда кнопками)."""

    def __init__(self, rate: float = 0.35):
        self.rate = rate
        self.last: dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        u = getattr(event, "from_user", None)
        if u is not None:
            now = time.monotonic()
            if now - self.last.get(u.id, 0) < self.rate:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Не так быстро")
                return None
            self.last[u.id] = now
            if len(self.last) > 50_000:
                self.last.clear()
        return await handler(event, data)
