"""Документы пользователя для Telegram (§18 ТЗ, этап 4A.5, батч 27): студент присылает файл
(конспект, старый экзамен и т.п.) — он попадает в «📄 Мои документы» и включается ✅. Откуда
отвечать (документы / учебники / вместе) выбирается панелью (bot/handlers/panel.py);
сами вопросы обслуживает общий хендлер bot/handlers/query.py."""

import logging
import os
import tempfile

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.observability.context import request_context
from app.workflows.user_documents import ingest_user_document, list_user_documents
from constants import SCOPE_DOCUMENTS, SCOPE_TEXTBOOK
from db.crud import (
    add_active_document,
    get_or_create_user,
    set_answer_scope,
    user_scope,
)
from db.models import User
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "⚠️ Не получилось обработать документ, попробуй ещё раз через минуту."
EXIT_TEXT = "Режим: только учебники. Документы можно снова включить кнопкой «🔀 Откуда отвечать»."


async def _set_status(status: Message | None, text: str) -> None:
    if status is None:
        return
    try:
        await status.edit_text(text)
    except Exception:
        pass


@router.message(F.document)
async def handle_document_upload(message: Message, db_user: User, usage_ctx: dict) -> None:
    status = await message.answer("📄 Загружаю документ…")

    document = message.document
    extension = os.path.splitext(document.file_name or "")[1].lower()
    fd, tmp_path = tempfile.mkstemp(suffix=extension)
    try:
        buffer = await message.bot.download(document)
        with os.fdopen(fd, "wb") as out:
            out.write(buffer.read())

        with request_context(user_id=f"telegram:{db_user.id}", channel="telegram", workflow="DOCUMENT_QA"):
            result = await ingest_user_document(
                tmp_path,
                document.file_name or "document",
                user_id=str(db_user.id),
            )
    except Exception:
        logger.exception("Ошибка при загрузке документа пользователя")
        await _set_status(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return
    finally:
        os.remove(tmp_path)

    if result.error or result.document is None:
        await _set_status(status, f"⚠️ {result.error or 'Не удалось загрузить документ.'}")
        usage_ctx["count"] = False
        return

    async with async_session() as session:
        docs_now = await list_user_documents(str(db_user.id))  # уже включая только что загруженный
        user = await get_or_create_user(session, message.from_user)
        await add_active_document(session, user.id, result.document.id)
        # Первый документ студента — сразу отвечаем по нему (кнопки режима под рукой).
        if len(docs_now) <= 1 and user_scope(user) == SCOPE_TEXTBOOK:
            await set_answer_scope(session, user.id, SCOPE_DOCUMENTS)
        await session.commit()
        user = await session.get(User, user.id)

    await _set_status(
        status,
        f"✅ Загружено «{result.document.title}» — {result.document.chunks_count} фрагментов, "
        "документ включён ✅.",
    )
    # Нижняя панель (с актуальной подсказкой в поле ввода) и панель документов с галочками.
    from bot.handlers.panel import render_docs_panel, show_main  # поздний импорт: panel ↔ этот модуль

    await show_main(message, user, "Теперь можно задавать вопросы по документу.")
    text, markup = await render_docs_panel(user)
    await message.answer(text, reply_markup=markup)


@router.message(Command("exitdocument"))
async def exit_document_mode(message: Message) -> None:
    """Совместимость со старой командой: «только учебники». Основной способ — панель."""
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        if user_scope(user) == SCOPE_TEXTBOOK:
            await session.commit()
            return
        await set_answer_scope(session, user.id, SCOPE_TEXTBOOK)
        await session.commit()
    await message.answer(EXIT_TEXT)
