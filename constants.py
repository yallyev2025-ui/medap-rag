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
