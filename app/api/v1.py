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
import re
import os
import tempfile
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.evidence.viewer import fetch_evidence
from app.llm import provider as llm
from app.llm.registry import cost_usd, profile, to_rub
from app.llm.task_map import Task
from app.observability.context import request_context
from app.security.auth import document_upload_rate_limiter, rate_limiter, require_service_token
from app.workflows.ask import ask_grounded
from app.workflows.conspect import MAX_POINTS, generate_conspect
from app.workflows.content_studio import CONTENT_CASE, CONTENT_RECALL, CONTENT_TEST, generate_content
from app.workflows.evaluate import EvaluationResult, evaluate_free_recall, evaluate_oral, evaluate_recall
from app.workflows.quick_outline import QuickOutlineResult, generate_quick_outline
from app.workflows.user_documents import (
    UserDocument,
    ask_user_document,
    ask_user_documents,
    delete_user_document,
    ingest_user_document,
    list_user_documents,
    search_user_documents,
)
from app.workflows import book_admin
from app.workflows.sources import book_outline, fragments_from_chunks, get_figure_url, list_sources, search_figures
from app.workflows.vision import TestSolveResult, solve_from_image
from app.workflows.presentations import MAX_OUTPUT_TOKENS as PRESENTATION_MAX_TOKENS
from app.workflows.presentations import RelayError, complete as presentation_complete
from app.workflows.pubmed import PubMedResult, _SourcesUnavailable, search_pubmed
from app.workflows import images as image_relay
from app.workflows import pubmed_ru, pubmed_sources
from app.workflows.web_research import WebResearchResult, research_url
from app.workflows.web_page import fetch_page_text
from app.workflows.web_search import WebSearchResult, search_and_answer
from config import settings
from constants import SOURCE_TEXTBOOK
from db.crud import month_spend_rub_by_id, request_cost, usage_summary
from db.session import async_session
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
    # Формат текста ответа (батч 29): "plain" — чистый текст-конспект (абзацы через пустую строку,
    # без разметки и эмодзи) — по умолчанию, для сайта, который рисует ответ как обычный текст;
    # "markdown" — конспект с заголовками «## », списками и таблицами для клиента, умеющего их рисовать.
    outputFormat: Literal["plain", "markdown"] = "plain"
    # Режим ответа (батч 31): "fast" — 12–15 фрагментов и дешёвая модель, "deep" — 30 фрагментов
    # и сильная модель (дороже по лимиту). Пусто — прежнее поведение (маппинг по умолчанию).
    # Право на глубокий проверяет вызывающий сервис (сайт): у бота нет данных о его подписках.
    tier: Literal["fast", "deep"] | None = None


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
    # Контракт ответа (§36, батч 10) — производные поля, без нового вызова LLM.
    # sourceMode: MEDAP/CLINICAL_RECOMMENDATION (GENERAL_KNOWLEDGE/WEB/PUBMED —
    # отдельные эндпоинты, не этот). evidenceStatus: SUFFICIENT/PARTIAL/
    # INSUFFICIENT/CONFLICTING. verificationStatus: VERIFIED/PARTIALLY_VERIFIED/
    # UNVERIFIED/ABSTAINED.
    sourceMode: str = "MEDAP"
    evidenceStatus: str = "INSUFFICIENT"
    verificationStatus: str = "UNVERIFIED"
    # Сколько стоил весь запрос (все вызовы модели, батч 31+): сайт ведёт по нему месячный лимит
    # студента. 0 — ответ из кеша или стоимость не записана.
    costUsd: float = 0.0
    costRub: float = 0.0
    unsupportedAreas: list[str] = Field(default_factory=list)
    clinicalWarnings: list[str] = Field(default_factory=list)


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
    # Заполнено только для устного ответа (§20, этап 4A.7) — что распознал Whisper.
    transcript: str | None = None


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
        transcript=result.transcript,
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


class VisionQuestion(BaseModel):
    number: int
    question: str
    options: list[str]
    diagramDescription: str | None = None


class VisionAnalyzeResponse(BaseModel):
    # question/options — ПЕРВЫЙ вопрос на изображении (обратная совместимость);
    # все вопросы — в questions (батч 27: на одном скрине их может быть много).
    question: str
    options: list[str]
    questions: list[VisionQuestion] = []
    diagramDescription: str | None
    confidence: float
    # True — распознавание неуверенное, нужно попросить переснять; answer в
    # этом случае всегда null.
    needsRetake: bool
    qualityIssue: str | None
    # Сбой сервиса распознавания (не вина фото; needsRetake при этом false).
    serviceError: str | None = None
    answer: str | None
    verified: bool | None
    citations: list[Citation]
    requestId: str | None


def _vision_response(result: TestSolveResult) -> VisionAnalyzeResponse:
    return VisionAnalyzeResponse(
        question=result.extraction.question,
        options=result.extraction.options,
        questions=[
            VisionQuestion(number=q.number, question=q.question, options=q.options, diagramDescription=q.diagram)
            for q in result.extraction.questions
        ],
        diagramDescription=result.extraction.diagram_description,
        confidence=result.extraction.confidence,
        needsRetake=result.needs_retake,
        qualityIssue=result.extraction.quality_issue,
        serviceError=result.service_error,
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
            output_format=payload.outputFormat,
            tier=payload.tier,
        )

    cost_usd_total = cost_rub_total = 0.0
    if result.request_id:
        try:
            async with async_session() as session:
                cost_usd_total, cost_rub_total = await request_cost(session, result.request_id)
        except Exception:  # учёт не должен ронять ответ студенту
            logger.warning("Не удалось посчитать стоимость запроса %s", result.request_id, exc_info=True)

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
        sourceMode=result.source_mode,
        evidenceStatus=result.evidence_status,
        verificationStatus=result.verification_status,
        unsupportedAreas=result.unsupported_areas,
        clinicalWarnings=result.clinical_warnings,
        costUsd=round(cost_usd_total, 6),
        costRub=round(cost_rub_total, 4),
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


class EvaluateOralRequest(BaseModel):
    """Устный ответ (§20 ТЗ, этап 4A.7). Base64, тот же JSON-контракт, что у Vision."""

    question: str = Field(..., min_length=1)
    audioBase64: str = Field(..., min_length=1)
    context: StudentAIContext


@router.post("/evaluate/oral", response_model=EvaluationResponse)
async def evaluate_oral_endpoint(payload: EvaluateOralRequest, request: Request) -> EvaluationResponse:
    """Голосовой ответ студента: Whisper-транскрипт → та же оценка, что и Recall
    (§20, §21 ТЗ, этап 4A.7). Сбой STT — честная просьба перезаписать в overallFeedback,
    не выдуманная оценка по обрывкам."""
    rate_limiter.check(payload.context.userId)
    try:
        audio_bytes = base64.b64decode(payload.audioBase64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise HTTPException(status_code=400, detail="audioBase64 is not valid base64")

    if len(audio_bytes) > settings.ORAL_MAX_AUDIO_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Аудио больше {settings.ORAL_MAX_AUDIO_MB} МБ")

    with request_context(user_id=payload.context.userId, channel="api", workflow="ORAL_EVALUATE"):
        result = await evaluate_oral(
            payload.question,
            audio_bytes,
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

    if len(image_bytes) > settings.VISION_MAX_IMAGE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Изображение больше {settings.VISION_MAX_IMAGE_MB} МБ")

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


# --- Content Studio (§31 ТЗ, этап 4B) -------------------------------------------
# Как Quick Outline: вызывается с сайта владельца, черновик хранится и публикуется
# там. Ответ всегда status="AI_DRAFT" — без проверки человеком не публикуется.


class ContentRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    count: int = Field(5, ge=1, le=20)
    context: StudentAIContext


class ContentDraftResponse(BaseModel):
    topic: str
    contentType: str
    status: str
    # Каждый элемент несёт citationIds/evidenceIds — трассировку к конкретным
    # фрагментам (evidenceReferences ниже и /v1/evidence/{evidenceId}).
    items: list[dict[str, Any]]
    evidenceReferences: list[Citation]
    droppedUnsupported: int = 0
    error: str | None = None
    requestId: str | None = None


async def _generate_content(content_type: str, payload: ContentRequest) -> ContentDraftResponse:
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow=f"CONTENT_{content_type.upper()}"):
        result = await generate_content(
            content_type,
            payload.topic,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
            count=payload.count,
        )
    return ContentDraftResponse(
        topic=result.topic,
        contentType=result.content_type,
        status=result.status,
        items=result.items,
        evidenceReferences=[Citation(**c) for c in result.evidence_references],
        droppedUnsupported=result.dropped_unsupported,
        error=result.error,
        requestId=result.request_id,
    )


@router.post("/content/recall-items", response_model=ContentDraftResponse)
async def content_recall_items(payload: ContentRequest, request: Request) -> ContentDraftResponse:
    """Recall-вопросы с эталонными пунктами ответа — черновик для сайта владельца."""
    return await _generate_content(CONTENT_RECALL, payload)


@router.post("/content/test-questions", response_model=ContentDraftResponse)
async def content_test_questions(payload: ContentRequest, request: Request) -> ContentDraftResponse:
    """Тестовые вопросы (варианты, правильный, объяснение) — черновик для сайта владельца."""
    return await _generate_content(CONTENT_TEST, payload)


@router.post("/content/clinical-case", response_model=ContentDraftResponse)
async def content_clinical_case(payload: ContentRequest, request: Request) -> ContentDraftResponse:
    """Учебный клинический кейс с вопросами и разбором — черновик для сайта владельца."""
    return await _generate_content(CONTENT_CASE, payload)


# --- Конспект по пунктам темы для сайта владельца (батч 30) ---------------------
# Поиск по каждому пункту, генерация порциями, промпты КРАТКИЙ/ПОДРОБНЫЙ + предмет.
# Результат — черновик: сайт показывает его редактору и публикует только после проверки.


class ConspectPointIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    # Суть пункта в 1–2 фразах (необязательно): уточняет поиск и подсказывает модели границы пункта.
    intent: str | None = Field(None, max_length=500)


class ConspectRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=300)
    points: list[ConspectPointIn] = Field(..., min_length=1, max_length=MAX_POINTS)
    mode: Literal["brief", "detailed"] = "detailed"
    # context.subjectId — код предмета для поиска и подбора надстройки (пусто — определится сам);
    # context.sourceMode — 'учебник' (по умолчанию) или 'клинрек'.
    context: StudentAIContext


class ConspectSection(BaseModel):
    number: int
    title: str
    citationIds: list[str]


class ConspectMissing(BaseModel):
    number: int
    title: str
    note: str


class ConspectWarning(BaseModel):
    number: int | None = None
    kind: str
    message: str


class ConspectResponse(BaseModel):
    topic: str
    mode: str
    # Markdown-диалект конспекта: «## N. Пункт», «**Термин** — …», «> Внимание: …»,
    # «> В клинике: …», таблицы, итоговый «## Нет в источниках». Без эмодзи и скобок с источниками.
    markdown: str
    citations: list[Citation]
    sections: list[ConspectSection]
    missing: list[ConspectMissing]
    warnings: list[ConspectWarning]
    subject: str | None = None
    batches: int = 0
    truncated: bool = False
    error: str | None = None
    requestId: str | None = None


@router.post("/conspect/generate", response_model=ConspectResponse)
async def conspect_generate(payload: ConspectRequest, request: Request) -> ConspectResponse:
    """Конспект темы по пунктам плана: подробный или краткий, по учебникам MedAP."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="CONSPECT_WRITE"):
        result = await generate_conspect(
            payload.topic,
            [(p.title, p.intent) for p in payload.points],
            mode=payload.mode,
            source_type=payload.context.sourceMode or SOURCE_TEXTBOOK,
            subject=payload.context.subjectId,
        )
    return ConspectResponse(
        topic=result.topic,
        mode=result.mode,
        markdown=result.markdown,
        citations=[Citation(**c) for c in result.citations],
        sections=[ConspectSection(**s) for s in result.sections],
        missing=[ConspectMissing(**m) for m in result.missing],
        warnings=[ConspectWarning(**w) for w in result.warnings],
        subject=result.subject,
        batches=result.batches,
        truncated=result.truncated,
        error=result.error,
        requestId=result.request_id,
    )


# --- Написание части конспекта по промпту сайта --------------------------------
# Второй способ конспекта, рядом с /conspect/generate: сайт сам ищет фрагменты,
# собирает промпт, проверяет структуру и разбирает ответ в блоки; сюда приходит
# только «напиши по этому». Бот выбирает модель по TaskModelMap (CONSPECT_WRITE —
# та же задача, что у /conspect/generate: одна настройка на оба пути) и считает
# расход. Никакой своей логики поверх: правила конспекта живут на сайте, рядом
# с проверкой ответа. Нужен, потому что сайт стоит в России, откуда OpenAI не
# отвечает, а бот — в Нидерландах (04.10.2026, владелец: «подключить просто из
# medap-rag»).

# Потолок длины ответа: страховка расхода от неверного запроса, а не ограничение
# для нормальной части конспекта (она укладывается в 16 тыс.).
CONSPECT_WRITE_MAX_TOKENS = 32_000


class ConspectWriteRequest(BaseModel):
    system: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    maxTokens: int | None = Field(None, ge=256, le=CONSPECT_WRITE_MAX_TOKENS)
    # Просить у модели строго валидный JSON (режим провайдера) — сайт ждёт блоки
    jsonMode: bool = False
    context: StudentAIContext


class ConspectWriteResponse(BaseModel):
    text: str
    provider: str | None = None
    model: str | None = None
    # Ответ упёрся в лимит длины и оборвался — сайт просит писать короче
    truncated: bool = False
    inputTokens: int = 0
    cachedInputTokens: int = 0
    outputTokens: int = 0
    # Сколько стоил вызов по ценам бота (формула §65): сайт показывает это
    # владельцу рядом с конспектом
    costUsd: float = 0.0
    costRub: float = 0.0
    error: str | None = None
    requestId: str | None = None


@router.post("/content/conspect", response_model=ConspectWriteResponse)
async def content_conspect(payload: ConspectWriteRequest, request: Request) -> ConspectWriteResponse:
    """Написать часть конспекта по промпту сайта. Текст возвращается как есть — разбирает сайт."""
    rate_limiter.check(payload.context.userId)
    messages = [
        {"role": "system", "content": payload.system},
        {"role": "user", "content": payload.prompt},
    ]

    with request_context(user_id=payload.context.userId, channel="api", workflow="CONSPECT_WRITE"):
        try:
            result = await llm.complete(
                Task.CONSPECT_WRITE,
                messages,
                temperature=0.1,  # переписывание чужого текста: чем ниже, тем меньше отсебятины
                max_output_tokens=payload.maxTokens,
                json_mode=payload.jsonMode,
            )
        except llm.LLMError as exc:
            logger.exception("Конспект (промпт сайта): сбой провайдера")
            # Настоящая причина (`reason`) — сайту, чтобы владелец видел её на экране
            return ConspectWriteResponse(text="", error=exc.reason or str(exc))

    usd = cost_usd(profile(result.provider), result.input_tokens, result.cached_input_tokens, result.output_tokens)

    return ConspectWriteResponse(
        text=result.text,
        provider=result.provider,
        model=result.model,
        truncated=result.truncated,
        inputTokens=result.input_tokens,
        cachedInputTokens=result.cached_input_tokens,
        outputTokens=result.output_tokens,
        costUsd=round(usd, 6),
        costRub=round(to_rub(usd), 4),
    )


# --- Презентации сайта: посредник к Sonnet и Luna (этап 3 презентаций) ---------
# Сайт собирает промпты и разбирает ответ сам; боту нужен только вызов модели: Sonnet и Luna доступны
# лишь с этого сервера. Список моделей закрыт (имя называет сайт), потолки токенов и размера запроса
# проверяются до платного вызова, расход возвращается сайту и пишется в AIUsageEvent.


class PresentationImage(BaseModel):
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    data: str = Field(min_length=1, max_length=600_000)


class PresentationCompleteRequest(BaseModel):
    model: Literal["sonnet", "luna"]
    step: str = Field(min_length=1, max_length=40)
    system: str = Field(max_length=150_000)
    user: str = Field(min_length=1, max_length=150_000)
    maxTokens: int = Field(ge=1, le=PRESENTATION_MAX_TOKENS)
    json_: bool = Field(default=False, alias="json")
    images: list[PresentationImage] = Field(default_factory=list, max_length=12)
    context: StudentAIContext

    model_config = {"populate_by_name": True}


class PresentationCompleteResponse(BaseModel):
    text: str = ""
    inputTokens: int = 0
    outputTokens: int = 0
    model: str = ""
    truncated: bool = False
    error: str | None = None
    requestId: str | None = None


@router.post("/presentations/complete", response_model=PresentationCompleteResponse)
async def presentations_complete(payload: PresentationCompleteRequest, request: Request) -> PresentationCompleteResponse:
    """Один вызов Sonnet или Luna по просьбе сайта. Ошибки поставщика возвращаются в поле `error` (не 5xx)."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="PRESENTATION_COMPLETE"):
        try:
            result = await presentation_complete(
                payload.model, payload.step, payload.system, payload.user, payload.maxTokens, payload.json_,
                [i.model_dump() for i in payload.images],
            )
        except RelayError as exc:
            logger.warning("Презентации: %s/%s — %s", payload.model, payload.step, exc)
            return PresentationCompleteResponse(error=str(exc))

    return PresentationCompleteResponse(
        text=result.text,
        inputTokens=result.input_tokens,
        outputTokens=result.output_tokens,
        model=result.model,
        truncated=result.truncated,
    )


class PresentationImageRequest(BaseModel):
    prompt: str = Field(min_length=10, max_length=image_relay.MAX_PROMPT_CHARS)
    aspect: Literal["wide", "square", "tall"] = "wide"
    # Какой моделью рисовать (решает сайт: обычным людям — не задаётся, владельцу — выбор); пусто — модель по умолчанию
    model: str | None = Field(default=None, max_length=40)
    context: StudentAIContext


class PresentationImageResponse(BaseModel):
    imageBase64: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    model: str = ""
    costUsd: float = 0.0
    error: str | None = None
    requestId: str | None = None


@router.post("/images/generate", response_model=PresentationImageResponse)
async def images_generate(payload: PresentationImageRequest, request: Request) -> PresentationImageResponse:
    """Одна картинка по описанию сайта (Flux Schnell). Ошибки поставщика и потолки возвращаются в поле `error` (не 5xx)."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="PRESENTATION_IMAGE"):
        try:
            result = await image_relay.generate(payload.context.userId, payload.prompt, payload.aspect, payload.model)
        except image_relay.ImageError as exc:
            logger.warning("Картинки: %s", exc)
            return PresentationImageResponse(error=str(exc))
    return PresentationImageResponse(imageBase64=image_relay.to_b64(result), mime=result.mime, width=result.width, height=result.height, model=result.model, costUsd=result.cost_usd)


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


class DocumentsAskRequest(BaseModel):
    """Батч 13: вопрос по НЕСКОЛЬКИМ документам сразу — тело, не путь, потому
    что список id не ложится в path-параметр."""

    question: str = Field(..., min_length=1)
    documentIds: list[int] = Field(..., min_length=1)
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


@router.post("/documents/ask", response_model=DocumentAskResponse)
async def documents_ask_many(payload: DocumentsAskRequest, request: Request) -> DocumentAskResponse:
    """Вопрос сразу по НЕСКОЛЬКИМ личным документам пользователя (батч 13) —
    один retrieval+генерация по объединённому набору чанков, а не отдельный
    ответ на каждый документ. Чужой/несуществующий id в списке молча
    отбрасывается — не даёт чужих данных и не роняет запрос целиком, если
    остальные id принадлежат пользователю."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="DOCUMENT_QA"):
        result = await ask_user_documents(
            payload.question,
            user_id=payload.context.userId,
            document_ids=payload.documentIds,
            exam_id=payload.context.examId,
        )
    return DocumentAskResponse(
        answer=result.answer,
        verified=result.verified,
        citations=[Citation(**c) for c in result.evidence_references],
        error=result.error,
        requestId=result.request_id,
    )


class DocumentsSearchRequest(BaseModel):
    """Поиск кусков по документам без ответа модели (для презентаций: слайд пишет сайт, строго по этим кускам)."""

    queries: list[str] = Field(..., min_length=1, max_length=6)
    documentIds: list[int] = Field(..., min_length=1, max_length=10)
    topK: int = Field(8, ge=1, le=20)
    context: StudentAIContext


class DocumentFragmentItem(BaseModel):
    documentId: str
    title: str
    pageFrom: int | None = None
    pageTo: int | None = None
    order: int
    text: str
    score: float | None = None


class DocumentsSearchResponse(BaseModel):
    fragments: list[DocumentFragmentItem]
    totalChunks: int = 0
    error: str | None = None
    requestId: str | None = None


@router.post("/documents/search", response_model=DocumentsSearchResponse)
async def documents_search(payload: DocumentsSearchRequest, request: Request) -> DocumentsSearchResponse:
    """Куски личных документов пользователя, ближайшие по смыслу к запросам. Чужой/несуществующий id молча отбрасывается."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="DOCUMENT_QA"):
        result = await search_user_documents(
            payload.queries,
            user_id=payload.context.userId,
            document_ids=payload.documentIds,
            top_k=payload.topK,
        )
    return DocumentsSearchResponse(
        fragments=[
            DocumentFragmentItem(
                documentId=str(f.document_id),
                title=f.title,
                pageFrom=f.page_from,
                pageTo=f.page_to,
                order=f.order,
                text=f.text,
                score=f.score,
            )
            for f in result.fragments
        ],
        totalChunks=result.total_chunks,
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


# --- Настоящий веб-поиск (Tavily) и PubMed (§19 ТЗ + сверх ТЗ, батч 8) ----------


class WebSearchSource(BaseModel):
    title: str
    url: str
    # Что именно сообщает этот источник (описание; ссылка в `answer` стоит сразу под ним).
    finding: str = ""


class WebSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    context: StudentAIContext


class WebSearchResponse(BaseModel):
    query: str
    answer: str
    sources: list[WebSearchSource]
    error: str | None = None
    requestId: str | None = None
    summary: str = ""


def _web_search_response(result: WebSearchResult) -> WebSearchResponse:
    return WebSearchResponse(
        query=result.query,
        answer=result.answer,
        sources=[
            WebSearchSource(title=s.get("title", ""), url=s.get("url", ""), finding=s.get("finding", ""))
            for s in result.sources
        ],
        error=result.error,
        requestId=result.request_id,
        summary=result.summary,
    )


@router.post("/web/search", response_model=WebSearchResponse)
async def web_search_endpoint(payload: WebSearchRequest, request: Request) -> WebSearchResponse:
    """Настоящий поиск по интернету (Yandex Search API, §19 ТЗ) — в отличие от
    /v1/web/research (одна заданная страница), здесь запрос уходит в поисковый API
    и ответ строится по нескольким найденным результатам. Пусто YANDEX_SEARCH_API_KEY
    или YANDEX_FOLDER_ID -> честная ошибка."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="WEB_SEARCH"):
        result = await search_and_answer(payload.query)
    return _web_search_response(result)


class PubMedArticleModel(BaseModel):
    pmid: str
    title: str
    journal: str | None = None
    year: str | None = None
    url: str
    # Тип исследования из данных PubMed и описание результата (на русском).
    design: str | None = None
    finding: str = ""


class PubMedSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    question: str | None = None
    context: StudentAIContext


class PubMedSearchResponse(BaseModel):
    query: str
    answer: str
    articles: list[PubMedArticleModel]
    error: str | None = None
    requestId: str | None = None
    # «PubMed» — напрямую из NCBI; «Europe PMC» — запасной вход к тем же статьям MEDLINE.
    source: str | None = None
    # Английский запрос, по которому искали (если студент писал не по-английски).
    queryEn: str | None = None
    summary: str = ""
    # strong | moderate | limited | conflicting
    evidenceStrength: str | None = None


def _pubmed_response(result: PubMedResult) -> PubMedSearchResponse:
    return PubMedSearchResponse(
        query=result.query,
        answer=result.answer,
        articles=[
            PubMedArticleModel(
                pmid=card["pmid"], title=card["title"], journal=card["journal"], year=card["year"],
                url=card["url"], design=card["design"], finding=card["finding"],
            )
            for card in result.studies
        ]
        or [
            PubMedArticleModel(pmid=a.pmid, title=a.title, journal=a.journal, year=a.year, url=a.url, design=a.design)
            for a in result.articles
        ],
        error=result.error,
        requestId=result.request_id,
        source=result.source,
        queryEn=result.query_en,
        summary=result.summary,
        evidenceStrength=result.evidence_strength,
    )


@router.post("/pubmed/search", response_model=PubMedSearchResponse)
async def pubmed_search_endpoint(payload: PubMedSearchRequest, request: Request) -> PubMedSearchResponse:
    """Поиск по научным статьям PubMed (сверх исходного ТЗ) — NCBI E-utilities,
    заземлённый ответ по реальным абстрактам, не по общим знаниям модели."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="PUBMED_SEARCH"):
        result = await search_pubmed(payload.query, payload.question)
    return _pubmed_response(result)


# --- Статьи PubMed для презентаций: список на выбор и текст выбранных (без модели слайдов) -------
# Бот только добывает: слайды по этому тексту пишет сайт. Полный текст — лишь из открытого подмножества
# PMC (через Europe PMC); закрытое издательствами не берётся.


class PubMedCandidate(BaseModel):
    pmid: str
    title: str
    authors: str = ""
    journal: str | None = None
    year: str | None = None
    design: str = ""
    abstract: str = ""
    # Перевод для показа человеку (пусто, если перевести не получилось — тогда видно оригинал)
    titleRu: str = ""
    abstractRu: str = ""
    doi: str = ""
    url: str = ""
    pmcid: str = ""
    # true — полный текст открыт; false — закрыт; null — узнать не удалось
    openAccess: bool | None = None
    license: str = ""


class PubMedCandidatesRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=10, ge=1, le=10)
    # «Ещё 10»: сколько статей пропустить в общем списке (0, 10, 20)
    offset: int = Field(default=0, ge=0, le=20)
    context: StudentAIContext


class PubMedCandidatesResponse(BaseModel):
    query: str
    queryEn: str | None = None
    source: str | None = None
    articles: list[PubMedCandidate] = []
    error: str | None = None
    requestId: str | None = None


class PubMedFulltextRequest(BaseModel):
    pmids: list[str] = Field(min_length=1, max_length=4)
    context: StudentAIContext


class PubMedSectionModel(BaseModel):
    title: str
    text: str


class PubMedArticleText(BaseModel):
    pmid: str
    title: str = ""
    authors: str = ""
    journal: str | None = None
    year: str | None = None
    doi: str = ""
    url: str = ""
    pmcid: str = ""
    # fulltext | abstract | none
    level: str = "none"
    abstract: str = ""
    sections: list[PubMedSectionModel] = []
    truncated: bool = False
    note: str = ""


class PubMedFulltextResponse(BaseModel):
    articles: list[PubMedArticleText] = []
    error: str | None = None
    requestId: str | None = None


# --- Ссылки из ТЗ презентации (24.10.2026): DOI → PMID и текст страницы или PDF -------------------


class PubMedResolveRequest(BaseModel):
    dois: list[str] = Field(default_factory=list, max_length=12)
    pmcids: list[str] = Field(default_factory=list, max_length=12)
    context: StudentAIContext


class PubMedResolved(BaseModel):
    doi: str = ""
    pmcid: str = ""
    pmid: str | None = None


class PubMedResolveResponse(BaseModel):
    items: list[PubMedResolved] = []
    error: str | None = None
    requestId: str | None = None


_DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s]{1,200}$")


@router.post("/pubmed/resolve", response_model=PubMedResolveResponse)
async def pubmed_resolve_endpoint(payload: PubMedResolveRequest, request: Request) -> PubMedResolveResponse:
    """DOI и номера PMC → PMID для ссылок из ТЗ презентации: поиск по идентификатору статьи (Europe PMC). Нет статьи в PubMed — pmid пуст."""
    rate_limiter.check(payload.context.userId)
    if not payload.dois and not payload.pmcids:
        raise HTTPException(status_code=422, detail="нужен хотя бы один DOI или номер PMC")
    items: list[PubMedResolved] = []
    with request_context(user_id=payload.context.userId, channel="api", workflow="PUBMED_RESOLVE"):
        try:
            for raw in payload.dois:
                doi = raw.strip().lower()
                items.append(PubMedResolved(doi=doi[:200], pmid=await pubmed_sources.resolve_doi(doi) if _DOI_RE.match(doi) else None))
            for raw in payload.pmcids:
                pmcid = raw.strip().upper()
                items.append(PubMedResolved(pmcid=pmcid[:20], pmid=await pubmed_sources.resolve_pmcid(pmcid)))
        except _SourcesUnavailable:
            return PubMedResolveResponse(items=items, error="PubMed сейчас недоступен — попробуйте позже")
    return PubMedResolveResponse(items=items)


class WebPageRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    context: StudentAIContext


class WebPageResponse(BaseModel):
    url: str
    finalUrl: str = ""
    title: str = ""
    text: str = ""
    abstract: str = ""
    kind: str = ""
    truncated: bool = False
    # Содержимое — данные, не инструкции (правило в сайте и промптах).
    authorityLevel: str = "web"
    error: str | None = None
    requestId: str | None = None


@router.post("/web/page", response_model=WebPageResponse)
async def web_page_endpoint(payload: WebPageRequest, request: Request) -> WebPageResponse:
    """Чистый текст страницы или PDF по адресу (без пересказа моделью). SSRF-защита на каждом шаге редиректа; лимиты размера и объёма."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="WEB_PAGE"):
        page = await fetch_page_text(payload.url)
    return WebPageResponse(url=page.url, finalUrl=page.final_url, title=page.title, text=page.text, abstract=page.abstract, kind=page.kind, truncated=page.truncated, error=page.error, requestId=page.request_id)


@router.post("/pubmed/candidates", response_model=PubMedCandidatesResponse)
async def pubmed_candidates_endpoint(payload: PubMedCandidatesRequest, request: Request) -> PubMedCandidatesResponse:
    """До 10 статей по теме: аннотация, авторы, тип исследования и пометка «полный текст открыт» — на выбор человеку."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="PUBMED_CANDIDATES"):
        try:
            english, source, found = await pubmed_sources.candidates(payload.query, payload.limit, payload.offset)
        except _SourcesUnavailable as exc:
            logger.warning("PubMed: список статей не получен (%s / %s)", exc.ncbi_reason, exc.europepmc_reason)
            return PubMedCandidatesResponse(query=payload.query, error="PubMed сейчас недоступен — попробуйте позже")
        # русский перевод названий и аннотаций для показа; сбой перевода не мешает списку
        try:
            ru = await pubmed_ru.translate([{"pmid": c.article.pmid, "title": c.article.title, "abstract": c.article.abstract} for c in found])
        except Exception:
            logger.warning("PubMed: перевод списка не получен", exc_info=True)
            ru = {}
    return PubMedCandidatesResponse(
        query=payload.query,
        queryEn=english if english != payload.query else None,
        source=source,
        articles=[
            PubMedCandidate(
                pmid=c.article.pmid, title=c.article.title, authors=c.authors, journal=c.article.journal,
                year=c.article.year, design=c.article.design, abstract=c.article.abstract,
                titleRu=ru.get(c.article.pmid, ("", ""))[0], abstractRu=ru.get(c.article.pmid, ("", ""))[1], doi=c.doi,
                url=c.article.url, pmcid=c.pmcid, openAccess=c.open_access, license=c.license,
            )
            for c in found
        ],
    )


@router.post("/pubmed/fulltext", response_model=PubMedFulltextResponse)
async def pubmed_fulltext_endpoint(payload: PubMedFulltextRequest, request: Request) -> PubMedFulltextResponse:
    """Текст выбранных статей: полный по разделам, если открыт, иначе аннотация. Модель не вызывается."""
    rate_limiter.check(payload.context.userId)
    with request_context(user_id=payload.context.userId, channel="api", workflow="PUBMED_FULLTEXT"):
        try:
            texts = await pubmed_sources.fulltext(payload.pmids)
        except _SourcesUnavailable as exc:
            logger.warning("PubMed: тексты статей не получены (%s)", exc.ncbi_reason)
            return PubMedFulltextResponse(error="PubMed сейчас недоступен — попробуйте позже")
    return PubMedFulltextResponse(
        articles=[
            PubMedArticleText(
                pmid=t.pmid, title=t.title, authors=t.authors, journal=t.journal, year=t.year, doi=t.doi, url=t.url,
                pmcid=t.pmcid, level=t.level, abstract=t.abstract, truncated=t.truncated, note=t.note,
                sections=[PubMedSectionModel(title=s.title, text=s.text) for s in t.sections],
            )
            for t in texts
        ]
    )


# --- Месячный расход (для лимита на сайте) ---------------------------------------------
class UsageMonthResponse(BaseModel):
    userId: str
    month: str
    spendRub: float


class UsageChannel(BaseModel):
    channel: str
    calls: int
    users: int
    usd: float
    rub: float


class UsageSummaryResponse(BaseModel):
    days: int
    channels: list[UsageChannel]
    telegramUsersThisMonth: int
    telegramUsersAtLimit: int
    freeLimitRub: float
    premiumLimitRub: float


class SourceBook(BaseModel):
    id: int
    title: str
    authors: list[str]
    subject: str
    sourceType: str
    edition: str | None = None
    year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    authorityLevel: str
    rights: str
    status: str
    processingVersion: int
    activeVersion: int
    chunks: int
    tables: int
    figures: int
    needsReview: int
    processedAt: str | None = None


class SourceBooksResponse(BaseModel):
    books: list[SourceBook]


class FigureOut(BaseModel):
    id: int
    bookId: int
    bookTitle: str
    page: int
    pagePrinted: int | None = None
    sectionPath: str | None = None
    kind: str
    number: str | None = None
    caption: str | None = None
    description: str | None = None
    rights: str


class FiguresResponse(BaseModel):
    figures: list[FigureOut]


class FigureDetailResponse(BaseModel):
    figure: FigureOut
    # Временная ссылка на картинку; None — у книги права «только внутри» или картинки нет
    imageUrl: str | None = None


def _figure_out(item) -> FigureOut:
    return FigureOut(
        id=item.id,
        bookId=item.book_id,
        bookTitle=item.book_title,
        page=item.page,
        pagePrinted=item.page_printed,
        sectionPath=item.section_path,
        kind=item.kind,
        number=item.number,
        caption=item.caption,
        description=item.description,
        rights=item.rights,
    )


@router.get("/books", response_model=SourceBooksResponse)
async def source_books(subject: str | None = None) -> SourceBooksResponse:
    """Реестр источников для сайта (MedAP, этап 1): книги предмета со счётчиками активной версии."""
    items = await list_sources(subject)
    return SourceBooksResponse(
        books=[
            SourceBook(
                id=i.id,
                title=i.title,
                authors=i.authors,
                subject=i.subject,
                sourceType=i.source_type,
                edition=i.edition,
                year=i.year,
                publisher=i.publisher,
                isbn=i.isbn,
                authorityLevel=i.authority_level,
                rights=i.rights,
                status=i.status,
                processingVersion=i.processing_version,
                activeVersion=i.active_version,
                chunks=i.chunks,
                tables=i.tables,
                figures=i.figures,
                needsReview=i.needs_review,
                processedAt=i.processed_at,
            )
            for i in items
        ]
    )


@router.get("/figures", response_model=FiguresResponse)
async def figures_search(
    bookId: int | None = None, q: str | None = None, section: str | None = None, limit: int = 20
) -> FiguresResponse:
    """Рисунки учебников активной версии: по книге, разделу и словам подписи/описания."""
    return FiguresResponse(figures=[_figure_out(i) for i in await search_figures(bookId, q, limit, section)])


@router.get("/figures/{figure_id}", response_model=FigureDetailResponse)
async def figure_detail(figure_id: int) -> FigureDetailResponse:
    found = await get_figure_url(figure_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Рисунок не найден")
    item, url = found
    return FigureDetailResponse(figure=_figure_out(item), imageUrl=url)


class VersionOut(BaseModel):
    version: int
    active: bool
    chunks: int
    tables: int
    needsReview: int
    pagesCovered: int
    figures: int


class BookTaskOut(BaseModel):
    id: int
    kind: str
    status: str
    detail: str
    createdAt: str
    finishedAt: str | None = None


class BookDetailResponse(BaseModel):
    versions: list[VersionOut]
    tasks: list[BookTaskOut]


class BookTaskStartResponse(BaseModel):
    taskId: int | None = None
    error: str | None = None


class OutlineEntryOut(BaseModel):
    path: str
    page: int | None = None
    level: int


class BookOutlineResponse(BaseModel):
    entries: list[OutlineEntryOut]


class BookSearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    subject: str | None = None
    bookIds: list[int] | None = Field(default=None, max_length=30)
    limit: int = Field(default=8, ge=1, le=20)


class FragmentOut(BaseModel):
    id: int
    bookId: int | None = None
    title: str
    author: str
    page: int | None = None
    section: str | None = None
    text: str
    score: float | None = None


class BookSearchResponse(BaseModel):
    fragments: list[FragmentOut]


class SwitchVersionRequest(BaseModel):
    version: int = Field(ge=1)
    force: bool = False


class SwitchVersionResponse(BaseModel):
    ok: bool
    version: int | None = None
    chunks: int | None = None
    # Замечание перед переключением (новая версия хуже действующей); с `force` переключение проходит
    error: str | None = None


@router.get("/books/{book_id}", response_model=BookDetailResponse)
async def book_detail(book_id: int) -> BookDetailResponse:
    """Версии разбора книги и последние задачи — для отчёта перед переключением (Studio → Знания)."""
    try:
        reports = await book_admin.version_reports(book_id)
    except book_admin.BookAdminError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    tasks = await book_admin.list_tasks(book_id)
    return BookDetailResponse(
        versions=[
            VersionOut(
                version=r.version, active=r.active, chunks=r.chunks, tables=r.tables,
                needsReview=r.needs_review, pagesCovered=r.pages_covered, figures=r.figures,
            )
            for r in reports
        ],
        tasks=[
            BookTaskOut(
                id=t.id, kind=t.kind, status=t.status, detail=t.detail, createdAt=t.created_at.isoformat(),
                finishedAt=t.finished_at.isoformat() if t.finished_at else None,
            )
            for t in tasks
        ],
    )


async def _start_book_task(book_id: int, kind: str) -> BookTaskStartResponse:
    try:
        return BookTaskStartResponse(taskId=await book_admin.start_task(book_id, kind))
    except book_admin.BookAdminError as exc:
        return BookTaskStartResponse(error=str(exc))


@router.post("/books/{book_id}/reprocess", response_model=BookTaskStartResponse)
async def book_reprocess(book_id: int) -> BookTaskStartResponse:
    """Пересобрать книгу глубоким разбором новой версией рядом со старой; поиск не меняется."""
    return await _start_book_task(book_id, "reprocess")


@router.post("/books/{book_id}/describe-figures", response_model=BookTaskStartResponse)
async def book_describe_figures(book_id: int) -> BookTaskStartResponse:
    """Описать рисунки книги моделью (один раз на картинку, с кэшем и потолком)."""
    return await _start_book_task(book_id, "describe")


@router.post("/books/{book_id}/switch", response_model=SwitchVersionResponse)
async def book_switch(book_id: int, payload: SwitchVersionRequest) -> SwitchVersionResponse:
    """Переключить поиск на версию книги (или откатить на прежнюю)."""
    try:
        result = await book_admin.switch_version(book_id, payload.version, payload.force)
    except (book_admin.BookAdminError, ValueError) as exc:
        return SwitchVersionResponse(ok=False, error=str(exc))
    return SwitchVersionResponse(ok=True, version=result["version"], chunks=result["chunks"])


class TaskModelOut(BaseModel):
    task: str
    provider: str
    # код | окружение | админка
    source: str
    model: str


class TaskModelsResponse(BaseModel):
    tasks: list[TaskModelOut]
    providers: list[str]


class TaskModelSetRequest(BaseModel):
    task: str
    # Пусто — вернуть значение из кода/окружения (снять переопределение)
    provider: str | None = None


@router.get("/task-models", response_model=TaskModelsResponse)
async def task_models() -> TaskModelsResponse:
    """Какая модель стоит за каждой AI-задачей бота (страница Studio «Модели»)."""
    from app.llm.registry import model_registry
    from app.llm.task_map import effective_task_model_map

    registry = model_registry()
    mapping = await effective_task_model_map()
    return TaskModelsResponse(
        tasks=[
            TaskModelOut(task=t.value, provider=p, source=src, model=registry[p].model_id if p in registry else "")
            for t, (p, src) in sorted(mapping.items(), key=lambda kv: kv[0].value)
        ],
        providers=sorted(registry),
    )


@router.post("/task-models", response_model=TaskModelsResponse)
async def task_models_set(payload: TaskModelSetRequest) -> TaskModelsResponse:
    """Сменить модель задачи без выкладки (история сохраняется, возврат — пустой provider)."""
    from app.llm.registry import model_registry
    from app.llm.task_map import Task, clear_override_cache
    from db.crud import apply_model_override

    try:
        task = Task(payload.task)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неизвестная задача")
    async with async_session() as session:
        if payload.provider:
            if payload.provider not in model_registry():
                raise HTTPException(status_code=400, detail="Неизвестный провайдер")
            await apply_model_override(session, task.value, payload.provider, actor="studio")
        else:
            from sqlalchemy import update as sa_update

            from db.models import TaskModelOverride

            await session.execute(
                sa_update(TaskModelOverride).where(TaskModelOverride.task == task.value).values(active=False)
            )
        await session.commit()
    clear_override_cache()
    return await task_models()


@router.get("/usage/month", response_model=UsageMonthResponse)
async def usage_month(userId: str) -> UsageMonthResponse:
    """Сколько AI-расхода (₽) записано на идентификатор учёта за текущий календарный месяц (UTC).

    `userId` — ровно тот идентификатор, с которым сайт вызывает бота: «telegram:<id>» у аккаунта,
    привязанного к Telegram (расход Telegram и сайта тогда общий), иначе id аккаунта сайта.
    Лимит и его размер решает вызывающий: у бота нет данных о подписках сайта."""
    from datetime import datetime, timezone

    async with async_session() as session:
        spend = await month_spend_rub_by_id(session, userId.strip()[:64])
    return UsageMonthResponse(userId=userId.strip()[:64], month=datetime.now(timezone.utc).strftime("%Y-%m"), spendRub=round(spend, 4))


@router.get("/usage/summary", response_model=UsageSummaryResponse)
async def usage_summary_endpoint(days: int = 30) -> UsageSummaryResponse:
    """Расход ИИ по каналам (Telegram, сайт, админка, разбор книг) за `days` суток — для пульта владельца."""
    async with async_session() as session:
        return UsageSummaryResponse(**await usage_summary(session, days))


@router.get("/books/{book_id}/outline", response_model=BookOutlineResponse)
async def book_outline_endpoint(book_id: int) -> BookOutlineResponse:
    """Оглавление книги (разделы по порядку) — для структуры предмета на сайте (MedAP, этап 2)."""
    entries = await book_outline(book_id)
    if entries is None:
        raise HTTPException(status_code=404, detail="книги нет")
    return BookOutlineResponse(entries=[OutlineEntryOut(path=e.path, page=e.page, level=e.level) for e in entries])


@router.post("/books/search", response_model=BookSearchResponse)
async def books_search(payload: BookSearchRequest) -> BookSearchResponse:
    """Фрагменты учебников для агентов сайта (паспорт, извлечение знаний, проверка): настоящие id и страницы,
    чтобы сайт мог проверить цитату дословно. Только активные версии книг; личные документы не ищутся."""
    with request_context(user_id="system", channel="api", workflow="BOOKS_SEARCH"):
        # Берём с запасом: после отбора по книгам должно остаться `limit`
        chunks = await retrieve(payload.query, subject=payload.subject, source_type=SOURCE_TEXTBOOK, top_k=min(60, payload.limit * 3))
    books = set(payload.bookIds) if payload.bookIds else None
    frags = fragments_from_chunks(chunks, books, payload.limit)
    return BookSearchResponse(
        fragments=[
            FragmentOut(id=f.id, bookId=f.book_id, title=f.title, author=f.author, page=f.page, section=f.section, text=f.text, score=f.score)
            for f in frags
        ]
    )
