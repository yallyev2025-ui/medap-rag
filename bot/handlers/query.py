"""Хендлер вопросов: роутер интентов (одна болезнь / дифдиагноз / сочетание /
приветствие), поиск под стратегию и генерация. Плюс согласие на ответ из общих
знаний, когда в загруженных материалах ничего нет, и статус-индикатор «думает»."""

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

ERROR_TEXT = "⚠️ Произошла ошибка, попробуй ещё раз через минуту."
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


async def _set_status(status: Message | None, text: str) -> None:
    """Обновляет статус-сообщение «думает». Молча игнорирует сбои (например, если
    текст не изменился или сообщение удалено)."""
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

    # Статус-индикатор: сразу показываем, что бот жив и работает, и меняем по этапам.
    status = await message.answer("🔎 Определяю тип вопроса…")

    try:
        start_time = time.monotonic()
        intent = await detect_intent(question)

        # Приветствие / small talk — дружелюбный ответ, лимит не тратим.
        if intent == "CHITCHAT":
            await _set_status(status, "💬 Отвечаю…")
            answer = await generate_fallback(question)
            await _clear_status(status)
            await _send_answer(message, answer)
            usage_ctx["count"] = False
            return

        reasoning = source_type == SOURCE_CLINREK and intent in ("DIFFERENTIAL", "MULTI")

        await _set_status(status, "📚 Ищу в материалах…")
        chunks = await retrieve(
            question,
            source_type=source_type,
            subject=subject,
            focus_document=(source_type == SOURCE_CLINREK and not reasoning),
            top_k=settings.DIFFERENTIAL_TOP_K if reasoning else settings.RERANK_TOP_K,
        )

        # В материалах ничего релевантного — спрашиваем согласие на общие знания.
        if not relevant_chunks(chunks):
            await _clear_status(status)
            _pending_general[db_user.id] = question
            await message.answer(NOT_FOUND_ASK, reply_markup=CONSENT_KEYBOARD)
            usage_ctx["count"] = False
            return

        if reasoning:
            await _set_status(status, "🩺 Провожу клинический разбор…")
        else:
            await _set_status(status, "🧠 Готовлю ответ…")

        if intent == "DIFFERENTIAL":
            answer = await generate_differential(question, chunks, source_type)
        elif intent == "MULTI":
            answer = await generate_multi(question, chunks, source_type)
        else:
            answer = await generate_answer(question, chunks, source_type=source_type)

        response_time_ms = int((time.monotonic() - start_time) * 1000)
    except Exception:
        logger.exception("Ошибка при обработке вопроса")
        await _set_status(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return

    await _clear_status(status)
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

    status = await callback.message.answer("🧠 Готовлю ответ из общих знаний…")
    try:
        answer = await generate_fallback(question)
    except Exception:
        logger.exception("Ошибка при ответе из общих знаний")
        await _set_status(status, ERROR_TEXT)
        return

    await _clear_status(status)
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
