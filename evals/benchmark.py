"""Benchmark провайдеров (§60, §62 ТЗ, этап 4B): один и тот же eval-датасет
прогоняется на каждом провайдере с ключом, отчёт сравнивает качество, цену и
задержку по задачам генерации.

Провайдер меняется только внутри прогона (`force_providers` — ContextVar), и
только для задач генерации: верификатор, роутер интента и переписывание запроса
остаются на production-маппинге, чтобы сравнивалась именно генерация на одном
и том же retrieval. Production-маппинг прогон не меняет — смена делается
отдельно на странице Models (с откатом).

Не покрыто: задачи оценки ответов студента (RECALL_EVALUATE, ORAL_EVALUATE и
т.д.) — для них нужен отдельный датасет «ответ студента → ожидаемые ошибки»,
которого пока нет.

Осмысленные цифры — только на загруженных настоящих учебниках: на тестовых
файлах и десятке вопросов разница между провайдерами — шум.

Запуск: python -m evals.benchmark [--out evals/reports/benchmark.json]
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.llm.registry import model_registry
from app.llm.task_map import Task, force_providers
from evals.gates import check_gates
from evals.run import run_eval

GENERATION_TASKS = (Task.GROUNDED_QA, Task.EXPLAIN, Task.CLASS_QUICK, Task.TEST_SOLVE_TEXT)


def _provider_summary(provider: str, model: str, report: dict[str, Any]) -> dict[str, Any]:
    summary = report["summary"]
    generation = summary.get("generation") or {}
    total = generation.get("total") or 0
    return {
        "provider": provider,
        "model": model,
        "cases": summary.get("cases", 0),
        "passRate": (generation.get("passed", 0) / total) if total else None,
        "passed": generation.get("passed", 0),
        "total": total,
        "hallucinations": generation.get("hallucinations", 0),
        "verifiedRate": generation.get("verifiedRate"),
        "costTotalRub": summary.get("cost", {}).get("totalRub", 0.0),
        "costAvgRub": summary.get("cost", {}).get("avgRub", 0.0),
        "latencyMsAvg": summary.get("latencyMsAvg", 0),
        "byCategory": summary.get("byCategory", {}),
        "gates": [g.to_dict() for g in check_gates(report)],
    }


async def run_benchmark(
    dataset_path: str = "eval/dataset.jsonl",
    tasks: tuple[Task, ...] = GENERATION_TASKS,
    include_db_cases: bool = True,
) -> dict[str, Any]:
    registry = model_registry()
    providers = [key for key, prof in registry.items() if prof.enabled]

    results = []
    for provider in providers:
        with force_providers({task: provider for task in tasks}):
            report = await run_eval(dataset_path, include_db_cases=include_db_cases)
        results.append(_provider_summary(provider, registry[provider].model_id, report))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "tasks": [t.value for t in tasks],
        "providers": results,
        # Ключ не задан — провайдер в сравнении не участвует (честно, без выдуманных цифр).
        "skippedProviders": [key for key, prof in registry.items() if not prof.enabled],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark провайдеров MedAP на eval-датасете")
    parser.add_argument("--dataset", default="eval/dataset.jsonl")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    report = await run_benchmark(args.dataset)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
