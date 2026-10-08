"""Нижняя панель Telegram (батч 27): постоянные кнопки вместо списков под сообщениями.

Telegram сам сворачивает/разворачивает такую клавиатуру значком у поля ввода — кодом это
не управляется. Каждый экран, кроме главного, содержит «⬅️ Назад». Нажатие кнопки приходит
как обычный текст, поэтому у служебных текстов есть узнаваемые приставки/значения
(`is_panel_text`) — они не считаются вопросами студента и не тратят лимит.
"""

import json

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from constants import (
    CLINREK_CATEGORIES,
    TIER_LABELS,
    SCOPE_BOTH,
    SCOPE_DOCUMENTS,
    SCOPE_TEXTBOOK,
    SOURCE_CLINREK,
    clinrek_label,
    subject_label,
)

BTN_TEXTBOOKS = "📚 Учебники"
BTN_DOCS = "📄 Мои документы"
BTN_CLINREK = "📋 Клин. рекомендации"
BTN_SCOPE = "🔀 Откуда отвечать"
BTN_TIER = "⚙️ Режим ответа"
BTN_BACK = "⬅️ Назад"
BTN_UPLOAD = "➕ Загрузить документ"
BTN_SYMPTOM = "🩺 Разбор по симптомам"

# Приставки динамических кнопок (предметы и категории зависят от базы).
SUBJECT_PREFIX = "📘 "
CATEGORY_PREFIX = "🗂 "

SCOPE_LABELS = {
    SCOPE_DOCUMENTS: "📄 Только мои документы",
    SCOPE_TEXTBOOK: "📚 Только учебники",
    SCOPE_BOTH: "📄+📚 Мои и учебники",
}
_ACTIVE_MARK = "✅ "

_STATIC_TEXTS = frozenset(
    {BTN_TEXTBOOKS, BTN_DOCS, BTN_CLINREK, BTN_SCOPE, BTN_TIER, BTN_BACK, BTN_UPLOAD, BTN_SYMPTOM}
    | set(SCOPE_LABELS.values())
    | {_ACTIVE_MARK + label for label in SCOPE_LABELS.values()}
    | set(TIER_LABELS.values())
    | {_ACTIVE_MARK + label for label in TIER_LABELS.values()}
)

PLACEHOLDER_MAX = 64


def is_panel_text(text: str | None) -> bool:
    """Текст — нажатие кнопки нижней панели, а не вопрос студента."""
    if not text:
        return False
    return text in _STATIC_TEXTS or text.startswith(SUBJECT_PREFIX) or text.startswith(CATEGORY_PREFIX)


def scope_from_button(text: str) -> str | None:
    plain = text[len(_ACTIVE_MARK):] if text.startswith(_ACTIVE_MARK) else text
    for scope, label in SCOPE_LABELS.items():
        if label == plain:
            return scope
    return None


def tier_from_button(text: str) -> str | None:
    plain = text[len(_ACTIVE_MARK):] if text.startswith(_ACTIVE_MARK) else text
    for tier, label in TIER_LABELS.items():
        if label == plain:
            return tier
    return None


def subject_from_button(text: str, subjects: list[str]) -> str | None:
    label = text[len(SUBJECT_PREFIX):] if text.startswith(SUBJECT_PREFIX) else text
    return next((s for s in subjects if subject_label(s) == label), None)


def category_from_button(text: str) -> tuple[str, str | None] | None:
    """(код категории, значение subject) по тексту кнопки или None."""
    label = text[len(CATEGORY_PREFIX):] if text.startswith(CATEGORY_PREFIX) else text
    for code, cat_label, value in CLINREK_CATEGORIES:
        if cat_label == label:
            return code, value
    return None


def _placeholder(text: str) -> str:
    return text[:PLACEHOLDER_MAX]


def context_placeholder(source_type: str | None, subject: str | None, scope: str, docs_count: int, symptom: bool = False) -> str:
    """Подсказка в поле ввода — всегда видно, откуда сейчас отвечает бот."""
    if source_type == SOURCE_CLINREK:
        if symptom:
            return _placeholder("Опиши жалобы: 🩺 разбор по симптомам")
        return _placeholder(f"Спроси по: 📋 {clinrek_label(subject)}")
    subj = subject_label(subject) if subject else "предмет не выбран"
    if scope == SCOPE_DOCUMENTS:
        return _placeholder(f"Спроси по: 📄 моим документам ({docs_count})")
    if scope == SCOPE_BOTH:
        return _placeholder(f"Спроси по: 📄 ({docs_count}) + 📚 {subj}")
    return _placeholder(f"Спроси по: 📚 {subj}")


def status_line(source_type: str | None, subject: str | None, scope: str, docs_count: int, symptom: bool = False) -> str:
    if source_type == SOURCE_CLINREK:
        if symptom:
            return "Сейчас: 📋 Клин. рекомендации — 🩺 разбор по симптомам"
        return f"Сейчас: 📋 Клин. рекомендации — {clinrek_label(subject)}"
    subj = subject_label(subject) if subject else "предмет не выбран"
    if scope == SCOPE_DOCUMENTS:
        return f"Сейчас: 📄 только мои документы (включено: {docs_count})"
    if scope == SCOPE_BOTH:
        return f"Сейчас: 📄 мои документы ({docs_count}) + 📚 учебники — {subj}"
    return f"Сейчас: 📚 только учебники — {subj}"


def main_keyboard(show_clinrek: bool, placeholder: str = "Задай вопрос…") -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_TEXTBOOKS), KeyboardButton(text=BTN_DOCS)]]
    second = [KeyboardButton(text=BTN_SCOPE)]
    if show_clinrek:
        second.insert(0, KeyboardButton(text=BTN_CLINREK))
    rows.append(second)
    # Режим ответа — отдельной строкой: выбор «Быстрый/Глубокий» (глубокий — Premium).
    rows.append([KeyboardButton(text=BTN_TIER)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=_placeholder(placeholder),
    )


def _grid(buttons: list[KeyboardButton], per_row: int = 2) -> list[list[KeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def subjects_keyboard(subjects: list[str]) -> ReplyKeyboardMarkup:
    buttons = [KeyboardButton(text=SUBJECT_PREFIX + subject_label(s)) for s in subjects]
    rows = _grid(buttons) + [[KeyboardButton(text=BTN_BACK)]]
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Выбери предмет"
    )


def categories_keyboard() -> ReplyKeyboardMarkup:
    buttons = [KeyboardButton(text=CATEGORY_PREFIX + label) for _code, label, _value in CLINREK_CATEGORIES]
    rows = _grid(buttons) + [[KeyboardButton(text=BTN_SYMPTOM)], [KeyboardButton(text=BTN_BACK)]]
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Выбери категорию"
    )


def scope_keyboard(current: str) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=(_ACTIVE_MARK if scope == current else "") + label)]
        for scope, label in SCOPE_LABELS.items()
    ]
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Откуда отвечать?"
    )


def tier_keyboard(current: str) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=(_ACTIVE_MARK if tier == current else "") + label)]
        for tier, label in TIER_LABELS.items()
    ]
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Режим ответа"
    )


def docs_bottom_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_UPLOAD)], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Пришли файл или выбери документ",
    )


# --- Inline-панель «Мои документы» (редактируется на месте, без спама в чат) ----------

_TITLE_MAX = 32


def _short(title: str) -> str:
    return title if len(title) <= _TITLE_MAX else title[: _TITLE_MAX - 1] + "…"


def docs_panel(
    documents: list[tuple[int, str]], active_ids: list[int], scope: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Корневая панель: документы с галочками, режим ответа, загрузка/удаление, «Назад»."""
    active = set(active_ids)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✅ " if doc_id in active else "⬜ ") + _short(title),
                callback_data=f"docs:t:{doc_id}",
            )
        ]
        for doc_id, title in documents
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=("● " if scope == key else "") + label.replace("Только ", ""),
                callback_data=f"docs:s:{key}",
            )
            for key, label in SCOPE_LABELS.items()
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="➕ Загрузить", callback_data="docs:up"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="docs:del"),
        ]
    )
    rows.append([InlineKeyboardButton(text=BTN_BACK, callback_data="docs:home")])
    if documents:
        text = "📄 Мои документы\nНажми на документ, чтобы включить ✅ или выключить ⬜. Бот ищет по включённым."
    else:
        text = "📄 Мои документы\nПока нет ни одного. Пришли файл PDF, DOCX или TXT (до 20 МБ) — я его разберу."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def docs_delete_list(documents: list[tuple[int, str]]) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [InlineKeyboardButton(text="🗑 " + _short(title), callback_data=f"docs:d:{doc_id}")]
        for doc_id, title in documents
    ]
    rows.append([InlineKeyboardButton(text=BTN_BACK, callback_data="docs:back")])
    return "Какой документ удалить?", InlineKeyboardMarkup(inline_keyboard=rows)


def docs_delete_confirm(doc_id: int, title: str) -> tuple[str, InlineKeyboardMarkup]:
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить", callback_data=f"docs:y:{doc_id}")],
            [InlineKeyboardButton(text=BTN_BACK, callback_data="docs:del")],
        ]
    )
    return f"Удалить «{title}» без возможности восстановить?", markup


def all_back_buttons(markup: InlineKeyboardMarkup | ReplyKeyboardMarkup) -> bool:
    """Есть ли на экране «⬅️ Назад» (проверка в тестах: ни один экран без «Назад»)."""
    if isinstance(markup, InlineKeyboardMarkup):
        return any(BTN_BACK in b.text for row in markup.inline_keyboard for b in row)
    return any(b.text == BTN_BACK for row in markup.keyboard for b in row)


def encode_ids(ids: list[int]) -> str:
    return json.dumps(sorted(set(ids)))
