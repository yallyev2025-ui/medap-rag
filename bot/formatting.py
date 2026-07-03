"""Преобразование Markdown-ответов генератора в Telegram HTML."""

import html
import re

_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

TELEGRAM_MESSAGE_LIMIT = 4096


def to_telegram_html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _BOLD_PATTERN.sub(r"<b>\1</b>", escaped)


def split_for_telegram(html_text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Разбивает HTML-текст на части по лимиту Telegram, не разрывая теги <b>."""
    chunks = []
    while len(html_text) > limit:
        split_at = html_text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit

        chunk = html_text[:split_at]
        rest = html_text[split_at:].lstrip("\n")

        unclosed = chunk.count("<b>") - chunk.count("</b>")
        if unclosed > 0:
            chunk += "</b>" * unclosed
            rest = "<b>" * unclosed + rest

        chunks.append(chunk)
        html_text = rest

    chunks.append(html_text)
    return chunks
