"""Чистый текст для сайта (батч 29).

Образовательный сайт MedAP показывает ответ бота как ОБЫЧНЫЙ текст: абзацы режутся по пустой
строке, одиночный перенос склеивается, разметки нет и не будет (HTML из ответа модели не
вставляется). Поэтому для канала `api` по умолчанию отдаётся «plain»: ни `**`, ни `#`, ни
значков-эмодзи, ни таблиц; каждый пункт списка — отдельный абзац. Основную работу делает
инструкция модели (rag/generator.py::SITE_FORMAT_*); здесь — детерминированная страховка на случай,
если модель всё же вставила разметку.
"""

import re

# Эмодзи и значки-украшения. Стрелки «→», греческие буквы, знаки «±», «≥» сюда не попадают.
_DECOR = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
_FENCE = re.compile(r"^\s*```.*$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*•+]\s+(\S.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,3}[.)])\s+(\S.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_ITALIC_UNDERSCORE = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")
_CODE = re.compile(r"`([^`\n]*)`")
_LINK = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")


def _inline(text: str) -> str:
    text = _LINK.sub(r"\1 (\2)", text)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDERSCORE.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    # Потерянные одиночные маркеры (оборванный ответ).
    return text.replace("**", "")


def to_plain_text(text: str) -> str:
    """Markdown/Telegram-разметка → чистый текст с абзацами через пустую строку."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if _FENCE.match(line) or _RULE.match(line):
            continue
        if _TABLE_ROW.match(line):
            if _TABLE_SEPARATOR.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = "— " + " — ".join(c for c in cells if c)
        elif (heading := _HEADING.match(line)):
            line = heading.group(1)
        elif (bullet := _BULLET.match(line)):
            line = ("– " if len(bullet.group(1)) >= 2 else "— ") + bullet.group(2)
        elif (numbered := _NUMBERED.match(line)):
            line = f"{numbered.group(2)} {numbered.group(3)}"
        line = _DECOR.sub("", _inline(line)).strip()
        out.append(line)

    # Каждая непустая строка — отдельный абзац: на сайте одиночный перенос склеивается.
    paragraphs = [line for line in out if line]
    return "\n\n".join(paragraphs).strip()
