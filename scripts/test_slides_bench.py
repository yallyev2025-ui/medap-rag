"""Проверка разбора результатов стенда презентаций: без сети, БД и ключей.

Запуск: DATABASE_URL=postgresql+asyncpg://u:p@localhost/db python scripts/test_slides_bench.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")

from app.admin.slides_bench import drafts_from_results  # noqa: E402


def main() -> None:
    results = {
        "runs": [
            {"model": "gpt-6-luna", "topic": "med", "draft": {"slides": [{"blockType": "title", "title": "А"}]}},
            {"model": "claude-sonnet-5-5", "topic": "law", "draft": {"slides": [{"blockType": "title", "title": "Б"}]}},
            {"model": "claude-sonnet-5-5", "topic": "biz", "draft": None, "error": "ответ не разобрался как JSON"},
            {"model": "gpt-6-luna", "topic": "biz", "skipped": "потолок расхода"},
        ]
    }
    items, skipped = drafts_from_results(results)
    assert [i["name"] for i in items] == ["med-luna", "law-sonnet"], items
    assert len(skipped) == 2 and "biz-sonnet" in skipped[0] and "biz-luna" in skipped[1], skipped
    print("ok: черновики разбираются, пропуски названы")


if __name__ == "__main__":
    main()
