"""Vision/Test Solver для Telegram (§17, §53.2 ТЗ, этап 4A.4): студент шлёт
фото/скрин теста, бот распознаёт вопрос и решает его через тот же
Evidence-конвейер, что и обычные текстовые вопросы (app/workflows/vision.py)."""

import logging

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import Message

from app.observability.context import request_context
from app.workflows.vision import solve_from_image
from bot.formatting import split_for_telegram, to_telegram_html
from bot.handlers.menu import send_main_menu
from bot.handlers.panel import explain_problem, prepare_scope
from config import settings
from constants import SCOPE_DOCUMENTS, SOURCE_TEXTBOOK
from db.crud import get_or_create_user, is_limit_exceeded, log_query, user_scope
from db.models import User
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

CHOOSE_MODE_TEXT = "Сначала выберите режим — я ищу ответы строго по выбранной базе."
ERROR_TEXT = "⚠️ Не получилось разобрать фото, попробуй ещё раз через минуту."


async def _set_status(status: Message | None, text: str) -> None:
    if status is None:
        return
    try:
        await status.edit_text(text)
    except Exception:
        pass


async def _clear_status(status: Message | None) -> None:
    if status is None:
        return
    try:
        await status.delete()
    except Exception:
        pass


@router.message(F.photo)
async def handle_photo(message: Message, db_user: User, usage_ctx: dict) -> None:
    # Источник — по режиму панели (учебники / мои документы / вместе), как у текстовых вопросов.
    mode = user_scope(db_user)
    scope, problem = await prepare_scope(db_user)
    if problem is not None:
        await explain_problem(message, db_user, problem)
        usage_ctx["count"] = False
        return
    if mode != SCOPE_DOCUMENTS and db_user.current_source_type is None:
        await message.answer(CHOOSE_MODE_TEXT)
        await send_main_menu(message)
        usage_ctx["count"] = False
        return

    async def budget_ok() -> bool:
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user)
            return not await is_limit_exceeded(session, user)

    status = await message.answer("📷 Распознаю фото…")

    try:
        photo = message.photo[-1]
        buffer = await message.bot.download(photo)
        image_bytes = buffer.read()

        with request_context(
            user_id=f"telegram:{db_user.id}", channel="telegram", workflow="VISION_EXTRACT"
        ):
            result = await solve_from_image(
                image_bytes,
                source_type=db_user.current_source_type or SOURCE_TEXTBOOK,
                subject=db_user.current_subject if mode != SCOPE_DOCUMENTS else None,
                scope=scope,
                budget_ok=budget_ok,
            )
    except Exception:
        logger.exception("Ошибка при разборе фото")
        await _set_status(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return

    if result.service_error:
        # Сбой сервиса, а не фото: переснимать не нужно. Настоящую причину видит админ.
        text = "⚠️ Сервис распознавания фото сейчас недоступен — дело не в снимке, попробуй позже."
        if db_user.id in settings.ADMIN_IDS:
            text += f"\n\nПричина (видно только админу): {result.service_error}"
        await _set_status(status, text)
        usage_ctx["count"] = False
        return

    if result.needs_retake:
        issue = result.extraction.quality_issue
        detail = f" ({issue})" if issue else ""
        await _set_status(
            status,
            f"📷 Не могу уверенно прочитать фото{detail}. Переснимите крупно, при хорошем "
            "освещении, чтобы текст вопроса был полностью виден.",
        )
        usage_ctx["count"] = False
        return

    await _clear_status(status)
    for part in split_for_telegram(to_telegram_html(result.answer or "")):
        await message.answer(part, parse_mode=ParseMode.HTML)

    # Память диалога: «разбери №3 подробнее» после скрина должно знать, что на нём было.
    questions = result.extraction.questions
    if questions and result.answer:
        recognized = "[фото] " + " ".join(f"{q.number}) {q.question}" for q in questions)
        async with async_session() as session:
            await log_query(session, db_user.id, recognized[:2000], result.answer, None, None)
            await session.commit()
