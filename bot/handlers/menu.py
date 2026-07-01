"""Главное меню и выбор режима работы: Учебники (по предметам) / Клин. рекомендации.

Выбор сохраняется в БД (users.current_source_type/current_subject), поэтому
переживает перезапуски бота. RAG-логика (rag/) от Telegram не зависит — те же
фильтры (source_type, subject) сможет использовать будущий сайт.
"""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from constants import (
    CLINREK_CATEGORIES,
    SOURCE_CLINREK,
    SOURCE_TEXTBOOK,
    clinrek_label,
    subject_label,
)
from db.crud import get_or_create_user, get_textbook_subjects, set_user_selection
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

MAIN_MENU_TEXT = (
    "Привет! Я медицинский ассистент MedAP.\n\n"
    "Выберите режим — я буду искать ответы строго по выбранной базе "
    "и указывать источник:"
)

MAIN_MENU_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📚 Учебники", callback_data="menu:textbook")],
        [InlineKeyboardButton(text="📋 Клин. рекомендации", callback_data="menu:clinrek")],
    ]
)

# Кнопка возврата к выбору режима, добавляется под подтверждением выбора.
_CHANGE_MODE_ROW = [InlineKeyboardButton(text="🔄 Сменить режим", callback_data="menu:main")]


async def send_main_menu(message: Message) -> None:
    await message.answer(MAIN_MENU_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


def _subjects_keyboard(subjects: list[str]) -> InlineKeyboardMarkup:
    # Индекс вместо самого предмета в callback_data — предметы бывают кириллические
    # и длинные, а на callback_data есть лимит 64 байта. Список детерминирован
    # (ORDER BY subject), так что индекс стабилен между сообщением и нажатием.
    rows = [
        [InlineKeyboardButton(text=subject_label(s), callback_data=f"subj:{i}")]
        for i, s in enumerate(subjects)
    ]
    rows.append(_CHANGE_MODE_ROW)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _categories_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"cat:{code}")]
        for code, label, _value in CLINREK_CATEGORIES
    ]
    rows.append(_CHANGE_MODE_ROW)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


@router.callback_query(F.data == "menu:textbook")
async def cb_textbook_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    async with async_session() as session:
        subjects = await get_textbook_subjects(session)

    if not subjects:
        await callback.message.edit_text(
            "Учебники пока не загружены. Загрузите их через /addbook.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[_CHANGE_MODE_ROW]),
        )
        return

    await callback.message.edit_text(
        "Выберите предмет:", reply_markup=_subjects_keyboard(subjects)
    )


@router.callback_query(F.data.startswith("subj:"))
async def cb_pick_subject(callback: CallbackQuery) -> None:
    await callback.answer()
    index = int(callback.data.split(":", 1)[1])

    async with async_session() as session:
        subjects = await get_textbook_subjects(session)
        if index >= len(subjects):
            await callback.message.edit_text(
                "Список предметов изменился. Откройте меню заново: /start"
            )
            return
        subject = subjects[index]
        await get_or_create_user(session, callback.from_user)
        await set_user_selection(session, callback.from_user.id, SOURCE_TEXTBOOK, subject)
        await session.commit()

    await callback.message.edit_text(
        f"📚 Режим: Учебники — {subject_label(subject)}.\n\n"
        "Задавайте вопросы — отвечу строго по учебникам этого предмета "
        "с указанием источника.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📚 Сменить предмет", callback_data="menu:textbook")],
                _CHANGE_MODE_ROW,
            ]
        ),
    )


@router.callback_query(F.data == "menu:clinrek")
async def cb_clinrek_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "Выберите категорию клинических рекомендаций:",
        reply_markup=_categories_keyboard(),
    )


@router.callback_query(F.data.startswith("cat:"))
async def cb_pick_category(callback: CallbackQuery) -> None:
    await callback.answer()
    code = callback.data.split(":", 1)[1]
    subject = next((value for c, _label, value in CLINREK_CATEGORIES if c == code), None)

    async with async_session() as session:
        await get_or_create_user(session, callback.from_user)
        await set_user_selection(session, callback.from_user.id, SOURCE_CLINREK, subject)
        await session.commit()

    await callback.message.edit_text(
        f"📋 Режим: Клин. рекомендации — {clinrek_label(subject)}.\n\n"
        "Задавайте вопросы — отвечу строго по клиническим рекомендациям "
        "с указанием источника.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Сменить категорию", callback_data="menu:clinrek")],
                _CHANGE_MODE_ROW,
            ]
        ),
    )
