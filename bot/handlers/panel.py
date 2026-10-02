"""Нижняя панель и «Мои документы» (батч 27).

Навигация целиком на постоянных кнопках внизу чата (bot/panel.py): учебники и предметы,
клинреки, откуда отвечать, мои документы. На каждом экране есть «⬅️ Назад». Панель документов —
inline-сообщение с галочками, оно редактируется на месте и не засоряет чат.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TelegramUser

from app.workflows.scope import Scope
from app.workflows.user_documents import delete_user_document, list_user_documents, resolve_scope
from bot import panel
from bot.handlers.menu import CLINREK_PREMIUM_TEXT, has_clinrek_access
from constants import (
    SCOPE_BOTH,
    SCOPE_DOCUMENTS,
    SCOPE_TEXTBOOK,
    SOURCE_CLINREK,
    SOURCE_TEXTBOOK,
    subject_label,
)
from db.crud import (
    get_or_create_user,
    get_textbook_subjects,
    remove_active_document,
    set_answer_scope,
    set_user_selection,
    toggle_active_document,
    user_active_document_ids,
    user_scope,
)
from db.models import User
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

NEED_DOCUMENT_TEXT = "Сначала включи хотя бы один документ ✅ (или пришли файл)."
NEED_SUBJECT_TEXT = "Для учебников нужен предмет — сначала выбери его."


async def _load_user(tg_user: TelegramUser) -> User:
    async with async_session() as session:
        user = await get_or_create_user(session, tg_user)
        await session.commit()
        return user


def _status(user: User) -> tuple[str, str]:
    """(строка «Сейчас: …», подсказка для поля ввода)."""
    scope = user_scope(user)
    docs = len(user_active_document_ids(user))
    args = (user.current_source_type, user.current_subject, scope, docs)
    symptom = bool(user.clinrek_symptom_mode)
    return panel.status_line(*args, symptom=symptom), panel.context_placeholder(*args, symptom=symptom)


async def show_main(message: Message, user: User, title: str = "Главное меню") -> None:
    """Главная нижняя панель + строка «Сейчас: …» (и подсказка в поле ввода)."""
    status, placeholder = _status(user)
    await message.answer(
        f"{title}\n{status}",
        reply_markup=panel.main_keyboard(has_clinrek_access(user), placeholder),
    )


async def _documents(user_id: int) -> list[tuple[int, str]]:
    docs = await list_user_documents(str(user_id))
    return [(d.id, d.title) for d in docs]


async def render_docs_panel(user: User) -> tuple[str, "panel.InlineKeyboardMarkup"]:
    docs = await _documents(user.id)
    return panel.docs_panel(docs, user_active_document_ids(user), user_scope(user))


async def send_docs_panel(message: Message, user: User) -> None:
    """Нижняя панель «документов» + inline-панель с галочками."""
    await message.answer("📄 Мои документы", reply_markup=panel.docs_bottom_keyboard())
    text, markup = await render_docs_panel(user)
    await message.answer(text, reply_markup=markup)


# --- Главная и «Назад» ---------------------------------------------------------------


@router.message(Command("sources"))
async def cmd_sources(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _load_user(message.from_user)
    await show_main(message, user, "Панель источников")


@router.message(F.text == panel.BTN_BACK)
async def on_back(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _load_user(message.from_user)
    await show_main(message, user)


# --- Учебники и предметы -------------------------------------------------------------


@router.message(F.text == panel.BTN_TEXTBOOKS)
async def on_textbooks(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        subjects = await get_textbook_subjects(session)
    if not subjects:
        user = await _load_user(message.from_user)
        await show_main(message, user, "Учебники пока не загружены.")
        return
    await message.answer("Выбери предмет учебников:", reply_markup=panel.subjects_keyboard(subjects))


@router.message(F.text.startswith(panel.SUBJECT_PREFIX))
async def on_pick_subject(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        subjects = await get_textbook_subjects(session)
    subject = panel.subject_from_button(message.text, subjects)
    if subject is None:
        await message.answer(
            "Список предметов изменился — выбери ещё раз:", reply_markup=panel.subjects_keyboard(subjects)
        )
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await set_user_selection(session, user.id, SOURCE_TEXTBOOK, subject)
        # Выбор предмета = «учебники»; смешанный режим сохраняется как есть.
        if user_scope(user) != SCOPE_BOTH:
            await set_answer_scope(session, user.id, SCOPE_TEXTBOOK)
        await session.commit()
        user = await session.get(User, user.id)
    await show_main(message, user, f"📚 Предмет: {subject_label(subject)}.")


# --- Клинические рекомендации (премиум/админ) ----------------------------------------


@router.message(F.text == panel.BTN_CLINREK)
async def on_clinrek(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _load_user(message.from_user)
    if not has_clinrek_access(user):
        await message.answer(CLINREK_PREMIUM_TEXT, reply_markup=panel.main_keyboard(False))
        return
    await message.answer("Выбери категорию клинических рекомендаций:", reply_markup=panel.categories_keyboard())


@router.message(F.text.startswith(panel.CATEGORY_PREFIX))
async def on_pick_category(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _load_user(message.from_user)
    if not has_clinrek_access(user):
        await message.answer(CLINREK_PREMIUM_TEXT, reply_markup=panel.main_keyboard(False))
        return
    picked = panel.category_from_button(message.text)
    if picked is None:
        await message.answer("Не нашёл такую категорию.", reply_markup=panel.categories_keyboard())
        return
    _code, value = picked
    async with async_session() as session:
        await set_user_selection(session, user.id, SOURCE_CLINREK, value)
        await set_answer_scope(session, user.id, SCOPE_TEXTBOOK)
        await session.commit()
        user = await session.get(User, user.id)
    await show_main(message, user, "📋 Клин. рекомендации: задавай вопросы — отвечу строго по ним, с источником.")


@router.message(F.text == panel.BTN_SYMPTOM)
async def on_symptom(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _load_user(message.from_user)
    if not has_clinrek_access(user):
        await message.answer(CLINREK_PREMIUM_TEXT, reply_markup=panel.main_keyboard(False))
        return
    async with async_session() as session:
        await set_user_selection(session, user.id, SOURCE_CLINREK, None, symptom_mode=True)
        await set_answer_scope(session, user.id, SCOPE_TEXTBOOK)
        await session.commit()
        user = await session.get(User, user.id)
    await show_main(
        message,
        user,
        "🩺 Разбор по симптомам: опиши жалобы и картину пациента (пол, возраст, симптомы, длительность) — "
        "подберу версии по клиническим рекомендациям. Это ориентир, не диагноз.",
    )


# --- Откуда отвечать -----------------------------------------------------------------


async def _try_set_scope(user: User, scope: str) -> str | None:
    """Меняет режим ответа. Возвращает текст-причину, если режим включить нельзя."""
    if scope in (SCOPE_DOCUMENTS, SCOPE_BOTH) and not user_active_document_ids(user):
        return NEED_DOCUMENT_TEXT
    if scope in (SCOPE_TEXTBOOK, SCOPE_BOTH) and (
        user.current_source_type != SOURCE_TEXTBOOK or not user.current_subject
    ):
        return NEED_SUBJECT_TEXT
    async with async_session() as session:
        await set_answer_scope(session, user.id, scope)
        await session.commit()
    return None


@router.message(F.text == panel.BTN_SCOPE)
async def on_scope(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _load_user(message.from_user)
    status, _ = _status(user)
    await message.answer(f"Откуда отвечать?\n{status}", reply_markup=panel.scope_keyboard(user_scope(user)))


@router.message(F.text.func(lambda t: isinstance(t, str) and panel.scope_from_button(t) is not None))
async def on_pick_scope(message: Message, state: FSMContext) -> None:
    await state.clear()
    scope = panel.scope_from_button(message.text)
    user = await _load_user(message.from_user)
    problem = await _try_set_scope(user, scope)
    if problem == NEED_DOCUMENT_TEXT:
        await message.answer(problem)
        await send_docs_panel(message, user)
        return
    if problem == NEED_SUBJECT_TEXT:
        async with async_session() as session:
            subjects = await get_textbook_subjects(session)
        await message.answer(problem, reply_markup=panel.subjects_keyboard(subjects))
        return
    user = await _load_user(message.from_user)
    await show_main(message, user, "✅ Режим изменён.")


# --- Мои документы -------------------------------------------------------------------


@router.message(F.text == panel.BTN_DOCS)
async def on_docs(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await _load_user(message.from_user)
    await send_docs_panel(message, user)


@router.message(F.text == panel.BTN_UPLOAD)
async def on_upload_hint(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Пришли файл PDF, DOCX или TXT (до 20 МБ) — разберу и включу в «Мои документы».")


async def _edit(callback: CallbackQuery, text: str, markup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        # «message is not modified» и т.п. — не повод падать.
        pass


@router.callback_query(F.data.startswith("docs:"))
async def docs_callback(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = await _load_user(callback.from_user)
    docs = await _documents(user.id)
    owned = {doc_id for doc_id, _title in docs}
    titles = dict(docs)

    if action == "t" and len(parts) == 3 and parts[2].isdigit() and int(parts[2]) in owned:
        async with async_session() as session:
            await toggle_active_document(session, user.id, int(parts[2]))
            await session.commit()
        await callback.answer()
        user = await _load_user(callback.from_user)
        await _edit(callback, *await render_docs_panel(user))
    elif action == "s" and len(parts) == 3:
        problem = await _try_set_scope(user, parts[2])
        await callback.answer(problem or "Режим изменён", show_alert=bool(problem))
        user = await _load_user(callback.from_user)
        await _edit(callback, *await render_docs_panel(user))
    elif action == "up":
        await callback.answer()
        await callback.message.answer("Пришли файл PDF, DOCX или TXT (до 20 МБ).")
    elif action == "del":
        await callback.answer()
        await _edit(callback, *panel.docs_delete_list(docs))
    elif action == "d" and len(parts) == 3 and parts[2].isdigit() and int(parts[2]) in owned:
        await callback.answer()
        await _edit(callback, *panel.docs_delete_confirm(int(parts[2]), titles[int(parts[2])]))
    elif action == "y" and len(parts) == 3 and parts[2].isdigit() and int(parts[2]) in owned:
        doc_id = int(parts[2])
        await delete_user_document(str(user.id), doc_id)
        async with async_session() as session:
            await remove_active_document(session, user.id, doc_id)
            fresh = await session.get(User, user.id)
            # Документов не осталось, а режим требовал их — возвращаемся к учебникам.
            if not user_active_document_ids(fresh) and user_scope(fresh) in (SCOPE_DOCUMENTS, SCOPE_BOTH):
                await set_answer_scope(session, user.id, SCOPE_TEXTBOOK)
            await session.commit()
        await callback.answer("Удалено")
        user = await _load_user(callback.from_user)
        await _edit(callback, *await render_docs_panel(user))
    elif action == "back":
        await callback.answer()
        await _edit(callback, *await render_docs_panel(user))
    elif action == "home":
        await callback.answer()
        try:
            await callback.message.delete()
        except Exception:
            pass
        await show_main(callback.message, user)
    else:
        await callback.answer("Список изменился — открой «📄 Мои документы» заново.", show_alert=True)


# --- Режим ответа для вопросов и фото -------------------------------------------------

PROBLEM_DOCS = "docs"
PROBLEM_SUBJECT = "subject"


async def prepare_scope(user: User) -> tuple[Scope | None, str | None]:
    """(scope, проблема). scope=None без проблемы — обычный режим «учебники/клинреки».
    Проблема — `PROBLEM_DOCS` (нет включённых документов) или `PROBLEM_SUBJECT` (для
    смешанного режима не выбран предмет учебников)."""
    mode = user_scope(user)
    if mode == SCOPE_TEXTBOOK:
        return None, None
    scope = await resolve_scope(
        str(user.id),
        mode,
        user_active_document_ids(user),
        user.current_source_type or SOURCE_TEXTBOOK,
        user.current_subject,
    )
    if not scope.document_ids:
        return None, PROBLEM_DOCS
    if mode == SCOPE_BOTH and (user.current_source_type != SOURCE_TEXTBOOK or not user.current_subject):
        return None, PROBLEM_SUBJECT
    return scope, None


async def explain_problem(message: Message, user: User, problem: str) -> None:
    """Подсказка студенту, что сделать, и нужная панель под рукой."""
    if problem == PROBLEM_DOCS:
        await message.answer(NEED_DOCUMENT_TEXT)
        await send_docs_panel(message, user)
        return
    async with async_session() as session:
        subjects = await get_textbook_subjects(session)
    await message.answer(NEED_SUBJECT_TEXT, reply_markup=panel.subjects_keyboard(subjects))
