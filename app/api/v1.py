"""MedAP Student AI API v1 — стабильный контракт для образовательного сайта (§27 ТЗ).

Сайт MedAP и Telegram-бот вызывают один и тот же workflow; отдельного
пользовательского сайта Student AI не существует (см. дополнение к ТЗ, раздел 1).

Аутентификация — service-to-service: `Authorization: Bearer <SERVICE_TOKEN>`.
`userId` в теле запроса не является доказательством личности (§28), он лишь
связывает запрос с пользователем вызывающего сервиса — для лимитов, телеметрии и
диагностики.
"""

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.evidence.viewer import fetch_evidence
from app.observability.context import request_context
from app.security.auth import rate_limiter, require_service_token
from app.workflows.ask import ask_grounded
from app.workflows.evaluate import EvaluationResult, evaluate_free_recall, evaluate_recall
from app.workflows.vision import TestSolveResult, solve_from_image
from constants import SOURCE_TEXTBOOK
from rag.generator import generate_repair
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["student-ai"], dependencies=[Depends(require_service_token)])


class StudentAIContext(BaseModel):
    """Контекст запроса из §28 ТЗ. Все поля, кроме userId, опциональны."""

    userId: str
    subjectId: str | None = None
    topicId: str | None = None
    contentId: str | None = None
    examId: str | None = None
    examQuestionId: str | None = None
    knowledgeUnitIds: list[str] | None = None
    selectedText: str | None = None
    # Какие коллекции источников разрешены: 'учебник' или 'клинрек'.
    sourceMode: str | None = None
    locale: str | None = "ru"


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    context: StudentAIContext
    # Явный режим: GROUNDED_QA, EXPLAIN, CLASS_QUICK. Пусто — решает Orchestrator.
    workflow: str | None = None
    # Лёгкая память диалога: пары (вопрос, ответ) в хронологическом порядке.
    history: list[tuple[str, str]] | None = None


class Citation(BaseModel):
    citationId: str
    # evidenceId/sourceId — реальные id из БД (BookChunk.id / Book.id): по ним
    # /v1/evidence/{evidenceId} отдаёт точную страницу и координаты фрагмента
    # для подсветки на образовательном сайте (§13 ТЗ, Source Viewer).
    evidenceId: str
    sourceId: str
    sourceTitle: str
    author: str
    subject: str
    page: int | None = None
    pageTo: int | None = None
    section: str | None = None
    exactSupportingText: str
    authorityLevel: str | None = None
    verificationStatus: str | None = None
    relevance: float | None = None


class Conflict(BaseModel):
    """Содержательное расхождение между двумя процитированными источниками (§10 ТЗ)."""

    claim: str
    sourceATitle: str
    sourceBTitle: str
    sourceAEvidenceId: str
    sourceBEvidenceId: str
    difference: str
    contextRecommendation: str


class ChatResponse(BaseModel):
    answer: str
    workflow: str
    # False — в материалах MedAP подтверждения не нашлось; ответ честно говорит об
    # этом, а не выдумывает медицинский текст (§15).
    grounded: bool
    # Verification Layer (§12, §36): True — прошёл проверку; False — не прошёл
    # даже после перегенерации (answer уже честный отказ); None — верификация не
    # проводилась или сам верификатор был недоступен.
    verified: bool | None = None
    citations: list[Citation]
    conflicts: list[Conflict] = Field(default_factory=list)
    requestId: str | None
    versions: dict[str, str]
    diagnostics: dict


class EvidenceDetail(BaseModel):
    """Source Viewer (§13 ТЗ, раздел 7 дополнения): по evidenceId — точная
    страница/раздел/координаты фрагмента, чтобы сайт подсветил его в источнике."""

    evidenceId: str
    sourceId: str
    sourceTitle: str
    author: str
    subject: str
    page: int | None
    pageTo: int | None
    section: str | None
    exactSupportingText: str
    charStart: int | None
    charEnd: int | None
    authorityLevel: str
    verificationStatus: str
    # Presigned-ссылка на оригинал в S3 — None, если S3 не настроен (мягкая
    # деградация, см. app/storage/s3.py) или у источника ещё нет file_path.
    url: str | None = None


class EvaluateRequest(BaseModel):
    """Recall — ответ на конкретный вопрос; Free-recall — свободное воспроизведение
    темы (§21 ТЗ, этап 4A.2). В обоих случаях `question` — то, по чему искать
    материалы (вопрос или тема), `studentAnswer` — текст, который оценивается."""

    question: str = Field(..., min_length=1)
    studentAnswer: str = Field(..., min_length=1)
    context: StudentAIContext


class EvaluationResponse(BaseModel):
    """AI НЕ возвращает mastery score — только сырую структурированную оценку;
    решение, как обновлять состояние обучения, принимает продуктовый backend (§21, §25)."""

    covered: list[str]
    missing: list[str]
    incorrect: list[str]
    partiallyCorrect: list[str]
    causalErrors: list[str]
    terminologyErrors: list[str]
    contradictions: list[str]
    unsupportedStatements: list[str]
    overallFeedback: str
    evidenceReferences: list[Citation]
    # Короткая адресная коррекция (§23) — только если найдены ошибки, иначе None.
    repair: str | None = None
    requestId: str | None = None


def _evaluation_response(result: EvaluationResult) -> EvaluationResponse:
    return EvaluationResponse(
        covered=result.covered,
        missing=result.missing,
        incorrect=result.incorrect,
        partiallyCorrect=result.partially_correct,
        causalErrors=result.causal_errors,
        terminologyErrors=result.terminology_errors,
        contradictions=result.contradictions,
        unsupportedStatements=result.unsupported_statements,
        overallFeedback=result.overall_feedback,
        evidenceReferences=[Citation(**c) for c in result.evidence_references],
        repair=result.repair,
        requestId=result.request_id,
    )


class RepairRequest(BaseModel):
    """Точечная коррекция без повторной оценки — когда ошибки уже известны
    (например, из предыдущего /v1/evaluate/recall)."""

    question: str = Field(..., min_length=1)
    errors: list[str] = Field(..., min_length=1)
    context: StudentAIContext


class RepairResponse(BaseModel):
    repair: str
    verified: bool | None = None
    requestId: str | None = None


class VisionAnalyzeRequest(BaseModel):
    """Фото/скрин теста (§17 ТЗ, этап 4A.4). Base64, не multipart — тот же
    JSON-контракт, что у остальных /v1 эндпоинтов."""

    imageBase64: str = Field(..., min_length=1)
    mimeType: str = "image/jpeg"
    context: StudentAIContext


class VisionAnalyzeResponse(BaseModel):
    question: str
    options: list[str]
    diagramDescription: str | None
    confidence: float
    # True — распознавание неуверенное, нужно попросить переснять; answer в
    # этом случае всегда null.
    needsRetake: bool
    qualityIssue: str | None
    answer: str | None
    verified: bool | None
    citations: list[Citation]
    requestId: str | None


def _vision_response(result: TestSolveResult) -> VisionAnalyzeResponse:
    return VisionAnalyzeResponse(
        question=result.extraction.question,
        options=result.extraction.options,
        diagramDescription=result.extraction.diagram_description,
        confidence=result.extraction.confidence,
        needsRetake=result.needs_retake,
        qualityIssue=result.extraction.quality_issue,
        answer=result.answer,
        verified=result.verified,
        citations=[Citation(**c) for c in result.citations],
        requestId=result.request_id,
    )


async def _answer(payload: ChatRequest, request: Request, forced_workflow: str | None = None) -> ChatResponse:
    rate_limiter.check(payload.context.userId)

    with request_context(user_id=payload.context.userId, channel="api") as ctx:
        # Вопрос студента может приходить с выделенным на сайте фрагментом — он
        # уточняет, о чём именно спрашивают (§28).
        question = payload.question
        if payload.context.selectedText:
            question = f"{question}\n\nВыделенный фрагмент: {payload.context.selectedText}"

        result = await ask_grounded(
            question,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
            turns=payload.history,
            requested_workflow=forced_workflow or payload.workflow,
        )

    logger.info(
        "v1 ответ: workflow=%s grounded=%s citations=%d request_id=%s",
        result.workflow,
        result.has_relevant,
        len(result.citations),
        ctx.request_id,
    )
    return ChatResponse(
        answer=result.answer or "",
        workflow=result.workflow,
        grounded=result.has_relevant,
        verified=result.verified,
        citations=[Citation(**c) for c in result.citations],
        conflicts=[Conflict(**c) for c in result.conflicts],
        requestId=result.request_id,
        versions=result.versions,
        diagnostics=result.diagnostics,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    """Основной медицинский вопрос студента: ответ по материалам MedAP с источниками."""
    return await _answer(payload, request)


@router.post("/explain", response_model=ChatResponse)
async def explain(payload: ChatRequest, request: Request) -> ChatResponse:
    """Объяснение темы или механизма (§2, workflow EXPLAIN/LEARN)."""
    return await _answer(payload, request, forced_workflow="EXPLAIN")


@router.get("/evidence/{evidence_id}", response_model=EvidenceDetail)
async def get_evidence(evidence_id: int, request: Request) -> EvidenceDetail:
    """Source Viewer backend (§13 ТЗ, раздел 7 дополнения): по evidenceId из
    citation — точная страница/раздел/координаты, чтобы сайт открыл источник и
    подсветил именно этот фрагмент."""
    detail = await fetch_evidence(evidence_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="evidence not found")

    return EvidenceDetail(
        evidenceId=detail.evidence_id,
        sourceId=detail.source_id,
        sourceTitle=detail.source_title,
        author=detail.author,
        subject=detail.subject,
        page=detail.page,
        pageTo=detail.page_to,
        section=detail.section,
        exactSupportingText=detail.exact_supporting_text,
        charStart=detail.char_start,
        charEnd=detail.char_end,
        authorityLevel=detail.authority_level,
        verificationStatus=detail.verification_status,
        url=detail.url,
    )


@router.post("/evaluate/recall", response_model=EvaluationResponse)
async def evaluate_recall_endpoint(payload: EvaluateRequest, request: Request) -> EvaluationResponse:
    """Студент отвечает на конкретный вопрос — структурированная оценка по
    материалам (§21 ТЗ, этап 4A.2), не пословное сравнение с эталоном."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="RECALL_EVALUATION"):
        result = await evaluate_recall(
            payload.question,
            payload.studentAnswer,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
        )
    return _evaluation_response(result)


@router.post("/evaluate/free-answer", response_model=EvaluationResponse)
async def evaluate_free_answer_endpoint(payload: EvaluateRequest, request: Request) -> EvaluationResponse:
    """Студент свободно воспроизводит тему целиком (не отвечает на точечный
    вопрос) — тот же механизм оценки, что и recall."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="FREE_RECALL_EVALUATION"):
        result = await evaluate_free_recall(
            payload.question,
            payload.studentAnswer,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
        )
    return _evaluation_response(result)


@router.post("/repair", response_model=RepairResponse)
async def repair(payload: RepairRequest, request: Request) -> RepairResponse:
    """Точечная коррекция по уже известным ошибкам (§23 ТЗ, этап 4A.3) — без
    повторной оценки, если она уже была сделана раньше (например, evaluate/recall)."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="TARGETED_REPAIR") as ctx:
        chunks = await retrieve(
            payload.question,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
        )
        result = await generate_repair(payload.question, payload.errors, chunks)
    return RepairResponse(repair=result.text, verified=result.verified, requestId=ctx.request_id)


@router.post("/vision/analyze", response_model=VisionAnalyzeResponse)
async def vision_analyze(payload: VisionAnalyzeRequest, request: Request) -> VisionAnalyzeResponse:
    """Vision/Test Solver (§16, §17, §53.2 ТЗ, этап 4A.4): фото/скрин теста →
    распознавание → решение по материалам MedAP с verification и citations.
    Vision не источник медицинской истины — решает тот же Evidence-конвейер,
    что и обычные вопросы."""
    rate_limiter.check(payload.context.userId)
    try:
        image_bytes = base64.b64decode(payload.imageBase64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise HTTPException(status_code=400, detail="imageBase64 is not valid base64")

    with request_context(user_id=payload.context.userId, channel="api", workflow="VISION_EXTRACT"):
        result = await solve_from_image(
            image_bytes,
            payload.mimeType,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
        )
    return _vision_response(result)
