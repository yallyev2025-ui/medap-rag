"""Конспект по пунктам темы для сайта владельца (батч 30): КРАТКИЙ и ПОДРОБНЫЙ.

Вызывается с сайта через `/v1/conspect/generate`; результат — черновик, который редактор
сайта проверяет и утверждает (как Quick Outline / Content Studio). Что здесь делается иначе,
чем в обычном вопросе студента:

- поиск ПО ПУНКТАМ темы, а не один на всю тему: у каждого пункта свои лучшие фрагменты,
  поэтому последние пункты не остаются без материала (это деньги не тратит — CPU);
- генерация порциями по нескольку пунктов (одна на порцию) с сквозной нумерацией — каждая
  порция укладывается в лимит вывода, обрыв ловится и дописывается отдельной порцией;
- промпты КРАТКИЙ/ПОДРОБНЫЙ — дословно от владельца, плюс диалект вывода и надстройка
  по предмету (rag/conspect_prompts.py), все тексты правятся в админке без деплоя;
- детерминированные страховки без лишних вызовов LLM: каждый пункт раскрыт или назван в
  «Нет в источниках», числа с единицами сверяются с фрагментами, источники раздела
  сопоставляются с реальными чанками по меткам [F#] (выдумать источник нельзя).
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.pack import build_citations
from app.llm import provider as llm
from app.llm.prompts import get_prompt
from app.llm.task_map import Task
from app.observability.context import current_request_id
from app.verification.numeric import find_unsupported_numbers
from app.workflows.content_studio import numbered_fragments
from app.workflows.dialog import strip_source_brackets
from app.workflows.plain_text import strip_decor
from config import settings
from constants import SOURCE_TEXTBOOK
from rag.conspect_prompts import (
    CONSPECT_BRIEF_PROMPT,
    CONSPECT_DETAILED_PROMPT,
    CONSPECT_FORMAT,
    CONSPECT_SUBJECT_GENERIC,
    CONSPECT_SUBJECTS,
    conspect_subject_key,
    default_conspect_subject_text,
    fill_prompt,
)
from rag.generator import detect_subject, relevant_chunks, trim_to_sentence
from rag.retriever import ChunkResult, retrieve_per_question

logger = logging.getLogger(__name__)

MODE_BRIEF = "brief"
MODE_DETAILED = "detailed"

MAX_POINTS = 40
MAX_ROUNDS = 3  # первая генерация + до двух добавочных для пунктов, не влезших в лимит вывода
_RETRIEVAL_GROUP = 8  # пунктов в одном параллельном поиске — чтобы не исчерпать пул соединений
_TOPIC_FRAGMENTS = 4  # общие фрагменты по теме целиком — добавляются к фрагментам пунктов
_MAX_WARNING_NUMBERS = 5

NO_MATERIAL_MESSAGE = "По этой теме в загруженных материалах MedAP ничего не найдено — конспект писать не из чего."
PROVIDER_ERROR_MESSAGE = "Генерация сейчас недоступна, попробуй позже."
NO_DATA_LINE = "Нет данных в источниках."
MISSING_NOT_FOUND = "в загруженных учебниках материала не найдено"
MISSING_NOT_COVERED = "модель не раскрыла пункт по предоставленным фрагментам"
MISSING_TRUNCATED = "не поместился в лимит вывода — сгенерируйте этот пункт отдельно"
MISSING_PROVIDER = "не удалось сгенерировать из-за сбоя провайдера — повторите генерацию"

_SECTION = re.compile(r"^##\s+(\d{1,3})(?:\s*[.)]\s*|\s+)(.*)$", re.MULTILINE)
_MISSING_HEADING = re.compile(r"^##\s+Нет\s+в\s+источниках\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
_MISSING_LINE = re.compile(r"Пункт\s+(\d{1,3})\s*[—–:-]\s*(.+)", re.IGNORECASE)
_SOURCES_LINE = re.compile(r"^\s*Источники\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_LABEL = re.compile(r"F\s*(\d+)", re.IGNORECASE)


@dataclass
class ConspectPoint:
    number: int
    title: str
    intent: str | None = None


@dataclass
class ConspectResult:
    topic: str
    mode: str
    markdown: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    # [{number, title, citationIds}] — какие цитаты относятся к какому разделу.
    sections: list[dict[str, Any]] = field(default_factory=list)
    # [{number, title, note}] — пункты, по которым в источниках данных нет.
    missing: list[dict[str, Any]] = field(default_factory=list)
    # [{number|None, kind, message}] — что редактору стоит перепроверить.
    warnings: list[dict[str, Any]] = field(default_factory=list)
    subject: str | None = None
    batches: int = 0
    truncated: bool = False
    error: str | None = None
    request_id: str | None = None


@dataclass
class _Section:
    number: int
    body: str
    chunks: list[ChunkResult]
    missing_note: str | None = None


def _point_query(topic: str, point: ConspectPoint) -> str:
    query = f"{topic}: {point.title}"
    if point.intent:
        query += f" — {point.intent}"
    return " ".join(query.split())[:400]


def _interleave(groups: list[list[ChunkResult]], limit: int) -> list[ChunkResult]:
    """Фрагменты групп по кругу без дублей: обрезка по лимиту не оставляет без материала
    ни один пункт (иначе первые пункты съели бы весь контекст)."""
    seen: set[int] = set()
    out: list[ChunkResult] = []
    for rank in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if rank < len(group) and group[rank].id not in seen:
                seen.add(group[rank].id)
                out.append(group[rank])
                if len(out) >= limit:
                    return out
    return out


def _split_missing(text: str) -> tuple[str, dict[int, str]]:
    """Отделяет итоговый блок «## Нет в источниках» от разделов; возвращает (разделы, пункт → пометка)."""
    match = _MISSING_HEADING.search(text)
    if not match:
        return text, {}
    notes: dict[int, str] = {}
    for line in text[match.end():].splitlines():
        found = _MISSING_LINE.search(line)
        if found:
            notes.setdefault(int(found.group(1)), found.group(2).strip().rstrip("."))
    return text[: match.start()], notes


def _parse_sections(text: str, batch: list[ConspectPoint], chunks: list[ChunkResult]) -> tuple[list[_Section], dict[int, str], list[dict]]:
    """Разбирает ответ порции: разделы по заголовкам «## N.», источники по меткам F#."""
    body_text, tail_notes = _split_missing(text)
    allowed = {p.number for p in batch}
    sections: list[_Section] = []
    warnings: list[dict] = []
    seen: set[int] = set()

    matches = list(_SECTION.finditer(body_text))
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body_text)
        body = body_text[match.end():end].strip()
        if number not in allowed or number in seen:
            warnings.append({"number": number, "kind": "unexpected_section",
                             "message": f"Модель вернула лишний раздел {number} — он отброшен."})
            continue
        seen.add(number)

        used: list[ChunkResult] = []
        for line in _SOURCES_LINE.findall(body):
            for label in _LABEL.findall(line):
                position = int(label)
                if 1 <= position <= len(chunks) and chunks[position - 1] not in used:
                    used.append(chunks[position - 1])
        body = _SOURCES_LINE.sub("", body).strip()
        sections.append(_Section(number=number, body=body, chunks=used))
    return sections, tail_notes, warnings


def _is_empty(body: str) -> bool:
    cleaned = re.sub(r"[\W_]+", "", body.lower())
    return not cleaned or cleaned == re.sub(r"[\W_]+", "", NO_DATA_LINE.lower())


def _clean(text: str) -> str:
    return strip_decor(strip_source_brackets(text)).strip()


async def _compose_messages(
    mode: str, topic: str, batch: list[ConspectPoint], others: list[ConspectPoint],
    chunks: list[ChunkResult], subject: str | None,
) -> list[dict]:
    template_key, template_default = (
        ("CONSPECT_BRIEF_PROMPT", CONSPECT_BRIEF_PROMPT) if mode == MODE_BRIEF
        else ("CONSPECT_DETAILED_PROMPT", CONSPECT_DETAILED_PROMPT)
    )
    template = await get_prompt(template_key, template_default)
    format_text = await get_prompt("CONSPECT_FORMAT", CONSPECT_FORMAT)
    if subject in CONSPECT_SUBJECTS:
        subject_text = await get_prompt(conspect_subject_key(subject), default_conspect_subject_text(subject) or "")
    else:
        subject_text = await get_prompt(conspect_subject_key("generic"), CONSPECT_SUBJECT_GENERIC)

    def line(p: ConspectPoint) -> str:
        return f"{p.number}. {p.title}" + (f" — {p.intent}" if p.intent else "")

    topic_block = f"{topic}\nПункты, которые нужно раскрыть (номера сквозные, сохрани их):\n" + "\n".join(line(p) for p in batch)
    if others:
        topic_block += (
            "\nОстальные пункты темы раскрываются в других частях: здесь их не раскрывай, "
            "на них можно ссылаться «см. раздел N»:\n" + "\n".join(f"{p.number}. {p.title}" for p in others)
        )

    system = format_text + ("\n\n" + subject_text if subject_text else "")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": fill_prompt(template, topic_block, numbered_fragments(chunks))},
    ]


async def _retrieve_all(
    topic: str, points: list[ConspectPoint], source_type: str, subject: str | None
) -> tuple[dict[int, list[ChunkResult]], list[ChunkResult]]:
    """Лучшие фрагменты каждого пункта + общие по теме. Релевантность — обычным порогом учебников."""
    per_point: dict[int, list[ChunkResult]] = {}
    for start in range(0, len(points), _RETRIEVAL_GROUP):
        group = points[start : start + _RETRIEVAL_GROUP]
        found = await retrieve_per_question(
            [_point_query(topic, p) for p in group],
            settings.CONSPECT_FRAGMENTS_PER_POINT,
            candidates=20,
            source_type=source_type,
            subject=subject,
        )
        for point, chunks in zip(group, found):
            per_point[point.number] = relevant_chunks(chunks)
    topic_found = await retrieve_per_question(
        [topic], _TOPIC_FRAGMENTS, candidates=20, source_type=source_type, subject=subject
    )
    return per_point, relevant_chunks(topic_found[0]) if topic_found else []


async def _write_batch(
    mode: str, topic: str, batch: list[ConspectPoint], others: list[ConspectPoint],
    per_point: dict[int, list[ChunkResult]], topic_chunks: list[ChunkResult], subject: str | None,
) -> tuple[list[_Section], dict[int, str], list[dict], list[ConspectPoint], bool]:
    """Одна генерация. Возвращает (разделы, пометки «нет в источниках», предупреждения,
    пункты, не влезшие в лимит вывода, ответ был обрезан)."""
    groups = [per_point.get(p.number, []) for p in batch] + [topic_chunks]
    chunks = _interleave(groups, settings.CONSPECT_MAX_FRAGMENTS)
    if not chunks:
        return [], {p.number: MISSING_NOT_FOUND for p in batch}, [], [], False

    messages = await _compose_messages(mode, topic, batch, others, chunks, subject)
    result = await llm.complete(
        Task.CONSPECT_WRITE, messages, temperature=0.2, max_output_tokens=settings.CONSPECT_MAX_OUTPUT_TOKENS
    )
    sections, tail_notes, warnings = _parse_sections(result.text, batch, chunks)

    leftover: list[ConspectPoint] = []
    if result.truncated:
        # Последний раздел почти наверняка оборван — выбрасываем его целиком и пишем заново.
        if len(sections) > 1 or (sections and len(batch) > 1):
            dropped = max(sections, key=lambda s: s.number) if sections else None
            if dropped is not None:
                sections = [s for s in sections if s is not dropped]
        elif sections:
            sections[0].body = trim_to_sentence(sections[0].body)
        covered = {s.number for s in sections}
        leftover = [p for p in batch if p.number not in covered] if len(batch) > 1 else []

    # Проверка чисел: единицы и значения из раздела должны стоять во фрагментах порции.
    context = "\n".join(c.content for c in chunks)
    for section in sections:
        unsupported = find_unsupported_numbers(section.body, context)
        if unsupported:
            shown = ", ".join(unsupported[:_MAX_WARNING_NUMBERS])
            warnings.append({
                "number": section.number, "kind": "unsupported_number",
                "message": f"Раздел {section.number}: {shown} — такого значения нет во фрагментах, проверьте по учебнику.",
            })
        if not section.chunks and not _is_empty(section.body):
            warnings.append({
                "number": section.number, "kind": "no_sources",
                "message": f"Раздел {section.number}: модель не указала источники — проверьте раздел вручную.",
            })
    # Флаг «обрезан» — только если текст остался оборванным; оборванный раздел, пересозданный
    # добавочной порцией, на результат не влияет.
    return sections, tail_notes, warnings, leftover, bool(result.truncated) and not leftover


async def generate_conspect(
    topic: str,
    points: list[tuple[str, str | None]],
    mode: str = MODE_DETAILED,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
) -> ConspectResult:
    """Конспект темы по пунктам. points — [(название, суть-подсказка | None)]; номера — по порядку."""
    mode = MODE_BRIEF if mode == MODE_BRIEF else MODE_DETAILED
    topic = " ".join(topic.split())
    pts = [ConspectPoint(i, " ".join(t.split()), (" ".join(h.split()) if h else None)) for i, (t, h) in enumerate(points[:MAX_POINTS], start=1) if t.strip()]
    outcome = ConspectResult(topic=topic, mode=mode, request_id=current_request_id())
    if not pts:
        outcome.error = "Не переданы пункты темы."
        return outcome

    per_point, topic_chunks = await _retrieve_all(topic, pts, source_type, subject)
    everything = [c for chunks in per_point.values() for c in chunks] + topic_chunks
    if not everything:
        outcome.error = NO_MATERIAL_MESSAGE
        outcome.missing = [{"number": p.number, "title": p.title, "note": MISSING_NOT_FOUND} for p in pts]
        return outcome
    outcome.subject = subject or detect_subject(everything)

    size = settings.CONSPECT_POINTS_PER_BATCH_BRIEF if mode == MODE_BRIEF else settings.CONSPECT_POINTS_PER_BATCH_DETAILED
    size = max(1, size)
    queue = [pts[i : i + size] for i in range(0, len(pts), size)]
    by_number = {p.number: p for p in pts}

    sections: dict[int, _Section] = {}
    notes: dict[int, str] = {}
    semaphore = asyncio.Semaphore(max(1, settings.CONSPECT_CONCURRENCY))

    async def run(batch: list[ConspectPoint]):
        others = [p for p in pts if p.number not in {b.number for b in batch}]
        async with semaphore:
            return await _write_batch(mode, topic, batch, others, per_point, topic_chunks, outcome.subject)

    for round_index in range(MAX_ROUNDS):
        if not queue:
            break
        results = await asyncio.gather(*[run(b) for b in queue], return_exceptions=True)
        if all(isinstance(r, llm.LLMError) for r in results):
            logger.error("Конспект: сбой провайдера на всех порциях")
            outcome.error = PROVIDER_ERROR_MESSAGE
            return outcome
        outcome.batches += len(queue)

        next_queue: list[list[ConspectPoint]] = []
        for batch, result in zip(queue, results):
            if isinstance(result, BaseException):
                if not isinstance(result, llm.LLMError):
                    raise result
                # Сбой одной порции не выбрасывает остальные уже оплаченные порции.
                logger.warning("Конспект: порция пунктов %s не сгенерирована: %s", [p.number for p in batch], result)
                for point in batch:
                    notes.setdefault(point.number, MISSING_PROVIDER)
                    outcome.warnings.append({"number": point.number, "kind": "provider_error",
                                             "message": f"Пункт {point.number}: {MISSING_PROVIDER}."})
                continue
            got, tail, warns, leftover, truncated = result
            outcome.warnings.extend(warns)
            outcome.truncated = outcome.truncated or truncated
            for section in got:
                sections.setdefault(section.number, section)
            notes.update({n: t for n, t in tail.items() if n not in notes})
            covered = {s.number for s in got}
            if leftover:
                # Не вышло ни одного раздела — порция слишком велика, делим пополам; иначе дописываем остаток разом.
                if len(leftover) == len(batch) and len(leftover) > 1:
                    half = (len(leftover) + 1) // 2
                    next_queue.extend(leftover[i : i + half] for i in range(0, len(leftover), half))
                else:
                    next_queue.append(leftover)
            for point in batch:
                if point.number in covered or point in leftover:
                    continue
                if point.number not in notes:
                    notes[point.number] = MISSING_NOT_COVERED
                    outcome.warnings.append({
                        "number": point.number, "kind": "point_not_covered",
                        "message": f"Пункт {point.number} не раскрыт моделью — отмечен как «нет в источниках».",
                    })
        queue = next_queue

    for batch in queue:  # не успели за MAX_ROUNDS
        for point in batch:
            notes.setdefault(point.number, MISSING_TRUNCATED)
            outcome.truncated = True
            outcome.warnings.append({"number": point.number, "kind": "truncated", "message": f"Пункт {point.number}: {MISSING_TRUNCATED}."})

    _assemble(outcome, pts, by_number, sections, notes)
    return outcome


def _assemble(
    outcome: ConspectResult, pts: list[ConspectPoint], by_number: dict[int, ConspectPoint],
    sections: dict[int, _Section], notes: dict[int, str],
) -> None:
    """Склеивает итоговый Markdown по порядку пунктов, собирает цитаты и итоговый блок «Нет в источниках»."""
    order: list[ChunkResult] = []
    for point in pts:
        section = sections.get(point.number)
        if section is None:
            continue
        for chunk in section.chunks:
            if chunk not in order:
                order.append(chunk)
    citations = build_citations(order)
    ids = {chunk.id: citation.citation_id for chunk, citation in zip(order, citations)}
    outcome.citations = [c.to_dict() for c in citations]

    parts: list[str] = []
    missing: list[dict[str, Any]] = []
    for point in pts:
        section = sections.get(point.number)
        heading = f"## {point.number}. {point.title}"
        if section is None:
            note = notes.get(point.number, MISSING_NOT_COVERED)
            missing.append({"number": point.number, "title": point.title, "note": note})
            parts.append(f"{heading}\n\n{NO_DATA_LINE}")
            continue
        body = _clean(section.body)
        if _is_empty(body):
            note = notes.get(point.number, MISSING_NOT_FOUND)
            missing.append({"number": point.number, "title": point.title, "note": note})
            parts.append(f"{heading}\n\n{NO_DATA_LINE}")
            continue
        parts.append(f"{heading}\n\n{body}")
        outcome.sections.append({
            "number": point.number, "title": point.title,
            "citationIds": [ids[c.id] for c in section.chunks],
        })
        # Пункт раскрыт, но модель отметила нехватку данных (частичное покрытие) — сохраняем пометку.
        if point.number in notes and notes[point.number] not in (MISSING_NOT_COVERED,):
            missing.append({"number": point.number, "title": point.title, "note": notes[point.number]})

    if missing:
        lines = "\n".join(f"- Пункт {m['number']} — {m['note']}" for m in missing)
        parts.append(f"## Нет в источниках\n\n{lines}")
    outcome.missing = missing
    outcome.markdown = "\n\n".join(parts).strip()
