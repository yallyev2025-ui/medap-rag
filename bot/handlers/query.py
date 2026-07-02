"""Хендлер вопросов: роутер интентов (одна болезнь / дифдиагноз / сочетание /
приветствие), поиск под стратегию и генерация. Плюс согласие на ответ из общих
знаний, когда в загруженных материалах ничего нет."""

import logging
import time

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.formatting import split_for_telegram, to_telegram_html
from bot.handlers.menu import send_main_menu
from config import settings
from constants import SOURCE_CLINREK
from db.crud import get_or_create_user, increment_usage, log_query
from db.models import User
from db.session import async_session
from rag.generator import (
    detect_intent,
    detect_subject,
    generate_answer,
    generate_differential,
    generate_fallback,
    generate_multi,
    relevant_chunks,
)
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "Произошла ошибка, попробуй ещё раз через минуту."
CHOOSE_MODE_TEXT = "Сначала выберите режим — я ищу ответы строго по выбранной базе."
NOT_FOUND_ASK = (
    "В загруженных материалах по этому вопросу ничего нет.\n"
    "Ответить из общих знаний ИИ? Это не официальный источник — перепроверьте."
)

CONSENT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Да, из общих знаний", callback_data="genk:yes"),
            InlineKeyboardButton(text="Нет", callback_data="genk:no"),
        ]
    ]
)

# Вопрос, ожидающий согласия на ответ из общих знаний (по пользователю).
# In-memory: сбрасывается при рестарте — для транзиентного согласия это приемлемо.
_pending_general: dict[int, str] = {}


async def _send_answer(message: Message, answer: str) -> None:
    for part in split_for_telegram(to_telegram_html(answer)):
        await message.answer(part, parse_mode=ParseMode.HTML)


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
    question = message.text

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        start_time = time.monotonic()
        intent = await detect_intent(question)

        # Приветствие / small talk — дружелюбный ответ, лимит не тратим.
        if intent == "CHITCHAT":
            answer = await generate_fallback(question)
            await _send_answer(message, answer)
            usage_ctx["count"] = False
            return

        # Дифдиагноз/сочетание болезней (только клинреки) — широкий поиск по многим
        # рекомендациям. Иначе — фокус на одной рекомендации (клинреки) или обычный
        # поиск (учебники).
        reasoning = source_type == SOURCE_CLINREK and intent in ("DIFFERENTIAL", "MULTI")
        chunks = await retrieve(
            question,
            source_type=source_type,
            subject=subject,
            focus_document=(source_type == SOURCE_CLINREK and not reasoning),
            top_k=settings.DIFFERENTIAL_TOP_K if reasoning else settings.RERANK_TOP_K,
        )

        # В материалах ничего релевантного — спрашиваем согласие на общие знания.
        if not relevant_chunks(chunks):
            _pending_general[db_user.id] = question
            await message.answer(NOT_FOUND_ASK, reply_markup=CONSENT_KEYBOARD)
            usage_ctx["count"] = False
            return

        if intent == "DIFFERENTIAL":
            answer = await generate_differential(question, chunks, source_type)
        elif intent == "MULTI":
            answer = await generate_multi(question, chunks, source_type)
        else:
            answer = await generate_answer(question, chunks, source_type=source_type)

        response_time_ms = int((time.monotonic() - start_time) * 1000)
    except Exception:
        logger.exception("Ошибка при обработке вопроса")
        await message.answer(ERROR_TEXT)
        usage_ctx["count"] = False
        return

    await _send_answer(message, answer)

    subject_used = detect_subject(chunks)
    async with async_session() as session:
        await log_query(session, db_user.id, question, answer, subject_used, response_time_ms)
        await session.commit()


@router.callback_query(F.data == "genk:yes")
async def consent_general_yes(callback: CallbackQuery) -> None:
    await callback.answer()
    question = _pending_general.pop(callback.from_user.id, None)
    try:
        await callback.message.edit_reply_markup()  # убрать кнопки
    except Exception:
        pass

    if not question:
        await callback.message.answer("Запрос устарел — задайте вопрос заново.")
        return

    await callback.message.bot.send_chat_action(callback.message.chat.id, "typing")
    try:
        answer = await generate_fallback(question)
    except Exception:
        logger.exception("Ошибка при ответе из общих знаний")
        await callback.message.answer(ERROR_TEXT)
        return

    await _send_answer(callback.message, answer)

    # Это полноценный ответ — засчитываем в дневной лимит (middleware колбэки не ловит).
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await increment_usage(session, user.id)
        await session.commit()


@router.callback_query(F.data == "genk:no")
async def consent_general_no(callback: CallbackQuery) -> None:
    await callback.answer()
    _pending_general.pop(callback.from_user.id, None)
    try:
        await callback.message.edit_reply_markup()
    except Exception:
        pass
    await callback.message.answer(
        "Хорошо — отвечаю только по загруженным материалам. Задайте другой вопрос."
    )
