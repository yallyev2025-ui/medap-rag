"""Документы пользователя для Telegram (§18 ТЗ, этап 4A.5): студент присылает файл
(конспект, старый экзамен и т.п.), дальше просто пишет вопросы — они уходят не в
общий поиск по учебникам, а строго в этот один документ (app/workflows/user_documents.py)."""

import logging
import os
import tempfile

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from app.observability.context import request_context
from app.workflows.user_documents import ask_user_document, ingest_user_document
from bot.formatting import split_for_telegram, to_telegram_html
from db.crud import get_or_create_user, set_active_document
from db.models import User
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "⚠️ Не получилось обработать документ, попробуй ещё раз через минуту."
EXIT_TEXT = "Вышел из режима «свой документ» — вопросы снова ищутся по обычным материалам."


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
        await set_active_document(session, db_user.id, str(result.document.id))
        await session.commit()

    await _set_status(
        status,
        f"✅ Загружено «{result.document.title}» — {result.document.chunks_count} фрагментов.\n"
        "Теперь просто пиши вопросы — отвечаю строго по этому документу.\n"
        "Команда /exitdocument — вернуться к обычным вопросам.",
    )


@router.message(Command("exitdocument"))
async def exit_document_mode(message: Message) -> None:
    # Команда — middleware не инжектит db_user для сообщений с "/" (см.
    # bot/middlewares/limits.py), поэтому пользователь ищется здесь напрямую,
    # как и в cmd_new (bot/handlers/query.py).
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        if user.current_document_id is None:
            await session.commit()
            return
        await set_active_document(session, user.id, None)
        await session.commit()
    await message.answer(EXIT_TEXT)


async def handle_document_question(message: Message, db_user: User, usage_ctx: dict) -> None:
    """Вызывается из bot/handlers/query.py, когда у пользователя выбран документ."""
    status = await message.answer("📄 Ищу в документе…")
    try:
        with request_context(user_id=f"telegram:{db_user.id}", channel="telegram", workflow="DOCUMENT_QA"):
            result = await ask_user_document(
                message.text,
                user_id=str(db_user.id),
                document_id=int(db_user.current_document_id),
            )
    except Exception:
        logger.exception("Ошибка при ответе по документу пользователя")
        await _set_status(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return

    if result.error:
        await _set_status(status, f"⚠️ {result.error}")
        usage_ctx["count"] = False
        return

    try:
        await status.delete()
    except Exception:
        pass
    for part in split_for_telegram(to_telegram_html(result.answer or "")):
        await message.answer(part, parse_mode=ParseMode.HTML)
