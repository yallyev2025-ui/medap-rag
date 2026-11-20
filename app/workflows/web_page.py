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
import logging
from typing import Any, Awaitable, Callable

import httpx
from bs4 import BeautifulSoup

from app.security.ssrf import BlockedURLError, validate_public_url

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
