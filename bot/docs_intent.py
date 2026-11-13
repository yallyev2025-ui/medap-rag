"""Что просит студент в Telegram: конспект из видео YouTube или документ (PDF/Word) — чистая логика без сети и базы.

Явные просьбы узнаются кодом бесплатно («сделай конспект по теме …», «собери документ из этого ответа», «шпаргалка pdf»); обычный вопрос
остаётся обычным вопросом — документ из него не делается. Те же слова и правила, что в чате Ментора на сайте (`packages/domain/src/doc-chat.ts`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_YT = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/|embed/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def find_youtube_url(text: str | None) -> str | None:
    """Ссылка на видео YouTube в тексте → нормальный адрес `https://www.youtube.com/watch?v=<id>`; нет ссылки — None."""
    if not text:
        return None
    m = _YT.search(text)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[«»\"“”„]", " ", text.lower().replace("ё", "е"))).strip()


_DOC_NOUN = re.compile(r"конспект|документ|шпаргалк|памятк|(?<![a-zа-я])docx?(?![a-z])|(?<![а-я])ворд|(?<![a-z])word(?![a-z])|(?<![a-z])pdf(?![a-z])|(?<![а-я])пдф|(?<![а-я])файл")
_MAKE = re.compile(r"сдела|собер|собра|состав|созда|оформ|подготов|сформир|нужен|нужна|нужно|хочу|выдай|дай|скинь|пришли|скачат|сгенерир|напиш|распечат|преврат")
_NOT_COMMAND = re.compile(r"(?:^|\s)(?:как (?:мне )?(?:сделать|составить|написать|оформить|собрать|создать)|что такое|зачем|почему|чем отличается|объясни|что значит|для чего нужен)(?:\s|$)")
_PREVIOUS = re.compile(r"из этого|по этому|из ответа|из твоего|на основе (?:этого|ответа)|из вышесказанного|по тексту выше|из сказанного|вот это|из последнего|по последнему|из него")
_TOPIC = re.compile(r"(?<![а-я])(?:по теме|на тему|тема:|про|по|об|о)\s+([^\n]{3,200})$")


@dataclass(frozen=True)
class DocRequest:
    topic: str | None
    fmt: str  # "pdf" | "docx" | "both"
    from_previous: bool
    wishes: str | None = None


def _format(t: str) -> str:
    docx = bool(re.search(r"докс|(?<![a-z])docx?(?![a-z])|(?<![а-я])ворд|(?<![a-z])word(?![a-z])", t))
    pdf = bool(re.search(r"(?<![a-z])pdf(?![a-z])|(?<![а-я])пдф", t))
    if docx and not pdf:
        return "docx"
    if pdf and not docx:
        return "pdf"
    return "both"


def _topic_of(raw: str) -> str | None:
    m = _TOPIC.search(raw.strip().rstrip(".!?"))
    if not m:
        return None
    t = m.group(1)
    quoted = re.search(r"[«\"“„]([^»\"”“]{3,200})[»\"”“]", t)
    if quoted:
        return quoted.group(1).strip()
    t = re.sub(r"\s*(?:в\s+)?(?:формате\s+)?(?:docx?|pdf|пдф|докс|ворд[а-я]*|word)(?![a-z]).*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*(?:в|на)\s+\S+(?:ых|их|ой|ом)\s+(?:тонах|цветах|цвете|палитре|гамме|стиле|оформлении)(?![а-я]).*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[,;]\s*$", "", t).strip(" «»\"“”„")
    n = _norm(t)
    if len(t) < 3 or _PREVIOUS.search(n) or n in {"это", "этому", "этого", "этом", "ним", "нему"}:
        return None
    return t


def parse_doc_request(text: str | None) -> DocRequest | None:
    """Явная просьба о документе → разбор; обычный вопрос → None."""
    if not text:
        return None
    raw = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(raw) < 6 or len(raw) > 400:
        return None
    t = _norm(raw)
    if not _DOC_NOUN.search(t) or not _MAKE.search(t) or _NOT_COMMAND.search(t):
        return None
    topic = _topic_of(raw)
    return DocRequest(topic=topic, fmt=_format(t), from_previous=topic is None)
