"""Conflict Engine (§10 ТЗ): расхождение между реально процитированными
источниками должно вернуться структурой, а не потеряться/смешаться в прозе
ответа.

Запускается только когда среди процитированных источников ≥2 разных названий —
не на каждый ответ (§56.3: не вызывать вторую модель без необходимости).
"""

import logging

from app.evidence.pack import Citation, Conflict
from app.llm import provider as llm
from app.llm.task_map import Task

logger = logging.getLogger(__name__)

CONFLICT_SYSTEM_PROMPT = """Тебе даны вопрос, ответ ассистента и фрагменты нескольких источников, использованных в ответе.
Проверь, есть ли между источниками СОДЕРЖАТЕЛЬНОЕ расхождение по теме вопроса (разные цифры, классификации, определения, схемы лечения).
Верни JSON вида {"conflicts": [...]}. Если расхождений нет — {"conflicts": []}.
Каждый элемент conflicts: {"claim": "...", "sourceATitle": "...", "sourceBTitle": "...", "difference": "...", "contextRecommendation": "..."}.
sourceATitle/sourceBTitle должны быть НАЗВАНИЯМИ РОВНО из переданных источников, дословно. Только JSON, без пояснений."""

CONFLICT_USER_TEMPLATE = """Вопрос: {question}

Ответ: {answer}

Источники:
{sources}"""

_JSON_SCHEMA = {"required": ["conflicts"]}


def _source_block(citations: list[Citation]) -> str:
    return "\n---\n".join(
        f"[{c.source_title}, стр. {c.page}]\n{c.exact_supporting_text}" for c in citations
    )


async def detect_conflicts(question: str, answer: str, citations: list[Citation]) -> list[Conflict]:
    distinct_titles = {c.source_title for c in citations}
    if len(distinct_titles) < 2:
        return []

    by_title: dict[str, Citation] = {}
    for c in citations:
        by_title.setdefault(c.source_title, c)

    messages = [
        {"role": "system", "content": CONFLICT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": CONFLICT_USER_TEMPLATE.format(
                question=question, answer=answer, sources=_source_block(citations)
            ),
        },
    ]
    try:
        result = await llm.complete(
            Task.CLAIM_EVIDENCE_CHECK, messages, temperature=0.0, json_schema=_JSON_SCHEMA
        )
    except llm.LLMError:
        logger.warning("Conflict Engine недоступен, пропускаю проверку конфликтов")
        return []

    raw_items = (result.data or {}).get("conflicts")
    if not isinstance(raw_items, list):
        return []

    conflicts: list[Conflict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        a_title = item.get("sourceATitle")
        b_title = item.get("sourceBTitle")
        if not a_title or not b_title:
            continue
        a = by_title.get(a_title)
        b = by_title.get(b_title)
        conflicts.append(
            Conflict(
                claim=item.get("claim", ""),
                source_a_title=a_title,
                source_b_title=b_title,
                source_a_evidence_id=a.evidence_id if a else "",
                source_b_evidence_id=b.evidence_id if b else "",
                difference=item.get("difference", ""),
                context_recommendation=item.get("contextRecommendation", ""),
            )
        )
    return conflicts
