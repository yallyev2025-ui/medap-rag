"""Хендлер вопросов: лёгкая память диалога, роутер интентов (одна болезнь /
дифдиагноз / сочетание / приветствие), поиск под стратегию и генерация. Плюс
согласие на общие знания, режим «Разбор по симптомам» и статус-индикатор «думает»."""

import logging
import time

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.formatting import split_for_telegram, to_telegram_html
from bot.handlers.menu import CLINREK_PREMIUM_TEXT, has_clinrek_access, send_main_menu
from config import settings
from constants import SOURCE_CLINREK
from db.crud import (
    get_or_create_user,
    get_recent_turns,
    increment_usage,
    log_query,
    reset_chat,
)
from db.models import User
from db.session import async_session
from rag.generator import (
    build_history_messages,
    detect_intent,
    detect_subject,
    generate_answer,
    generate_differential,
    generate_fallback,
    generate_multi,
    relevant_chunks,
    rewrite_query,
)
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

router = Router()

# Сколько последних обменов держим в лёгкой памяти диалога.
HISTORY_TURNS = 3

ERROR_TEXT = "⚠️ Произошла ошибка, попробуй ещё раз через минуту."
CHOOSE_MODE_TEXT = "Сначала выберите режим — я ищу ответы строго по выбранной базе."
NEW_CHAT_TEXT = "🆕 Начал новую тему — предыдущий разговор забыт."
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
_pending_general: dict[int, str] = {}


async def _send_answer(message: Message, answer: str) -> None:
    for part in split_for_telegram(to_telegram_html(answer)):
        await message.answer(part, parse_mode=ParseMode.HTML)


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


@router.message(Command("new"))
async def cmd_new(message: Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await reset_chat(session, user.id)
        await session.commit()
    await message.answer(NEW_CHAT_TEXT)


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

    # Клинреки — только премиум/админ (режим мог быть выбран до отзыва премиума).
    if source_type == SOURCE_CLINREK and not has_clinrek_access(db_user):
        await message.answer(CLINREK_PREMIUM_TEXT)
        await send_main_menu(message)
        usage_ctx["count"] = False
        return

    status = await message.answer("🔎 Определяю тип вопроса…")

    try:
        start_time = time.monotonic()

        # Лёгкая память: последние обмены текущего чата.
        async with async_session() as session:
            turns = await get_recent_turns(
                session, db_user.id, db_user.chat_started_at, HISTORY_TURNS
            )

        # Уточняющий вопрос («а дозы?») превращаем в самостоятельный запрос для поиска.
        search_query = await rewrite_query(question, turns)
        intent = await detect_intent(search_query)

        # Приветствие / small talk — дружелюбный ответ, лимит не тратим.
        if intent == "CHITCHAT":
            await _set_status(status, "💬 Отвечаю…")
            answer = await generate_fallback(question)
            await _clear_status(status)
            await _send_answer(message, answer)
            usage_ctx["count"] = False
            return

        # Явный режим «Разбор по симптомам» (кнопкой) — всегда дифдиагноз.
        if source_type == SOURCE_CLINREK and db_user.clinrek_symptom_mode:
            intent = "DIFFERENTIAL"

        reasoning = source_type == SOURCE_CLINREK and intent in ("DIFFERENTIAL", "MULTI")

        await _set_status(status, "📚 Ищу в материалах…")
        chunks = await retrieve(
            search_query,
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

        history = build_history_messages(turns)

        if reasoning:
            await _set_status(status, "🩺 Провожу клинический разбор…")
        else:
            await _set_status(status, "🧠 Готовлю ответ…")

        if intent == "DIFFERENTIAL":
            answer = await generate_differential(question, chunks, source_type, history)
        elif intent == "MULTI":
            answer = await generate_multi(question, chunks, source_type, history)
        else:
            answer = await generate_answer(question, chunks, source_type, history)

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
        await callback.message.edit_reply_markup()
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
