"""Проверка выбора статей PubMed для презентаций: без сети, БД и ключей (поставщики подменены).

Запуск: python scripts/test_pubmed_sources.py
Проверяется: разбор полного текста (JATS), открытый доступ, авторы и DOI, список кандидатов, тексты выбранных статей
(полный → аннотация при закрытом доступе и сбоях), отбраковка чужих идентификаторов, защита адреса и потолки эндпоинтов.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

from unittest.mock import MagicMock  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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

import httpx  # noqa: E402

from app.workflows import pubmed as pm  # noqa: E402
from app.workflows import pubmed_sources as ps  # noqa: E402
from config import settings  # noqa: E402

JATS = """<?xml version="1.0"?><article><front><article-meta><title-group><article-title>Заглавие</article-title></title-group></article-meta></front>
<body>
<sec><title>Introduction</title><p>Heart failure is common<xref ref-type="bibr" rid="B1">1</xref>, <xref>2</xref> and costly.</p></sec>
<sec><title>Methods</title><p>We enrolled 120 patients.</p>
  <sec><title>Participants</title><p>Adults aged 40-80.</p><table-wrap><table><tr><td>999</td></tr></table></table-wrap></sec></sec>
<sec><title>Results</title><p>Mortality fell by 18% (p=0.03).<fig><caption><p>Figure 7 text</p></caption></fig> No harm seen.</p></sec>
<sec><title>Conflict of interest</title><p>None.</p></sec>
<sec><title>Acknowledgements</title><p>Thanks to nurses.</p></sec>
<sec><title>References</title><p>1. Smith</p></sec>
<sec><title>Reference standard</title><p>Echocardiography was the reference standard.</p></sec>
</body></article>"""

EFETCH = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>111</PMID><Article><Journal><ISOAbbreviation>Lancet</ISOAbbreviation>
<JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal><ArticleTitle>Heart failure update</ArticleTitle>
<Abstract><AbstractText Label="AIM">Test aim.</AbstractText></Abstract>
<AuthorList><Author><LastName>Ivanov</LastName><Initials>AA</Initials></Author><Author><LastName>Petrov</LastName><Initials>B</Initials></Author>
<Author><CollectiveName>HF Study Group</CollectiveName></Author><Author><LastName>A</LastName><Initials>A</Initials></Author><Author><LastName>B</LastName><Initials>B</Initials></Author>
<Author><LastName>C</LastName><Initials>C</Initials></Author><Author><LastName>D</LastName><Initials>D</Initials></Author></AuthorList>
<PublicationTypeList><PublicationType>Review</PublicationType></PublicationTypeList></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="doi">10.1000/xyz</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""

EPMC = {"resultList": {"result": [
    {"pmid": "111", "pmcid": "PMC123456", "isOpenAccess": "Y", "doi": "10.1000/xyz", "authorString": "Ivanov AA, Petrov B.", "license": "cc by"},
    {"pmid": "222", "pmcid": "PMC555", "isOpenAccess": "N", "doi": "", "authorString": ""},
    {"pmid": "333; DROP", "pmcid": "PMC999999", "isOpenAccess": "Y"},
    {"pmid": "444", "pmcid": "../etc/passwd", "isOpenAccess": "Y"},
]}}


def art(pmid: str, title: str = "T", abstract: str = "Abstract.") -> pm.PubMedArticle:
    return pm.PubMedArticle(pmid=pmid, title=title, abstract=abstract, journal="J", year="2024", url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", design="обзор")


def test_parsing() -> None:
    secs, cut = ps.parse_jats(JATS)
    titles = [s.title for s in secs]
    assert titles == ["Introduction", "Methods", "Methods: Participants", "Results", "Reference standard"], titles
    text = " ".join(s.text for s in secs)
    assert "Heart failure is common , and costly." in text or "Heart failure is common, and costly." in text.replace(" ,", ","), text
    assert "999" not in text and "Figure 7" not in text, "таблицы и подписи рисунков в текст не попадают"
    assert "Thanks to nurses" not in text and "Smith" not in text and "None." not in text
    assert "Echocardiography" in text, "«Reference standard» — раздел методов, а не список литературы"
    assert "18%" in text and cut is False
    short, cut2 = ps.parse_jats(JATS, limit=60)
    assert cut2 is True and sum(len(s.text) for s in short) <= 60
    assert ps.parse_jats("<article><front/></article>") == ([], False)

    arts = pm._parse_efetch_xml(EFETCH)
    assert arts[0].authors == "Ivanov AA, Petrov B, HF Study Group, A A, B B, C C и др.", arts[0].authors
    assert arts[0].doi == "10.1000/xyz"
    meta = ps.parse_oa_meta(EPMC)
    assert set(meta) == {"111", "222", "444"}, "чужой PMID с мусором отброшен"
    assert meta["111"].open_access and meta["111"].pmcid == "PMC123456" and meta["111"].authors == "Ivanov AA, Petrov B"
    assert not meta["222"].open_access
    assert meta["444"].pmcid == "" and not meta["444"].open_access, "подозрительный PMCID не принимается"
    assert ps.valid_pmids(["123", " 123 ", "12a", "../x", "1; DROP", "456", ""]) == ["123", "456"]


async def test_candidates_and_fulltext() -> None:
    async def fake_translate(text):
        return "heart failure", True

    async def fake_find(query, candidates, limit):
        assert query == "heart failure" and candidates == 25 and limit == 3
        return [art("111"), art("222"), art("555")], "PubMed"

    async def fake_meta(pmids):
        return ps.parse_oa_meta(EPMC)

    ps.translate_query, ps._find_articles, ps.oa_meta = fake_translate, fake_find, fake_meta
    english, source, found = await ps.candidates("сердечная недостаточность", 3)
    assert (english, source) == ("heart failure", "PubMed")
    # «Ещё 10»: вторая страница — срез общего списка, кандидатов берётся больше
    asked = {}

    async def fake_find2(query, candidates, limit):
        asked.update(candidates=candidates, limit=limit)
        return [art(str(i)) for i in range(100, 100 + limit)], "PubMed"

    keep = ps._find_articles
    ps._find_articles = fake_find2
    _e2, _s2, page2 = await ps.candidates("сердечная недостаточность", 10, 10)
    assert asked == {"candidates": 45, "limit": 20}, asked
    assert [c.article.pmid for c in page2] == [str(i) for i in range(110, 120)], "вторая страница — вторые десять"
    _e3, _s3, page_far = await ps.candidates("сердечная недостаточность", 10, 99)
    assert asked["limit"] == 30, "дальше третьей страницы список не растёт"
    ps._find_articles = keep
    assert [c.open_access for c in found] == [True, False, None], "для статьи, которой нет у Europe PMC, доступ «неизвестно»"
    assert found[0].authors == "Ivanov AA, Petrov B" and found[0].doi == "10.1000/xyz"

    async def no_meta(pmids):
        return {}

    ps.oa_meta = no_meta
    _e, _s, found = await ps.candidates("сердечная недостаточность", 3)
    assert all(c.open_access is None for c in found), "сбой Europe PMC не роняет список"

    # тексты выбранных
    async def fake_efetch(ids):
        return [art(i, title=f"Статья {i}", abstract=f"Аннотация {i}") for i in ids if i != "999"]

    xml_calls: list[str] = []

    async def fake_xml(pmcid):
        xml_calls.append(pmcid)
        return JATS

    ps._efetch, ps.oa_meta, ps._fulltext_xml = fake_efetch, fake_meta, fake_xml
    out = await ps.fulltext(["111", "222", "555", "999", "bad", "111"])
    by = {t.pmid: t for t in out}
    assert list(by) == ["111", "222", "555", "999"], list(by)
    assert by["111"].level == "fulltext" and len(by["111"].sections) == 5 and by["111"].abstract == "Аннотация 111"
    assert by["222"].level == "abstract" and "закрыт" in by["222"].note and by["222"].sections == []
    assert by["555"].level == "abstract" and "узнать не удалось" in by["555"].note
    assert by["999"].level == "none"
    assert xml_calls == ["PMC123456"], "полный текст запрошен только у открытой статьи"

    # сбои полного текста — аннотация, а не падение
    async def broken_xml(pmcid):
        raise httpx.ConnectError("нет сети")

    ps._fulltext_xml = broken_xml
    assert (await ps.fulltext(["111"]))[0].level == "abstract"

    async def empty_xml(pmcid):
        return None

    ps._fulltext_xml = empty_xml
    item = (await ps.fulltext(["111"]))[0]
    assert item.level == "abstract" and "недоступен" in item.note

    async def bad_xml(pmcid):
        return "<article><body></body></article>"

    ps._fulltext_xml = bad_xml
    assert "не разобрались" in (await ps.fulltext(["111"]))[0].note

    # не более четырёх статей за раз
    many = await ps.fulltext(["1", "2", "3", "4", "5", "6"])
    assert len(many) == 4


def test_endpoints() -> None:
    settings.SERVICE_TOKEN = "t" * 32
    head = {"Authorization": "Bearer " + "t" * 32}
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    ctx = {"userId": "u1"}

    async def fake_cands(query, limit, offset=0):
        c = ps.Candidate(article=art("111", abstract="Annotation"), pmcid="PMC123456", open_access=True, authors="Ivanov AA", doi="10.1000/xyz", license="cc by")
        return "heart failure", "PubMed", [c]

    ps.candidates = fake_cands
    r = client.post("/v1/pubmed/candidates", json={"query": "сердечная недостаточность", "limit": 8, "context": ctx}, headers=head)
    assert r.status_code == 200, r.text
    a = r.json()["articles"][0]
    assert a["openAccess"] is True and a["abstract"] == "Annotation" and a["authors"] == "Ivanov AA" and a["doi"] == "10.1000/xyz", a
    assert r.json()["queryEn"] == "heart failure"
    assert client.post("/v1/pubmed/candidates", json={"query": "x", "context": ctx}, headers=head).status_code == 422
    assert client.post("/v1/pubmed/candidates", json={"query": "сердечная недостаточность", "limit": 10, "offset": 10, "context": ctx}, headers=head).status_code == 200
    assert client.post("/v1/pubmed/candidates", json={"query": "сердечная недостаточность", "offset": 40, "context": ctx}, headers=head).status_code == 422
    assert client.post("/v1/pubmed/candidates", json={"query": "сердце", "limit": 99, "context": ctx}, headers=head).status_code == 422
    assert client.post("/v1/pubmed/candidates", json={"query": "сердце", "context": ctx}).status_code == 401

    async def down(*_a):
        raise pm._SourcesUnavailable("таймаут", "нет соединения")

    ps.candidates = down
    r = client.post("/v1/pubmed/candidates", json={"query": "сердце", "context": ctx}, headers=head)
    assert r.status_code == 200 and "недоступен" in r.json()["error"] and r.json()["articles"] == []

    async def fake_full(pmids):
        return [ps.ArticleText(pmid="111", title="T", level="fulltext", sections=[ps.Section("Results", "Text")], truncated=False)]

    ps.fulltext = fake_full
    r = client.post("/v1/pubmed/fulltext", json={"pmids": ["111"], "context": ctx}, headers=head)
    assert r.status_code == 200 and r.json()["articles"][0]["sections"][0] == {"title": "Results", "text": "Text"}, r.text
    assert client.post("/v1/pubmed/fulltext", json={"pmids": ["1", "2", "3", "4", "5"], "context": ctx}, headers=head).status_code == 422
    assert client.post("/v1/pubmed/fulltext", json={"pmids": [], "context": ctx}, headers=head).status_code == 422
    ps.fulltext = down
    r = client.post("/v1/pubmed/fulltext", json={"pmids": ["1"], "context": ctx}, headers=head)
    assert r.status_code == 200 and "недоступен" in r.json()["error"]


def main() -> None:
    test_parsing()
    asyncio.run(test_candidates_and_fulltext())
    test_endpoints()
    print("ok: статьи PubMed для презентаций — разбор текста, открытый доступ, список, тексты, защита и потолки")


if __name__ == "__main__":
    main()
