"""Подготовка текста ответа ментора к озвучке (MedAP, этап 5). Чистая логика: без сети, базы и моделей.

Что делает: убирает разметку, ссылки и метки источников, раскрывает сокращения и единицы,
подставляет ударения из словаря (для голосов, которые принимают «+» перед ударной гласной), режет длинный
текст на части по абзацам и предложениям.
"""

from __future__ import annotations

import re

# Сокращения и единицы → как их читают вслух. Порядок важен: длинные сначала.
SPOKEN: list[tuple[str, str]] = [
    (r"мм\s*рт\.?\s*ст\.?", "миллиметров ртутного столба"),
    (r"мкмоль/л", "микромоль на литр"),
    (r"ммоль/л", "миллимоль на литр"),
    (r"мг/дл", "миллиграммов на децилитр"),
    (r"мг/кг", "миллиграммов на килограмм"),
    (r"мкг/мл", "микрограммов на миллилитр"),
    (r"г/л", "граммов на литр"),
    (r"уд\./мин\.?", "ударов в минуту"),
    (r"в\s+ч\.\s*числе", "в том числе"),
    (r"т\.\s*е\.", "то есть"),
    (r"т\.\s*к\.", "так как"),
    (r"т\.\s*д\.", "так далее"),
    (r"т\.\s*п\.", "тому подобное"),
    (r"и\s+др\.", "и другие"),
    (r"напр\.", "например"),
    (r"см\.", "смотрите"),
    (r"\bАД\b", "артериальное давление"),
    (r"\bЧСС\b", "частота сердечных сокращений"),
    (r"\bЧДД\b", "частота дыхательных движений"),
    (r"\bСОЭ\b", "скорость оседания эритроцитов"),
    (r"\bДВС\b", "диссеминированное внутрисосудистое свёртывание"),
    (r"\bТЭЛА\b", "тромбоэмболия лёгочной артерии"),
    (r"\bИБС\b", "ишемическая болезнь сердца"),
    (r"\bОРДС\b", "острый респираторный дистресс-синдром"),
    (r"\bPubMed\b", "паб-мед"),
    (r"%", " процентов"),
    (r"°С|°C", " градусов Цельсия"),
]

# Слова с трудным ударением: слово → слово с «+» перед ударной гласной (формат принимают Piper и подобные).
STRESS: dict[str, str] = {
    "эмболия": "эмбол+ия",
    "тромбоз": "тромб+оз",
    "ишемия": "ишем+ия",
    "гипоксия": "гипокс+ия",
    "гангрена": "гангр+ена",
    "некроз": "некр+оз",
    "воспаление": "воспалени+е",
    "патогенез": "патог+енез",
    "этиология": "этиол+огия",
    "дефис": "деф+ис",
    "анамнез": "ан+амнез",
    "диагноз": "диагн+оз",
    "ацидоз": "ацид+оз",
    "алкалоз": "алкал+оз",
    "отёк": "от+ёк",
}

_LINK = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_MARK = re.compile(r"\[\d+(?:[,–-]\s*\d+)*\]")
_CODE = re.compile(r"`+([^`]*)`+")
_EMPH = re.compile(r"(\*\*|__|\*|_)(.+?)\1")
_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_SOURCE_LINE = re.compile(r"^\s*(?:Источники?|Литература)\s*:.*$", re.MULTILINE | re.IGNORECASE)
_SPACES = re.compile(r"[ \t]+")


def strip_markup(text: str) -> str:
    """Разметка, ссылки и метки источников уходят: вслух их не читают."""
    t = text.replace("\r", "")
    t = _SOURCE_LINE.sub("", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _LINK.sub("", t)
    t = _MARK.sub("", t)
    t = _CODE.sub(r"\1", t)
    t = _EMPH.sub(r"\2", t)
    t = _HEAD.sub("", t)
    t = _BULLET.sub("", t)
    t = _SPACES.sub(" ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def expand_spoken(text: str) -> str:
    t = text
    for pattern, spoken in SPOKEN:
        t = re.sub(pattern, spoken, t)
    return _SPACES.sub(" ", t).strip()


def apply_stress(text: str, stress: dict[str, str] | None = None) -> str:
    """Подставляет «+» перед ударной гласной в словарных словах (регистр сохраняется, окончания — по началу слова)."""
    table = STRESS if stress is None else stress

    def repl(m: re.Match[str]) -> str:
        word = m.group(0)
        low = word.lower()
        for key, marked in table.items():
            if low.startswith(key):
                out = marked + word[len(key):]
                return out[0].upper() + out[1:] if word[0].isupper() else out
        return word

    return re.sub(r"[А-Яа-яЁё]{4,}", repl, text)


def prepare_for_speech(text: str, *, stress: bool = False) -> str:
    """Полный путь: разметка → сокращения → (по желанию) ударения. Для OpenAI ударения не нужны."""
    t = expand_spoken(strip_markup(text))
    return apply_stress(t) if stress else t


def split_for_speech(text: str, limit: int = 1200) -> list[str]:
    """Режет по абзацам, а слишком длинный абзац — по предложениям; части не длиннее `limit` знаков."""
    parts: list[str] = []
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        if len(para) <= limit:
            parts.append(para)
            continue
        buf = ""
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > limit // 2 else limit
                if buf:
                    parts.append(buf)
                    buf = ""
                parts.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if len(buf) + len(sentence) + 1 > limit and buf:
                parts.append(buf)
                buf = sentence
            else:
                buf = f"{buf} {sentence}".strip()
        if buf:
            parts.append(buf)
    return parts
