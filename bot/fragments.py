"""Показ фрагмента источника в Telegram («📖 Показать фрагмент»).

Под ответом — кнопки по реально процитированным источникам. Нажатие присылает точный
текст фрагмента из учебника/документа с названием и страницей; предложения, на которые
опирается ответ, выделены жирным. Выделение — по совпадению слов с ответом, без LLM
(детерминированно и бесплатно). Модуль чистый (без БД и Telegram-вызовов) — легко тестируется.
"""

import html
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_PREFIX = "ev:"
MAX_BUTTONS = 6
USER_MATERIAL = "user_material"

# Сообщение Telegram — до 4096 знаков; оставляем запас под заголовок и разметку.
MAX_FRAGMENT_CHARS = 3300
_STEM_LEN = 5
_MIN_WORD = 5
_MIN_OVERLAP = 3
_MAX_HIGHLIGHTED = 2

_WORD = re.compile(r"[а-яёa-z]{%d,}" % _MIN_WORD, re.IGNORECASE)
_SPLIT_KEEP_SPACES = re.compile(r"(?<=[.!?])(\s+)")


def _source_name(citation: dict) -> str:
    title = (citation.get("sourceTitle") or "").strip()
    author = (citation.get("author") or "").strip()
    if author and author.lower() not in title.lower():
        return f"{author}, {title}"
    return title


def _pages(page: int | None, page_to: int | None) -> str:
    if page is None:
        return ""
    if page_to and page_to != page:
        return f"стр. {page}–{page_to}"
    return f"стр. {page}"


def _short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def citation_buttons(citations: list[dict]) -> InlineKeyboardMarkup | None:
    """Кнопки «📖 источник · стр.» по уникальным цитатам (не больше MAX_BUTTONS)."""
    rows: list[list[InlineKeyboardButton]] = []
    seen: set[str] = set()
    for citation in citations or []:
        evidence_id = str(citation.get("evidenceId") or "")
        if not evidence_id.isdigit() or evidence_id in seen:
            continue
        seen.add(evidence_id)
        icon = "📄" if citation.get("authorityLevel") == USER_MATERIAL else "📖"
        pages = _pages(citation.get("page"), citation.get("pageTo"))
        label = f"{icon} {_short(_source_name(citation))}" + (f" · {pages}" if pages else "")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{CALLBACK_PREFIX}{evidence_id}")])
        if len(rows) >= MAX_BUTTONS:
            break
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def evidence_id_from_callback(data: str | None) -> int | None:
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    tail = data[len(CALLBACK_PREFIX):]
    return int(tail) if tail.isdigit() else None


def _stems(text: str) -> set[str]:
    return {w.lower()[:_STEM_LEN] for w in _WORD.findall(text or "")}


def highlight_fragment(fragment: str, answer: str) -> tuple[str, bool]:
    """(HTML фрагмента, были ли выделения). Жирным — до двух предложений, сильнее всего
    совпадающих по смыслу (корням слов) с ответом, минимум _MIN_OVERLAP общих корней."""
    fragment = fragment.strip()
    if len(fragment) > MAX_FRAGMENT_CHARS:
        fragment = fragment[: MAX_FRAGMENT_CHARS].rsplit(" ", 1)[0] + "…"

    answer_stems = _stems(answer)
    pieces = _SPLIT_KEEP_SPACES.split(fragment)
    # Чётные элементы — предложения, нечётные — разделители (пробелы/переводы строк).
    scored = [
        (len(_stems(piece) & answer_stems), index)
        for index, piece in enumerate(pieces)
        if index % 2 == 0 and piece.strip()
    ]
    best = {index for score, index in sorted(scored, reverse=True)[:_MAX_HIGHLIGHTED] if score >= _MIN_OVERLAP}

    out = []
    for index, piece in enumerate(pieces):
        escaped = html.escape(piece, quote=False)
        out.append(f"<b>{escaped}</b>" if index in best and piece.strip() else escaped)
    return "".join(out), bool(best)


def render_fragment(detail, answer: str) -> str:
    """Текст сообщения с фрагментом: источник, страница, выделенный текст, пояснение."""
    name = _source_name({"sourceTitle": detail.source_title, "author": detail.author})
    pages = _pages(detail.page, detail.page_to)
    icon = "📄" if detail.authority_level == USER_MATERIAL else "📖"
    head = f"{icon} <b>{html.escape(name, quote=False)}</b>" + (f" — {pages}" if pages else "")
    if detail.section:
        head += f"\n<i>{html.escape(detail.section, quote=False)}</i>"
    body, highlighted = highlight_fragment(detail.exact_supporting_text, answer)
    note = "\n\n<i>Жирным — места, на которые опирается ответ.</i>" if highlighted else ""
    return f"{head}\n\n{body}{note}"
