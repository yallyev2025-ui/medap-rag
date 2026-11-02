"""Поиск по учебникам для сайта: если по предмету ничего нет (название предмета на сайте и в базе разное), ищем без фильтра предмета.
python -m scripts.test_books_search_fallback (без сети и базы)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.workflows.book_search import search_with_subject_fallback  # noqa: E402

checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"НЕ ПРОШЛО: {label}")


calls: list = []


async def fake_retrieve(query, subject=None, source_type=None, top_k=10):
    calls.append(subject)
    return [] if subject else ["chunk"]


res = asyncio.run(search_with_subject_fallback(fake_retrieve, "что такое воспаление", "Патфиз", "textbook", 12))
check(calls == ["Патфиз", None] and res == ["chunk"], f"сначала по предмету, потом без него: {calls}")
calls.clear()
res = asyncio.run(search_with_subject_fallback(fake_retrieve, "x", None, "textbook", 12))
check(calls == [None], "без предмета — один поиск")
calls.clear()


async def found(query, subject=None, source_type=None, top_k=10):
    calls.append(subject)
    return ["c"]


asyncio.run(search_with_subject_fallback(found, "x", "Патфиз", "textbook", 12))
check(calls == ["Патфиз"], "нашлось по предмету — второй поиск не нужен")
print(f"OK: {checks} проверок")
