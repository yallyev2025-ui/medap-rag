"""Хендлер текстовых вопросов студентов."""

import logging

from aiogram import F, Router
from aiogram.types import Message

from rag.generator import generate_answer
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "Произошла ошибка, попробуй ещё раз через минуту."


@router.message(F.text & ~F.text.startswith("/"))
async def handle_question(message: Message) -> None:
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        chunks = await retrieve(message.text)
        answer = await generate_answer(message.text, chunks)
    except Exception:
        logger.exception("Ошибка при обработке вопроса")
        await message.answer(ERROR_TEXT)
        return

    await message.answer(answer)
