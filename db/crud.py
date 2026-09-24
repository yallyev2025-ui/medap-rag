"""CRUD-операции для пользователей, лимитов и истории запросов."""

from datetime import date, datetime, timezone

from aiogram.types import User as TelegramUser
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from constants import SOURCE_TEXTBOOK
from db.models import AIUsageEvent, AnswerLog, Book, BookChunk, EvalCase, PromptVersion, Query, Usage, User


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


def daily_limit_for(user: User) -> int | None:
    """УСТАРЕЛО (батч 10): дневной лимит по количеству больше не используется
    для gate — см. monthly_budget_rub_for()/is_limit_exceeded() ниже. Оставлено
    для обратной совместимости (FREE_DAILY_LIMIT/PREMIUM_DAILY_LIMIT в конфиге
    больше не читаются нигде, кроме этой функции)."""
    if user.id in settings.ADMIN_IDS:
        return None
    if user.is_premium:
        return settings.PREMIUM_DAILY_LIMIT
    return settings.FREE_DAILY_LIMIT


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_spend_rub(session: AsyncSession, user_id: int) -> float:
    """Сумма AI-расхода (₽) пользователя Telegram за текущий календарный месяц
    (батч 10) — заменяет дневной счётчик количества запросов: точнее отражает
    реальную стоимость, т.к. голос/фото стоят больше обычного текстового вопроса."""
    stmt = select(func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0)).where(
        AIUsageEvent.user_id == f"telegram:{user_id}",
        AIUsageEvent.created_at >= _month_start(),
    )
    result = await session.execute(stmt)
    return float(result.scalar_one())


def monthly_budget_rub_for(user: User) -> float | None:
    """Месячный ₽-бюджет пользователя. None = безлимит (только админы)."""
    if user.id in settings.ADMIN_IDS:
        return None
    return settings.PREMIUM_MONTHLY_BUDGET_RUB if user.is_premium else settings.FREE_MONTHLY_BUDGET_RUB


async def is_limit_exceeded(session: AsyncSession, user: User) -> bool:
    budget = monthly_budget_rub_for(user)
    if budget is None:
        return False
    return await month_spend_rub(session, user.id) >= budget


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


async def set_user_selection(
    session: AsyncSession,
    user_id: int,
    source_type: str | None,
    subject: str | None,
    symptom_mode: bool = False,
) -> None:
    """Сохраняет выбранный режим (source_type), предмет/категорию и режим симптомов."""
    user = await session.get(User, user_id)
    if user is None:
        return
    user.current_source_type = source_type
    user.current_subject = subject
    user.clinrek_symptom_mode = symptom_mode


async def set_active_document(session: AsyncSession, user_id: int, document_id: str | None) -> None:
    """Переключает режим «спрашиваю по своему документу» (§18, этап 4A.5). document_id=None — выход из режима."""
    user = await session.get(User, user_id)
    if user is None:
        return
    user.current_document_id = document_id


async def reset_chat(session: AsyncSession, user_id: int) -> None:
    """«Новая тема»: сдвигает границу — прошлые сообщения перестают учитываться в памяти."""
    user = await session.get(User, user_id)
    if user is not None:
        user.chat_started_at = datetime.now(timezone.utc)


async def get_recent_turns(
    session: AsyncSession, user_id: int, since: datetime | None, limit: int
) -> list[tuple[str, str]]:
    """Последние ходы диалога (вопрос, ответ) в хронологическом порядке — лёгкая память.
    Учитываются только сообщения после начала текущего чата (since)."""
    stmt = select(Query.question, Query.answer).where(Query.user_id == user_id)
    if since is not None:
        stmt = stmt.where(Query.created_at >= since)
    stmt = stmt.order_by(Query.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [(r.question, r.answer) for r in reversed(rows)]


async def get_textbook_subjects(session: AsyncSession) -> list[str]:
    """Уникальные предметы реально загруженных учебников — для динамического меню."""
    stmt = (
        select(Book.subject)
        .where(Book.source_type == SOURCE_TEXTBOOK)
        .distinct()
        .order_by(Book.subject)
    )
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


async def list_books(session: AsyncSession) -> list[Book]:
    result = await session.execute(select(Book).order_by(Book.id))
    return list(result.scalars().all())


async def delete_book(session: AsyncSession, book_id: int) -> str | None:
    """Удаляет книгу и все её чанки. Возвращает название удалённой книги или None."""
    book = await session.get(Book, book_id)
    if book is None:
        return None
    title = book.title
    await session.execute(delete(BookChunk).where(BookChunk.book_id == book_id))
    await session.delete(book)
    return title


async def update_book(
    session: AsyncSession,
    book_id: int,
    *,
    title: str | None = None,
    author: str | None = None,
    subject: str | None = None,
    section: str | None = None,
    topic: str | None = None,
    edition: str | None = None,
    year: int | None = None,
    authority_level: str | None = None,
    verification_status: str | None = None,
    language: str | None = None,
    status: str | None = None,
) -> Book | None:
    """Правит метаданные источника. Возвращает обновлённую книгу или None,
    если такой книги нет.

    BookChunk хранит собственные копии большинства этих полей (денормализация
    для фильтрации поиска одним WHERE без JOIN — см. db/models.py), поэтому при
    правке синхронизируем их той же операцией: иначе поиск и citations ещё
    долго показывали бы старые значения, хотя в списке книг уже новые.
    status — исключение: это состояние жизненного цикла самого источника
    (draft/production/disabled/archived), retrieval фильтрует по нему через
    JOIN на books, а не по копии в каждом чанке.
    """
    book = await session.get(Book, book_id)
    if book is None:
        return None

    chunk_fields: dict[str, str | int] = {}
    if title is not None and title.strip():
        chunk_fields["title"] = title.strip()
    if author is not None:
        chunk_fields["author"] = author.strip()
    if subject is not None and subject.strip():
        # Нижний регистр по тем же причинам, что и при загрузке (см.
        # app/admin/routes.py upload_source): коды предметов в системе строго
        # lowercase, иначе поиск считает это другим предметом.
        chunk_fields["subject"] = subject.strip().lower()
    if section is not None:
        chunk_fields["section"] = section.strip() or None
    if topic is not None:
        chunk_fields["topic"] = topic.strip() or None
    if edition is not None:
        chunk_fields["edition"] = edition.strip() or None
    if year is not None:
        chunk_fields["year"] = year
    if authority_level is not None and authority_level.strip():
        chunk_fields["authority_level"] = authority_level.strip()
    if verification_status is not None and verification_status.strip():
        chunk_fields["verification_status"] = verification_status.strip()
    if language is not None and language.strip():
        chunk_fields["language"] = language.strip()

    book_fields: dict[str, str | int] = dict(chunk_fields)
    if status is not None and status.strip():
        book_fields["status"] = status.strip()

    if not book_fields:
        return book

    for key, value in book_fields.items():
        setattr(book, key, value)
    if chunk_fields:
        await session.execute(
            update(BookChunk).where(BookChunk.book_id == book_id).values(**chunk_fields)
        )
    return book


async def get_active_user_ids(session: AsyncSession) -> list[int]:
    result = await session.execute(select(User.id).where(User.is_banned.is_(False)))
    return [row[0] for row in result.all()]


async def get_base_stats(session: AsyncSession) -> dict:
    """Статистика базы знаний для админа: чанки и книги по source_type и subject."""
    chunk_rows = (
        await session.execute(
            select(BookChunk.source_type, BookChunk.subject, func.count(BookChunk.id))
            .group_by(BookChunk.source_type, BookChunk.subject)
            .order_by(BookChunk.source_type, BookChunk.subject)
        )
    ).all()
    books_rows = (
        await session.execute(
            select(Book.source_type, func.count(Book.id)).group_by(Book.source_type)
        )
    ).all()
    return {
        "chunks_by_subject": chunk_rows,  # (source_type, subject, count)
        "books_by_source": {src: cnt for src, cnt in books_rows},
    }


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


# --- Answer Inspector (§8 дополнения к ТЗ) --------------------------------------


async def list_answer_logs(
    session: AsyncSession,
    limit: int = 50,
    verified: bool | None = None,
    only_flagged: bool = False,
) -> list[AnswerLog]:
    stmt = select(AnswerLog).order_by(AnswerLog.id.desc()).limit(limit)
    if verified is not None:
        stmt = stmt.where(AnswerLog.verified.is_(verified))
    if only_flagged:
        stmt = stmt.where(AnswerLog.feedback_reason.is_not(None))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def set_answer_feedback(
    session: AsyncSession, answer_id: int, reason: str, note: str
) -> AnswerLog | None:
    log = await session.get(AnswerLog, answer_id)
    if log is None:
        return None
    log.feedback_reason = reason or None
    log.feedback_note = note.strip() or None
    return log


# --- Prompts & Policies (раздел 10 дополнения к ТЗ, упрощённая версия) ---------


async def current_prompt(session: AsyncSession, key: str) -> PromptVersion | None:
    stmt = (
        select(PromptVersion)
        .where(PromptVersion.prompt_key == key, PromptVersion.status == "production")
        .order_by(PromptVersion.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def list_prompt_versions(session: AsyncSession, key: str) -> list[PromptVersion]:
    stmt = select(PromptVersion).where(PromptVersion.prompt_key == key).order_by(PromptVersion.id.desc())
    return list((await session.execute(stmt)).scalars().all())


async def publish_prompt(session: AsyncSession, key: str, content: str, created_by: str) -> PromptVersion:
    """Публикует новую production-версию промпта и переводит предыдущую в
    archived — ничего не удаляется, полная история для отката."""
    await session.execute(
        update(PromptVersion)
        .where(PromptVersion.prompt_key == key, PromptVersion.status == "production")
        .values(status="archived")
    )
    version = PromptVersion(prompt_key=key, content=content, status="production", created_by=created_by)
    session.add(version)
    await session.flush()
    return version


# --- Evals (раздел 13 дополнения к ТЗ) -----------------------------------------


async def list_eval_cases(session: AsyncSession) -> list[EvalCase]:
    result = await session.execute(select(EvalCase).order_by(EvalCase.id))
    return list(result.scalars().all())


async def create_eval_case(session: AsyncSession, **fields) -> EvalCase:
    case = EvalCase(**fields)
    session.add(case)
    await session.flush()
    return case
