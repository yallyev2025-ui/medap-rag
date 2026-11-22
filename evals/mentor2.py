"""Прогон приёмки Mentor 2.0: полнота по рубрике, ход исследования, время и стоимость.

Отличие от `evals/run.py`: там измеряется «нашёлся ли ожидаемый кусок и есть ли ключевые
факты», а здесь — **полнота ответа**: у каждого случая есть список обязательных сторон
(«механизмы», «стадии», «неотложная помощь»), и видно, какие из них в ответе раскрыты.
Это единственный способ доказать, что после research-контура студент не пишет «а где
лечение»: сравнение идёт с сохранённым отчётом «до», а не с ощущением.

Запуск (нужны DATABASE_URL с загруженными источниками и ключ провайдера):

    python -m evals.mentor2                       # прогон по 24 случаям
    python -m evals.mentor2 --out evals/reports/mentor2-after.json
    python -m evals.mentor2 --compare evals/reports/mentor2-before.json

Что пишется по каждому случаю: раскрытые и пропущенные стороны, число кругов поиска,
сколько пунктов плана покрыто материалом, время, токены, стоимость в рублях, отказ.
Стоимость берётся из `ai_usage_events` по `request_id` — не оценка.

Порог «сторона раскрыта» — по скелетам слов (app/research/text.py): если в ответе есть
слова этой темы, сторона считается раскрытой. Это грубая метрика, поэтому в отчёт
включён сам текст ответа: спорные случаи владелец смотрит глазами.
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.llm.usage import usage_for_request
from app.observability.context import request_context
from app.research.text import has_partial_match, stems
from app.workflows.ask import ask
from config import settings
from constants import SOURCE_TEXTBOOK

DATASET = "eval/mentor2/cases.jsonl"


def load_cases(path: str) -> list[dict]:
    items: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def aspect_report(aspects: list[str], answer: str) -> dict:
    """Какие обязательные стороны видны в готовом ответе."""
    found = stems(answer or "")
    covered: list[str] = []
    missing: list[str] = []
    for aspect in aspects:
        need = stems(aspect)
        if not need:
            continue
        hit = sum(1 for stem in need if has_partial_match(stem, found))
        # Сторона считается раскрытой, если нашлось хотя бы половина её слов.
        if hit / len(need) >= 0.5:
            covered.append(aspect)
        else:
            missing.append(aspect)
    total = len(covered) + len(missing)
    return {
        "covered": covered,
        "missing": missing,
        "completeness": round(len(covered) / total, 2) if total else None,
    }


async def run_case(case: dict) -> dict:
    question = case["question"]
    source_type = case.get("source_type", SOURCE_TEXTBOOK)
    subject = case.get("subject")

    with request_context(user_id="eval-mentor2", channel="admin", workflow="EVAL") as ctx:
        result = await ask(question, source_type=source_type, subject=subject)
        request_id = ctx.request_id

    research = (result.diagnostics or {}).get("research") or {}
    answer = result.answer or ""
    return {
        "id": case.get("id"),
        "question": question,
        "type": case.get("type"),
        "answerable": case.get("answerable", True),
        "workflow": result.workflow,
        "verified": result.verified,
        "refused": result.answer is None,
        "aspects": aspect_report(case.get("aspects") or [], answer),
        "research": {
            "kind": research.get("kind"),
            "origin": research.get("origin"),
            "rounds": research.get("rounds"),
            "aspects": len(research.get("aspects") or []),
            "covered": len(research.get("covered") or []),
            "gaps": [g.get("title") for g in research.get("gaps") or []],
            "steps": research.get("steps") or [],
        },
        "citations": len(result.citations or []),
        "answer": answer,
        "requestId": request_id,
    }


def summarize(cases: list[dict], usages: dict[str, dict]) -> dict:
    graded = [c for c in cases if c["aspects"]["completeness"] is not None]
    completeness = [c["aspects"]["completeness"] for c in graded]
    cost = sum(float(u.get("costRub") or 0) for u in usages.values())
    tokens_in = sum(int(u.get("inputTokens") or 0) for u in usages.values())
    tokens_out = sum(int(u.get("outputTokens") or 0) for u in usages.values())
    calls = sum(int(u.get("calls") or 0) for u in usages.values())
    return {
        "cases": len(cases),
        "completenessAvg": round(sum(completeness) / len(completeness), 3) if completeness else None,
        "completenessFull": sum(1 for c in graded if c["aspects"]["missing"] == []),
        "graded": len(graded),
        "refusals": sum(1 for c in cases if c["refused"]),
        "unexpectedRefusals": sum(1 for c in cases if c["refused"] and c["answerable"]),
        "verifiedFailed": sum(1 for c in cases if c["verified"] is False),
        "roundsAvg": round(sum(int(c["research"].get("rounds") or 0) for c in cases) / len(cases), 2) if cases else 0,
        "llmCalls": calls,
        "inputTokens": tokens_in,
        "outputTokens": tokens_out,
        "costRub": round(cost, 3),
        "costRubAvg": round(cost / len(cases), 4) if cases else 0,
    }


def compare(before: dict, after: dict) -> dict:
    """Разница «после − до» по главным числам. Положительная разница полноты — хорошо,
    положительная разница стоимости — повод объяснить, за что заплатили."""
    keys = ("completenessAvg", "completenessFull", "refusals", "unexpectedRefusals", "costRubAvg", "llmCalls")
    return {key: {
        "before": (before.get("summary") or {}).get(key),
        "after": (after.get("summary") or {}).get(key),
        "delta": _delta((before.get("summary") or {}).get(key), (after.get("summary") or {}).get(key)),
    } for key in keys}


def _delta(before, after):
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return round(after - before, 4)
    return None


async def main() -> None:
    parser = argparse.ArgumentParser(description="Приёмка Mentor 2.0")
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--out", default=None, help="куда сохранить JSON-отчёт")
    parser.add_argument("--compare", default=None, help="отчёт «до» для сравнения")
    args = parser.parse_args()

    cases: list[dict] = []
    usages: dict[str, dict] = {}
    for index, case in enumerate(load_cases(args.dataset), start=1):
        result = await run_case(case)
        cases.append(result)
        if result["requestId"]:
            usages[result["requestId"]] = await usage_for_request(result["requestId"])
        aspects = result["aspects"]
        mark = "OK  " if not aspects["missing"] else "PART"
        if result["refused"]:
            mark = "REF "
        print(f"[{mark}] {index}. {result['question'][:70]}")
        print(
            f"       полнота: {aspects['completeness'] if aspects['completeness'] is not None else '—'}"
            f" | круги: {result['research'].get('rounds')} | план: {result['research'].get('origin')}"
            f" | источников: {result['citations']} | {usages.get(result['requestId'], {}).get('costRub', 0)} ₽"
        )
        if aspects["missing"]:
            print(f"       не раскрыто: {', '.join(aspects['missing'])}")

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "versions": {
            "prompt": settings.PROMPT_VERSION,
            "retrieval": settings.RETRIEVAL_VERSION,
            "research": settings.RESEARCH_ENABLED,
            "researchExtraQueries": settings.RESEARCH_EXTRA_QUERIES,
        },
        "summary": summarize(cases, usages),
        "cases": cases,
    }

    print("\n" + "=" * 60)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nОтчёт сохранён: {out_path}")

    if args.compare:
        before = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print("\nСравнение с отчётом «до»:")
        print(json.dumps(compare(before, report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
