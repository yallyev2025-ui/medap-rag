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
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.evidence.viewer import fetch_evidence
from app.observability.context import request_context
from app.security.auth import document_upload_rate_limiter, rate_limiter, require_service_token
from app.workflows.ask import ask_grounded
from app.workflows.evaluate import EvaluationResult, evaluate_free_recall, evaluate_recall
from app.workflows.quick_outline import QuickOutlineResult, generate_quick_outline
from app.workflows.user_documents import (
    UserDocument,
    ask_user_document,
    delete_user_document,
    ingest_user_document,
    list_user_documents,
)
from app.workflows.vision import TestSolveResult, solve_from_image
from app.workflows.web_research import WebResearchResult, research_url
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


class QuickOutlineBlock(BaseModel):
    title: str
    items: list[str]


class QuickOutlineRequest(BaseModel):
    """Quick Outline (plans/medap-ai/QUICK_OUTLINE_SPEC.md) — вызывается с сайта владельца
    продукта для подготовки контента; к студенческому Q&A отношения не имеет."""

    topic: str = Field(..., min_length=1)
    context: StudentAIContext


class QuickOutlineResponse(BaseModel):
    topic: str
    type: str | None
    blocks: list[QuickOutlineBlock]
    requiredPoints: list[str]
    evidenceReferences: list[Citation]
    # Заполнено, только если по теме нет материалов или генерация недоступна —
    # в обоих случаях schema пустая, а не выдуманная.
    error: str | None = None
    requestId: str | None = None


def _quick_outline_response(result: QuickOutlineResult) -> QuickOutlineResponse:
    return QuickOutlineResponse(
        topic=result.topic,
        type=result.outline_type,
        blocks=[QuickOutlineBlock(title=b.title, items=b.items) for b in result.blocks],
        requiredPoints=result.required_points,
        evidenceReferences=[Citation(**c) for c in result.evidence_references],
        error=result.error,
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


@router.post("/quick-outline/generate", response_model=QuickOutlineResponse)
async def quick_outline_generate(payload: QuickOutlineRequest, request: Request) -> QuickOutlineResponse:
    """Quick Outline — строго типизированная «схема для тетради» по загруженным
    учебникам (plans/medap-ai/QUICK_OUTLINE_SPEC.md). Вызывается с сайта
    владельца продукта для подготовки контента; никак не задействован в
    обычном студенческом Q&A (тот отвечает как раньше)."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="QUICK_OUTLINE"):
        result = await generate_quick_outline(
            payload.topic,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
        )
    return _quick_outline_response(result)


# --- Документы пользователя (§18 ТЗ, этап 4A.5) --------------------------------
# Приватный документ студента (конспект, старый экзамен и т.п.): изоляция по
# userId+documentId в самом retrieval (rag/retriever.py), не только на уровне API.


class UserDocumentItem(BaseModel):
    documentId: str
    title: str
    subject: str | None = None
    chunksCount: int
    loadedAt: str | None = None


def _document_item(document: UserDocument) -> UserDocumentItem:
    return UserDocumentItem(
        documentId=str(document.id),
        title=document.title,
        subject=document.subject,
        chunksCount=document.chunks_count,
        loadedAt=document.loaded_at.isoformat() if document.loaded_at else None,
    )


class DocumentUploadRequest(BaseModel):
    """Base64, тот же JSON-контракт, что у Vision — без multipart."""

    filename: str = Field(..., min_length=1)
    fileBase64: str = Field(..., min_length=1)
    title: str | None = None
    context: StudentAIContext


class DocumentUploadResponse(BaseModel):
    document: UserDocumentItem | None
    error: str | None = None
    requestId: str | None = None


class DocumentListResponse(BaseModel):
    documents: list[UserDocumentItem]


class DocumentAskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    context: StudentAIContext


class DocumentAskResponse(BaseModel):
    answer: str
    verified: bool | None = None
    citations: list[Citation]
    error: str | None = None
    requestId: str | None = None


class DocumentDeleteResponse(BaseModel):
    deleted: bool


@router.post("/documents", response_model=DocumentUploadResponse)
async def documents_upload(payload: DocumentUploadRequest, request: Request) -> DocumentUploadResponse:
    """Загрузка личного документа студента (§18 ТЗ, этап 4A.5). Приватно: этот
    документ никогда не попадает в retrieval другого пользователя и не становится
    MedAP Verified содержимым автоматически."""
    document_upload_rate_limiter.check(payload.context.userId)
    try:
        file_bytes = base64.b64decode(payload.fileBase64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise HTTPException(status_code=400, detail="fileBase64 is not valid base64")

    extension = os.path.splitext(payload.filename)[1].lower()
    fd, tmp_path = tempfile.mkstemp(suffix=extension)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(file_bytes)
        with request_context(user_id=payload.context.userId, channel="api", workflow="DOCUMENT_QA"):
            result = await ingest_user_document(
                tmp_path,
                payload.filename,
                user_id=payload.context.userId,
                exam_id=payload.context.examId,
                title=payload.title,
                subject=payload.context.subjectId,
            )
    finally:
        os.remove(tmp_path)

    return DocumentUploadResponse(
        document=_document_item(result.document) if result.document else None,
        error=result.error,
        requestId=result.request_id,
    )


@router.get("/documents", response_model=DocumentListResponse)
async def documents_list(userId: str, request: Request) -> DocumentListResponse:
    rate_limiter.check(userId)
    documents = await list_user_documents(userId)
    return DocumentListResponse(documents=[_document_item(d) for d in documents])


@router.post("/documents/{document_id}/ask", response_model=DocumentAskResponse)
async def documents_ask(document_id: int, payload: DocumentAskRequest, request: Request) -> DocumentAskResponse:
    """Вопрос строго по ОДНОМУ личному документу пользователя — не по общему
    корпусу учебников. Чужой documentId даёт честную «не найдено», а не чужие данные."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="DOCUMENT_QA"):
        result = await ask_user_document(
            payload.question,
            user_id=payload.context.userId,
            document_id=document_id,
            exam_id=payload.context.examId,
        )
    return DocumentAskResponse(
        answer=result.answer,
        verified=result.verified,
        citations=[Citation(**c) for c in result.evidence_references],
        error=result.error,
        requestId=result.request_id,
    )


@router.delete("/documents/{document_id}", response_model=DocumentDeleteResponse)
async def documents_delete(document_id: int, userId: str, request: Request) -> DocumentDeleteResponse:
    """Немедленно убирает документ и все его чанки/эмбеддинги из выдачи (§18 ТЗ)."""
    rate_limiter.check(userId)
    with request_context(user_id=userId, channel="api", workflow="DOCUMENT_QA"):
        deleted = await delete_user_document(userId, document_id)
    return DocumentDeleteResponse(deleted=deleted)


# --- Web Research (§19, §34 ТЗ, этап 4A.6) -------------------------------------
# Одна конкретная страница по URL — не общий поиск по интернету (никакого
# поискового API в репозитории не сконфигурировано). Отдельно от ответа по
# учебникам: не идёт через retrieve()/generate_answer(), никогда не verified.


class WebResearchRequest(BaseModel):
    url: str = Field(..., min_length=1)
    question: str | None = None
    context: StudentAIContext


class WebResearchResponse(BaseModel):
    url: str
    answer: str
    sourceTitle: str | None = None
    # Последний по приоритету в constants.AUTHORITY_LEVELS — веб-контент никогда
    # не становится verified автоматически (§19, §34).
    authorityLevel: str = "web"
    verified: bool | None = None
    error: str | None = None
    requestId: str | None = None


def _web_research_response(result: WebResearchResult) -> WebResearchResponse:
    return WebResearchResponse(
        url=result.url,
        answer=result.answer,
        sourceTitle=result.source_title,
        error=result.error,
        requestId=result.request_id,
    )


@router.post("/web/research", response_model=WebResearchResponse)
async def web_research_endpoint(payload: WebResearchRequest, request: Request) -> WebResearchResponse:
    """Web Research (§19, §34 ТЗ, этап 4A.6): по конкретному URL — не общий поиск.
    SSRF-защита (app/security/ssrf.py) выполняется до первого байта ответа страницы;
    содержимое страницы передаётся модели как данные для анализа, не инструкции."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="WEB_RESEARCH"):
        result = await research_url(payload.url, payload.question)
    return _web_research_response(result)
