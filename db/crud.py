"""CRUD-операции для пользователей, лимитов и истории запросов."""

from datetime import date, datetime, timezone

from aiogram.types import User as TelegramUser
from sqlalchemy import func, select
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


async def set_ban(session: AsyncSession, user_id: int, banned: bool) -> bool:
    user = await session.get(User, user_id)
    if user is None:
        return False
    user.is_banned = banned
    return True


async def set_premium(session: AsyncSession, user_id: int, premium: bool) -> bool:
    user = await session.get(User, user_id)
    if user is None:
        return False
    user.is_premium = premium
    return True


async def get_active_user_ids(session: AsyncSession) -> list[int]:
    result = await session.execute(select(User.id).where(User.is_banned.is_(False)))
    return [row[0] for row in result.all()]


async def get_stats(session: AsyncSession) -> dict:
    today = _today()

    total_users = await session.scalar(select(func.count(User.id))) or 0
    new_today = (
        await session.scalar(select(func.count(User.id)).where(func.date(User.created_at) == today)) or 0
    )
    active_today = (
        await session.scalar(
            select(func.count(func.distinct(Query.user_id))).where(func.date(Query.created_at) == today)
        )
        or 0
    )
    premium_count = await session.scalar(select(func.count(User.id)).where(User.is_premium.is_(True))) or 0

    total_queries = await session.scalar(select(func.count(Query.id))) or 0
    today_queries = (
        await session.scalar(select(func.count(Query.id)).where(func.date(Query.created_at) == today)) or 0
    )

    active_days = await session.scalar(select(func.count(func.distinct(func.date(Query.created_at))))) or 0
    avg_per_day = total_queries / active_days if active_days else 0.0

    top_questions = (
        await session.execute(
            select(Query.question, func.count(Query.id))
            .group_by(Query.question)
            .order_by(func.count(Query.id).desc())
            .limit(10)
        )
    ).all()

    subject_counts = (
        await session.execute(select(Query.subject, func.count(Query.id)).group_by(Query.subject))
    ).all()

    avg_response_time_ms = await session.scalar(select(func.avg(Query.response_time_ms)))

    return {
        "total_users": total_users,
        "new_today": new_today,
        "active_today": active_today,
        "premium_count": premium_count,
        "total_queries": total_queries,
        "today_queries": today_queries,
        "avg_per_day": avg_per_day,
        "top_questions": top_questions,
        "subject_counts": subject_counts,
        "avg_response_time_ms": avg_response_time_ms,
    }
