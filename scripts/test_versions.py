"""Версии обработки книги не меняют действующий поиск: python -m scripts.test_versions"""

import sys
import types

try:
    import torch  # noqa: F401
except ImportError:
    sys.modules.setdefault("rag.embedder", types.SimpleNamespace(embed_query=None))

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.workflows.answer_cache import overlap
from db.models import BookChunk
from rag.retriever import _base_filters


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_search_filters_by_active_version() -> None:
    sql = _sql(_base_filters(select(BookChunk.id), None, "pathphys", None))
    assert "book_chunks.processing_version = books.active_version" in sql
    assert "books.status IN" in sql  # прежний фильтр статуса на месте


def test_new_version_chunks_do_not_hit_old_cached_answers() -> None:
    # Кеш ответов отдаёт ответ только при общих фрагментах. После переключения версии книги поиск
    # возвращает новые id, пересечения со старым кешем нет — ответ пишется заново, ничего чистить не надо.
    old_ids = [101, 102, 103]
    new_ids = [9001, 9002, 9003]
    assert overlap(old_ids, new_ids) == 0.0
    assert overlap(old_ids, [101, 102, 9001]) > 0.5  # до переключения кеш работает как раньше


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
