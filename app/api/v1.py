"""MedAP Student AI API v1 — стабильный контракт для образовательного сайта (§27 ТЗ).

Сайт MedAP и Telegram-бот вызывают один и тот же workflow; отдельного
пользовательского сайта Student AI не существует (см. дополнение к ТЗ, раздел 1).

Аутентификация — service-to-service: `Authorization: Bearer <SERVICE_TOKEN>`.
`userId` в теле запроса не является доказательством личности (§28), он лишь
связывает запрос с пользователем вызывающего сервиса — для лимитов, телеметрии и
диагностики.
"""

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.observability.context import request_context
from app.security.auth import rate_limiter, require_service_token
from app.workflows.ask import ask_grounded
from constants import SOURCE_TEXTBOOK

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
    sourceTitle: str
    author: str
    subject: str
    page: int | None = None
    pageTo: int | None = None
    exactSupportingText: str
    relevance: float | None = None


class ChatResponse(BaseModel):
    answer: str
    workflow: str
    # False — в материалах MedAP подтверждения не нашлось; ответ честно говорит об
    # этом, а не выдумывает медицинский текст (§15).
    grounded: bool
    citations: list[Citation]
    requestId: str | None
    versions: dict[str, str]
    diagnostics: dict


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
        citations=[Citation(**c) for c in result.citations],
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
