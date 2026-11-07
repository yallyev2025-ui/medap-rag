"""Бюджет процессора: реранкеру весь процессор (владелец 06.11.2026 отменил «оставлять 10%»). Запуск: python scripts/test_cpu_budget.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

from config import cpu_plan, ocr_workers_for  # noqa: E402

fails = 0


def check(cond: bool, msg: str) -> None:
    global fails
    print(("OK   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


for cores in (4, 8, 11, 16, 32):
    budget, rerank, embed = cpu_plan(cores)
    check(rerank == budget == cores and 1 <= embed <= 4, f"{cores} ядер: реранкеру весь бюджет ({rerank}), эмбеддингам {embed}")
b, r, e = cpu_plan(11)
check((b, r, e) == (11, 11, 3), f"11 ядер → реранкер 11 потоков, эмбеддинг 3 (ничего не оставляем впустую)")
check(cpu_plan(1)[0] == 1 and cpu_plan(2)[0] == 2, "на 1–2 ядрах реранкеру всё")
check(cpu_plan(11, 100, limit_override=5)[:2] == (5, 5), "ручной CPU_THREAD_LIMIT главнее")
check(cpu_plan(11, 200)[0] <= 11 and cpu_plan(11, 0)[0] >= 1, "процент вне 10–100 приводится к допустимому")
# Распознавание сканов: больше ядер (владелец 08.11.2026), но три потока всегда остаются ответам и реранкеру
check(ocr_workers_for(cpu_plan(11, 90)[0]) == 6, "11 ядер, бюджет 90% → 6 потоков распознавания (было 2)")
check(ocr_workers_for(cpu_plan(11, 100)[0]) == 8, "11 ядер, бюджет 100% → 8 потоков распознавания")
check(ocr_workers_for(1) == 2 and ocr_workers_for(4) == 2, "на малых тарифах не меньше двух потоков")
check(ocr_workers_for(64) == 8, "потолок 8 потоков: память под страницы сканов")
print("OK" if fails == 0 else f"ПРОВАЛ: {fails}")
sys.exit(1 if fails else 0)
