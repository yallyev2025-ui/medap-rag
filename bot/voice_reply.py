"""Озвучка ответов в Telegram (MedAP, этап 5): только подписчикам; ответ приходит голосовым сообщением (OGG/Opus)."""

from __future__ import annotations

import logging
import re

from aiogram.types import BufferedInputFile, Message

from app.llm import provider as llm
from app.voice.text import prepare_for_speech, split_for_speech
from config import settings

logger = logging.getLogger(__name__)

VOICE_PREMIUM_TEXT = "🔊 Голосовые ответы и голосовые вопросы — для подписчиков. Остальное работает как обычно, текстом."
SPEAK_ERROR_TEXT = "Не удалось озвучить ответ — вот он текстом выше."
MAX_VOICE_PARTS = 3

_TAGS = re.compile(r"<[^>]+>")


async def speak_to(message: Message, text: str, voice: str | None) -> bool:
    """Озвучить текст ответом на сообщение. Возвращает, получилось ли; ошибка озвучки ответ не ломает."""
    spoken = prepare_for_speech(_TAGS.sub("", text))
    if not spoken:
        return False
    parts = split_for_speech(spoken, settings.TTS_MAX_CHARS)[:MAX_VOICE_PARTS]
    try:
        for part in parts:
            audio, _usd = await llm.synthesize(part, voice or settings.TTS_DEFAULT_VOICE, 1.0, "opus")
            await message.answer_voice(BufferedInputFile(audio, filename="answer.ogg"))
    except Exception:
        logger.exception("Ошибка озвучки ответа")
        await message.answer(SPEAK_ERROR_TEXT)
        return False
    return True
