"""Общие константы режимов работы (используются ботом, поиском и загрузчиками).

Две логические базы живут в одной таблице book_chunks и различаются полем
source_type. Для учебников subject — это предмет (physiology, pathanatomy, ...),
для клинреков — категория пациентов (взрослые/дети/взрослые_и_дети).
"""

SOURCE_TEXTBOOK = "учебник"
SOURCE_CLINREK = "клинрек"

# Категории клинреков: (код для callback_data, человекочитаемая метка, значение subject в БД).
# Значение None у "Все категории" означает поиск без фильтра по subject.
CLINREK_CATEGORIES: list[tuple[str, str, str | None]] = [
    ("adults", "Взрослые", "взрослые"),
    ("children", "Дети", "дети"),
    ("both", "Взрослые и дети", "взрослые_и_дети"),
    ("all", "Все категории", None),
]

# Человекочитаемые названия предметов учебников для кодов из bot.handlers.admin.SUBJECTS.
# Для произвольных (кастомных) предметов метка берётся из самого значения.
SUBJECT_LABELS: dict[str, str] = {
    "pathanatomy": "Патанатомия",
    "pathphys": "Патофизиология",
    "physiology": "Физиология",
    "anatomy": "Анатомия",
    "biochemistry": "Биохимия",
    "pharmacology": "Фармакология",
    "other": "Другой",
}


def subject_label(subject: str) -> str:
    """Метка предмета для кнопок/сообщений: из словаря либо само значение с заглавной."""
    return SUBJECT_LABELS.get(subject, subject.capitalize())


def clinrek_label(subject: str | None) -> str:
    """Метка категории клинреков по значению subject из БД."""
    for _code, label, value in CLINREK_CATEGORIES:
        if value == subject:
            return label
    return subject or "Все категории"


# --- Иерархия источников и provenance (§9 ТЗ, этап 2) ---------------------------
# По убыванию приоритета. Кафедральный материал может иметь приоритет в
# exam-контексте, но не переписывает глобальную медицинскую истину MedAP —
# приоритет применяется при отборе evidence (этап 3), а не подменяет фильтры
# источника здесь. Порядок в списке = порядок в выпадающем списке админки.
AUTHORITY_LEVELS: list[tuple[str, str]] = [
    ("department_exam", "Кафедральный / экзаменационный материал"),
    ("medap_verified", "MedAP Verified Content"),
    ("primary_textbook", "Основной учебник предмета"),
    ("secondary_textbook", "Дополнительный учебник"),
    ("clinical_guideline", "Официальная клиническая рекомендация"),
    ("user_material", "Пользовательский материал"),
    ("web", "Веб-источник"),
]
AUTHORITY_LEVEL_CODES = [code for code, _ in AUTHORITY_LEVELS]
DEFAULT_AUTHORITY_LEVEL = "primary_textbook"

# Статус проверки содержимого источника человеком.
VERIFICATION_STATUSES: list[tuple[str, str]] = [
    ("unverified", "Не проверено"),
    ("verified", "Проверено"),
    ("disputed", "Оспаривается"),
]
DEFAULT_VERIFICATION_STATUS = "unverified"

# Жизненный цикл источника в Knowledge Base. "disabled" и "archived" исключают
# источник из retrieval, не удаляя чанки/эмбеддинги — можно вернуть без
# повторной загрузки и пересчёта.
SOURCE_STATUSES: list[tuple[str, str]] = [
    ("draft", "Черновик"),
    ("production", "В работе (используется поиском)"),
    ("disabled", "Отключён"),
    ("archived", "В архиве"),
]
DEFAULT_SOURCE_STATUS = "production"
# Статусы, при которых источник участвует в поиске.
ACTIVE_SOURCE_STATUSES = ("production",)


def authority_label(code: str) -> str:
    for value, label in AUTHORITY_LEVELS:
        if value == code:
            return label
    return code


def verification_label(code: str) -> str:
    for value, label in VERIFICATION_STATUSES:
        if value == code:
            return label
    return code


def source_status_label(code: str) -> str:
    for value, label in SOURCE_STATUSES:
        if value == code:
            return label
    return code


# --- Answer Inspector: причины плохого ответа (раздел 8 дополнения к ТЗ) --------
FEEDBACK_REASONS: list[tuple[str, str]] = [
    ("incorrect_answer", "Неверный ответ"),
    ("bad_retrieval", "Плохой поиск"),
    ("bad_citation", "Неверная цитата"),
    ("insufficient_source", "Недостаточно материала в источнике"),
    ("explanation_problem", "Проблема объяснения"),
    ("source_conflict", "Конфликт источников"),
    ("evaluation_problem", "Проблема оценки"),
    ("other", "Другое"),
]
