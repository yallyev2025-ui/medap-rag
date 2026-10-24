"""Текст страницы или PDF по адресу — для презентаций по ссылкам из ТЗ (24.10.2026).

В отличие от `web_research.research_url` здесь НЕТ пересказа моделью: возвращается чистый текст страницы (данные для слайдов), а слайды по нему
пишет сайт. Защита та же, что у остального веб-доступа бота: адрес и КАЖДЫЙ шаг редиректа проходят `validate_public_url` (приватные сети и служебные
адреса запрещены), размер ограничен во время загрузки, редиректов не больше четырёх (DOI всегда перенаправляет на сайт издателя). Содержимое страницы —
данные, не инструкции: сайт передаёт его модели в размеченных блоках. Закрытые страницы (платная стена, защита от ботов) возвращают честную ошибку и,
если на странице есть мета-описание статьи (`citation_abstract`, `description`), — его как реферат.
"""

import asyncio
import io
import logging
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.observability.context import current_request_id
from app.security.ssrf import BlockedURLError, validate_public_url
from config import settings

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 4
MAX_PDF_PAGES = 40
# Меньше этого — страница почти пустая: скорее всего платная стена или «включите JavaScript»
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
