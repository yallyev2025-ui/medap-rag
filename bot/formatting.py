"""Преобразование Markdown-ответов генератора в Telegram HTML.

Модели пишут «настоящий» Markdown (заголовки #, маркеры «*», *курсив*, таблицы,
линии ---), а Telegram в HTML-режиме понимает только небольшой набор тегов.
Всё неподдерживаемое превращаем в читаемый текст, а не оставляем сырыми символами.
"""

import html
import re

TELEGRAM_MESSAGE_LIMIT = 4096

_FENCE = re.compile(r"^\s*```.*$")
# Огороженный блок ```…``` (схема-дерево, сравнение колонками): выводится моноширинно,
# как есть, без обработки жирного/курсива/маркеров внутри.
_FENCED_BLOCK = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*•+]\s+(?=\S)")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_ITALIC_UNDERSCORE = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")
_CODE = re.compile(r"`([^`\n]+)`")
# [текст](https://…) → кликабельная ссылка. Только http/https (никаких javascript: и т.п.).
_LINK = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()<>\"]+)\)")


# Абзац длиннее этого читается как стена текста и режется (список или короткие абзацы)
_WALL_CHARS = 400
_LIST_LINE = re.compile(r"^\s*(?:[-*•+]\s|\d{1,3}\s*[.)]\s|#{1,6}\s|\|)")
# Конец предложения: точка/!/? и дальше заглавная (аббревиатуры «т.е.», «напр.» строчные — не режутся)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«\"*])")


def _split_outside_parens(text: str, sep: str) -> list[str]:
    """Делит по `sep`, не трогая то, что в скобках («(стрептококк; стафилококк)»)."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == sep and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def _break_wall(paragraph: str) -> str:
    """Длинный абзац → список (если это перечень через «;») или короткие абзацы по два предложения.

    Модель иногда пишет учебным слогом одним полотном; промпт это запрещает, но надёжнее
    подстраховать кодом, на любой модели."""
    if len(paragraph) <= _WALL_CHARS or _LIST_LINE.match(paragraph):
        return paragraph

    items = _split_outside_parens(paragraph, ";")
    if len(items) >= 3:
        intro = ""
        colon = items[0].find(": ")
        # «Название: первый элемент» — до двоеточия вводная строка, дальше пункты
        if 0 < colon <= 220:
            intro = items[0][: colon + 1]
            items[0] = items[0][colon + 2 :]
        # Первый элемент — обычно целое предложение-вводка и первый пункт: вводка отдельной
        # строкой; в последнем элементе после точки начинается уже новая мысль — абзацем после списка
        if not intro:
            head = _SENTENCE_END.split(items[0])
            if len(head) > 1:
                intro = " ".join(head[:-1])
                items[0] = head[-1]
        tail = ""
        end = _SENTENCE_END.split(items[-1])
        if len(end) > 1:
            items[-1] = end[0]
            tail = " ".join(end[1:])

        lines = [intro] if intro else []
        lines += [f"- {item[:1].upper()}{item[1:]}".rstrip(".") for item in items]
        if tail:
            lines += ["", tail]
        return "\n".join(lines)

    sentences = _SENTENCE_END.split(paragraph)
    if len(sentences) < 3:
        return paragraph
    chunks = [" ".join(sentences[i : i + 2]) for i in range(0, len(sentences), 2)]
    return "\n\n".join(chunks)


def _break_walls(text: str) -> str:
    return "\n".join(_break_wall(line) if len(line) > _WALL_CHARS else line for line in text.split("\n"))


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
    # Схемы в ``` прячем за метками, чтобы разметка внутри них не трогалась.
    blocks: list[str] = []

    def _stash_block(match: re.Match) -> str:
        blocks.append(f"<pre>{html.escape(match.group(1).rstrip(), quote=False)}</pre>")
        return f"\n\x01{len(blocks) - 1}\x01\n"

    text = _FENCED_BLOCK.sub(_stash_block, text.replace("\r\n", "\n"))
    escaped = html.escape(_normalize_lines(_break_walls(text)), quote=False)
    # Ссылки прячем за метками до обработки жирного/курсива: «_» и «*» внутри URL
    # не должны превратиться в разметку.
    links: list[str] = []

    def _stash(match: re.Match) -> str:
        links.append(f'<a href="{match.group(2)}">{match.group(1)}</a>')
        return f"\x00{len(links) - 1}\x00"

    escaped = _LINK.sub(_stash, escaped)
    escaped = _CODE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_STAR.sub(r"<i>\1</i>", escaped)
    escaped = _ITALIC_UNDERSCORE.sub(r"<i>\1</i>", escaped)
    # Незакрытый/потерянный «**» (например, ответ оборвался) не должен светиться в чате.
    escaped = escaped.replace("**", "")
    escaped = re.sub("\x00(\\d+)\x00", lambda m: links[int(m.group(1))], escaped)
    escaped = re.sub("\x01(\\d+)\x01", lambda m: blocks[int(m.group(1))], escaped)
    return re.sub(r"\n{3,}", "\n\n", escaped).strip()


def split_for_telegram(html_text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Разбивает HTML-текст на части по лимиту Telegram, не разрывая теги <b>/<i>/<pre>."""
    chunks = []
    while len(html_text) > limit:
        split_at = html_text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit

        chunk = html_text[:split_at]
        rest = html_text[split_at:].lstrip("\n")

        for tag in ("b", "i", "pre"):
            unclosed = chunk.count(f"<{tag}>") - chunk.count(f"</{tag}>")
            if unclosed > 0:
                chunk += f"</{tag}>" * unclosed
                rest = f"<{tag}>" * unclosed + rest

        chunks.append(chunk)
        html_text = rest

    chunks.append(html_text)
    return chunks
