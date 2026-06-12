"""Хендлер текстовых вопросов студентов."""

import logging
import time

from aiogram import F, Router
from aiogram.types import Message

from db.crud import log_query
from db.models import User
from db.session import async_session
from rag.generator import detect_subject, generate_answer
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "Произошла ошибка, попробуй ещё раз через минуту."


@router.message(F.text & ~F.text.startswith("/"))
async def handle_question(message: Message, db_user: User) -> None:
    await message.bot.send_chat_action(message.chat.id, "typing")
    question = message.text

    try:
        start_time = time.monotonic()
        chunks = await retrieve(question)
        answer = await generate_answer(question, chunks)
        response_time_ms = int((time.monotonic() - start_time) * 1000)
    except Exception:
        logger.exception("Ошибка при обработке вопроса")
        await message.answer(ERROR_TEXT)
        return

    await message.answer(answer)

    subject = detect_subject(chunks)
    async with async_session() as session:
        await log_query(session, db_user.id, question, answer, subject, response_time_ms)
        await session.commit()
