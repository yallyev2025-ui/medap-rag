"""Vision/Test Solver для Telegram (§17, §53.2 ТЗ, этап 4A.4): студент шлёт
фото/скрин теста, бот распознаёт вопрос и решает его через тот же
Evidence-конвейер, что и обычные текстовые вопросы (app/workflows/vision.py)."""

import logging

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import Message

from app.observability.context import request_context
from app.workflows.vision import solve_from_image
from bot.formatting import split_for_telegram, to_telegram_html
from bot.handlers.menu import send_main_menu
from db.models import User

logger = logging.getLogger(__name__)

router = Router()

CHOOSE_MODE_TEXT = "Сначала выберите режим — я ищу ответы строго по выбранной базе."
ERROR_TEXT = "⚠️ Не получилось разобрать фото, попробуй ещё раз через минуту."


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


@router.message(F.photo)
async def handle_photo(message: Message, db_user: User, usage_ctx: dict) -> None:
    if db_user.current_source_type is None:
        await message.answer(CHOOSE_MODE_TEXT)
        await send_main_menu(message)
        usage_ctx["count"] = False
        return

    status = await message.answer("📷 Распознаю фото…")

    try:
        photo = message.photo[-1]
        buffer = await message.bot.download(photo)
        image_bytes = buffer.read()

        with request_context(
            user_id=f"telegram:{db_user.id}", channel="telegram", workflow="VISION_EXTRACT"
        ):
            result = await solve_from_image(
                image_bytes,
                source_type=db_user.current_source_type,
                subject=db_user.current_subject,
            )
    except Exception:
        logger.exception("Ошибка при разборе фото")
        await _set_status(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return

    if result.needs_retake:
        issue = result.extraction.quality_issue
        detail = f" ({issue})" if issue else ""
        await _set_status(
            status,
            f"📷 Не могу уверенно прочитать фото{detail}. Переснимите крупно, при хорошем "
            "освещении, чтобы текст вопроса был полностью виден.",
        )
        usage_ctx["count"] = False
        return

    await _clear_status(status)
    for part in split_for_telegram(to_telegram_html(result.answer or "")):
        await message.answer(part, parse_mode=ParseMode.HTML)
