"""Middleware лимитов запросов."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import settings
from db.crud import get_or_create_user, increment_usage, is_limit_exceeded
from db.models import User
from db.session import async_session

FREE_LIMIT_TEXT = f"""Ты израсходовал месячный лимит стоимости бесплатных AI-ответов ({settings.FREE_MONTHLY_BUDGET_RUB:.0f}₽).
Лимит обновится 1 числа.

Хочешь больше? → MedAP Premium [ссылка]"""

PREMIUM_LIMIT_TEXT = f"""Ты израсходовал месячный лимит стоимости AI-ответов Premium ({settings.PREMIUM_MONTHLY_BUDGET_RUB:.0f}₽).
Лимит обновится 1 числа."""


def _limit_message(user: User) -> str:
    return PREMIUM_LIMIT_TEXT if user.is_premium else FREE_LIMIT_TEXT


class LimitsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        # Фото (Vision/Test Solver, §17 ТЗ), документы (§18 ТЗ, этап 4A.5) и голосовые
        # (устная самопроверка, батч 8) тоже расходуют лимит и требуют db_user — раньше
        # middleware пропускал их мимо (event.text пуст у всех), и хендлер остался бы
        # без db_user/usage_ctx в data.
        if event.from_user is None:
            return await handler(event, data)
        if event.text and event.text.startswith("/"):
            return await handler(event, data)
        if not event.text and not event.photo and not event.document and not event.voice:
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
