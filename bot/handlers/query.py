"""Хендлер текстовых вопросов студентов."""

import logging
import time

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import Message

from bot.formatting import split_for_telegram, to_telegram_html
from bot.handlers.menu import send_main_menu
from constants import SOURCE_CLINREK
from db.crud import log_query
from db.models import User
from db.session import async_session
from rag.generator import detect_subject, generate_answer
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "Произошла ошибка, попробуй ещё раз через минуту."

CHOOSE_MODE_TEXT = "Сначала выберите режим — я ищу ответы строго по выбранной базе."


@router.message(F.text & ~F.text.startswith("/"))
async def handle_question(message: Message, db_user: User, usage_ctx: dict) -> None:
    # Режим не выбран — просим выбрать и не тратим лимит на это сообщение.
    if db_user.current_source_type is None:
        await message.answer(CHOOSE_MODE_TEXT)
        await send_main_menu(message)
        usage_ctx["count"] = False
        return

    source_type = db_user.current_source_type
    subject = db_user.current_subject
    # Для клинреков — фокус на одной рекомендации (без мешанины из разных документов).
    focus_document = source_type == SOURCE_CLINREK

    await message.bot.send_chat_action(message.chat.id, "typing")
    question = message.text

    try:
        start_time = time.monotonic()
        chunks = await retrieve(
            question, source_type=source_type, subject=subject, focus_document=focus_document
        )
        answer = await generate_answer(question, chunks, source_type=source_type)
        response_time_ms = int((time.monotonic() - start_time) * 1000)
    except Exception:
        logger.exception("Ошибка при обработке вопроса")
        await message.answer(ERROR_TEXT)
        usage_ctx["count"] = False
        return

    for chunk in split_for_telegram(to_telegram_html(answer)):
        await message.answer(chunk, parse_mode=ParseMode.HTML)

    subject = detect_subject(chunks)
    async with async_session() as session:
        await log_query(session, db_user.id, question, answer, subject, response_time_ms)
        await session.commit()
