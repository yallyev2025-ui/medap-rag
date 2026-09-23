"""Прогон eval-набора: retrieval и генерация измеряются РАЗДЕЛЬНО (§39 ТЗ).

Плохой retrieval и плохую генерацию нельзя смешивать в одну метрику: если
фрагмент не нашёлся, претензия к поиску, а не к модели. Поэтому отчёт содержит
два независимых блока плюс фактическую стоимость и задержку по каждому случаю.

Запуск (нужны DATABASE_URL с загруженными источниками и ключ провайдера):

    python -m evals.run                      # retrieval + генерация
    python -m evals.run --retrieval-only     # без вызовов LLM и без затрат
    python -m evals.run --out evals/reports/baseline.json

Формат строки `eval/dataset.jsonl` (JSON на строку):

    {"question": "...",
     "category": "grounded_qa",        # категория из §38/§61 ТЗ
     "answerable": true|false,          # есть ли подтверждение в материалах
     "source_type": "учебник",         # необязательно
     "subject": "pathphys",            # необязательно: ожидаемый предмет
     "expect_source": "Новицкий",      # необязательно: подстрока названия/автора
     "expect_page": 314,                # необязательно: страница-ориентир
     "expect_keywords": ["некроз"],    # для answerable=true
     "note": "..."}

Отчёт сохраняется в JSON: он и есть baseline, относительно которого измеряются
следующие этапы. Без файла отчёта фраза «стало лучше» — мнение, а не факт.
"""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.observability.context import request_context
from app.workflows.ask import ask
from config import settings
from constants import SOURCE_TEXTBOOK
from db.models import AIUsageEvent
from db.session import async_session
from rag.generator import NO_CONTEXT_ANSWER, relevant_chunks
from rag.retriever import ChunkResult, retrieve


def load_dataset(path: str) -> list[dict]:
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


# --- Retrieval (§39) ---------------------------------------------------------


def _matches_expected(chunk: ChunkResult, expected: str) -> bool:
    needle = expected.lower()
    return needle in chunk.title.lower() or needle in chunk.author.lower()


def retrieval_metrics(item: dict, chunks: list[ChunkResult]) -> dict[str, Any]:
    """Recall@K, ранг первого попадания, корректность предмета и страницы."""
    relevant = relevant_chunks(chunks)
    expected = item.get("expect_source")

    rank: int | None = None
    if expected:
        for position, chunk in enumerate(chunks, start=1):
            if _matches_expected(chunk, expected):
                rank = position
                break

    expected_page = item.get("expect_page")
    page_hit = None
    if expected_page is not None:
        page_hit = any(
            c.page_from is not None
            and c.page_from <= expected_page <= (c.page_to or c.page_from)
            and (not expected or _matches_expected(c, expected))
            for c in chunks
        )

    subject_hit = None
    if item.get("subject"):
        subject_hit = any(c.subject == item["subject"] for c in relevant)

    return {
        "candidates": len(chunks),
        "relevant": len(relevant),
        # Для answerable=false «успех» — это как раз отсутствие релевантного:
        # поиск не должен подсовывать материал, которого нет.
        "found_expected": None if not expected else rank is not None,
        "rank": rank,
        # Reciprocal rank: основа MRR по всему набору.
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
        "page_recovered": page_hit,
        "subject_correct": subject_hit,
        "has_relevant": bool(relevant),
    }


# --- Генерация ---------------------------------------------------------------


def generation_metrics(item: dict, answer: str | None) -> dict[str, Any]:
    """Отказ вместо выдумки, наличие ключевых фактов и ссылки на источник."""
    abstained = answer is None or answer.strip() == NO_CONTEXT_ANSWER.strip()
    answerable = item.get("answerable", True)

    if not answerable:
        return {
            "passed": abstained,
            "abstained": abstained,
            # Ответ на вопрос без подтверждения — самый тяжёлый провал (§15).
            "hallucination": not abstained,
            "reason": "корректно отказался" if abstained else "ВЫДУМКА: ответил без подтверждения",
        }

    if abstained:
        return {
            "passed": False,
            "abstained": True,
            "hallucination": False,
            "reason": "пропуск: отказался, хотя материал есть",
        }

    missing = [kw for kw in item.get("expect_keywords", []) if kw.lower() not in answer.lower()]
    has_citation = "[" in answer and "]" in answer
    passed = not missing and has_citation
    if missing:
        reason = f"нет ключевых фактов: {', '.join(missing)}"
    elif not has_citation:
        reason = "нет ссылки на источник"
    else:
        reason = "ответ корректен и со ссылкой"

    return {
        "passed": passed,
        "abstained": False,
        "hallucination": False,
        "missing_keywords": missing,
        "has_citation": has_citation,
        "reason": reason,
    }


# --- Стоимость ---------------------------------------------------------------


async def usage_for_request(request_id: str) -> dict[str, Any]:
    """Фактические токены и стоимость одного случая — из записей о расходе (§59)."""
    async with async_session() as session:
        row = (
            await session.execute(
                select(
                    func.count(AIUsageEvent.id),
                    func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(AIUsageEvent.provider_cost_rub), 0.0),
                ).where(AIUsageEvent.request_id == request_id)
            )
        ).one()
    return {
        "calls": int(row[0]),
        "inputTokens": int(row[1]),
        "outputTokens": int(row[2]),
        "costRub": float(row[3]),
    }


# --- Прогон ------------------------------------------------------------------


async def run_case(item: dict, retrieval_only: bool) -> dict[str, Any]:
    question = item["question"]
    source_type = item.get("source_type", SOURCE_TEXTBOOK)
    subject = item.get("subject")
    started = time.monotonic()

    with request_context(user_id="eval", channel="admin", workflow="EVAL") as ctx:
        if retrieval_only:
            chunks = await retrieve(question, source_type=source_type, subject=subject)
            answer = None
            workflow = "RETRIEVAL_ONLY"
            verified = None
        else:
            result = await ask(question, source_type=source_type, subject=subject)
            chunks = result.chunks
            answer = result.answer
            workflow = result.workflow
            verified = result.verified
        request_id = ctx.request_id

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = {"calls": 0, "inputTokens": 0, "outputTokens": 0, "costRub": 0.0}
    if not retrieval_only:
        usage = await usage_for_request(request_id)

    return {
        "question": question,
        "category": item.get("category", "uncategorized"),
        "answerable": item.get("answerable", True),
        "workflow": workflow,
        # Verification Layer (§12, §36 ТЗ): True/False/None — см. app/verification/verify.py.
        "verified": verified,
        "retrieval": retrieval_metrics(item, chunks),
        "generation": None if retrieval_only else generation_metrics(item, answer),
        "latencyMs": latency_ms,
        "usage": usage,
        "requestId": request_id,
    }


def summarize(cases: list[dict[str, Any]], retrieval_only: bool) -> dict[str, Any]:
    with_expected = [c for c in cases if c["retrieval"]["found_expected"] is not None]
    recall = (
        sum(1 for c in with_expected if c["retrieval"]["found_expected"]) / len(with_expected)
        if with_expected
        else None
    )
    mrr = (
        sum(c["retrieval"]["reciprocal_rank"] for c in with_expected) / len(with_expected)
        if with_expected
        else None
    )
    page_cases = [c for c in cases if c["retrieval"]["page_recovered"] is not None]
    subject_cases = [c for c in cases if c["retrieval"]["subject_correct"] is not None]

    summary: dict[str, Any] = {
        "cases": len(cases),
        "retrieval": {
            "recallAtK": recall,
            "mrr": mrr,
            "pageRecovered": (
                sum(1 for c in page_cases if c["retrieval"]["page_recovered"]) / len(page_cases)
                if page_cases
                else None
            ),
            "subjectCorrect": (
                sum(1 for c in subject_cases if c["retrieval"]["subject_correct"]) / len(subject_cases)
                if subject_cases
                else None
            ),
            "topK": settings.RERANK_TOP_K,
            "candidates": settings.RETRIEVAL_CANDIDATES,
        },
        "latencyMsAvg": round(sum(c["latencyMs"] for c in cases) / len(cases)) if cases else 0,
    }

    if not retrieval_only:
        generation = [c["generation"] for c in cases]
        checked = [c["verified"] for c in cases if c["verified"] is not None]
        summary["generation"] = {
            "passed": sum(1 for g in generation if g["passed"]),
            "total": len(generation),
            # Главная метрика безопасности: ответ там, где подтверждения нет.
            "hallucinations": sum(1 for g in generation if g["hallucination"]),
            # Доля прошедших Verification Layer среди случаев, где она вообще
            # выполнялась (исключая verified=None — сбой верификатора, §36).
            "verifiedRate": (sum(checked) / len(checked)) if checked else None,
            "verifiedChecked": len(checked),
        }
        summary["cost"] = {
            "totalRub": round(sum(c["usage"]["costRub"] for c in cases), 4),
            "avgRub": round(sum(c["usage"]["costRub"] for c in cases) / len(cases), 4) if cases else 0,
        }

    by_category: dict[str, dict[str, Any]] = defaultdict(lambda: {"cases": 0, "passed": 0})
    for case in cases:
        bucket = by_category[case["category"]]
        bucket["cases"] += 1
        if case["generation"] and case["generation"]["passed"]:
            bucket["passed"] += 1
    summary["byCategory"] = dict(by_category)
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон eval-набора MedAP")
    parser.add_argument("--dataset", default="eval/dataset.jsonl")
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="только поиск, без вызовов LLM (бесплатно и быстро)",
    )
    parser.add_argument("--out", default=None, help="куда сохранить JSON-отчёт")
    args = parser.parse_args()

    items = load_dataset(args.dataset)
    cases = []
    for index, item in enumerate(items, start=1):
        case = await run_case(item, args.retrieval_only)
        cases.append(case)
        status = "—" if case["generation"] is None else ("OK  " if case["generation"]["passed"] else "FAIL")
        print(f"[{status}] {index}. {case['question']}")
        print(f"       поиск: релевантных {case['retrieval']['relevant']}/{case['retrieval']['candidates']}"
              f", ранг ожидаемого источника: {case['retrieval']['rank'] or '—'}")
        if case["generation"]:
            print(f"       ответ: {case['generation']['reason']}"
                  f" | {case['usage']['costRub']:.3f} ₽ | {case['latencyMs']} мс")

    summary = summarize(cases, args.retrieval_only)
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "versions": {
            "prompt": settings.PROMPT_VERSION,
            "retrieval": settings.RETRIEVAL_VERSION,
            "pricing": settings.PRICING_VERSION,
        },
        "summary": summary,
        "cases": cases,
    }

    print("\n" + "=" * 60)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nОтчёт сохранён: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
