"""Страховка формы ответа (батч 31): ответ «полотном» перестраивается, факты не меняются.

DeepSeek в «Быстром» режиме иногда отдаёт учебный текст одним сплошным абзацем, хотя промпт
это запрещает. Промпт и образец оформления снижают долю таких ответов, а здесь — последняя
линия: детерминированно узнаём «полотно» и один раз просим модель только перестроить форму.
Результат принимается лишь если числа и ссылки на источники остались теми же — иначе
остаётся исходный ответ.
"""

import re

from app.verification.numeric import extract_numeric_claims

LONG_PARAGRAPH = 450
LONG_TEXT = 900

_LIST_ITEM = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*(?:\*\*[^*\n]{2,80}\*\*:?\s*|#{1,6}\s+.+)$")
_BRACKETS = re.compile(r"\[[^\[\]\n]{3,}\]")


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def is_wall(text: str) -> bool:
    """Ответ — сплошное полотно: очень длинный абзац без списков или большой текст вообще без структуры."""
    lines = _lines(text)
    items = sum(1 for line in lines if _LIST_ITEM.match(line))
    headings = sum(1 for line in lines if _HEADING.match(line))
    prose = [len(line) for line in lines if not _LIST_ITEM.match(line) and not _HEADING.match(line)]
    longest = max(prose, default=0)
    if longest > LONG_PARAGRAPH and items < 2:
        return True
    return len(text) > LONG_TEXT and items == 0 and headings == 0


def preserved(original: str, rewritten: str) -> bool:
    """Перестроенный текст принимается, только если не потерял и не добавил факты."""
    if not rewritten.strip():
        return False
    ratio = len(rewritten) / max(1, len(original))
    if not 0.6 <= ratio <= 1.4:
        return False
    if not extract_numeric_claims(rewritten) <= extract_numeric_claims(original):
        return False
    return sorted(_BRACKETS.findall(rewritten)) == sorted(_BRACKETS.findall(original))
