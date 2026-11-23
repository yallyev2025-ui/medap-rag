"""Разбор клинических рекомендаций с clinreky.ru — без сети (образец страницы).

python -m scripts.test_clinreky
"""

from app.integrations.clinreky import compare_edition, parse_catalog, parse_catalog_json, parse_page

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset>
  <url><loc>https://clinreky.ru/nosology/J44/</loc></url>
  <url><loc>https://clinreky.ru/nosology/I10/</loc></url>
  <url><loc>https://clinreky.ru/about/</loc></url>
  <url><loc>https://clinreky.ru/nosology/J44/</loc></url>
</urlset>"""

PAGE = """
<html><head><title>ХОБЛ</title>
<script type="application/ld+json">{"@type":"MedicalWebPage","name":"Хроническая обструктивная болезнь лёгких","identifier":"КР603"}</script>
</head><body>
<p>Редакция 2024</p>
<h2 id="b-appendix_1">Диагностика</h2>
<p>Спирометрия обязательна. ОФВ1/ФЖЕЛ менее 0,70 подтверждает бронхиальную обструкцию.</p>
<h2 id="b-appendix_2">Терапия</h2>
<p>Базисная терапия — бронходилататоры длительного действия.</p>
<ul>
  <li class="krv2-rec" data-rec-id="1">
    <span class="ev-badge">УУР A, УДД 1</span>
    <div class="krv2-rec-original">Рекомендуется спирометрия для подтверждения диагноза.</div>
    <div class="krv2-rec-text">ИИ-выжимка: кратко про спирометрию.</div>
  </li>
  <li class="krv2-rec" data-rec-id="2">
    <div class="krv2-rec-original">Рекомендуются бронходилататоры длительного действия при стабильной ХОБЛ.</div>
  </li>
</ul>
<table><tr><th>Показатель</th><th>Норма</th></tr><tr><td>ОФВ1/ФЖЕЛ</td><td>&lt; 0,70</td></tr></table>
<p>Официальный документ: https://cr.minzdrav.gov.ru/schema/603_2</p>
</body></html>
"""


def test_catalog_deduplicates_and_keeps_only_nosology() -> None:
    urls = parse_catalog(SITEMAP)
    assert urls == ["https://clinreky.ru/nosology/J44/", "https://clinreky.ru/nosology/I10/"]


def test_catalog_json_keeps_only_published_pages() -> None:
    items = parse_catalog_json(
        """{"items": [
            {"id": "714_2", "title": "Пневмония (внебольничная)", "url": "/nosology/J18-ped/",
             "age": "child", "mkb_all": ["J12", "J18"], "published": true},
            {"id": "1_1", "title": "Черновик", "url": "/nosology/X00/", "published": false}
        ]}"""
    )
    assert len(items) == 1
    assert items[0]["title"] == "Пневмония (внебольничная)"
    assert items[0]["url"] == "https://clinreky.ru/nosology/J18-ped/"
    assert items[0]["age"] == "child"
    assert items[0]["mkb"] == ("J12", "J18")


def test_page_meta_sections_tables_official_link() -> None:
    page = parse_page(PAGE, "https://clinreky.ru/nosology/J44/")
    assert page.status == "parsed"
    assert page.title.startswith("Хроническая обструктивная")
    assert page.kr_id == "КР603"
    assert page.edition == "2024"
    assert [s["title"] for s in page.sections] == ["Диагностика", "Терапия"]
    assert "ОФВ1" in page.sections[0]["text"]
    assert page.tables[0]["rows"][1] == ["ОФВ1/ФЖЕЛ", "< 0,70"]
    assert "cr.minzdrav.gov.ru" in page.official_url


def test_main_sections_and_recommendation_section_are_parsed() -> None:
    page = parse_page(
        """<html><head><title>Пневмония</title>
        <script type="application/ld+json">{"@type":"MedicalWebPage","name":"Внебольничная пневмония"}</script>
        </head><body>
        <details class="krv2-section"><summary>🔬 2. Диагностика 25 рек. ▾</summary>
          <p>Диагностические исследования направлены на верификацию диагноза.</p>
        </details>
        <details class="krv2-section"><summary>⚕️ 3. Лечение 47 рек. ▾</summary>
          <p>Лечение начинается с антибактериальной терапии.</p>
        </details>
        <ul class="krv2-rec-list"><li class="krv2-rec" data-rec-stages="ai:Диагностика,ai:Пневмония"
          data-rec-essence="dg:Жалобы и анамнез">
          <div class="krv2-rec-original">Всем пациентам с подозрением на ВП рекомендуется собрать анамнез.</div>
        </li></ul>
        </body></html>"""
    )
    assert [s["title"] for s in page.sections] == ["2. Диагностика", "3. Лечение"]
    assert page.recommendations[0].section == "Диагностика · Жалобы и анамнез"


def test_recommendations_take_original_text_not_ai_summary() -> None:
    page = parse_page(PAGE)
    texts = [r.text for r in page.recommendations]
    assert len(texts) == 2
    assert "спирометрия для подтверждения" in texts[0].lower()
    assert all("ИИ-выжимка" not in t for t in texts), "выжимки не берём"
    assert page.recommendations[0].level == "УУР A, УДД 1"
    assert page.recommendations[1].level is None


def test_empty_page_is_failed_with_reasons() -> None:
    page = parse_page("<html><body></body></html>")
    assert page.status == "failed"
    assert "разделы не найдены" in page.missing and "название не найдено" in page.missing


def test_edition_compared_with_rubricator() -> None:
    assert compare_edition("3", ["J44_2", "J44_3"]) == "current"
    assert compare_edition("2", ["J44_3"]) == "outdated"
    assert compare_edition("", ["J44_3"]) == "unknown"
    assert compare_edition("3", []) == "unknown"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
