"""Vision / Test Solver (§16, §17, §53.2 ТЗ, этап 4A.4).

```
Photo/Screenshot → VISION_EXTRACT (GPT-5.4 Mini) → question+options+diagram →
confidence check → retrieval → solving → evidence verification → answer
```

Vision интерпретирует ВХОД, но не является источником медицинской истины: то,
что распознано на фото, дальше решается через тот же Evidence+Verification
конвейер, что и обычные вопросы (rag/generator.py, app/verification/verify.py).
При низкой уверенности распознавания — честная просьба переснять, а не
угадывание текста (V1 не позиционируется как диагностическая система чтения
рентгена/КТ/МРТ — только текстовые тестовые вопросы/скрины).
"""

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

from app.evidence.citations import extract_cited_chunks
from app.evidence.pack import build_citations
from app.llm import provider as llm
from app.llm.task_map import Task
from app.observability.context import current_request_id
from constants import SOURCE_TEXTBOOK
from rag.generator import generate_answer, relevant_chunks
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

VISION_EXTRACT_SYSTEM_PROMPT = """Ты распознаёшь фото или скрин экзаменационного теста/учебного вопроса.
Извлеки СТРОГО то, что видно на изображении, ничего не добавляя и не придумывая.

Верни ТОЛЬКО JSON по схеме:
{
  "question": "текст вопроса, как написан на фото",
  "options": ["вариант A", "вариант B", ...],
  "diagramDescription": "краткое описание схемы/рисунка на фото, если есть, иначе null",
  "confidence": число от 0 до 1 — насколько уверенно распознан текст,
  "qualityIssue": "что мешает распознать (размыто/обрезано/плохое освещение/текст не виден) или null, если фото нормальное"
}
options — пустой массив [], если вопрос открытый (без вариантов ответа). Не выдумывай текст,
которого не видно: если что-то нечитаемо, снижай confidence и укажи qualityIssue."""

_VISION_SCHEMA = {"required": ["question", "options", "confidence"]}

# Ниже этого порога честно просим переснять, а не угадываем нечитаемый текст.
MIN_CONFIDENCE = 0.5


@dataclass
class VisionExtraction:
    question: str
    options: list[str]
    diagram_description: str | None
    confidence: float
    quality_issue: str | None


@dataclass
class TestSolveResult:
    extraction: VisionExtraction
    # True — распознавание неуверенное или вопрос не извлёкся, нужно попросить
    # переснять; в этом случае answer всегда None.
    needs_retake: bool
    answer: str | None = None
    verified: bool | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None


async def extract_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> VisionExtraction:
    """VISION_EXTRACT: распознаёт вопрос/варианты/схему с фото. При сбое
    провайдера — низкая уверенность и просьба переснять, а не исключение
    наружу (см. вызывающий solve_from_image)."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": VISION_EXTRACT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Распознай тест/вопрос на этом фото."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
            ],
        },
    ]
    result = await llm.complete(
        Task.VISION_EXTRACT, messages, temperature=0.0, json_schema=_VISION_SCHEMA, image_units=1
    )
    data = result.data or {}
    return VisionExtraction(
        question=(data.get("question") or "").strip(),
        options=[o for o in (data.get("options") or []) if isinstance(o, str) and o.strip()],
        diagram_description=data.get("diagramDescription") or None,
        confidence=float(data.get("confidence") or 0.0),
        quality_issue=data.get("qualityIssue") or None,
    )


async def solve_from_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
) -> TestSolveResult:
    try:
        extraction = await extract_from_image(image_bytes, mime_type)
    except llm.LLMError:
        logger.exception("Vision недоступен (сбой провайдера)")
        return TestSolveResult(
            extraction=VisionExtraction(
                question="", options=[], diagram_description=None, confidence=0.0,
                quality_issue="Распознавание фото сейчас недоступно, попробуй позже.",
            ),
            needs_retake=True,
            request_id=current_request_id(),
        )

    if extraction.confidence < MIN_CONFIDENCE or not extraction.question:
        return TestSolveResult(extraction=extraction, needs_retake=True, request_id=current_request_id())

    question_text = extraction.question
    if extraction.options:
        question_text += "\nВарианты ответа: " + "; ".join(extraction.options)

    chunks = await retrieve(question_text, source_type=source_type, subject=subject)
    relevant = relevant_chunks(chunks)
    if not relevant:
        return TestSolveResult(
            extraction=extraction,
            needs_retake=False,
            answer="По этой теме в материалах MedAP ничего не найдено — не могу дать заземлённый ответ.",
            request_id=current_request_id(),
        )

    generated = await generate_answer(
        question_text, chunks, source_type=source_type, task=Task.TEST_SOLVE_TEXT
    )

    citations: list[dict[str, Any]] = []
    if generated.verified is not False:
        cited_chunks = extract_cited_chunks(generated.text, relevant)
        citations = [c.to_dict() for c in build_citations(cited_chunks)]

    return TestSolveResult(
        extraction=extraction,
        needs_retake=False,
        answer=generated.text,
        verified=generated.verified,
        citations=citations,
        request_id=current_request_id(),
    )
