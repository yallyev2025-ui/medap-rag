"""«📖 Показать фрагмент» (Telegram): по нажатию кнопки под ответом присылает точный текст
процитированного фрагмента (bot/fragments.py) и, если настроен S3, кнопку «Открыть оригинал»."""

import logging

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.evidence.viewer import fetch_evidence
from bot.fragments import CALLBACK_PREFIX, evidence_id_from_callback, render_fragment

logger = logging.getLogger(__name__)

router = Router()

NOT_FOUND_TEXT = "Фрагмент не найден — возможно, источник уже удалён."
FAILED_TEXT = "Не удалось открыть фрагмент, попробуй ещё раз."


@router.callback_query(F.data.startswith(CALLBACK_PREFIX))
async def show_fragment(callback: CallbackQuery) -> None:
    evidence_id = evidence_id_from_callback(callback.data)
    if evidence_id is None:
        await callback.answer(NOT_FOUND_TEXT, show_alert=True)
        return

    try:
        detail = await fetch_evidence(evidence_id)
    except Exception:
        logger.exception("Не удалось получить фрагмент %s", evidence_id)
        await callback.answer(FAILED_TEXT, show_alert=True)
        return

    # Личный документ студента видит только владелец; чужое — как «не найдено».
    if detail is None or (detail.owner_id is not None and detail.owner_id != str(callback.from_user.id)):
        await callback.answer(NOT_FOUND_TEXT, show_alert=True)
        return

    await callback.answer()
    # Ответ, под которым нажата кнопка, — для выделения опорных предложений.
    answer_text = (callback.message.text or callback.message.caption or "") if callback.message else ""
    markup = None
    if detail.url:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть оригинал", url=detail.url)]]
        )
    await callback.message.answer(
        render_fragment(detail, answer_text),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
