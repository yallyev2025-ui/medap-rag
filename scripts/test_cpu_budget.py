"""Бюджет процессора: расчёты бота вместе не берут больше 90% ядер (владелец 05.11.2026). Запуск: python scripts/test_cpu_budget.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

from config import cpu_plan  # noqa: E402

fails = 0


def check(cond: bool, msg: str) -> None:
    global fails
    print(("OK   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


for cores in (4, 8, 11, 16, 32):
    budget, rerank, embed = cpu_plan(cores, 90)
    check(rerank + embed <= max(budget, 2) and budget <= cores * 0.9 + 1e-9, f"{cores} ядер: бюджет {budget}, реранкер {rerank} + эмбеддинги {embed} ≤ 90%")
b, r, e = cpu_plan(11, 90)
check((b, r, e) == (9, 6, 3), f"11 ядер → 9 потоков всего: 6 реранкер + 3 эмбеддинг (занято {b / 11:.0%}, свободно {1 - b / 11:.0%})")
check(cpu_plan(1)[0] == 1 and cpu_plan(2)[0] == 1, "на 1–2 ядрах бюджет минимум 1 поток")
check(cpu_plan(11, 90, limit_override=5) == (5, 4, 1), "ручной CPU_THREAD_LIMIT главнее и тоже делится")
check(cpu_plan(11, 200)[0] <= 11 and cpu_plan(11, 0)[0] >= 1, "процент вне 10–100 приводится к допустимому")
print("OK" if fails == 0 else f"ПРОВАЛ: {fails}")
sys.exit(1 if fails else 0)
