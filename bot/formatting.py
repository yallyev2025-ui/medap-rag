"""Преобразование Markdown-ответов генератора в Telegram HTML.

Модели пишут «настоящий» Markdown (заголовки #, маркеры «*», *курсив*, таблицы,
линии ---), а Telegram в HTML-режиме понимает только небольшой набор тегов.
Всё неподдерживаемое превращаем в читаемый текст, а не оставляем сырыми символами.
"""

import html
import re

TELEGRAM_MESSAGE_LIMIT = 4096

_FENCE = re.compile(r"^\s*```.*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*•+]\s+(?=\S)")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_ITALIC_UNDERSCORE = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")
_CODE = re.compile(r"`([^`\n]+)`")


def _normalize_lines(text: str) -> str:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if _FENCE.match(line) or _RULE.match(line):
            continue
        if _TABLE_ROW.match(line):
            if _TABLE_SEPARATOR.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = "- " + " — ".join(c for c in cells if c)
        heading = _HEADING.match(line)
        if heading:
            line = f"**{heading.group(1)}**"
        else:
            line = _BULLET.sub(lambda m: "    ◦ " if len(m.group(1)) >= 2 else "• ", line)
        out.append(line)
    return "\n".join(out)


def to_telegram_html(text: str) -> str:
    escaped = html.escape(_normalize_lines(text), quote=False)
    escaped = _CODE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_STAR.sub(r"<i>\1</i>", escaped)
    escaped = _ITALIC_UNDERSCORE.sub(r"<i>\1</i>", escaped)
    # Незакрытый/потерянный «**» (например, ответ оборвался) не должен светиться в чате.
    escaped = escaped.replace("**", "")
    return re.sub(r"\n{3,}", "\n\n", escaped).strip()


def split_for_telegram(html_text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Разбивает HTML-текст на части по лимиту Telegram, не разрывая теги <b>/<i>."""
    chunks = []
    while len(html_text) > limit:
        split_at = html_text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit

        chunk = html_text[:split_at]
        rest = html_text[split_at:].lstrip("\n")

        for tag in ("b", "i"):
            unclosed = chunk.count(f"<{tag}>") - chunk.count(f"</{tag}>")
            if unclosed > 0:
                chunk += f"</{tag}>" * unclosed
                rest = f"<{tag}>" * unclosed + rest

        chunks.append(chunk)
        html_text = rest

    chunks.append(html_text)
    return chunks
