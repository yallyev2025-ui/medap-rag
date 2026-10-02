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

from collections.abc import Awaitable, Callable

from app.evidence.citations import extract_cited_chunks
from app.evidence.pack import build_citations
from app.llm import provider as llm
from app.llm.task_map import Task
from app.observability.context import current_request_id
from app.workflows.multi import answer_questions
from app.workflows.scope import Scope
from config import settings
from constants import SOURCE_TEXTBOOK
from rag.generator import generate_answer, relevant_chunks
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

VISION_EXTRACT_SYSTEM_PROMPT = """Ты распознаёшь фото или скрин экзаменационного теста/учебных вопросов.
На изображении может быть ОДИН или НЕСКОЛЬКО вопросов (в том числе десятки). Извлеки ВСЕ вопросы в порядке следования,
СТРОГО то, что видно, ничего не добавляя и не придумывая.

Верни ТОЛЬКО JSON по схеме:
{
  "questions": [
    {
      "number": номер вопроса как на изображении (или порядковый),
      "question": "текст вопроса, как написан на изображении",
      "options": ["вариант A", "вариант B", ...],
      "diagramDescription": "краткое описание схемы/рисунка к этому вопросу, если есть, иначе null"
    }
  ],
  "confidence": число от 0 до 1 — насколько уверенно распознан текст,
  "qualityIssue": "что мешает распознать (размыто/обрезано/плохое освещение/текст не виден) или null, если нормально"
}
options — пустой массив [], если вопрос открытый (без вариантов ответа). Не выдумывай текст, которого не видно:
если что-то нечитаемо, снижай confidence и укажи qualityIssue. Если вопрос на изображении один — в questions один элемент."""

# Обязателен только текст вопроса: у открытого вопроса нет вариантов, а оценку
# уверенности модель может опустить — это не повод считать прочитанное фото сбоем.
# Допустимы и новый формат (questions[]), и старый (один question): наличие вопроса
# проверяется в extract_from_image, а не схемой.
_VISION_SCHEMA: dict = {"required": []}

# Ниже этого порога честно просим переснять, а не угадываем нечитаемый текст.
MIN_CONFIDENCE = 0.5
# Если модель не назвала уверенность, но вопрос прочитан — умеренное значение выше порога.
DEFAULT_CONFIDENCE = 0.6


def _confidence(value: Any, has_question: bool) -> float:
    """Число 0..1 из того, что вернула модель (число, строка «0,85», проценты)."""
    number: float | None = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ".").rstrip("%"))
        except ValueError:
            number = None
    if number is None:
        return DEFAULT_CONFIDENCE if has_question else 0.0
    if number > 1:
        number /= 100.0
    return max(0.0, min(number, 1.0))


@dataclass
class ExtractedQuestion:
    number: int
    question: str
    options: list[str]
    diagram: str | None = None

    def as_text(self) -> str:
        text = self.question
        if self.options:
            text += "\nВарианты ответа: " + "; ".join(self.options)
        if self.diagram:
            text += f"\n(схема на изображении: {self.diagram})"
        return text


@dataclass
class VisionExtraction:
    # question/options/diagram_description — ПЕРВЫЙ вопрос (обратная совместимость
    # со старым форматом ответа); полный список — в questions.
    question: str
    options: list[str]
    diagram_description: str | None
    confidence: float
    quality_issue: str | None
    questions: list[ExtractedQuestion] = field(default_factory=list)


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
    # Сбой самого сервиса распознавания (провайдер недоступен) — причина по-русски.
    # Это НЕ вина фото: просить переснять в этом случае нельзя.
    service_error: str | None = None
    # Сколько порций генерации понадобилось (много вопросов на одном скрине).
    batches: int = 1


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
    questions = _parse_questions(data)
    first = questions[0] if questions else None
    return VisionExtraction(
        question=first.question if first else "",
        options=first.options if first else [],
        diagram_description=(first.diagram if first else None) or data.get("diagramDescription") or None,
        confidence=_confidence(data.get("confidence"), bool(first)),
        quality_issue=data.get("qualityIssue") or None,
        questions=questions,
    )


def _clean_options(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [o.strip() for o in raw if isinstance(o, str) and o.strip()]


def _parse_questions(data: dict[str, Any]) -> list[ExtractedQuestion]:
    """Вопросы из ответа модели: новый формат `questions[]`, а если его нет — старый
    одиночный `question`/`options`. Пустые вопросы отбрасываются; число вопросов ограничено
    VISION_MAX_QUESTIONS (страховка от разрастания расхода)."""
    found: list[ExtractedQuestion] = []
    raw = data.get("questions")
    if isinstance(raw, list):
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                continue
            text = str(item.get("question") or "").strip()
            if not text:
                continue
            number = item.get("number")
            found.append(
                ExtractedQuestion(
                    number=number if isinstance(number, int) and not isinstance(number, bool) else index,
                    question=text,
                    options=_clean_options(item.get("options")),
                    diagram=item.get("diagramDescription") or None,
                )
            )
    if not found:
        text = str(data.get("question") or "").strip()
        if text:
            found.append(ExtractedQuestion(1, text, _clean_options(data.get("options")), data.get("diagramDescription") or None))
    return found[: max(1, settings.VISION_MAX_QUESTIONS)]


async def solve_from_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
    scope: Scope | None = None,
    budget_ok: Callable[[], Awaitable[bool]] | None = None,
) -> TestSolveResult:
    """Распознаёт скрин и отвечает на ВСЕ вопросы на нём. Источник — строго по режиму
    студента (`scope`: учебники / документы / оба); без `scope` — учебники, как раньше."""
    try:
        extraction = await extract_from_image(image_bytes, mime_type)
    except llm.LLMError as exc:
        logger.exception("Vision недоступен (сбой провайдера)")
        return TestSolveResult(
            extraction=VisionExtraction(
                question="", options=[], diagram_description=None, confidence=0.0, quality_issue=None,
            ),
            needs_retake=False,
            request_id=current_request_id(),
            service_error=exc.reason or "сервис распознавания не ответил",
        )

    if extraction.confidence < MIN_CONFIDENCE or not extraction.questions:
        return TestSolveResult(extraction=extraction, needs_retake=True, request_id=current_request_id())

    effective = scope if scope is not None else Scope(source_type=source_type, subject=subject)

    # Несколько вопросов или режим с документом: порциями, одна генерация на порцию.
    if len(extraction.questions) > 1 or effective.uses_documents:
        items = [q.as_text() for q in extraction.questions]
        multi = await answer_questions(
            items, effective, task=Task.TEST_SOLVE_TEXT, budget_ok=budget_ok
        )
        return TestSolveResult(
            extraction=extraction,
            needs_retake=False,
            answer=multi.text,
            citations=multi.citations,
            request_id=current_request_id(),
            batches=multi.batches,
        )

    question_text = extraction.questions[0].as_text()
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
