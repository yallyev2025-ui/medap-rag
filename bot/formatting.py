"""Преобразование Markdown-ответов генератора в Telegram HTML."""

import html
import re

_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def to_telegram_html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _BOLD_PATTERN.sub(r"<b>\1</b>", escaped)
