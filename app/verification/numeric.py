"""Числовая проверка ответов (§14 ТЗ): дозы, единицы измерения, диапазоны,
проценты, лабораторные значения.

Эвристика на регулярных выражениях, а не полноценный парсер: ищем пары «число +
медицинская единица» в ответе и в процитированном контексте. Число из ответа,
которого нет рядом с той же единицей нигде в контексте, — сигнал для
перегенерации, а не жёсткий отдельный gate (см. app/verification/verify.py) —
иначе ложные срабатывания эвристики роняли бы корректные ответы.
"""

import re

_UNITS = (
    r"мг(?:/кг)?|г(?:/л)?|мкг|мл(?:/мин)?|л|%|ммоль/л|мкмоль/л|моль/л|"
    r"мм\s?рт\.?\s?ст\.?|уд/мин|ед\.?|МЕ|ккал|кал|мин(?:ут(?:а|ы)?)?|час(?:а|ов)?|ч|"
    r"сут(?:ки|ок)?|дн(?:я|ей|ь)?|день|нед(?:еля|ели|ель)?|мес(?:яц(?:а|ев)?)?|лет|год(?:а|ов)?"
)
# Диапазон пишут по-разному: «6–12», «6-12», «от 6 до 12 мес». Раньше «до» не понималось, и из «от 6 до 12 мес»
# в учебнике получалась только пара «12 мес» — верное «6–12 мес» в ответе считалось выдумкой (замер 12.10.2026)
_NUMERIC_CLAIM = re.compile(
    rf"(\d+(?:[.,]\d+)?)\s*(?:(?:[-–—]|до)\s*(\d+(?:[.,]\d+)?)\s*)?({_UNITS})\b",
    re.IGNORECASE,
)

# Единицы времени и их падежные формы приводятся к одной: «6 мес» и «6 месяцев», «48 ч» и «48 часов» — одно и то же
_UNIT_FAMILIES = (
    (re.compile(r"^мин"), "мин"),
    (re.compile(r"^(?:час|ч$)"), "ч"),
    (re.compile(r"^(?:сут|дн|день)"), "сут"),
    (re.compile(r"^нед"), "нед"),
    (re.compile(r"^мес"), "мес"),
    (re.compile(r"^(?:лет|год)"), "год"),
)


def _canonical_unit(unit: str) -> str:
    text = re.sub(r"[\s.]", "", unit.lower())
    for pattern, family in _UNIT_FAMILIES:
        if pattern.match(text):
            return family
    return text


def _normalize(value: str) -> str:
    return value.replace(",", ".")


def extract_numeric_claims(text: str) -> set[tuple[str, str]]:
    """Множество (число, единица) — единица приводится к нормальной форме (падежи времени, пробелы и точки)."""
    claims: set[tuple[str, str]] = set()
    for number, number_to, unit in _NUMERIC_CLAIM.findall(text):
        unit_norm = _canonical_unit(unit)
        claims.add((_normalize(number), unit_norm))
        if number_to:
            claims.add((_normalize(number_to), unit_norm))
    return claims


def find_unsupported_numbers(answer: str, context: str) -> list[str]:
    """Числа+единицы из ответа, отсутствующие среди чисел+единиц контекста."""
    answer_claims = extract_numeric_claims(answer)
    if not answer_claims:
        return []
    context_claims = extract_numeric_claims(context)
    unsupported = answer_claims - context_claims
    return [f"{number} {unit}" for number, unit in sorted(unsupported)]
