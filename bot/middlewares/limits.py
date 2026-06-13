"""Middleware лимитов запросов."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import settings
from db.crud import get_or_create_user, increment_usage, is_limit_exceeded
from db.models import User
from db.session import async_session

FREE_LIMIT_TEXT = f"""Ты использовал {settings.FREE_DAILY_LIMIT} бесплатных запросов сегодня.
Лимит обновится в 00:00.

Хочешь больше? → MedAP Premium [ссылка]"""

PREMIUM_LIMIT_TEXT = f"""Ты использовал дневной лимит Premium ({settings.PREMIUM_DAILY_LIMIT} запросов).
Лимит обновится в 00:00."""


def _limit_message(user: User) -> str:
    return PREMIUM_LIMIT_TEXT if user.is_premium else FREE_LIMIT_TEXT


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
                await event.answer(_limit_message(user))
                return None

        data["db_user"] = user
        usage_ctx = {"count": True}
        data["usage_ctx"] = usage_ctx
        result = await handler(event, data)

        if usage_ctx["count"]:
            async with async_session() as session:
                await increment_usage(session, user.id)
                await session.commit()

        return result
