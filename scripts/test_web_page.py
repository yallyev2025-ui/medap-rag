"""Ссылки из ТЗ презентации (24.10.2026): текст страницы и PDF по адресу, DOI → PMID. Без сети: загрузка и поиск подменены."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from unittest.mock import MagicMock  # noqa: E402

for _ in range(30):
    try:
        from app.api import v1  # noqa: E402
        break
    except ModuleNotFoundError as _exc:
        _name = _exc.name or ""
        if not _name or _name.split(".")[0] in ("app", "db", "rag", "config", "constants"):
            raise
        sys.modules[_name] = MagicMock()
        for _k in [k for k in sys.modules if k.startswith("app.api")]:
            del sys.modules[_k]
from app.workflows import pubmed_sources as ps  # noqa: E402
from app.workflows import web_page as wp  # noqa: E402
from config import settings  # noqa: E402

LONG = "Результаты исследования показали значимое снижение показателя. " * 40
ARTICLE = f"<html><head><title>Сайт</title><meta name='citation_title' content='Настоящая статья'/><meta name='citation_abstract' content='{'Реферат статьи. ' * 20}'/></head><body><nav>меню</nav><article><p>{LONG}</p></article><script>alert(1)</script></body></html>".encode()
WALL = "<html><head><meta name='description' content='" + "Краткое описание закрытой статьи. " * 8 + "'/></head><body><p>Subscribe to read the full article.</p></body></html>"


def test_extract() -> None:
    text, title, abstract = wp.extract_html(ARTICLE)
    assert "Результаты исследования" in text and "меню" not in text and "alert" not in text, "меню и скрипты убраны"
    assert title == "Настоящая статья" and abstract.startswith("Реферат статьи"), (title, abstract[:30])
    assert not wp.looks_paywalled(text)
    assert wp.looks_paywalled("Subscribe to read the full article.")


def _fake_client(routes):
    class Resp:
        def __init__(self, status, headers, body):
            self.status_code, self.headers, self._body = status, headers, body

        async def aiter_bytes(self):
            yield self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            if url not in routes:
                raise httpx.ConnectError("нет маршрута")
            return Resp(*routes[url])

    return Client


async def test_fetch() -> None:
    orig = wp.httpx.AsyncClient
    routes = {
        "https://doi.org/10.1000/abc": (302, {"location": "https://journal.example.org/a1"}, b""),
        "https://journal.example.org/a1": (200, {"content-type": "text/html; charset=utf-8"}, ARTICLE),
        "https://wall.example.org/x": (200, {"content-type": "text/html"}, WALL.encode()),
        "https://bad.example.org/loop": (302, {"location": "https://bad.example.org/loop"}, b""),
        "https://down.example.org/x": (403, {}, b""),
    }
    wp.httpx.AsyncClient = _fake_client(routes)
    orig_validate = wp.validate_public_url
    wp.validate_public_url = lambda u: None
    try:
        r = await wp.fetch_page_text("https://doi.org/10.1000/abc")
        assert not r.error and r.final_url == "https://journal.example.org/a1" and "Результаты исследования" in r.text and r.title == "Настоящая статья", r
        r = await wp.fetch_page_text("https://wall.example.org/x")
        assert r.error is None and r.text == "" and r.abstract.startswith("Краткое описание"), "закрытая страница: текста нет, но реферат из мета-описания отдан"
        r = await wp.fetch_page_text("https://bad.example.org/loop")
        assert r.error and "перенаправлений" in r.error
        r = await wp.fetch_page_text("https://down.example.org/x")
        assert r.error and "403" in r.error
    finally:
        wp.httpx.AsyncClient = orig
        wp.validate_public_url = orig_validate
    # настоящая защита от локальных адресов (без подмены)
    for bad in ("http://127.0.0.1/x", "http://localhost:8000/x", "http://169.254.169.254/latest", "file:///etc/passwd", "ftp://example.org/x"):
        r = await wp.fetch_page_text(bad)
        assert r.error and not r.text, (bad, r)


def test_endpoints() -> None:
    settings.SERVICE_TOKEN = "t" * 32
    head = {"Authorization": "Bearer " + "t" * 32}
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    ctx = {"userId": "u1"}

    async def fake_resolve(doi):
        return "12345" if doi == "10.1000/abc" else None

    ps.resolve_doi = fake_resolve
    r = client.post("/v1/pubmed/resolve", json={"dois": ["10.1000/ABC", "10.9999/none", "не doi"], "context": ctx}, headers=head)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["doi"] == "10.1000/abc" and items[0]["pmid"] == "12345" and items[1]["pmid"] is None and items[2]["pmid"] is None, items
    assert client.post("/v1/pubmed/resolve", json={"dois": [], "context": ctx}, headers=head).status_code == 422

    async def fake_pmc(pmcid):
        return "777" if pmcid == "PMC6543210" else None

    ps.resolve_pmcid = fake_pmc
    ps.resolve_doi = fake_resolve
    r = client.post("/v1/pubmed/resolve", json={"pmcids": ["pmc6543210", "PMC1"], "dois": ["10.1000/abc"], "context": ctx}, headers=head)
    got = r.json()["items"]
    assert r.status_code == 200 and got[0] == {"doi": "10.1000/abc", "pmcid": "", "pmid": "12345"} and got[1]["pmcid"] == "PMC6543210" and got[1]["pmid"] == "777" and got[2]["pmid"] is None, got
    assert client.post("/v1/pubmed/resolve", json={"dois": ["10.1000/abc"], "context": ctx}).status_code == 401

    async def down(_doi):
        from app.workflows.pubmed import _SourcesUnavailable

        raise _SourcesUnavailable("", "нет связи")

    ps.resolve_doi = down
    r = client.post("/v1/pubmed/resolve", json={"dois": ["10.1000/abc"], "context": ctx}, headers=head)
    assert r.status_code == 200 and "недоступен" in r.json()["error"]

    r = client.post("/v1/web/page", json={"url": "http://127.0.0.1:8000/x", "context": ctx}, headers=head)
    assert r.status_code == 200 and r.json()["error"] and r.json()["text"] == "", "локальный адрес отвергнут"
    assert client.post("/v1/web/page", json={"url": "x", "context": ctx}, headers=head).status_code == 422
    assert client.post("/v1/web/page", json={"url": "https://example.org/a", "context": ctx}).status_code == 401


def main() -> None:
    test_extract()
    asyncio.run(test_fetch())
    test_endpoints()
    print("ok: ссылки из ТЗ — текст страницы и PDF, редиректы, платная стена, защита от локальных адресов, DOI → PMID")


if __name__ == "__main__":
    main()
