"""Приоритеты реранкера: фоновая работа (конспекты, проверки) занимает не больше одного места из трёх, срочные запросы идут мимо неё.

Запуск: python scripts/test_rerank_priority.py
Проверяется без модели и сети: подменённый `rerank_scores` считает, сколько вызовов идёт одновременно.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

import importlib  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

# Тяжёлые библиотеки моделей нужны только настоящему реранкеру; тесту хватает заглушек, если их нет на машине
for _name in ("torch", "sentence_transformers", "onnxruntime", "transformers", "optimum", "pgvector", "pgvector.sqlalchemy", "asyncpg"):
    try:
        importlib.import_module(_name)
    except Exception:  # noqa: BLE001
        sys.modules[_name] = MagicMock()

from rag import priority, retriever  # noqa: E402
from rag.retriever import ChunkResult  # noqa: E402

fails = 0


def check(cond: bool, msg: str) -> None:
    global fails
    print(("OK   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


def chunk() -> ChunkResult:
    return ChunkResult(**{**{f: None for f in ChunkResult.__dataclass_fields__}, "content": "т"}) if hasattr(ChunkResult, "__dataclass_fields__") else ChunkResult.model_construct(content="т")


running = {"bg": 0, "all": 0}
peak = {"bg": 0, "all": 0}


def fake_rerank(question: str, passages: list[str]) -> list[float]:
    running["all"] += 1
    peak["all"] = max(peak["all"], running["all"])
    if question.startswith("bg"):
        running["bg"] += 1
        peak["bg"] = max(peak["bg"], running["bg"])
    time.sleep(0.15)
    running["all"] -= 1
    if question.startswith("bg"):
        running["bg"] -= 1
    return [1.0 for _ in passages]


async def run(question: str, background: bool) -> float:
    priority._priority.set(priority.BACKGROUND if background else priority.INTERACTIVE)
    t0 = time.perf_counter()
    await retriever._rerank(question, [chunk()])
    return time.perf_counter() - t0


async def main() -> None:
    retriever.rerank_scores = fake_rerank  # type: ignore[assignment]
    retriever.settings.RERANK_CONCURRENCY = 3
    retriever.settings.BACKGROUND_RERANK_SLOTS = 1
    # шесть фоновых запросов подряд и один срочный в разгар работы
    bg = [asyncio.create_task(run(f"bg{i}", True)) for i in range(6)]
    await asyncio.sleep(0.05)
    urgent = await run("urgent", False)
    await asyncio.gather(*bg)
    check(peak["bg"] == 1, f"фоновая работа одновременно занимает не больше 1 места (пик {peak['bg']})")
    check(urgent < 0.3, f"срочный запрос не ждёт очереди фоновых ({urgent:.2f} с вместо ~1 с при общей очереди)")
    check(peak["all"] <= 3, f"всего одновременно не больше {3} (пик {peak['all']})")
    check(priority.current_priority() in (priority.BACKGROUND, priority.INTERACTIVE), "метка приоритета читается")


asyncio.run(main())
print("OK" if fails == 0 else f"ПРОВАЛ: {fails}")
sys.exit(1 if fails else 0)
