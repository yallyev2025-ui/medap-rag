"""Клинические рекомендации РФ с clinreky.ru (Mentor 2.0, этап 3).

Зачем свой разбор, если есть рубрикатор: рубрикатор даёт список и PDF, а нам нужен
полный текст рекомендаций из HTML — по разделам и таблицам, чтобы искать по ним и
показывать ссылку на официальный документ.

Правила (владелец 22.11.2026):
- берём **оригинальный текст** рекомендации (`krv2-rec-original`), а не ИИ-выжимку;
- каталог — `sitemap.xml`, страница нозологии — `/nosology/<КОД>/`;
- ничего не выдумываем: не разобралось — так и пишем в статусе.

Модуль без сети в разборе: `parse_*` — чистые функции, их проверяет тест на образце
страницы. Загрузка (`fetch_*`) — отдельно и только по запросу.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://clinreky.ru"
USER_AGENT = "MedAP-Student-AI/1.0 (educational use)"

# Раздел «рекомендации» на странице: каждый пункт — li с data-rec-id
REC_ITEM = "li.krv2-rec"
REC_ORIGINAL = ".krv2-rec-original"
REC_LEVEL = ".ev-badge"


@dataclass
class Recommendation:
    """Один пункт рекомендаций: текст и, если указаны, уровень убедительности/достоверности."""

    text: str
    level: str | None = None


@dataclass
class ClinrekyPage:
    """Страница нозологии: метаданные, разделы и рекомендации."""

    url: str
    title: str = ""
    kr_id: str = ""
    edition: str = ""
    sections: list[dict[str, str]] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    official_url: str = ""
    status: str = "parsed"
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "krId": self.kr_id,
            "edition": self.edition,
            "sections": self.sections,
            "recommendations": [{"text": r.text, "level": r.level} for r in self.recommendations],
            "tables": self.tables,
            "officialUrl": self.official_url,
            "status": self.status,
            "missing": self.missing,
        }


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def parse_catalog(sitemap_xml: str) -> list[str]:
    """Адреса страниц нозологий из sitemap.xml: `/nosology/<КОД>/`."""
    found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap_xml or "")
    urls = [u for u in found if "/nosology/" in u]

    return list(dict.fromkeys(urls))


def _json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") in {"MedicalWebPage", "MedicalGuideline", "WebPage"}:
                return item

    return {}


def _edition(page_text: str) -> str:
    """Год редакции, если он написан на странице («2024», «редакция 2023»)."""
    match = re.search(r"(?:редакц\w*|год\w*)\D{0,20}(20\d{2})", page_text, re.IGNORECASE)

    return match.group(1) if match else ""


def _official(text: str) -> str:
    match = re.search(r"https?://[^\s\"'<>]*(?:cr\.minzdrav\.gov\.ru|minzdrav\.gov\.ru)[^\s\"'<>]*", text)

    return match.group(0) if match else ""


def parse_page(html: str, url: str = "") -> ClinrekyPage:
    """Разобрать страницу нозологии. Неразобранное честно попадает в `missing`."""
    soup = BeautifulSoup(html or "", "html.parser")
    page = ClinrekyPage(url=url)
    ld = _json_ld(soup)
    page.title = str(ld.get("name") or (_text(soup.title) if soup.title else "")).strip()
    page.kr_id = str(ld.get("identifier") or ld.get("@id") or "").strip()
    body_text = _text(soup.body or soup)
    page.edition = _edition(body_text)
    page.official_url = _official(str(soup))

    for heading in soup.find_all(["h2", "h3"]):
        title = _text(heading)
        if not title:
            continue
        parts: list[str] = []
        for sibling in heading.find_next_siblings():
            if sibling.name in {"h2", "h3"}:
                break
            if sibling.name in {"p", "ul", "ol", "table"}:
                parts.append(_text(sibling))
        text = " ".join(parts).strip()
        if text:
            page.sections.append({"title": title, "text": text[:8000]})

    for item in soup.select(REC_ITEM):
        # Только оригинальный текст рекомендации; ИИ-выжимку (`data-rec-text`) не берём
        original = item.select_one(REC_ORIGINAL)
        node = original or item
        text = _text(node)
        if len(text) < 20:
            continue
        badge = item.select_one(REC_LEVEL)
        page.recommendations.append(Recommendation(text=text[:4000], level=_text(badge) if badge else None))

    for index, table in enumerate(soup.find_all("table")):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [_text(td) for td in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if len(rows) >= 2:
            page.tables.append({"index": index + 1, "rows": rows[:200]})

    if not page.sections:
        page.missing.append("разделы не найдены")
    if not page.recommendations:
        page.missing.append("рекомендации не найдены")
    if not page.title:
        page.missing.append("название не найдено")
    page.status = "parsed" if page.sections or page.recommendations else "failed"

    return page


async def fetch_catalog(timeout: float = 20.0) -> list[str]:
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        response = await client.get(f"{BASE}/sitemap.xml")
        response.raise_for_status()

        return parse_catalog(response.text)


async def fetch_page(url: str, timeout: float = 30.0) -> ClinrekyPage | None:
    """Загрузить и разобрать страницу; None — не открылась (причина в логе)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()

            return parse_page(response.text, url)
    except Exception as error:
        logger.warning("clinreky: страница не открылась %s: %s", url, error)
        return None
