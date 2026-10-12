"""Сколько ответ занимает по шагам: python -m scripts.latency_report [--days 14] [--channel telegram]

Читает diagnostics из answer_log и печатает для каждого шага медиану и «худшие 5%» (p95), а также
какой реранкер работал (поле reranker у шага retrieval). Снимите отчёт ДО включения onnx и после —
разница видна сразу. Это журнал настоящих запросов, а не тест: числа включают очередь на реранк.
"""

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import AnswerLog
from db.session import async_session


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    statement = select(AnswerLog.diagnostics, AnswerLog.latency_ms, AnswerLog.channel).where(
        AnswerLog.created_at >= since
    )
    if args.channel:
        statement = statement.where(AnswerLog.channel == args.channel)

    async with async_session() as session:
        rows = (await session.execute(statement)).all()

    per_stage: dict[str, list[float]] = defaultdict(list)
    totals: list[float] = []
    rerankers: dict[str, int] = defaultdict(int)

    for diagnostics, latency, _channel in rows:
        try:
            data = json.loads(diagnostics or "{}")
        except ValueError:
            continue
        totals.append(float(latency or 0))
        for stage in data.get("stages", []):
            per_stage[stage.get("name", "?")].append(float(stage.get("durationMs", 0)))
            if stage.get("name") == "retrieval":
                rerankers[str(stage.get("details", {}).get("reranker", "не записан"))] += 1

    if not totals:
        print("За выбранный срок записей нет.")
        return

    print(f"Ответов: {len(totals)} за {args.days} дн.")
    print(f"{'шаг':<22}{'медиана, с':>12}{'p95, с':>10}{'вызовов':>10}")
    for name, values in sorted(per_stage.items(), key=lambda kv: -statistics.median(kv[1])):
        print(f"{name:<22}{statistics.median(values) / 1000:>12.1f}{percentile(values, 0.95) / 1000:>10.1f}{len(values):>10}")
    print(f"{'ВСЕГО на ответ':<22}{statistics.median(totals) / 1000:>12.1f}{percentile(totals, 0.95) / 1000:>10.1f}")
    print("Реранкер в записях: " + ", ".join(f"{k} — {v}" for k, v in rerankers.items()))


if __name__ == "__main__":
    asyncio.run(main())
