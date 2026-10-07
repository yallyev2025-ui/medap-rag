"""Несколько вопросов в одном сообщении или на одном скрине (батч 27).

Принцип «технологически правильно и дёшево»: вопросов может быть много, но
- поиск по каждому вопросу — CPU (гибрид BM25+вектор, мало кандидатов, реранк под очередью
  RERANK_CONCURRENCY), деньги не тратятся;
- генерация — ОДНА на порцию до MULTI_QUESTION_BATCH вопросов (скрин целиком — обычно одна);
- ответ короткий: «N) Правильный ответ: X — строка почему»; подробно — по просьбе.
Источник строго по выбранному режиму: учебники / документ / оба (с пометкой 📄/📚).
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.evidence.citations import extract_cited_chunks
from app.evidence.pack import build_citations
from app.llm.task_map import Task
from app.workflows.dialog import compact_sources, strip_source_brackets
from app.workflows.question_kind import is_open_exam_list
from app.workflows.scope import Scope
from config import settings
from constants import SCOPE_BOTH, SCOPE_DOCUMENTS, SOURCE_USER_DOCUMENT
from rag.generator import (
    MULTI_QUESTION_INSTRUCTION,
    document_threshold,
    generate_answer,
    generate_combined,
    relevant_chunks,
)
from rag.retriever import (
    ChunkResult,
    count_document_chunks,
    fetch_document_chunks,
    retrieve,
    retrieve_per_question,
)

logger = logging.getLogger(__name__)

_NUMBERED = re.compile(r"^\s*(\d{1,3})\s*[.)]\s+(.*\S)")
_MIN_ITEM_CHARS = 8

NOT_FOUND_LINE = "в материалах не найдено"
STOPPED_BY_BUDGET_TEXT = (
    "⏸ Остановился: месячный лимит стоимости исчерпан. Остальные вопросы — после обновления лимита."
)

COMBINED_MULTI_INSTRUCTION = (
    MULTI_QUESTION_INSTRUCTION
    + " Контекст разделён на «📄 ДОКУМЕНТ СТУДЕНТА» и «📚 УЧЕБНИКИ»: в каждой строке ответа пометь, "
    "откуда он взят — 📄 (из документа студента) или 📚 (из учебников); если источники дают разный "
    "ответ — покажи оба с пометками; нет ни там ни там — «N) в материалах не найдено»."
)


def split_questions(text: str) -> list[str]:
    """Делит сообщение на отдельные вопросы. Нумерованный список («1) …», «2. …») — по
    номерам, строки без номера (варианты ответа) остаются при своём вопросе. Иначе — по
    знакам «?», если их два и больше. Один вопрос — список из одного элемента."""
    lines = text.strip().splitlines()
    if sum(1 for line in lines if _NUMBERED.match(line)) >= 2:
        items: list[str] = []
        for line in lines:
            match = _NUMBERED.match(line)
            if match:
                items.append(match.group(2).strip())
            elif items and line.strip():
                items[-1] += "\n" + line.strip()
        items = [i for i in items if len(i) >= _MIN_ITEM_CHARS]
        if len(items) >= 2:
            return items

    if text.count("?") >= 2:
        parts = [p.strip() for p in re.split(r"(?<=\?)\s+", text.strip()) if p.strip()]
        if len(parts) >= 2 and all(len(p) >= _MIN_ITEM_CHARS for p in parts):
            return parts
    return [text.strip()]


@dataclass
class MultiAnswer:
    text: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    batches: int = 1
    stopped_by_budget: bool = False


def _dedupe(chunks: list[ChunkResult]) -> list[ChunkResult]:
    seen: set[int] = set()
    out: list[ChunkResult] = []
    for chunk in chunks:
        if chunk.id not in seen:
            seen.add(chunk.id)
            out.append(chunk)
    return out


def _numbered(items: list[str], start: int) -> str:
    return "\n".join(f"{start + i}) {item}" for i, item in enumerate(items))


async def _document_context(
    questions: list[str], scope: Scope, whole: list[ChunkResult] | None
) -> list[ChunkResult]:
    """Фрагменты документов для порции: небольшой документ целиком (один раз на все
    порции), иначе по лучшим фрагментам на каждый вопрос."""
    if whole is not None:
        return whole
    per_question = await retrieve_per_question(
        questions,
        settings.MULTI_QUESTION_CHUNKS_PER_QUESTION,
        source_type=SOURCE_USER_DOCUMENT,
        book_id=list(scope.document_ids),
        user_id=scope.owner_id,
    )
    return _dedupe([c for group in per_question for c in group])


async def answer_questions(
    items: list[str],
    scope: Scope,
    *,
    task: Task = Task.GROUNDED_QA,
    history: list[dict] | None = None,
    budget_ok: Callable[[], Awaitable[bool]] | None = None,
) -> MultiAnswer:
    """Отвечает на все вопросы порциями; возвращает единый текст с источниками."""
    items = [i for i in items if i.strip()]

    # Список экзаменационных вопросов (без вариантов ответа) по учебникам: каждый — полным
    # структурированным ответом, а не строкой на 1–2 предложения
    if scope.uses_textbooks and not scope.uses_documents and is_open_exam_list(items):
        return await _answer_open_questions(items, scope, task=task, history=history, budget_ok=budget_ok)

    batch_size = max(1, settings.MULTI_QUESTION_BATCH)
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]

    # Небольшой документ грузим целиком один раз — он общий для всех порций.
    whole: list[ChunkResult] | None = None
    if scope.uses_documents:
        total = await count_document_chunks(list(scope.document_ids), scope.owner_id)
        if 0 < total <= settings.DOC_FULL_CONTEXT_CHUNKS:
            whole = await fetch_document_chunks(list(scope.document_ids), scope.owner_id)

    parts: list[str] = []
    all_citations: list[dict[str, Any]] = []
    stopped = False
    number = 1
    for index, batch in enumerate(batches):
        if index > 0 and budget_ok is not None and not await budget_ok():
            stopped = True
            break

        # Поисковый запрос — вопрос вместе с вариантами ответа (в них часто названия препаратов).
        questions = [" ".join(b.split())[:400] for b in batch]
        prompt = _numbered(batch, number)

        doc_chunks: list[ChunkResult] = []
        book_chunks: list[ChunkResult] = []
        if scope.uses_documents:
            doc_chunks = await _document_context(questions, scope, whole)
        if scope.uses_textbooks:
            per_question = await retrieve_per_question(
                questions, settings.MULTI_QUESTION_CHUNKS_PER_QUESTION,
                source_type=scope.source_type, subject=scope.subject,
            )
            book_chunks = _dedupe([c for group in per_question for c in group])

        doc_relevant = relevant_chunks(doc_chunks, document_threshold())
        book_relevant = relevant_chunks(book_chunks)
        relevant = doc_relevant + book_relevant

        if not relevant:
            text = "\n".join(f"{number + i}) {NOT_FOUND_LINE}" for i in range(len(batch)))
        elif scope.mode == SCOPE_BOTH:
            generated = await generate_combined(
                prompt, doc_chunks, book_chunks, history, mode_override=COMBINED_MULTI_INSTRUCTION
            )
            text = generated.text
        elif scope.mode == SCOPE_DOCUMENTS:
            generated = await generate_answer(
                prompt, doc_chunks, SOURCE_USER_DOCUMENT, history,
                task=Task.DOCUMENT_QA, mode_override=MULTI_QUESTION_INSTRUCTION,
            )
            text = generated.text
        else:
            generated = await generate_answer(
                prompt, book_chunks, scope.source_type or "учебник", history,
                task=task, mode_override=MULTI_QUESTION_INSTRUCTION,
            )
            text = generated.text

        if relevant:
            cited = extract_cited_chunks(text, relevant)
            all_citations.extend(c.to_dict() for c in build_citations(cited))
        parts.append(strip_source_brackets(text))
        number += len(batch)

    if len(batches) > 1:
        parts = [f"**Часть {i}/{len(batches)}**\n{p}" for i, p in enumerate(parts, start=1)]
    body = "\n\n".join(p for p in parts if p)
    sources = compact_sources(all_citations)
    if sources:
        body += "\n\n" + sources
    if stopped:
        body += "\n\n" + STOPPED_BY_BUDGET_TEXT
    return MultiAnswer(text=body, citations=all_citations, batches=len(batches), stopped_by_budget=stopped)



async def _answer_open_questions(
    items: list[str],
    scope: Scope,
    *,
    task: Task,
    history: list[dict] | None,
    budget_ok: Callable[[], Awaitable[bool]] | None,
) -> MultiAnswer:
    """Экзаменационные вопросы списком: на каждый — обычный полный ответ (тот же поиск, что у
    одиночного вопроса, тот же промпт с форматированием), под жирным заголовком с номером.
    Деньги: одна генерация на вопрос, поэтому список ограничен MAX_OPEN_QUESTIONS."""
    parts: list[str] = []
    all_citations: list[dict[str, Any]] = []
    stopped = False

    for number, item in enumerate(items, start=1):
        if number > 1 and budget_ok is not None and not await budget_ok():
            stopped = True
            break

        chunks = await retrieve(" ".join(item.split())[:400], source_type=scope.source_type, subject=scope.subject)
        relevant = relevant_chunks(chunks)
        title = f"**{number}. {item.strip()}**"

        if not relevant:
            parts.append(f"{title}\n\n{NOT_FOUND_LINE}")
            continue

        generated = await generate_answer(
            item, relevant, scope.source_type or "учебник", history, task=task, dialog=False
        )
        cited = extract_cited_chunks(generated.text, relevant)
        all_citations.extend(c.to_dict() for c in build_citations(cited))
        parts.append(f"{title}\n\n{strip_source_brackets(generated.text)}")

    body = "\n\n".join(parts)
    sources = compact_sources(all_citations)
    if sources:
        body += "\n\n" + sources
    if stopped:
        body += "\n\n" + STOPPED_BY_BUDGET_TEXT
    return MultiAnswer(text=body, citations=all_citations, batches=len(parts), stopped_by_budget=stopped)
