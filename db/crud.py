"""CRUD-операции для пользователей, лимитов и истории запросов."""

from datetime import date, datetime, timezone

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Query, Usage, User


def _today() -> date:
    return datetime.now(timezone.utc).date()


async def get_or_create_user(session: AsyncSession, telegram_user: TelegramUser) -> User:
    user = await session.get(User, telegram_user.id)
    if user is None:
        user = User(
            id=telegram_user.id,
            username=telegram_user.username,
            full_name=telegram_user.full_name,
        )
        session.add(user)
        await session.flush()
    return user


async def get_today_usage(session: AsyncSession, user_id: int) -> int:
    stmt = select(Usage.count).where(Usage.user_id == user_id, Usage.date == _today())
    result = await session.execute(stmt)
    return result.scalar_one_or_none() or 0


async def increment_usage(session: AsyncSession, user_id: int) -> None:
    stmt = (
        insert(Usage)
        .values(user_id=user_id, date=_today(), count=1)
        .on_conflict_do_update(
            index_elements=["user_id", "date"],
            set_={"count": Usage.count + 1},
        )
    )
    await session.execute(stmt)


async def is_limit_exceeded(session: AsyncSession, user: User) -> bool:
    if user.is_premium or user.id in settings.ADMIN_IDS:
        return False
    return await get_today_usage(session, user.id) >= settings.FREE_DAILY_LIMIT


async def log_query(
    session: AsyncSession,
    user_id: int,
    question: str,
    answer: str,
    subject: str | None,
    response_time_ms: int | None,
) -> None:
    session.add(
        Query(
            user_id=user_id,
            question=question,
            answer=answer,
            subject=subject,
            response_time_ms=response_time_ms,
        )
    )


async def set_ban(session: AsyncSession, user_id: int, banned: bool) -> None:
    user = await session.get(User, user_id)
    if user is not None:
        user.is_banned = banned


async def set_premium(session: AsyncSession, user_id: int, premium: bool) -> None:
    user = await session.get(User, user_id)
    if user is not None:
        user.is_premium = premium
