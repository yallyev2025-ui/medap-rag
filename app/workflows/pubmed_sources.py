"""Статьи PubMed как источники для презентаций сайта (этап «выбор статей»).

Бот здесь только добывает данные: модель слайдов не вызывает и ничего не пишет. Два шага:
1. `candidates` — список статей (название, авторы, журнал, год, тип исследования, аннотация, DOI) и пометка, открыт ли полный текст;
2. `fulltext` — по выбранным PMID: полный текст по разделам, если он открыт (Europe PMC, открытое подмножество PMC),
   иначе аннотация. Закрытое содержимое издательств не берётся и не обходится.

Слайды по этому тексту пишет сайт по своему промпту.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from app.workflows.pubmed import (
    PubMedArticle,
    _EUROPEPMC_SEARCH,
    _SourcesUnavailable,
    _efetch,
    _find_articles,
    _get_url,
    _reason,
    translate_query,
)

logger = logging.getLogger(__name__)

_EUROPEPMC_REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_PMID = re.compile(r"^\d{1,9}$")
_PMCID = re.compile(r"^PMC\d{4,10}$")

MAX_CANDIDATES = 10
# «Ещё 10»: страницы по 10 лучших; дальше трёх страниц список не растёт (качество падает, запросы дорожают)
MAX_OFFSET = 20
MAX_FULLTEXT_ARTICLES = 4
# Потолок текста одной статьи: хватает на методы, результаты и обсуждение; длиннее — хвост отбрасывается честно (truncated).
MAX_FULLTEXT_CHARS = 60_000
# Из полного текста берутся основные разделы; список литературы, благодарности, конфликты интересов — нет.
_SKIP_SECTIONS = re.compile(r"^\s*(acknowledg|funding|conflicts?\b|competing|author contribution|abbreviations?\b|supplement|references?\s*$|data availability|ethics (approval|statement))", re.I)


@dataclass
class OaInfo:
    """Что Europe PMC знает о статье: открыт ли полный текст, где он лежит, DOI и авторы."""

    pmcid: str = ""
    open_access: bool = False
    doi: str = ""
    authors: str = ""
    license: str = ""


@dataclass
class Candidate:
    article: PubMedArticle
    pmcid: str = ""
    # None — не удалось узнать (Europe PMC недоступен); False — закрыт; True — полный текст открыт
    open_access: bool | None = None
    authors: str = ""
    doi: str = ""
    license: str = ""


@dataclass
class Section:
    title: str
    text: str


@dataclass
class ArticleText:
    pmid: str
    title: str = ""
    authors: str = ""
    journal: str | None = None
    year: str | None = None
    doi: str = ""
    url: str = ""
    pmcid: str = ""
    # fulltext — полный текст по разделам; abstract — только аннотация; none — статьи не нашли
    level: str = "none"
    abstract: str = ""
    sections: list[Section] = field(default_factory=list)
    truncated: bool = False
    note: str = ""


def valid_pmids(pmids: list[str]) -> list[str]:
    """Только настоящие номера PubMed (цифры): всё остальное отбрасывается до любого запроса наружу."""
    seen: list[str] = []
    for raw in pmids:
        pmid = str(raw).strip()
        if _PMID.match(pmid) and pmid not in seen:
            seen.append(pmid)
    return seen


def parse_oa_meta(data: dict) -> dict[str, OaInfo]:
    """Ответ поиска Europe PMC (`resultType=core`) → сведения по PMID."""
    out: dict[str, OaInfo] = {}
    for record in (data.get("resultList") or {}).get("result") or []:
        pmid = str(record.get("pmid") or "").strip()
        if not _PMID.match(pmid):
            continue
        pmcid = str(record.get("pmcid") or "").strip()
        out[pmid] = OaInfo(
            pmcid=pmcid if _PMCID.match(pmcid) else "",
            open_access=str(record.get("isOpenAccess") or "").upper() == "Y" and bool(_PMCID.match(pmcid)),
            doi=str(record.get("doi") or "").strip(),
            authors=" ".join(str(record.get("authorString") or "").split()).rstrip("."),
            license=str(record.get("license") or "").strip(),
        )
    return out


async def oa_meta(pmids: list[str]) -> dict[str, OaInfo]:
    """Сведения об открытом доступе одним запросом. Сбой — пустой ответ (флаг останется «неизвестно»), список не падает."""
    ids = valid_pmids(pmids)
    if not ids:
        return {}
    query = "(" + " OR ".join(f"EXT_ID:{p}" for p in ids) + ") AND SRC:MED"
    try:
        response = await _get_url(
            _EUROPEPMC_SEARCH, {"query": query, "resultType": "core", "format": "json", "pageSize": len(ids)}, 20.0
        )
        return parse_oa_meta(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Europe PMC: не удалось узнать открытый доступ (%s)", _reason(exc))
        return {}


# --- Полный текст: разбор JATS XML ---------------------------------------------------------


def _plain(el: ET.Element) -> str:
    """Текст элемента без номеров ссылок (<xref>), формул, таблиц и рисунков: их числа в тексте только мешают."""
    skip = {"xref", "table-wrap", "fig", "disp-formula", "inline-formula", "supplementary-material", "label"}
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag not in skip:
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(el)
    return " ".join("".join(parts).split())


def parse_jats(xml_text: str, limit: int = MAX_FULLTEXT_CHARS) -> tuple[list[Section], bool]:
    """Полный текст (JATS из Europe PMC) → разделы `body`. Вложенные разделы получают название «Методы: Участники».
    Второй результат — обрезан ли текст по потолку."""
    root = ET.fromstring(xml_text)
    body = next((el for el in root.iter() if el.tag.split("}")[-1] == "body"), None)
    if body is None:
        return [], False
    sections: list[Section] = []
    total = 0
    truncated = False

    def add(title: str, text: str) -> None:
        nonlocal total, truncated
        if not text or truncated:
            return
        room = limit - total
        if len(text) > room:
            text = text[:room].rsplit(" ", 1)[0]
            truncated = True
        if text:
            sections.append(Section(title=title or "Текст", text=text))
            total += len(text)

    def walk(sec: ET.Element, prefix: str) -> None:
        title = _plain(next((c for c in sec if c.tag.split("}")[-1] == "title"), ET.Element("title")))
        full = f"{prefix}: {title}" if prefix and title else (title or prefix)
        if title and _SKIP_SECTIONS.search(title):
            return
        paragraphs = [_plain(c) for c in sec if c.tag.split("}")[-1] == "p"]
        add(full, " ".join(p for p in paragraphs if p))
        for child in sec:
            if child.tag.split("}")[-1] == "sec":
                walk(child, full)

    direct = [c for c in body if c.tag.split("}")[-1] == "p"]
    if direct:
        add("Текст", " ".join(_plain(p) for p in direct))
    for sec in body:
        if sec.tag.split("}")[-1] == "sec":
            walk(sec, "")
    return sections, truncated


async def _fulltext_xml(pmcid: str) -> str | None:
    if not _PMCID.match(pmcid):
        return None
    try:
        response = await _get_url(f"{_EUROPEPMC_REST}/{pmcid}/fullTextXML", {}, 30.0)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 403):
            return None  # полный текст закрыт или отозван — это не сбой
        raise
    return response.text


# --- Публичные шаги ------------------------------------------------------------------------


async def candidates(query: str, limit: int = 10, offset: int = 0) -> tuple[str, str, list[Candidate]]:
    """(английский запрос, источник, статьи) — лучшие по уровню доказательности и свежести. Модель — только перевод запроса."""
    limit = max(1, min(limit, MAX_CANDIDATES))
    offset = max(0, min(offset, MAX_OFFSET))
    english, _translated = await translate_query(query)
    # ранжирование общее (уровень доказательности, свежесть): страница — срез общего списка; чем дальше страница, тем больше берётся кандидатов
    articles, source = await _find_articles(english, 25 + offset * 2, offset + limit)
    articles = articles[offset:offset + limit]
    meta = await oa_meta([a.pmid for a in articles])
    out: list[Candidate] = []
    for a in articles:
        info = meta.get(a.pmid)
        out.append(
            Candidate(
                article=a,
                pmcid=info.pmcid if info else "",
                open_access=info.open_access if info else None,
                authors=a.authors or (info.authors if info else ""),
                doi=a.doi or (info.doi if info else ""),
                license=info.license if info else "",
            )
        )
    return english, source, out


async def _europepmc_pmid(query: str, same: tuple[str, str]) -> str | None:
    """PMID по запросу Europe PMC; `same` — (поле ответа, ожидаемое значение): чужую статью с похожим идентификатором не берём."""
    try:
        response = await _get_url(f"{_EUROPEPMC_REST}/search", {"query": query, "format": "json", "resultType": "lite", "pageSize": "3"}, 15.0)
        results = (response.json().get("resultList") or {}).get("result") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise _SourcesUnavailable("", _reason(exc)) from exc
    for r in results:
        pmid = str(r.get("pmid") or "").strip()
        if _PMID.match(pmid) and str(r.get(same[0]) or "").lower() == same[1].lower():
            return pmid
    return None


async def resolve_doi(doi: str) -> str | None:
    """PMID статьи по её DOI (ссылки из ТЗ презентации): Europe PMC ищет по DOI и отдаёт PMID, если статья есть в PubMed. Нет статьи — `None`."""
    return await _europepmc_pmid(f'DOI:"{doi}"', ("doi", doi))


async def resolve_pmcid(pmcid: str) -> str | None:
    """PMID статьи по номеру PMC (ссылка на pmc.ncbi.nlm.nih.gov или europepmc.org)."""
    if not _PMCID.match(pmcid):
        return None
    return await _europepmc_pmid(f"PMCID:{pmcid}", ("pmcid", pmcid))


async def fulltext(pmids: list[str]) -> list[ArticleText]:
    """Текст выбранных статей (до четырёх): полный, если открыт, иначе аннотация. Статья, которой нет, получает level=none."""
    ids = valid_pmids(pmids)[:MAX_FULLTEXT_ARTICLES]
    if not ids:
        return []
    try:
        found = {a.pmid: a for a in await _efetch(ids)}
    except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
        raise _SourcesUnavailable(_reason(exc), "") from exc
    meta = await oa_meta(ids)

    async def one(pmid: str) -> ArticleText:
        a = found.get(pmid)
        info = meta.get(pmid)
        if a is None:
            return ArticleText(pmid=pmid, note="статья не найдена или без аннотации")
        item = ArticleText(
            pmid=pmid, title=a.title, authors=a.authors or (info.authors if info else ""), journal=a.journal, year=a.year,
            doi=a.doi or (info.doi if info else ""), url=a.url, pmcid=info.pmcid if info else "", level="abstract", abstract=a.abstract,
        )
        if info and info.open_access:
            try:
                xml_text = await _fulltext_xml(info.pmcid)
                if xml_text:
                    sections, truncated = parse_jats(xml_text)
                    if sections:
                        item.level, item.sections, item.truncated = "fulltext", sections, truncated
                    else:
                        item.note = "полный текст открыт, но разделы не разобрались — только аннотация"
                else:
                    item.note = "полный текст недоступен — только аннотация"
            except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                logger.warning("Europe PMC: полный текст %s не получен (%s)", info.pmcid, _reason(exc))
                item.note = "полный текст не получен — только аннотация"
        elif info is None:
            item.note = "открыт ли полный текст, узнать не удалось — только аннотация"
        else:
            item.note = "полный текст закрыт издательством — только аннотация"
        return item

    return list(await asyncio.gather(*(one(p) for p in ids)))
