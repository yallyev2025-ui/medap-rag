"""Оценка ответов студента (§21 ТЗ, этап 4A.2) и диагностика ошибок + repair
(§23, этап 4A.3).

Сравнение идёт не пословно с эталоном, а с материалами по теме — тем же
retrieval'ом, что и обычные вопросы (rag/retriever.py). Формальных Knowledge
Units в этом репозитории нет: `BookChunk.knowledge_unit_ids` зарезервировано по
§8, но не заполняется ничем в V1 — поэтому «covered/missing» формулируются как
пункты темы из найденного контекста, а не как id заранее размеченных Knowledge
Units. `evidenceReferences` — реальные citations из retrieval (см.
app/evidence/pack.py), не выдумываются моделью.

**AI не возвращает mastery score** — решение, как обновлять состояние обучения
студента, принимает продуктовый backend (§21, §25), которого в этом
репозитории нет; EvaluationResult отдаёт только сырую структурированную оценку.

ORAL_EVALUATE (голосовой ответ) сюда сознательно не входит — оральная оценка
требует транскрипта из STT, а голосовой workflow (§20) ещё не реализован
(отдельный батч этапа 4A).
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.evidence.pack import build_citations
from app.llm import provider as llm
from app.llm.task_map import Task
from app.observability.context import current_request_id
from constants import SOURCE_TEXTBOOK
from rag.generator import build_context, generate_repair, relevant_chunks
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

EVAL_SYSTEM_PROMPT = """Ты — строгий преподаватель-экзаменатор медицинского вуза. Тебе даны материалы по теме (КОНТЕКСТ) и ответ студента.
Сравни ответ студента с материалами и структурированно оцени его СТРОГО по контексту — не по общим знаниям.

Верни ТОЛЬКО JSON без пояснений, по схеме:
{
  "covered": ["пункт темы, который студент раскрыл верно", ...],
  "missing": ["пункт темы из контекста, который студент не упомянул", ...],
  "incorrect": ["фактически неверное утверждение студента", ...],
  "partiallyCorrect": ["утверждение студента, верное лишь частично/неточно", ...],
  "causalErrors": ["ошибка в причинно-следственной связи или механизме", ...],
  "terminologyErrors": ["неверный термин/название, использованный студентом", ...],
  "contradictions": ["прямое противоречие материалу", ...],
  "unsupportedStatements": ["утверждение студента, которое нельзя проверить по контексту", ...],
  "overallFeedback": "1-2 предложения общей обратной связи"
}

Пустой массив — если категория не применима. Не выдумывай факты вне контекста."""

EVAL_USER_TEMPLATE = """Тема/вопрос: {question}

Материалы:
{context}

Ответ студента:
{student_answer}"""

_EVAL_SCHEMA = {
    "required": [
        "covered",
        "missing",
        "incorrect",
        "partiallyCorrect",
        "causalErrors",
        "terminologyErrors",
        "contradictions",
        "unsupportedStatements",
        "overallFeedback",
    ]
}

_NO_MATERIAL_FEEDBACK = "По этой теме в материалах MedAP ничего не найдено — оценить ответ нечем."


@dataclass
class EvaluationResult:
    covered: list[str]
    missing: list[str]
    incorrect: list[str]
    partially_correct: list[str]
    causal_errors: list[str]
    terminology_errors: list[str]
    contradictions: list[str]
    unsupported_statements: list[str]
    overall_feedback: str
    evidence_references: list[dict[str, Any]] = field(default_factory=list)
    # Короткая адресная коррекция (§23), только если найдены ошибки — см. generate_repair.
    repair: str | None = None
    request_id: str | None = None


def _empty_result(feedback: str) -> EvaluationResult:
    return EvaluationResult(
        covered=[],
        missing=[],
        incorrect=[],
        partially_correct=[],
        causal_errors=[],
        terminology_errors=[],
        contradictions=[],
        unsupported_statements=[],
        overall_feedback=feedback,
        request_id=current_request_id(),
    )


async def _evaluate(
    question: str,
    student_answer: str,
    task: Task,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
) -> EvaluationResult:
    chunks = await retrieve(question, source_type=source_type, subject=subject)
    relevant = relevant_chunks(chunks)
    if not relevant:
        return _empty_result(_NO_MATERIAL_FEEDBACK)

    context = build_context(chunks)
    messages = [
        {"role": "system", "content": EVAL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": EVAL_USER_TEMPLATE.format(
                question=question, context=context, student_answer=student_answer
            ),
        },
    ]
    try:
        result = await llm.complete(task, messages, temperature=0.0, json_schema=_EVAL_SCHEMA)
        data = result.data or {}
    except llm.LLMError:
        logger.exception("Оценка ответа студента недоступна (сбой провайдера)")
        return _empty_result("Оценка сейчас недоступна, попробуй позже.")

    citations = build_citations(relevant[:5])

    errors = [
        *data.get("incorrect", []),
        *data.get("causalErrors", []),
        *data.get("contradictions", []),
    ]
    repair_text: str | None = None
    if errors:
        repair_answer = await generate_repair(question, errors, chunks)
        repair_text = repair_answer.text or None

    return EvaluationResult(
        covered=data.get("covered", []),
        missing=data.get("missing", []),
        incorrect=data.get("incorrect", []),
        partially_correct=data.get("partiallyCorrect", []),
        causal_errors=data.get("causalErrors", []),
        terminology_errors=data.get("terminologyErrors", []),
        contradictions=data.get("contradictions", []),
        unsupported_statements=data.get("unsupportedStatements", []),
        overall_feedback=data.get("overallFeedback", ""),
        evidence_references=[c.to_dict() for c in citations],
        repair=repair_text,
        request_id=current_request_id(),
    )


async def evaluate_recall(
    question: str,
    student_answer: str,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
) -> EvaluationResult:
    """Студент отвечает на КОНКРЕТНЫЙ вопрос — RECALL_EVALUATE (GPT-5.4 Mini, §56.2)."""
    return await _evaluate(question, student_answer, Task.RECALL_EVALUATE, source_type, subject)


async def evaluate_free_recall(
    topic: str,
    student_answer: str,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
) -> EvaluationResult:
    """Студент свободно вспоминает всё по ТЕМЕ (не отвечает на точечный вопрос) —
    FREE_RECALL_EVALUATE. Механизм оценки тот же, что и evaluate_recall."""
    return await _evaluate(topic, student_answer, Task.FREE_RECALL_EVALUATE, source_type, subject)
