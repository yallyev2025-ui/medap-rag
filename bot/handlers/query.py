"""Хендлер вопросов: лёгкая память диалога, роутер интентов (одна болезнь /
дифдиагноз / сочетание / приветствие), поиск под стратегию и генерация. Плюс
согласие на общие знания, режим «Разбор по симптомам» и статус-индикатор «думает»."""

import logging
import time

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.observability.context import request_context
from app.workflows.ask import ask
from app.workflows.pubmed import search_pubmed
from app.workflows.web_search import search_and_answer
from bot.formatting import split_for_telegram, to_telegram_html
from bot.handlers.menu import CLINREK_PREMIUM_TEXT, has_clinrek_access, send_main_menu
from bot.handlers.user_documents import handle_document_question
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
from rag.generator import generate_fallback

logger = logging.getLogger(__name__)

router = Router()

# Сколько последних обменов держим в лёгкой памяти диалога.
HISTORY_TURNS = 3

ERROR_TEXT = "⚠️ Произошла ошибка, попробуй ещё раз через минуту."
CHOOSE_MODE_TEXT = "Сначала выберите режим — я ищу ответы строго по выбранной базе."
NEW_CHAT_TEXT = "🆕 Начал новую тему — предыдущий разговор забыт."
NOT_FOUND_ASK = (
    "В загруженных материалах по этому вопросу ничего нет. Как ответить?\n"
    "Учти: общие знания ИИ, интернет и PubMed — не официальные источники MedAP, перепроверяйте."
)

CONSENT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Общие знания", callback_data="genk:yes"),
            InlineKeyboardButton(text="🌐 Интернет", callback_data="web:yes"),
            InlineKeyboardButton(text="🔬 PubMed", callback_data="pubmed:yes"),
        ],
        [
            InlineKeyboardButton(text="Нет", callback_data="genk:no"),
        ],
    ]
)

# Вопрос, ожидающий согласия на ответ из общих знаний (по пользователю).
_pending_general: dict[int, str] = {}


class SearchStates(StatesGroup):
    """Прямой вход в веб-поиск/PubMed по команде — не только когда бот сам не
    нашёл ответ (CONSENT_KEYBOARD ниже), но и когда студент сразу хочет искать."""

    waiting_web_query = State()
    waiting_pubmed_query = State()


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


async def _run_web_search(message: Message, user_id: int, query: str) -> None:
    status = await message.answer("🌐 Ищу в интернете…")
    try:
        with request_context(user_id=f"telegram:{user_id}", channel="telegram", workflow="WEB_SEARCH"):
            result = await search_and_answer(query)
    except Exception:
        logger.exception("Ошибка при веб-поиске")
        await _set_status(status, ERROR_TEXT)
        return

    if result.error:
        await _set_status(status, f"⚠️ {result.error}")
        return

    await _clear_status(status)
    answer = result.answer
    if result.sources:
        links = "\n".join(f"— {s['title'] or s['url']} ({s['url']})" for s in result.sources)
        answer = f"{answer}\n\nИсточники:\n{links}"
    await _send_answer(message, answer)


async def _run_pubmed_search(message: Message, user_id: int, query: str) -> None:
    status = await message.answer("🔬 Ищу в PubMed…")
    try:
        with request_context(user_id=f"telegram:{user_id}", channel="telegram", workflow="PUBMED_SEARCH"):
            result = await search_pubmed(query)
    except Exception:
        logger.exception("Ошибка при поиске в PubMed")
        await _set_status(status, ERROR_TEXT)
        return

    if result.error:
        await _set_status(status, f"⚠️ {result.error}")
        return

    await _clear_status(status)
    answer = result.answer
    if result.articles:
        links = "\n".join(f"— {a.title} ({a.journal or '—'}, {a.year or '—'}): {a.url}" for a in result.articles)
        answer = f"{answer}\n\nСтатьи:\n{links}"
    await _send_answer(message, answer)


@router.message(Command("new"))
async def cmd_new(message: Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await reset_chat(session, user.id)
        await session.commit()
    await message.answer(NEW_CHAT_TEXT)


@router.message(Command("websearch"))
async def cmd_websearch(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_web_query)
    await message.answer("Что найти в интернете?")


@router.message(SearchStates.waiting_web_query, F.text)
async def websearch_run(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    await state.clear()
    await _run_web_search(message, db_user.id, message.text)


@router.message(Command("pubmed"))
async def cmd_pubmed(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_pubmed_query)
    await message.answer("Что искать в PubMed? (на английском — так PubMed индексирует статьи)")


@router.message(SearchStates.waiting_pubmed_query, F.text)
async def pubmed_run(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    await state.clear()
    await _run_pubmed_search(message, db_user.id, message.text)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_question(message: Message, db_user: User, usage_ctx: dict) -> None:
    # Режим «свой документ» (§18) приоритетнее выбора учебник/клинрек.
    if db_user.current_document_id is not None:
        await handle_document_question(message, db_user, usage_ctx)
        return

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

        await _set_status(status, "📚 Ищу в материалах и готовлю ответ…")
        # Весь конвейер (роутинг интента, переписывание запроса, поиск, генерация)
        # живёт в общем workflow — том же, что обслуживает образовательный сайт
        # через /v1. Здесь остаётся только телеграмный UI.
        with request_context(user_id=f"telegram:{db_user.id}", channel="telegram"):
            result = await ask(
                question,
                source_type=source_type,
                subject=subject,
                turns=turns,
                symptom_mode=db_user.clinrek_symptom_mode,
            )

        # Приветствие / small talk — дружелюбный ответ, лимит не тратим.
        if result.intent == "CHITCHAT":
            await _clear_status(status)
            await _send_answer(message, result.answer)
            usage_ctx["count"] = False
            return

        # В материалах ничего релевантного — спрашиваем согласие на общие знания.
        if result.answer is None:
            await _clear_status(status)
            _pending_general[db_user.id] = question
            await message.answer(NOT_FOUND_ASK, reply_markup=CONSENT_KEYBOARD)
            usage_ctx["count"] = False
            return

        answer = result.answer
        response_time_ms = int((time.monotonic() - start_time) * 1000)
    except Exception:
        logger.exception("Ошибка при обработке вопроса")
        await _set_status(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return

    await _clear_status(status)
    await _send_answer(message, answer)

    async with async_session() as session:
        await log_query(
            session, db_user.id, question, answer, result.subject_used, response_time_ms
        )
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
        with request_context(user_id=f"telegram:{callback.from_user.id}", channel="telegram"):
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


@router.callback_query(F.data == "web:yes")
async def consent_web_search(callback: CallbackQuery) -> None:
    await callback.answer()
    question = _pending_general.pop(callback.from_user.id, None)
    try:
        await callback.message.edit_reply_markup()
    except Exception:
        pass

    if not question:
        await callback.message.answer("Запрос устарел — задайте вопрос заново.")
        return

    await _run_web_search(callback.message, callback.from_user.id, question)

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await increment_usage(session, user.id)
        await session.commit()


@router.callback_query(F.data == "pubmed:yes")
async def consent_pubmed_search(callback: CallbackQuery) -> None:
    await callback.answer()
    question = _pending_general.pop(callback.from_user.id, None)
    try:
        await callback.message.edit_reply_markup()
    except Exception:
        pass

    if not question:
        await callback.message.answer("Запрос устарел — задайте вопрос заново.")
        return

    await _run_pubmed_search(callback.message, callback.from_user.id, question)

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
