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
import re
from dataclasses import dataclass, field
from typing import Any

from app.evidence.citations import extract_cited_chunks
from app.evidence.pack import build_citations
from app.observability.context import current, current_request_id, set_workflow
from app.llm.task_map import Task, tier_providers
from app.observability.stages import StageLog
from app.orchestration.router import RoutingDecision, Workflow, route
from app.verification.conflicts import detect_conflicts
from app.workflows import answer_cache
from app.workflows.structure import is_wall, preserved
from app.workflows.dialog import (
    AFFIRM,
    ASK_TOPIC_TEXT,
    DIALOG_CHANNELS,
    continuation_from_turns,
    finalize_dialog_answer,
    needs_rewrite,
    strip_source_brackets,
)
from app.workflows.multi import answer_questions, split_questions
from app.workflows.question_kind import is_open_exam_list
from app.workflows.plain_text import to_plain_text
from app.workflows.scope import Scope, retrieve_documents
from config import settings
from constants import (
    ANSWER_TIERS,
    SCOPE_BOTH,
    SCOPE_DOCUMENTS,
    SOURCE_CLINREK,
    SOURCE_TEXTBOOK,
    SOURCE_USER_DOCUMENT,
    TIER_DEEP,
    TIER_FAST,
)
from db.models import AnswerLog
from db.session import async_session
from rag.generator import (
    CLINREK_PARTIAL_EVIDENCE_MARKER,
    NO_CONTEXT_ANSWER,
    OUTPUT_FORMATS,
    OUTPUT_PLAIN,
    PARTIAL_EVIDENCE_MARKER,
    build_history_messages,
    detect_intent,
    detect_mode,
    detect_subject,
    document_threshold,
    generate_answer,
    generate_combined,
    generate_differential,
    generate_fallback,
    generate_multi,
    relevant_chunks,
    restructure_answer,
    rewrite_query,
)
from rag.retriever import ChunkResult, retrieve

logger = logging.getLogger(__name__)

# Батч 10 — контракт ответа (§36): производные поля из уже посчитанных данных
# (verified/conflicts/текст ответа), НИ ОДНОГО нового вызова LLM.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_CLINICAL_WARNING_KEYWORDS = ("🚩", "противопоказан", "осторожно", "требует исключения", "красные флаги")


def _derive_source_mode(source_type: str) -> str:
    if source_type == SOURCE_USER_DOCUMENT:
        return "USER_DOCUMENT"
    return "CLINICAL_RECOMMENDATION" if source_type == SOURCE_CLINREK else "MEDAP"


def _lines_containing(text: str, needle_check) -> list[str]:
    return [line.strip() for line in _SENTENCE_SPLIT.split(text) if line.strip() and needle_check(line)]


def _derive_contract_fields(
    *,
    answer: str | None,
    source_type: str,
    has_relevant: bool,
    verified: bool | None,
    conflicts: list[dict[str, Any]],
) -> tuple[str, str, list[str], list[str]]:
    """evidence_status, verification_status, unsupported_areas, clinical_warnings.

    evidenceStatus: SUFFICIENT/PARTIAL/INSUFFICIENT/CONFLICTING.
    verificationStatus: VERIFIED/PARTIALLY_VERIFIED/UNVERIFIED/ABSTAINED.
    """
    is_clinrek = source_type == SOURCE_CLINREK
    marker = CLINREK_PARTIAL_EVIDENCE_MARKER if is_clinrek else PARTIAL_EVIDENCE_MARKER

    if not has_relevant or answer is None or answer == NO_CONTEXT_ANSWER:
        verification_status = "ABSTAINED" if verified is False else "UNVERIFIED"
        return "INSUFFICIENT", verification_status, [], []

    if conflicts:
        evidence_status = "CONFLICTING"
    elif marker in answer:
        evidence_status = "PARTIAL"
    else:
        evidence_status = "SUFFICIENT"

    if verified is True:
        verification_status = "PARTIALLY_VERIFIED" if evidence_status == "PARTIAL" else "VERIFIED"
    elif verified is False:
        verification_status = "ABSTAINED"
    else:
        verification_status = "UNVERIFIED"

    unsupported_areas = _lines_containing(answer, lambda line: marker in line) if evidence_status == "PARTIAL" else []
    clinical_warnings = (
        _lines_containing(answer, lambda line: any(k in line.lower() for k in _CLINICAL_WARNING_KEYWORDS))
        if is_clinrek
        else []
    )
    return evidence_status, verification_status, unsupported_areas, clinical_warnings


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
    # Контракт ответа (§36, батч 10) — производные поля, без нового вызова LLM.
    source_mode: str = "MEDAP"
    evidence_status: str = "INSUFFICIENT"
    verification_status: str = "UNVERIFIED"
    unsupported_areas: list[str] = field(default_factory=list)
    clinical_warnings: list[str] = field(default_factory=list)


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


def tier_top_k(tier: str | None) -> int:
    """Сколько фрагментов брать в контекст: быстрый — 12–15, глубокий — 30, без режима — как раньше."""
    if tier == TIER_FAST:
        return settings.TIER_FAST_TOP_K
    if tier == TIER_DEEP:
        return settings.TIER_DEEP_TOP_K
    return settings.RERANK_TOP_K


async def ask(question: str, *, tier: str | None = None, **kwargs) -> AskResult:
    """Отвечает на вопрос по материалам MedAP (параметры — см. `_ask`).

    `tier` (батч 31) — режим ответа: 'fast' | 'deep' | None. Он задаёт провайдера текстовых
    задач на время запроса (`tier_providers`) и число фрагментов (`tier_top_k`). Без режима
    (скрипты, прежние клиенты) всё работает, как раньше."""
    tier = tier if tier in ANSWER_TIERS else None
    with tier_providers(tier):
        return await _ask(question, tier=tier, **kwargs)


async def _ask(
    question: str,
    *,
    tier: str | None = None,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
    turns: list[tuple[str, str]] | None = None,
    symptom_mode: bool = False,
    requested_workflow: str | None = None,
    top_k: int | None = None,
    scope: Scope | None = None,
    budget_ok=None,
    output_format: str | None = None,
) -> AskResult:
    """Отвечает на вопрос по материалам MedAP и возвращает ответ с диагностикой.

    `output_format` (батч 29): 'plain' | 'markdown' — формат вывода для сайта. Не задан в канале
    api ⇒ 'plain' (сайт рисует ответ как обычный текст); в Telegram/админке не используется.

    `scope` (батч 27) — откуда отвечать: учебники / документы студента / оба. Без него —
    как раньше (учебники или клинреки по source_type/subject). Несколько вопросов в одном
    сообщении отвечаются порциями (app/workflows/multi.py), `budget_ok` — проверка
    месячного лимита между порциями.

    `turns` — последние обмены текущего чата (лёгкая память диалога): по ним
    уточняющий вопрос вроде «а какие дозы?» превращается в самостоятельный
    поисковый запрос. Всю историю в промпт не отправляем (§26).
    """
    stages = StageLog()
    turns = turns or []

    # Диалог (Telegram/админка): «да» продолжает прошлый ответ, в конце ответа — вопрос-
    # приглашение. Сайт (/v1, channel api) получает один максимально полный ответ.
    ctx = current()
    dialog = bool(ctx and ctx.channel in DIALOG_CHANNELS)
    fmt = output_format if output_format in OUTPUT_FORMATS else None
    if fmt is None and not dialog and ctx is not None and ctx.channel == "api":
        fmt = OUTPUT_PLAIN
    gen_question = question
    continuation_search: str | None = None
    if dialog and AFFIRM.match(question):
        if turns:
            gen_question, continuation_search = continuation_from_turns(turns)
        elif route(question, requested_workflow).workflow is not Workflow.SMALL_TALK:
            return await _log_and_return(
                AskResult(
                    answer=ASK_TOPIC_TEXT,
                    workflow=Workflow.SMALL_TALK.value,
                    intent="CHITCHAT",
                    has_relevant=False,
                    citations=[],
                    diagnostics=stages.as_dict(),
                    request_id=current_request_id(),
                    versions=_versions(),
                    source_mode=_derive_source_mode(source_type),
                    evidence_status="SUFFICIENT",
                    verification_status="UNVERIFIED",
                ),
                question,
            )

    with stages.measure("routing") as details:
        decision: RoutingDecision = route(question, requested_workflow)
        details["workflow"] = decision.workflow.value
        details["reason"] = decision.reason
    set_workflow(decision.workflow.value)

    # Уточняющий вопрос («а какие дозы?») превращаем в самостоятельный поисковый
    # запрос по истории диалога — и тип запроса определяем уже по нему.
    with stages.measure("query_rewrite") as details:
        if continuation_search is not None:
            search_query = continuation_search
            details["continuation"] = True
        elif turns and needs_rewrite(question):
            search_query = await rewrite_query(question, turns)
        else:
            search_query = question
        details["rewritten"] = search_query != question

    with stages.measure("intent") as details:
        # Явное бытовое сообщение распознаётся без обращения к модели (§5).
        if decision.workflow is Workflow.SMALL_TALK:
            intent = "CHITCHAT"
        elif source_type == SOURCE_CLINREK:
            # Тип клинического запроса (одна тема / дифдиагноз / сочетание) влияет
            # на стратегию поиска и промпт ТОЛЬКО у клинреков.
            intent = await detect_intent(search_query)
        else:
            # У учебников intent ни на что не влияет (DIFFERENTIAL/MULTI там
            # игнорируются, бытовое уже поймал route()) — платный вызов модели зря.
            intent = "SINGLE"
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
                # Small talk не оценивается по Evidence Pack — этот вопрос вне контракта.
                source_mode=_derive_source_mode(source_type),
                evidence_status="SUFFICIENT",
                verification_status="UNVERIFIED",
            ),
            question,
        )

    # Кнопочный режим «Разбор по симптомам» всегда означает дифдиагноз.
    if source_type == SOURCE_CLINREK and symptom_mode:
        intent = "DIFFERENTIAL"
    reasoning = source_type == SOURCE_CLINREK and intent in ("DIFFERENTIAL", "MULTI")

    # Откуда отвечать (батч 27). Без scope — прежнее поведение. Режим документов/смешанный
    # без включённых документов безопасно сводится к учебникам (клиент обязан сам
    # попросить включить документ раньше, см. bot/handlers/query.py).
    doc_scope = scope is not None and scope.uses_documents
    both_scope = doc_scope and scope.mode == SCOPE_BOTH
    docs_only = doc_scope and scope.mode == SCOPE_DOCUMENTS
    effective_scope = scope if scope is not None else Scope(source_type=source_type, subject=subject)
    gen_task = decision.task

    # Несколько вопросов в одном сообщении: порциями, одна генерация на порцию.
    # Только в диалоговых каналах (Telegram/админка): сайт (/v1) по-прежнему получает один полный ответ.
    if dialog and source_type != SOURCE_CLINREK and continuation_search is None and not reasoning:
        items = split_questions(question)
        # Список экзаменационных вопросов (без вариантов ответа) — не тест: идёт обычным путём,
        # одним поиском и одним полным структурированным ответом на все вопросы сразу. Компактный
        # режим «строка на вопрос» остаётся для тестов с вариантами
        if len(items) >= 2 and not is_open_exam_list(items):
            with stages.measure("multi_questions") as details:
                multi = await answer_questions(
                    items,
                    effective_scope,
                    task=Task.DOCUMENT_QA if docs_only else Task.GROUNDED_QA,
                    budget_ok=budget_ok,
                )
                details["questions"] = len(items)
                details["batches"] = multi.batches
            return await _log_and_return(
                AskResult(
                    answer=multi.text,
                    workflow=decision.workflow.value,
                    intent="MULTI_QUESTION",
                    has_relevant=True,
                    citations=multi.citations,
                    diagnostics=stages.as_dict(),
                    request_id=current_request_id(),
                    versions=_versions(),
                    source_mode=_derive_source_mode(SOURCE_USER_DOCUMENT if docs_only else source_type),
                    evidence_status="SUFFICIENT",
                    verification_status="UNVERIFIED",
                ),
                question,
            )

    book_chunks: list[ChunkResult] = []
    doc_chunks: list[ChunkResult] = []
    with stages.measure("retrieval") as details:
        if doc_scope:
            chunks, doc_details = await retrieve_documents(
                search_query, scope.document_ids, scope.owner_id, turns
            )
            doc_chunks = chunks
            details.update({f"doc_{k}": v for k, v in doc_details.items()})
            if both_scope:
                book_chunks = await retrieve(
                    search_query,
                    source_type=source_type,
                    subject=subject,
                    top_k=top_k or tier_top_k(tier) // 2,
                    spread=tier is not None,
                )
            relevant = relevant_chunks(chunks, document_threshold()) + relevant_chunks(book_chunks)
            all_chunks = chunks + book_chunks
        else:
            chunks = await retrieve(
                search_query,
                source_type=source_type,
                subject=subject,
                # Обычный клинический вопрос отвечается строго из одной рекомендации,
                # разбор симптомов — наоборот, по многим.
                focus_document=(source_type == SOURCE_CLINREK and not reasoning),
                top_k=top_k or (settings.DIFFERENTIAL_TOP_K if reasoning else tier_top_k(tier)),
                spread=tier is not None and source_type != SOURCE_CLINREK,
            )
            relevant = relevant_chunks(chunks)
            all_chunks = chunks
        details["candidates"] = len(all_chunks)
        details["relevant"] = len(relevant)
        # Реранкер мог быть недоступен — это важно видеть в диагностике (§36).
        details["reranked"] = any(c.rerank_score is not None for c in all_chunks)
    chunks = all_chunks

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
                source_mode=_derive_source_mode(source_type),
                evidence_status="INSUFFICIENT",
                verification_status="UNVERIFIED",
            ),
            question,
        )

    # Кеш по смыслу (батч 31): тот же вопрос, спрошенный другими словами, и тот же найденный
    # материал — готовый проверенный ответ без вызова модели. Только учебники и обычная подача.
    cache_key = None
    if (
        settings.CACHE_ENABLED
        and source_type == SOURCE_TEXTBOOK
        and not doc_scope
        and not turns
        and continuation_search is None
        and not reasoning
        and intent == "SINGLE"
        and answer_cache.cacheable_question(question)
    ):
        cache_key = dict(
            question=question.strip(),
            tier=tier or "default",
            source_type=source_type,
            subject=subject,
            mode=detect_mode(question),
            kind="dialog" if dialog else "api",
            fmt=fmt,
            chunk_ids=[c.id for c in relevant],
        )
        with stages.measure("answer_cache") as details:
            hit = await answer_cache.lookup(**cache_key)
            details["hit"] = hit is not None
        if hit is not None:
            return await _log_and_return(
                AskResult(
                    answer=hit.answer,
                    workflow=decision.workflow.value,
                    intent=intent,
                    has_relevant=True,
                    citations=hit.citations,
                    diagnostics=stages.as_dict(),
                    request_id=current_request_id(),
                    subject_used=detect_subject(chunks),
                    versions=_versions(),
                    chunks=chunks,
                    verified=hit.verified,
                    source_mode=_derive_source_mode(source_type),
                    evidence_status="SUFFICIENT",
                    verification_status="VERIFIED" if hit.verified else "UNVERIFIED",
                ),
                question,
            )

    history = build_history_messages(turns)
    with stages.measure("generation") as details:
        # DIFFERENTIAL/MULTI — клинический разбор по симптомам/сочетанию состояний,
        # промпты рассчитаны на врача и клинреки (rag/generator.py:DIFFERENTIAL_
        # SYSTEM_PROMPT/MULTI_SYSTEM_PROMPT). Гейт по source_type — иначе учебный
        # вопрос студента, который detect_intent() ошибочно принял за разбор
        # симптомов, ушёл бы во врачебный тон на чанках из учебника.
        if both_scope:
            generated = await generate_combined(
                gen_question,
                doc_chunks,
                book_chunks,
                history,
                dialog=dialog,
            )
        elif docs_only:
            generated = await generate_answer(
                gen_question, chunks, SOURCE_USER_DOCUMENT, history, task=Task.DOCUMENT_QA, dialog=dialog,
                output_format=fmt,
            )
        elif intent == "DIFFERENTIAL" and source_type == SOURCE_CLINREK:
            generated = await generate_differential(gen_question, chunks, source_type, history)
        elif intent == "MULTI" and source_type == SOURCE_CLINREK:
            generated = await generate_multi(gen_question, chunks, source_type, history)
        else:
            generated = await generate_answer(
                gen_question, chunks, source_type, history, task=decision.task, dialog=dialog,
                output_format=fmt,
            )
        details["chars"] = len(generated.text if generated else "")
        details["verified"] = generated.verified if generated else None

    # «Быстрый» режим: если дешёвая модель выдала ответ полотном — один проход «перестрой форму».
    # Принимается только результат с теми же числами и ссылками; иначе остаётся исходный ответ.
    if (
        tier == TIER_FAST
        and generated is not None
        and source_type != SOURCE_CLINREK
        and generated.verified is not False
        and generated.text != NO_CONTEXT_ANSWER
        and is_wall(generated.text)
    ):
        with stages.measure("restructure") as details:
            rewritten = await restructure_answer(generated.text)
            details["applied"] = bool(rewritten) and preserved(generated.text, rewritten)
            if details["applied"]:
                generated.text = rewritten

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
                source_mode=_derive_source_mode(source_type),
                evidence_status="INSUFFICIENT",
                verification_status="UNVERIFIED",
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
            citations, conflicts = await _build_evidence(gen_question, generated.text, relevant)
            details["cited"] = len(citations)
            details["conflicts"] = len(conflicts)

    evidence_status, verification_status, unsupported_areas, clinical_warnings = _derive_contract_fields(
        answer=generated.text,
        source_type=source_type,
        has_relevant=True,
        verified=generated.verified,
        conflicts=conflicts,
    )
    final_text = generated.text
    if dialog and generated.verified is not False and final_text != NO_CONTEXT_ANSWER:
        final_text = finalize_dialog_answer(final_text, citations, generated.truncated)
    elif generated.verified is not False and final_text != NO_CONTEXT_ANSWER:
        # Сайт (api): источники отдаются структурно в citations — скобки из текста убираем;
        # в режиме plain дополнительно вычищаем разметку (страховка поверх инструкции модели).
        final_text = strip_source_brackets(final_text)
        if fmt == OUTPUT_PLAIN:
            final_text = to_plain_text(final_text)

    # Запоминаем только проверенный, не оборванный ответ с реальными цитатами.
    if (
        cache_key is not None
        and generated.verified is not False
        and not generated.truncated
        and final_text != NO_CONTEXT_ANSWER
        and citations
    ):
        await answer_cache.store(**cache_key, answer=final_text, citations=citations, verified=generated.verified)
    return await _log_and_return(
        AskResult(
            answer=final_text,
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
            source_mode=_derive_source_mode(SOURCE_USER_DOCUMENT if docs_only else source_type),
            evidence_status=evidence_status,
            verification_status=verification_status,
            unsupported_areas=unsupported_areas,
            clinical_warnings=clinical_warnings,
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
    output_format: str | None = None,
    tier: str | None = None,
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
        output_format=output_format,
        tier=tier,
    )
    if result.answer is None:
        result.answer = NO_CONTEXT_ANSWER
    return result
