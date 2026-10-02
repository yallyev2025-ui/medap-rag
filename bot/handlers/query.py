"""Хендлер вопросов: лёгкая память диалога, роутер интентов (одна болезнь /
дифдиагноз / сочетание / приветствие), поиск под стратегию и генерация. Плюс
согласие на общие знания, режим «Разбор по симптомам» и статус-индикатор «думает»."""

import asyncio
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
from app.workflows.pubmed import SOURCE_EUROPEPMC, search_pubmed
from app.workflows.web_search import search_and_answer
from bot.formatting import split_for_telegram, to_telegram_html
from bot.waiting_phrases import FIRST_PHRASE, rotate_status
from bot.handlers.menu import CLINREK_PREMIUM_TEXT, has_clinrek_access, send_main_menu
from bot.handlers.panel import explain_problem, prepare_scope
from constants import SCOPE_BOTH, SCOPE_DOCUMENTS, SOURCE_CLINREK, SOURCE_TEXTBOOK
from db.crud import (
    get_or_create_user,
    get_recent_turns,
    increment_usage,
    is_limit_exceeded,
    log_query,
    reset_chat,
    user_scope,
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
# Для клинреков fallback (общие знания/интернет/PubMed) запрещён полностью —
# никаких вариантов не предлагается, в отличие от учебников выше.
CLINREK_NOT_FOUND_TEXT = "Этой информации нет в представленных клинических рекомендациях."
# Режимы с документами: только выбранные источники, общие знания не предлагаются.
DOCUMENT_NOT_FOUND_TEXT = "📄 В твоих документах по этому вопросу ничего нет. Переформулируй вопрос или включи другой документ."
BOTH_NOT_FOUND_TEXT = "📄📚 Ни в твоих документах, ни в учебниках по этому вопросу ничего нет. Переформулируй вопрос."

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
    # Ссылка каждого источника уже стоит под его описанием (собрано в app/workflows/web_search.py).
    await _send_answer(message, result.answer)


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
    # Ссылка каждой статьи уже стоит под её описанием (собрано в app/workflows/pubmed.py).
    answer = result.answer
    if result.source == SOURCE_EUROPEPMC:
        answer = f"{answer}\n\n*Источник данных: Europe PMC — те же статьи PubMed/MEDLINE (PubMed напрямую сейчас недоступен).*"
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
    await message.answer(
        "Что искать в PubMed? Пиши по-русски — я сам переведу запрос на английский и найду исследования."
    )


@router.message(SearchStates.waiting_pubmed_query, F.text)
async def pubmed_run(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    await state.clear()
    await _run_pubmed_search(message, db_user.id, message.text)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_question(message: Message, db_user: User, usage_ctx: dict) -> None:
    # Откуда отвечать (батч 27): учебники / мои документы / мои и учебники вместе.
    mode = user_scope(db_user)
    scope, problem = await prepare_scope(db_user)
    if problem is not None:
        await explain_problem(message, db_user, problem)
        usage_ctx["count"] = False
        return

    # Режим не выбран — просим выбрать и не тратим лимит на это сообщение.
    if mode != SCOPE_DOCUMENTS and db_user.current_source_type is None:
        await message.answer(CHOOSE_MODE_TEXT)
        await send_main_menu(message)
        usage_ctx["count"] = False
        return

    source_type = db_user.current_source_type or SOURCE_TEXTBOOK
    subject = db_user.current_subject if mode != SCOPE_DOCUMENTS else None
    question = message.text

    # Клинреки — только премиум/админ (режим мог быть выбран до отзыва премиума).
    if source_type == SOURCE_CLINREK and not has_clinrek_access(db_user):
        await message.answer(CLINREK_PREMIUM_TEXT)
        await send_main_menu(message)
        usage_ctx["count"] = False
        return

    async def budget_ok() -> bool:
        # Много вопросов порциями: перед каждой следующей порцией проверяем месячный лимит.
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user)
            return not await is_limit_exceeded(session, user)

    status = await message.answer(FIRST_PHRASE)

    try:
        start_time = time.monotonic()

        # Лёгкая память: последние обмены текущего чата.
        async with async_session() as session:
            turns = await get_recent_turns(
                session, db_user.id, db_user.chat_started_at, HISTORY_TURNS
            )

        # Пока идёт генерация — бесплатные «думаю»-фразы в статус-сообщении (без LLM).
        rotator = asyncio.create_task(rotate_status(lambda text: _set_status(status, text)))
        # Весь конвейер (роутинг интента, переписывание запроса, поиск, генерация)
        # живёт в общем workflow — том же, что обслуживает образовательный сайт
        # через /v1. Здесь остаётся только телеграмный UI.
        try:
            with request_context(user_id=f"telegram:{db_user.id}", channel="telegram"):
                result = await ask(
                    question,
                    source_type=source_type,
                    subject=subject,
                    turns=turns,
                    symptom_mode=db_user.clinrek_symptom_mode,
                    scope=scope,
                    budget_ok=budget_ok,
                )
        finally:
            rotator.cancel()

        # Приветствие / small talk — дружелюбный ответ, лимит не тратим.
        if result.intent == "CHITCHAT":
            await _clear_status(status)
            await _send_answer(message, result.answer)
            usage_ctx["count"] = False
            return

        # В материалах ничего релевантного.
        if result.answer is None:
            await _clear_status(status)
            # Клинреки: fallback (общие знания/интернет/PubMed) запрещён полностью —
            # не просто под кнопкой согласия, а вообще не предлагается (§ клинического
            # промпта). Учебники — как раньше, три варианта на выбор.
            if mode == SCOPE_DOCUMENTS:
                await message.answer(DOCUMENT_NOT_FOUND_TEXT)
            elif mode == SCOPE_BOTH:
                await message.answer(BOTH_NOT_FOUND_TEXT)
            elif source_type == SOURCE_CLINREK:
                await message.answer(CLINREK_NOT_FOUND_TEXT)
            else:
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
