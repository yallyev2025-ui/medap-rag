"""«📞 Звонок» (MedAP, этап 5; решения 40–42): живой голосовой разговор в мини-приложении Telegram.

Бот выдаёт подписанную ссылку на страницу сайта (`/call`) и больше в разговоре не участвует: там запись с микрофона,
распознавание, быстрый ответ по заранее собранной базе знаний и озвучка. Только подписчикам.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.voice.call_link import call_url, sign_call_token
from bot.panel import BTN_CALL
from config import settings
from db.crud import can_use_deep, get_or_create_user
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

CALL_PREMIUM_TEXT = "📞 Голосовой разговор — для подписчиков. Остальное работает как обычно: вопросы, «Экзамен» (текстом) и самопроверка."
CALL_NOT_READY_TEXT = "Звонок пока не настроен на сервере. Напиши администратору."
CALL_INTRO = (
    "📞 Звонок. Нажми кнопку — откроется окно разговора. Говори как с преподавателем: замолчишь — я отвечу голосом. "
    "Перебить меня можно, просто начав говорить. Ссылка работает 2 часа."
)


@router.message(Command("call"))
@router.message(F.text == BTN_CALL)
async def cmd_call(message: Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await session.commit()
    if not can_use_deep(user):
        await message.answer(CALL_PREMIUM_TEXT)
        return
    if not settings.SLIDES_SITE_URL or not settings.SERVICE_TOKEN:
        await message.answer(CALL_NOT_READY_TEXT)
        return
    token = sign_call_token(settings.SERVICE_TOKEN, user.id, user.current_subject)
    await message.answer(
        CALL_INTRO,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📞 Начать разговор", web_app=WebAppInfo(url=call_url(settings.SLIDES_SITE_URL, token)))]]
        ),
    )
