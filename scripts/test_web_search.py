"""Поиск в интернете: достоверные источники и разбор SearXNG (ТЗ 20.11.2026).

Проверяем без сети и без тяжёлых зависимостей: отбор адресов и разбор ответа.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.workflows.web_trust import parse_searxng_results, rank_results, trust_score


def test_junk_sites_dropped_and_trusted_kept():
    results = [
        {"url": "https://clinic-stomatologiya-msk.ru/anaphylaxis", "title": "Лечение в клинике"},
        {"url": "https://krasota-i-zdorovie.ru/pohudet", "title": "Красота и здоровье"},
        {"url": "https://www.who.int/news-room/fact-sheets/detail/anaphylaxis", "title": "Anaphylaxis — WHO"},
        {"url": "https://pubmed.ncbi.nlm.nih.gov/12345678/", "title": "Anaphylaxis mechanisms"},
        {"url": "https://www.harvard.edu/research/anaphylaxis", "title": "Harvard research"},
        {"url": "https://random-blog-notes.example/post/1", "title": "Как я лечил аллергию"},
    ]
    ranked = rank_results(results, limit=5)
    urls = [r["url"] for r in ranked]

    assert any("who.int" in u for u in urls), "ВОЗ остаётся"
    assert any("pubmed.ncbi.nlm.nih.gov" in u for u in urls), "PubMed остаётся"
    assert any("harvard.edu" in u for u in urls), "иностранный университет разрешён"
    assert not any("clinic" in u for u in urls), "клиника отсекается"
    assert not any("krasota" in u for u in urls), "«здоровье и красота» отсекается"


def test_trust_score_does_not_punish_foreign_sources():
    assert trust_score("https://www.ox.ac.uk/research") >= 2
    assert trust_score("https://www.minzdrav.gov.ru/docs") >= 5
    assert trust_score("https://zub-i-ulybka-spb.ru") < 1


def test_searxng_json_becomes_results():
    payload = {
        "results": [
            {"url": "https://www.who.int/a", "title": "WHO", "content": "Факт"},
            {"url": "", "title": "пусто", "content": ""},
            {"url": "https://pubmed.ncbi.nlm.nih.gov/1/", "title": "Статья", "content": "Аннотация"},
        ]
    }

    rows = parse_searxng_results(payload)
    assert [r["url"] for r in rows] == [
        "https://www.who.int/a",
        "https://pubmed.ncbi.nlm.nih.gov/1/",
    ]


def test_search_prefers_searxng_when_configured():
    """Модуль поиска выбирает SearXNG, если задан адрес; тяжёлые зависимости не трогаем."""
    try:
        from app.workflows import web_search
    except ModuleNotFoundError as error:  # локальная песочница без openai
        print("skip: зависимости бота не установлены —", error)
        return

    from types import SimpleNamespace
    from unittest.mock import patch

    fake_settings = SimpleNamespace(
        SEARXNG_URL="http://searxng:8080",
        SEARXNG_LANGUAGE="ru",
        YANDEX_SEARCH_API_KEY="",
        YANDEX_FOLDER_ID="",
        WEB_SEARCH_MAX_RESULTS=5,
        WEB_SEARCH_SNIPPET_MAX_CHARS=2000,
    )
    calls: list[str] = []

    async def fake_searx(query):
        calls.append(query)
        return []

    async def run():
        with patch.object(web_search, "settings", fake_settings), patch.object(
            web_search, "_searxng_search", fake_searx
        ), patch.object(web_search, "render_web_answer", lambda **_: ""):
            await web_search.search_and_answer("анафилаксия")

    asyncio.run(run())
    assert calls == ["анафилаксия"], "поиск идёт через SearXNG, а не через Yandex"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok:", name)
