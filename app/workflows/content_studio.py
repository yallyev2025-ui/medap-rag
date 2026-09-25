"""Content Studio (§31 ТЗ, этап 4B) — черновики учебного контента по проверенным
источникам: recall-вопросы, тестовые вопросы, клинические кейсы.

Как и Quick Outline, это НЕ студенческий workflow: вызывается с сайта владельца
через `/v1/content/*`, черновик хранится, правится и публикуется ТАМ. Здесь
только генерация — каждый ответ помечен `status="AI_DRAFT"`: без проверки
человеком такой контент не публикуется (§31), и это правило обеспечивает сайт.

Трассировка к evidence поэлементная: фрагменты передаются модели с метками
[F1], [F2]…, каждый элемент обязан сослаться на свои метки; backend
детерминированно сопоставляет их с реальными чанками. Элемент без единой
валидной метки отбрасывается — происхождение не выдумывается.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.pack import Citation, build_citations
from app.llm import provider as llm
from app.llm.task_map import Task
from app.observability.context import current_request_id
from constants import SOURCE_TEXTBOOK
from rag.generator import relevant_chunks
from rag.retriever import ChunkResult, retrieve

logger = logging.getLogger(__name__)

MAX_FRAGMENTS = 10
MAX_ITEMS = 20

CONTENT_RECALL = "recall_items"
CONTENT_TEST = "test_questions"
CONTENT_CASE = "clinical_case"

_NO_MATERIAL_MESSAGE = "По этой теме в загруженных материалах MedAP ничего не найдено — генерировать не из чего."
_PROVIDER_ERROR_MESSAGE = "Генерация сейчас недоступна, попробуй позже."

_COMMON_RULES = """ПРАВИЛА (строже любых пожеланий по объёму):
- Используй ТОЛЬКО фрагменты ниже. Никаких фактов из общих знаний.
- Термин ≠ доказательство: если фрагмент только называет тему/препарат без содержания — на нём ничего не строй.
- Не выдумывай дозы, цифры, сроки, критерии: каждая цифра должна дословно стоять во фрагменте.
- В каждом элементе поле "sources" — метки фрагментов, на которых он построен (например ["F1", "F3"]). Без меток элемент не принимается.
- Лучше меньше элементов, чем элемент без опоры на фрагменты.
- Язык — русский."""

_RECALL_PROMPT = f"""Ты готовишь для образовательной платформы MedAP вопросы для активного припоминания (recall) по теме.
Студент отвечает своими словами; ответ проверяется по эталонным пунктам.

{_COMMON_RULES}

Верни ТОЛЬКО JSON:
{{"items": [{{"question": "вопрос", "expectedPoints": ["пункт, который должен прозвучать в ответе", "..."], "sources": ["F1"]}}]}}"""

_TEST_PROMPT = f"""Ты готовишь для образовательной платформы MedAP тестовые вопросы с одним правильным вариантом по теме.

{_COMMON_RULES}
- 4–5 вариантов ответа, ровно один правильный; неправильные — правдоподобные, но опровергаемые фрагментами.
- correctIndex — индекс правильного варианта в options, начиная с 0.
- explanation — коротко, почему правильный вариант верен (по фрагментам).

Верни ТОЛЬКО JSON:
{{"items": [{{"question": "вопрос", "options": ["...", "..."], "correctIndex": 0, "explanation": "...", "sources": ["F2"]}}]}}"""

_CASE_PROMPT = f"""Ты готовишь для образовательной платформы MedAP учебный клинический кейс по теме — задачу для студента, а не назначение реальному пациенту.

{_COMMON_RULES}
- Сценарий (жалобы, анамнез, данные обследования) составляй только из признаков, описанных во фрагментах.
- К кейсу 2–5 вопросов с разбором; ответ на каждый должен следовать из фрагментов.

Верни ТОЛЬКО JSON:
{{"scenario": "описание случая", "sources": ["F1"], "questions": [{{"question": "...", "answer": "разбор", "sources": ["F1", "F2"]}}]}}"""

_USER_TEMPLATE = """Тема: {topic}
{count_line}
Фрагменты:
{fragments}"""

_LABEL = re.compile(r"F?\s*(\d+)", re.IGNORECASE)


@dataclass
class ContentDraftResult:
    topic: str
    content_type: str
    status: str = "AI_DRAFT"
    items: list[dict[str, Any]] = field(default_factory=list)
    evidence_references: list[dict[str, Any]] = field(default_factory=list)
    # Сколько элементов модель вернула, но без валидной опоры на фрагменты или с
    # нарушенной структурой — они отброшены, а не показаны как проверенные.
    dropped_unsupported: int = 0
    error: str | None = None
    request_id: str | None = None


def _numbered_fragments(chunks: list[ChunkResult]) -> str:
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        head = f"{chunk.author}, {chunk.title}" if chunk.author else chunk.title
        if chunk.page_from is not None:
            head += f", стр. {chunk.page_from}" if chunk.page_from == chunk.page_to else f", стр. {chunk.page_from}-{chunk.page_to}"
        blocks.append(f"[F{index}] ({head})\n{chunk.content}")
    return "\n---\n".join(blocks)


def _resolve_sources(raw: Any, citations: list[Citation]) -> list[Citation]:
    """Метки модели → реальные citations. Неизвестные метки молча отбрасываются."""
    if not isinstance(raw, list):
        return []
    resolved: list[Citation] = []
    for label in raw:
        match = _LABEL.fullmatch(str(label).strip().strip("[]"))
        if not match:
            continue
        index = int(match.group(1))
        if 1 <= index <= len(citations) and citations[index - 1] not in resolved:
            resolved.append(citations[index - 1])
    return resolved


def _evidence_fields(resolved: list[Citation]) -> dict[str, list[str]]:
    return {
        "citationIds": [c.citation_id for c in resolved],
        "evidenceIds": [c.evidence_id for c in resolved],
    }


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _parse_recall(data: dict, citations: list[Citation]) -> tuple[list[dict], int]:
    items, dropped = [], 0
    for raw in data.get("items", [])[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        question = str(raw.get("question") or "").strip()
        points = _clean_str_list(raw.get("expectedPoints"))
        resolved = _resolve_sources(raw.get("sources"), citations)
        if not question or not points or not resolved:
            dropped += 1
            continue
        items.append({"question": question, "expectedPoints": points, **_evidence_fields(resolved)})
    return items, dropped


def _parse_test(data: dict, citations: list[Citation]) -> tuple[list[dict], int]:
    items, dropped = [], 0
    for raw in data.get("items", [])[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        question = str(raw.get("question") or "").strip()
        options = _clean_str_list(raw.get("options"))
        correct = raw.get("correctIndex")
        resolved = _resolve_sources(raw.get("sources"), citations)
        valid_index = isinstance(correct, int) and not isinstance(correct, bool) and 0 <= correct < len(options)
        if not question or not 3 <= len(options) <= 6 or not valid_index or not resolved:
            dropped += 1
            continue
        items.append(
            {
                "question": question,
                "options": options,
                "correctIndex": correct,
                "explanation": str(raw.get("explanation") or "").strip(),
                **_evidence_fields(resolved),
            }
        )
    return items, dropped


def _parse_case(data: dict, citations: list[Citation]) -> tuple[list[dict], int]:
    scenario = str(data.get("scenario") or "").strip()
    scenario_sources = _resolve_sources(data.get("sources"), citations)
    if not scenario or not scenario_sources:
        return [], 1

    questions, dropped = [], 0
    for raw in data.get("questions", [])[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        question = str(raw.get("question") or "").strip()
        answer = str(raw.get("answer") or "").strip()
        resolved = _resolve_sources(raw.get("sources"), citations)
        if not question or not answer or not resolved:
            dropped += 1
            continue
        questions.append({"question": question, "answer": answer, **_evidence_fields(resolved)})

    if not questions:
        return [], dropped + 1
    return [{"scenario": scenario, **_evidence_fields(scenario_sources), "questions": questions}], dropped


_KINDS = {
    CONTENT_RECALL: (Task.CONTENT_RECALL, _RECALL_PROMPT, {"required": ["items"]}, _parse_recall),
    CONTENT_TEST: (Task.CONTENT_TEST, _TEST_PROMPT, {"required": ["items"]}, _parse_test),
    CONTENT_CASE: (Task.CONTENT_CASE, _CASE_PROMPT, {"required": ["scenario", "questions"]}, _parse_case),
}


async def generate_content(
    content_type: str,
    topic: str,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
    count: int = 5,
) -> ContentDraftResult:
    task, system_prompt, schema, parse = _KINDS[content_type]

    chunks = await retrieve(topic, source_type=source_type, subject=subject)
    relevant = relevant_chunks(chunks)[:MAX_FRAGMENTS]
    if not relevant:
        return ContentDraftResult(
            topic=topic, content_type=content_type, error=_NO_MATERIAL_MESSAGE, request_id=current_request_id()
        )

    citations = build_citations(relevant)
    count_line = "" if content_type == CONTENT_CASE else f"Сколько элементов: до {max(1, min(count, MAX_ITEMS))}\n"
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(topic=topic, count_line=count_line, fragments=_numbered_fragments(relevant)),
        },
    ]
    try:
        result = await llm.complete(task, messages, temperature=0.3, json_schema=schema)
    except llm.LLMError:
        logger.exception("Content Studio: сбой провайдера (%s)", content_type)
        return ContentDraftResult(
            topic=topic, content_type=content_type, error=_PROVIDER_ERROR_MESSAGE, request_id=current_request_id()
        )

    items, dropped = parse(result.data or {}, citations)
    used_ids = {eid for item in items for eid in item.get("evidenceIds", [])}
    for item in items:
        for question in item.get("questions", []):
            used_ids.update(question.get("evidenceIds", []))

    return ContentDraftResult(
        topic=topic,
        content_type=content_type,
        items=items,
        evidence_references=[c.to_dict() for c in citations if c.evidence_id in used_ids],
        dropped_unsupported=dropped,
        request_id=current_request_id(),
    )
