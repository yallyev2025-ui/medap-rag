"""Админ-команды: /stats, /addbook, /broadcast, /ban, /premium."""

import asyncio
import logging
import os
import tempfile

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import settings
from db.crud import delete_book, get_active_user_ids, get_stats, list_books, set_ban, set_premium
from db.session import async_session
from scripts.load_books import load_book

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.from_user.id.in_(settings.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(settings.ADMIN_IDS))

MAX_PDF_SIZE = 20 * 1024 * 1024  # лимит Telegram Bot API на скачивание файла ботом

# Поддерживаемые форматы учебников. Сканированные PDF распознаются OCR на сервере.
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt")

ADDBOOK_TMP_DIR = os.path.join(tempfile.gettempdir(), "medap_addbook")

SUBJECTS = [
    ("pathanatomy", "Патанатомия"),
    ("pathphys", "Патофизиология"),
    ("physiology", "Физиология"),
    ("anatomy", "Анатомия"),
    ("biochemistry", "Биохимия"),
    ("pharmacology", "Фармакология"),
    ("other", "Другой"),
]

SUBJECT_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"addbook_subject:{code}")] for code, label in SUBJECTS
    ]
)


class AddBookStates(StatesGroup):
    waiting_pdf = State()
    waiting_subject = State()
    waiting_subject_custom = State()
    waiting_author = State()
    waiting_title = State()


class BroadcastStates(StatesGroup):
    waiting_text = State()


def cleanup_addbook_tmp() -> None:
    """Удаляет временные PDF, оставшиеся от прерванных /addbook (например, после рестарта бота)."""
    if not os.path.isdir(ADDBOOK_TMP_DIR):
        os.makedirs(ADDBOOK_TMP_DIR, exist_ok=True)
        return

    for name in os.listdir(ADDBOOK_TMP_DIR):
        path = os.path.join(ADDBOOK_TMP_DIR, name)
        if os.path.isfile(path):
            os.remove(path)


def _parse_user_id(command: CommandObject) -> int | None:
    if not command.args:
        return None
    arg = command.args.strip().split()[0]
    return int(arg) if arg.isdigit() else None


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with async_session() as session:
        stats = await get_stats(session)

    top_questions = (
        "\n".join(f"{i}. {q} ({cnt})" for i, (q, cnt) in enumerate(stats["top_questions"], start=1)) or "—"
    )

    total = stats["total_queries"]
    subject_lines = []
    for subject, cnt in stats["subject_counts"]:
        name = subject or "без ответа в материалах"
        percent = (cnt / total * 100) if total else 0
        subject_lines.append(f"- {name}: {percent:.1f}%")
    subject_text = "\n".join(subject_lines) or "—"

    avg_response_time_ms = stats["avg_response_time_ms"]
    avg_response_text = f"{avg_response_time_ms:.0f}" if avg_response_time_ms is not None else "—"

    text = f"""Пользователи:
- Всего: {stats['total_users']}
- Новые сегодня: {stats['new_today']}
- Активные сегодня: {stats['active_today']}
- Премиум: {stats['premium_count']}

Запросы:
- Всего: {stats['total_queries']}
- Сегодня: {stats['today_queries']}
- Среднее в день: {stats['avg_per_day']:.1f}

Топ-10 тем:
{top_questions}

Разбивка по предметам:
{subject_text}

Среднее время ответа: {avg_response_text} мс"""

    await message.answer(text)


@router.message(Command("addbook"))
async def cmd_addbook(message: Message, state: FSMContext) -> None:
    await state.set_state(AddBookStates.waiting_pdf)
    await message.answer(
        "Отправь файл учебника: PDF (в т.ч. сканированный — распознаю текст сам), "
        "Word (.docx) или текстовый (.txt)."
    )


@router.message(AddBookStates.waiting_pdf, F.document)
async def addbook_receive_pdf(message: Message, state: FSMContext) -> None:
    document = message.document
    ext = os.path.splitext(document.file_name or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        await message.answer("Нужен файл PDF, Word (.docx) или текстовый (.txt). Попробуй снова.")
        return
    if document.file_size > MAX_PDF_SIZE:
        await message.answer(
            "Файл слишком большой (максимум 20 МБ — ограничение Telegram для ботов). "
            "Сожми файл или разбей на части и попробуй снова."
        )
        return

    fd, file_path = tempfile.mkstemp(suffix=ext, dir=ADDBOOK_TMP_DIR)
    os.close(fd)
    try:
        await message.bot.download(document, destination=file_path)
    except TelegramBadRequest:
        os.remove(file_path)
        await message.answer(
            "Не удалось скачать файл (слишком большой для Telegram Bot API, лимит 20 МБ). "
            "Сожми файл или разбей на части и попробуй снова."
        )
        return

    await state.update_data(file_path=file_path)
    await state.set_state(AddBookStates.waiting_subject)
    await message.answer("Выбери предмет:", reply_markup=SUBJECT_KEYBOARD)


@router.message(AddBookStates.waiting_pdf)
async def addbook_invalid_pdf(message: Message) -> None:
    await message.answer("Нужен файл-документ (PDF, .docx или .txt). Попробуй снова.")


@router.callback_query(AddBookStates.waiting_subject, F.data.startswith("addbook_subject:"))
async def addbook_choose_subject(callback: CallbackQuery, state: FSMContext) -> None:
    subject = callback.data.split(":", 1)[1]
    await callback.answer()

    if subject == "other":
        await state.set_state(AddBookStates.waiting_subject_custom)
        await callback.message.edit_text("Введи название предмета:")
        return

    await state.update_data(subject=subject)
    await state.set_state(AddBookStates.waiting_author)
    await callback.message.edit_text("Введи автора учебника:")


@router.message(AddBookStates.waiting_subject_custom, F.text)
async def addbook_custom_subject(message: Message, state: FSMContext) -> None:
    await state.update_data(subject=message.text.strip())
    await state.set_state(AddBookStates.waiting_author)
    await message.answer("Введи автора учебника:")


@router.message(AddBookStates.waiting_author, F.text)
async def addbook_author(message: Message, state: FSMContext) -> None:
    await state.update_data(author=message.text.strip())
    await state.set_state(AddBookStates.waiting_title)
    await message.answer("Введи название учебника:")


@router.message(AddBookStates.waiting_title, F.text)
async def addbook_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_path = data["file_path"]
    subject = data["subject"]
    author = data["author"]
    title = message.text.strip()

    await state.clear()
    await message.answer(
        "Загружаю учебник, это может занять время "
        "(для сканированных PDF дольше — распознаю текст)..."
    )

    try:
        chunks_count = await load_book(file_path, subject, author, title)
    except Exception:
        logger.exception("Ошибка при загрузке учебника")
        await message.answer(
            "Не удалось загрузить учебник: повреждённый файл или не найден текст."
        )
        return
    finally:
        os.remove(file_path)

    await message.answer(f"Учебник добавлен: {title}, {chunks_count} чанков")


@router.message(Command("delbook"))
async def cmd_delbook(message: Message) -> None:
    async with async_session() as session:
        books = await list_books(session)

    if not books:
        await message.answer("Учебников пока нет.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 {b.title} — {b.author}", callback_data=f"delbook:{b.id}")]
            for b in books
        ]
    )
    await message.answer("Выбери учебник для удаления:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("delbook:"))
async def delbook_ask_confirm(callback: CallbackQuery) -> None:
    book_id = int(callback.data.split(":", 1)[1])
    await callback.answer()

    async with async_session() as session:
        books = {b.id: b for b in await list_books(session)}

    book = books.get(book_id)
    if book is None:
        await callback.message.edit_text("Учебник уже удалён.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"delbook_yes:{book_id}"),
                InlineKeyboardButton(text="Отмена", callback_data="delbook_cancel"),
            ]
        ]
    )
    await callback.message.edit_text(
        f"Удалить учебник «{book.title}» ({book.author})?\nВместе с ним удалятся все его чанки.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("delbook_yes:"))
async def delbook_do(callback: CallbackQuery) -> None:
    book_id = int(callback.data.split(":", 1)[1])
    await callback.answer()

    async with async_session() as session:
        title = await delete_book(session, book_id)
        await session.commit()

    if title is None:
        await callback.message.edit_text("Учебник уже удалён.")
    else:
        await callback.message.edit_text(f"Учебник удалён: {title}")


@router.callback_query(F.data == "delbook_cancel")
async def delbook_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Удаление отменено.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer("Отправь текст рассылки:")


@router.message(BroadcastStates.waiting_text, F.text)
async def broadcast_send(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = message.text

    async with async_session() as session:
        user_ids = await get_active_user_ids(session)

    success = 0
    failed = 0
    for user_id in user_ids:
        try:
            await message.bot.send_message(user_id, text)
            success += 1
        except TelegramForbiddenError:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(f"Отправлено: {success}, не доставлено: {failed}")


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject) -> None:
    user_id = _parse_user_id(command)
    if user_id is None:
        await message.answer("Использование: /ban <user_id>")
        return

    async with async_session() as session:
        found = await set_ban(session, user_id, True)
        await session.commit()

    await message.answer(f"Пользователь {user_id} заблокирован" if found else f"Пользователь {user_id} не найден")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject) -> None:
    user_id = _parse_user_id(command)
    if user_id is None:
        await message.answer("Использование: /unban <user_id>")
        return

    async with async_session() as session:
        found = await set_ban(session, user_id, False)
        await session.commit()

    await message.answer(
        f"Пользователь {user_id} разблокирован" if found else f"Пользователь {user_id} не найден"
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message, command: CommandObject) -> None:
    user_id = _parse_user_id(command)
    if user_id is None:
        await message.answer("Использование: /premium <user_id>")
        return

    async with async_session() as session:
        found = await set_premium(session, user_id, True)
        await session.commit()

    await message.answer(
        f"Пользователю {user_id} выдан премиум" if found else f"Пользователь {user_id} не найден"
    )


@router.message(Command("unpremium"))
async def cmd_unpremium(message: Message, command: CommandObject) -> None:
    user_id = _parse_user_id(command)
    if user_id is None:
        await message.answer("Использование: /unpremium <user_id>")
        return

    async with async_session() as session:
        found = await set_premium(session, user_id, False)
        await session.commit()

    await message.answer(
        f"У пользователя {user_id} убран премиум" if found else f"Пользователь {user_id} не найден"
    )
