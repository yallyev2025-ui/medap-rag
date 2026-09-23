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
    r"мм\s?рт\.?\s?ст\.?|уд/мин|ед\.?|МЕ|ккал|кал|мин|час(?:а|ов)?|ч|сут(?:ки|ок)?|"
    r"нед(?:еля|ели|ель)?|мес(?:яц(?:а|ев)?)?|лет|год(?:а|ов)?"
)
_NUMERIC_CLAIM = re.compile(
    rf"(\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*(\d+(?:[.,]\d+)?))?\s*({_UNITS})\b",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    return value.replace(",", ".")


def extract_numeric_claims(text: str) -> set[tuple[str, str]]:
    """Множество (число, единица) — единица приводится к нижнему регистру."""
    claims: set[tuple[str, str]] = set()
    for number, number_to, unit in _NUMERIC_CLAIM.findall(text):
        unit_norm = unit.lower()
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
