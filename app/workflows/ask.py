"""Workflow ASK/EXPLAIN — единственная реализация «вопрос → ответ по материалам».

Этим кодом пользуются оба клиента: образовательный сайт MedAP через `/v1/chat` и
Telegram-бот. По §30 ТЗ Telegram — клиент AI-сервиса, а не владелец AI-логики, так
что конвейер (переписывание запроса, роутинг интента, поиск, генерация) существует
в одном экземпляре, а у клиентов остаётся только их UI.

На этапе 1 цитаты собираются из того, что уже есть в retrieval: источник, автор,
название, страницы. Полноценный Evidence Pack с `exactSupportingText` до
предложения и проверяемым `citationId` приходит на этапе 3.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.evidence.citations import extract_cited_chunks
from app.evidence.pack import build_citations
from app.observability.context import current, current_request_id, set_workflow
from app.observability.stages import StageLog
from app.orchestration.router import RoutingDecision, Workflow, route
from app.verification.conflicts import detect_conflicts
from config import settings
from constants import SOURCE_CLINREK, SOURCE_TEXTBOOK
from db.models import AnswerLog
from db.session import async_session
from rag.generator import (
    NO_CONTEXT_ANSWER,
    build_history_messages,
    detect_intent,
    detect_subject,
    generate_answer,
    generate_differential,
    generate_fallback,
    generate_multi,
    relevant_chunks,
    rewrite_query,
)
from rag.retriever import ChunkResult, retrieve

logger = logging.getLogger(__name__)


@dataclass
class AskResult:
    """Результат workflow.

    `answer is None` означает, что в материалах нет ничего релевантного и решение
    за клиентом: Telegram спрашивает согласие на ответ из общих знаний, а API по
    §15 отдаёт честный отказ, не выдумывая медицинский ответ.

    `verified` (Verification Layer, §12/§36): True — прошёл проверку; False —
    не прошёл даже после перегенерации, `answer` уже заменён на честный отказ;
    None — верификация не проводилась или сам верификатор был недоступен.
    """

    answer: str | None
    workflow: str
    intent: str
    has_relevant: bool
    citations: list[dict[str, Any]]
    diagnostics: dict[str, Any]
    request_id: str | None
    subject_used: str | None = None
    versions: dict[str, str] = field(default_factory=dict)
    chunks: list[ChunkResult] = field(default_factory=list)
    verified: bool | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)


async def _build_evidence(
    question: str, answer: str, relevant: list[ChunkResult]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Citations реально процитированных в ответе источников (§13 ТЗ) + при ≥2
    разных источниках среди них — проверка на содержательное расхождение (§10)."""
    cited_chunks = extract_cited_chunks(answer, relevant)
    citation_objs = build_citations(cited_chunks)
    citations = [c.to_dict() for c in citation_objs]

    conflicts: list[dict[str, Any]] = []
    if len({c.source_title for c in citation_objs}) >= 2:
        conflict_objs = await detect_conflicts(question, answer, citation_objs)
        conflicts = [c.to_dict() for c in conflict_objs]

    return citations, conflicts


def _versions() -> dict[str, str]:
    return {
        "prompt": settings.PROMPT_VERSION,
        "retrieval": settings.RETRIEVAL_VERSION,
        "pricing": settings.PRICING_VERSION,
    }


async def _log_and_return(result: AskResult, question: str) -> AskResult:
    """Персистит ответ для Answer Inspector (раздел 8 дополнения к ТЗ) и
    возвращает результат как есть. Пишем ВСЕ исходы, включая small talk и
    отсутствие доказательств — это тоже реальные ответы пользователю, а не
    только «успешные» генерации. Сбой записи не должен стоить пользователю
    ответа (тот же принцип, что record_usage для AIUsageEvent)."""
    ctx = current()
    try:
        async with async_session() as session:
            session.add(
                AnswerLog(
                    request_id=result.request_id or "no-request-context",
                    channel=ctx.channel if ctx else "api",
                    user_id=ctx.user_id if ctx else None,
                    question=question,
                    answer=result.answer or "",
                    subject=result.subject_used,
                    workflow=result.workflow,
                    intent=result.intent,
                    verified=result.verified,
                    citations=json.dumps(result.citations, ensure_ascii=False),
                    conflicts=json.dumps(result.conflicts, ensure_ascii=False),
                    diagnostics=json.dumps(result.diagnostics, ensure_ascii=False),
                    latency_ms=result.diagnostics.get("totalMs", 0),
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Не удалось сохранить AnswerLog")
    return result


async def ask(
    question: str,
    *,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
    turns: list[tuple[str, str]] | None = None,
    symptom_mode: bool = False,
    requested_workflow: str | None = None,
    top_k: int | None = None,
) -> AskResult:
    """Отвечает на вопрос по материалам MedAP и возвращает ответ с диагностикой.

    `turns` — последние обмены текущего чата (лёгкая память диалога): по ним
    уточняющий вопрос вроде «а какие дозы?» превращается в самостоятельный
    поисковый запрос. Всю историю в промпт не отправляем (§26).
    """
    stages = StageLog()
    turns = turns or []

    with stages.measure("routing") as details:
        decision: RoutingDecision = route(question, requested_workflow)
        details["workflow"] = decision.workflow.value
        details["reason"] = decision.reason
    set_workflow(decision.workflow.value)

    # Уточняющий вопрос («а какие дозы?») превращаем в самостоятельный поисковый
    # запрос по истории диалога — и тип запроса определяем уже по нему.
    with stages.measure("query_rewrite") as details:
        search_query = await rewrite_query(question, turns) if turns else question
        details["rewritten"] = search_query != question

    with stages.measure("intent") as details:
        # Явное бытовое сообщение распознаётся без обращения к модели (§5).
        if decision.workflow is Workflow.SMALL_TALK:
            intent = "CHITCHAT"
        else:
            # Иначе нужен тип клинического запроса: одна тема, дифдиагноз или
            # сочетание состояний — от него зависит стратегия поиска.
            intent = await detect_intent(search_query)
        details["intent"] = intent

    if intent == "CHITCHAT":
        with stages.measure("small_talk"):
            answer = await generate_fallback(question)
        return await _log_and_return(
            AskResult(
                answer=answer,
                workflow=Workflow.SMALL_TALK.value,
                intent=intent,
                has_relevant=False,
                citations=[],
                diagnostics=stages.as_dict(),
                request_id=current_request_id(),
                versions=_versions(),
            ),
            question,
        )

    # Кнопочный режим «Разбор по симптомам» всегда означает дифдиагноз.
    if source_type == SOURCE_CLINREK and symptom_mode:
        intent = "DIFFERENTIAL"
    reasoning = source_type == SOURCE_CLINREK and intent in ("DIFFERENTIAL", "MULTI")

    with stages.measure("retrieval") as details:
        chunks = await retrieve(
            search_query,
            source_type=source_type,
            subject=subject,
            # Обычный клинический вопрос отвечается строго из одной рекомендации,
            # разбор симптомов — наоборот, по многим.
            focus_document=(source_type == SOURCE_CLINREK and not reasoning),
            top_k=top_k or (settings.DIFFERENTIAL_TOP_K if reasoning else settings.RERANK_TOP_K),
        )
        relevant = relevant_chunks(chunks)
        details["candidates"] = len(chunks)
        details["relevant"] = len(relevant)
        # Реранкер мог быть недоступен — это важно видеть в диагностике (§36).
        details["reranked"] = any(c.rerank_score is not None for c in chunks)

    if not relevant:
        stages.note("no_evidence", policy="решение о фолбэке принимает клиент")
        return await _log_and_return(
            AskResult(
                answer=None,
                workflow=decision.workflow.value,
                intent=intent,
                has_relevant=False,
                citations=[],
                diagnostics=stages.as_dict(),
                request_id=current_request_id(),
                versions=_versions(),
                chunks=chunks,
            ),
            question,
        )

    history = build_history_messages(turns)
    with stages.measure("generation") as details:
        if intent == "DIFFERENTIAL":
            generated = await generate_differential(question, chunks, source_type, history)
        elif intent == "MULTI":
            generated = await generate_multi(question, chunks, source_type, history)
        else:
            generated = await generate_answer(question, chunks, source_type, history)
        details["chars"] = len(generated.text if generated else "")
        details["verified"] = generated.verified if generated else None

    # generate_differential/generate_multi могут вернуть None, если внутри
    # обнаружили отсутствие материала (защитная проверка — на практике сюда не
    # попадаем, т.к. relevant уже непустой). Ведём себя как «нет доказательств».
    if generated is None:
        stages.note("no_evidence", policy="решение о фолбэке принимает клиент")
        return await _log_and_return(
            AskResult(
                answer=None,
                workflow=decision.workflow.value,
                intent=intent,
                has_relevant=False,
                citations=[],
                diagnostics=stages.as_dict(),
                request_id=current_request_id(),
                versions=_versions(),
                chunks=chunks,
            ),
            question,
        )

    # Честный отказ после неудачной верификации (§15) — цитировать нечего, ответ
    # уже заменён на NO_CONTEXT_ANSWER внутри generate_answer/_reasoning_answer.
    if generated.verified is False:
        citations: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
    else:
        with stages.measure("evidence") as details:
            citations, conflicts = await _build_evidence(question, generated.text, relevant)
            details["cited"] = len(citations)
            details["conflicts"] = len(conflicts)

    return await _log_and_return(
        AskResult(
            answer=generated.text,
            workflow=decision.workflow.value,
            intent=intent,
            has_relevant=True,
            citations=citations,
            diagnostics=stages.as_dict(),
            request_id=current_request_id(),
            subject_used=detect_subject(chunks),
            versions=_versions(),
            chunks=chunks,
            verified=generated.verified,
            conflicts=conflicts,
        ),
        question,
    )


async def ask_grounded(
    question: str,
    *,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
    turns: list[tuple[str, str]] | None = None,
    requested_workflow: str | None = None,
) -> AskResult:
    """Вариант для API: при отсутствии подтверждения — честный отказ (§15).

    Общие знания модели не считаются достаточным доказательством медицинского
    утверждения, поэтому для образовательного сайта фолбэк на них не включается:
    лучше неполный подтверждённый ответ, чем полный выдуманный.
    """
    result = await ask(
        question,
        source_type=source_type,
        subject=subject,
        turns=turns,
        requested_workflow=requested_workflow,
    )
    if result.answer is None:
        result.answer = NO_CONTEXT_ANSWER
    return result
