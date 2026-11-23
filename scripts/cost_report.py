"""Отчёт по стоимости вызовов модели (Mentor 2.0, этап 5).

python -m scripts.cost_report [дней]
"""

import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import AIUsageEvent
from db.session import async_session


async def main(days: int) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as session:
        rows = (await session.execute(select(AIUsageEvent).where(AIUsageEvent.created_at >= since))).scalars().all()

    if not rows:
        print(f"За {days} дн. вызовов модели нет")
        return

    by_task: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "input": 0, "cached": 0, "output": 0, "usd": 0.0, "ms": 0}
    )
    for row in rows:
        item = by_task[(row.workflow, row.task, row.model)]
        item["calls"] += 1
        item["input"] += row.input_tokens
        item["cached"] += row.cached_input_tokens
        item["output"] += row.output_tokens
        item["usd"] += row.provider_cost_usd
        item["ms"] += row.latency_ms

    total_usd = sum(item["usd"] for item in by_task.values())
    print(f"За {days} дн.: вызовов {len(rows)}, стоимость ${total_usd:.4f}")
    print(f"{'workflow':<18} {'task':<22} {'model':<24} {'вызовов':>8} {'вход':>10} {'кеш':>10} {'выход':>9} {'$':>9} {'сек':>7}")
    for (workflow, task, model), item in sorted(by_task.items(), key=lambda pair: pair[1]["usd"], reverse=True):
        print(
            f"{workflow[:18]:<18} {task[:22]:<22} {model[:24]:<24} {int(item['calls']):>8} "
            f"{int(item['input']):>10} {int(item['cached']):>10} {int(item['output']):>9} "
            f"{item['usd']:>9.4f} {item['ms'] / 1000:>7.1f}"
        )


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
