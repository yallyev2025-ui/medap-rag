"""Middleware лимитов запросов."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import settings
from db.crud import get_or_create_user, increment_usage, is_limit_exceeded
from db.session import async_session

LIMIT_EXCEEDED_TEXT = f"""Ты использовал {settings.FREE_DAILY_LIMIT} бесплатных запросов сегодня.
Лимит обновится в 00:00.

Хочешь безлимит? → MedAP Premium [ссылка]"""


class LimitsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not event.text or event.text.startswith("/") or event.from_user is None:
            return await handler(event, data)

        async with async_session() as session:
            user = await get_or_create_user(session, event.from_user)
            await session.commit()

            if user.is_banned:
                return None

            if await is_limit_exceeded(session, user):
                await event.answer(LIMIT_EXCEEDED_TEXT)
                return None

        data["db_user"] = user
        result = await handler(event, data)

        async with async_session() as session:
            await increment_usage(session, user.id)
            await session.commit()

        return result
