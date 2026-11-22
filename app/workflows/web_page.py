"""Полный текст найденной страницы для поиска (ТЗ 20.11.2026).

ЗАЧЕМ. Поиск отдаёт короткий сниппет — его мало, чтобы раскрыть слайд.
Берём текст страницы: сначала обычным HTTP-запросом, а если страница
отдаёт пустоту (JavaScript, защита от роботов) — браузером (Playwright).

ГРАНИЦЫ. Адрес проверяется защитой SSRF до запроса; размер ограничен;
страница — данные, а не инструкции. Браузер не обязателен: если Playwright
не установлен, модуль честно работает без него.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.observability.context import current_request_id
from app.security.ssrf import BlockedURLError, validate_public_url
from config import settings

logger = logging.getLogger(__name__)

MAX_BYTES = 2_000_000
MIN_USEFUL = 400


def extract_text(html: bytes) -> str:
    """HTML → связный текст без скриптов и стилей. Чистая функция."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


async def _http_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "MedapSearch/1.0"}) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    break
                chunks.append(chunk)
    return extract_text(b"".join(chunks))


async def _browser_text(url: str) -> str:
    """Запасной путь для страниц с JavaScript и защитой. Нет Playwright — пусто."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                text = await page.inner_text("body")
            finally:
                await browser.close()
        return " ".join(text.split())
    except Exception as error:  # браузер не поднялся или страница не открылась — не падаем
        logger.warning("web_page: браузер не прочитал %s: %s", url, error)
        return ""


async def page_text(url: str) -> str:
    """Текст страницы: HTTP, затем браузер, если HTTP дал слишком мало."""
    try:
        await asyncio.to_thread(validate_public_url, url)
    except BlockedURLError:
        return ""

    try:
        text = await _http_text(url)
    except Exception as error:
        logger.info("web_page: HTTP не прочитал %s: %s", url, error)
        text = ""

    if len(text) >= MIN_USEFUL:
        return text

    browser = await _browser_text(url)

    return browser if len(browser) > len(text) else text


async def enrich_results(
    results: list[dict[str, Any]],
    *,
    fetch: Callable[[str], Awaitable[str]] = page_text,
    limit: int = 3,
    max_chars: int = 2000,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Дополнить короткие сниппеты полным текстом, не больше `limit` страниц.

    Запросы идут одновременно; сбой одной страницы не мешает остальным.
    """
    short = [row for row in results if len(str(row.get("content") or "")) < MIN_USEFUL][:limit]
    if not short:
        return results

    async def one(row: dict[str, Any]) -> tuple[str, str]:
        url = str(row.get("url") or "")
        try:
            text = await asyncio.wait_for(fetch(url), timeout=timeout)
        except Exception:
            text = ""
        return url, " ".join(text.split())[:max_chars]

    fetched = dict(await asyncio.gather(*(one(row) for row in short)))

    out: list[dict[str, Any]] = []
    for row in results:
        text = fetched.get(str(row.get("url") or ""), "")
        if text and len(text) > len(str(row.get("content") or "")):
            out.append({**row, "content": text})
        else:
            out.append(row)

    return out


# --- Полный текст статьи по адресу для /v1/web/page (24.10.2026) -------------------
#
# Эту функцию вызывал сайт (ссылка из ТЗ на слайд: «прочитай статью»). При переделке
# поиска (20.11.2026) её случайно убрали, а импорт в `app/api/v1.py` остался — из-за
# этого приложение падало на старте (ImportError) и деплой не проходил healthcheck.
# Возвращаем как было: HTTP с проверкой SSRF на каждом шаге редиректа, PDF, мета-реферат
# для закрытых страниц. Новый код выше (page_text/enrich_results) не тронут.

MAX_REDIRECTS = 4
MAX_PDF_PAGES = 40
MIN_ARTICLE_CHARS = 1200
_PAYWALL = ("subscribe to read", "purchase this article", "buy this article", "access through your institution", "sign in to read", "log in to read", "доступ ограничен", "купить статью", "оформите подписку")
_ABSTRACT_META = ("citation_abstract", "dc.description", "dcterms.abstract", "og:description", "description")


@dataclass
class PageText:
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    abstract: str = ""
    kind: str = ""  # html | pdf
    truncated: bool = False
    error: str | None = None
    request_id: str | None = None


def _meta_abstract(soup: BeautifulSoup) -> str:
    for name in _ABSTRACT_META:
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        content = (tag.get("content") or "").strip() if tag else ""
        if len(content) >= 120:
            return " ".join(content.split())[:3000]
    return ""


def extract_html(html: bytes) -> tuple[str, str, str]:
    """(текст, название, мета-реферат): основной текст берётся из <article>/<main>, если они есть."""
    soup = BeautifulSoup(html, "html.parser")
    abstract = _meta_abstract(soup)
    title = ""
    if soup.title:
        title = soup.title.get_text(strip=True)
    meta_title = soup.find("meta", attrs={"name": "citation_title"})
    if meta_title and meta_title.get("content"):
        title = str(meta_title.get("content")).strip()
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = " ".join(root.get_text(separator=" ").split())

    return text, title, abstract


def extract_pdf(data: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:MAX_PDF_PAGES]:
            parts.append(page.extract_text() or "")

    return "\n".join(" ".join(p.split()) for p in parts if p.strip())


def looks_paywalled(text: str) -> bool:
    low = text[:6000].lower()

    return len(text) < MIN_ARTICLE_CHARS or any(m in low for m in _PAYWALL) and len(text) < 6000


async def fetch_page_text(url: str) -> PageText:
    max_bytes = settings.WEB_PAGE_MAX_KB * 1024
    current = url
    body = bytearray()
    content_type = ""
    try:
        async with httpx.AsyncClient(timeout=settings.WEB_RESEARCH_TIMEOUT_SECONDS * 2, follow_redirects=False) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                try:
                    await asyncio.to_thread(validate_public_url, current)
                except BlockedURLError as exc:
                    return PageText(url=url, error=str(exc), request_id=current_request_id())
                async with client.stream("GET", current, headers={"User-Agent": "MedAP-Student-AI/1.0", "Accept": "text/html,application/pdf,*/*;q=0.5"}) as response:
                    if 300 <= response.status_code < 400 and response.headers.get("location"):
                        current = urljoin(current, response.headers["location"])
                        continue
                    if response.status_code >= 400:
                        return PageText(url=url, final_url=current, error=f"Страница вернула статус {response.status_code}", request_id=current_request_id())
                    content_type = response.headers.get("content-type", "").lower()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            return PageText(url=url, final_url=current, error="Файл слишком большой для разбора", request_id=current_request_id())
                    break
            else:
                return PageText(url=url, error="Слишком много перенаправлений", request_id=current_request_id())
    except httpx.HTTPError:
        logger.exception("Web Page: не удалось загрузить %s", url)
        return PageText(url=url, error="Не удалось загрузить страницу", request_id=current_request_id())

    data = bytes(body)
    is_pdf = "pdf" in content_type or data[:5] == b"%PDF-" or current.lower().split("?")[0].endswith(".pdf")
    limit = settings.WEB_PAGE_MAX_CHARS
    if is_pdf:
        try:
            text = await asyncio.to_thread(extract_pdf, data)
        except Exception:
            logger.exception("Web Page: PDF не разобран %s", url)
            return PageText(url=url, final_url=current, kind="pdf", error="PDF не удалось прочитать (скан без текста или повреждён)", request_id=current_request_id())
        if not text.strip():
            return PageText(url=url, final_url=current, kind="pdf", error="В PDF нет текста (скан)", request_id=current_request_id())
        return PageText(url=url, final_url=current, kind="pdf", text=text[:limit], truncated=len(text) > limit, request_id=current_request_id())

    text, title, abstract = extract_html(data)
    if looks_paywalled(text):
        return PageText(url=url, final_url=current, title=title, abstract=abstract, kind="html", error="Сайт не отдаёт текст статьи (платный доступ или защита)" if not abstract else None, request_id=current_request_id())

    return PageText(url=url, final_url=current, title=title, text=text[:limit], abstract=abstract, kind="html", truncated=len(text) > limit, request_id=current_request_id())
