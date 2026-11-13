"""Последний содержательный ответ бота в чате — из него собирается документ по просьбе «сделай конспект из этого».

Хранится в памяти процесса, не в базе: ответ нужен минуты, а не вечно; при перезапуске бот честно скажет «сначала задайте вопрос».
"""

from __future__ import annotations

import time

_TTL_SECONDS = 2 * 60 * 60
_MAX_CHATS = 2000
_store: dict[int, tuple[float, str]] = {}


def remember_last_answer(chat_id: int, text: str) -> None:
    """Запомнить ответ (HTML-разметка Telegram не нужна: сюда идёт текст до разметки). Слишком короткое не запоминается."""
    if not text or len(text.strip()) < 80:
        return
    now = time.time()
    if len(_store) >= _MAX_CHATS:
        for key in [k for k, (ts, _) in _store.items() if now - ts > _TTL_SECONDS][:200] or list(_store)[:200]:
            _store.pop(key, None)
    _store[chat_id] = (now, text)


def last_answer(chat_id: int) -> str | None:
    item = _store.get(chat_id)
    if item is None:
        return None
    ts, text = item
    if time.time() - ts > _TTL_SECONDS:
        _store.pop(chat_id, None)
        return None
    return text
