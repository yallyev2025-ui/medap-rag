"""Поиск кусков по документам студента без ответа модели (для презентаций): изоляция, порядок, пределы.

Запуск: python scripts/test_documents_search.py
Базы нет: поиск и проверка владельца подменены, проверяется логика функции и контракт адреса.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

from unittest.mock import MagicMock  # noqa: E402

# тяжёлых библиотек моделей в окружении проверки нет, а поиск в этом тесте подменён: заглушки позволяют импортировать модуль как есть
for _name in ("torch", "sentence_transformers", "transformers", "FlagEmbedding", "pdfplumber", "docx", "fitz", "pypdf", "pgvector", "pgvector.sqlalchemy", "bs4", "trafilatura", "lxml", "readability"):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = MagicMock()

from app.workflows import user_documents as ud  # noqa: E402
from rag.retriever import ChunkResult  # noqa: E402

checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


def chunk(cid: int, book: int, index: int, text: str, page: int | None = None) -> ChunkResult:
    return ChunkResult(id=cid, content=text, subject="s", author="a", title=f"Док {book}", page_from=page, page_to=page, book_id=book, chunk_index=index, rerank_score=0.9 - index / 100)


async def main() -> None:
    calls: dict = {}

    async def owned(user_id, ids):
        calls["owned"] = (user_id, list(ids))
        return [i for i in ids if i in (1, 2)]  # чужие id молча выпадают

    async def count(ids, user_id):
        return 120

    async def union(queries, rerank_with, candidates, top_k, source_type=None, subject=None, book_id=None, user_id=None):
        calls["union"] = dict(queries=queries, rerank_with=rerank_with, top_k=top_k, source_type=source_type, book_id=book_id, user_id=user_id)
        # выдача по рангу, не по порядку документа
        return [chunk(3, 2, 7, "Лечение. Второй документ.", 12), chunk(1, 1, 30, "Механизм. Первый документ, конец.", 40), chunk(2, 1, 4, "Определение. Первый документ, начало.", 3)]

    ud._owned_document_ids = owned
    ud.count_document_chunks = count
    ud.retrieve_union = union

    r = await ud.search_user_documents(["лечение гипертонии", "  ", "гипертония: лечение"], "u-1", [1, 2, 99], top_k=50)
    ok(calls["union"]["user_id"] == "u-1" and calls["union"]["book_id"] == [1, 2], f"поиск идёт только по документам владельца (чужой 99 отброшен): {calls['union']}")
    ok(calls["union"]["source_type"] == ud.SOURCE_USER_DOCUMENT, "ищем только среди личных документов, не среди учебников")
    ok(calls["union"]["queries"] == ["лечение гипертонии", "гипертония: лечение"] and calls["union"]["rerank_with"] == "лечение гипертонии", "пустые запросы отброшены, реранк по первому")
    ok(calls["union"]["top_k"] == 20, "потолок выдачи 20, сколько ни просили")
    ok([f.order for f in r.fragments] == [4, 30, 7] and [f.document_id for f in r.fragments] == [1, 1, 2], f"куски идут в порядке документа, а не по рангу: {[(f.document_id, f.order) for f in r.fragments]}")
    ok(r.fragments[0].page_from == 3 and r.fragments[0].text.startswith("Определение") and r.total_chunks == 120 and r.error is None, "страницы, текст и объём документов переданы")

    r = await ud.search_user_documents(["что угодно"], "u-2", [99, 100])
    ok(r.fragments == [] and r.error == ud._NOT_FOUND_MESSAGE, "чужие документы: честное «не найдено», без поиска")
    r = await ud.search_user_documents(["", "   "], "u-1", [1])
    ok(r.fragments == [] and r.error is None, "только пустые запросы — пустая выдача без ошибки")

    # контракт адреса: запрос ограничен по длине списков, ответ несёт те же поля, что ждёт сайт
    from app.api import v1  # noqa: E402

    req = v1.DocumentsSearchRequest(queries=["а"], documentIds=[1], context={"userId": "u-1"})
    ok(req.topK == 8, "по умолчанию 8 кусков")
    for bad in (dict(queries=[], documentIds=[1]), dict(queries=["а"] * 7, documentIds=[1]), dict(queries=["а"], documentIds=list(range(11))), dict(queries=["а"], documentIds=[1], topK=21)):
        try:
            v1.DocumentsSearchRequest(**bad, context={"userId": "u-1"})
        except Exception:
            continue
        raise AssertionError(f"запрос без ограничений принят: {bad}")
    resp = v1.DocumentsSearchResponse(fragments=[v1.DocumentFragmentItem(documentId="1", title="Док", pageFrom=3, pageTo=3, order=4, text="т", score=0.9)], totalChunks=120)
    ok(resp.model_dump()["fragments"][0]["documentId"] == "1" and "totalChunks" in resp.model_dump(), "ответ: documentId строкой, totalChunks")
    print(f"OK: {checks} проверок")


asyncio.run(main())
