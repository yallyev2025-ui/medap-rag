"""КР в ответе Ментора: id фрагментов, выбор рекомендации и отбор кусков.

python -m scripts.test_clinreky_answer
"""

from app.documents.clinreky_pure import (
    GuidelineCandidate,
    decode_evidence_id,
    evidence_id,
    page_fragments,
    pick_candidate,
    select_fragments,
)
from app.integrations.clinreky import ClinrekyPage, Recommendation


def test_evidence_id_roundtrip() -> None:
    for document_id, index in ((1, 0), (42, 7), (603, 112)):
        value = evidence_id(document_id, index)
        assert value < 0
        assert decode_evidence_id(value) == (document_id, index)
    assert decode_evidence_id(17) is None
    assert decode_evidence_id(-1) is None


def test_pick_candidate_by_title_words() -> None:
    candidates = [
        GuidelineCandidate(1, "Хроническая обструктивная болезнь лёгких", "J44_3", "взрослые", "Минздрав"),
        GuidelineCandidate(2, "Артериальная гипертензия у взрослых", "I10_4", "взрослые", "Минздрав"),
    ]
    assert pick_candidate("лечение ХОБЛ при обострении", candidates).book_id == 1
    assert pick_candidate("подбор терапии при гипертонии", candidates).book_id == 2
    assert pick_candidate("аппендицит", candidates) is None


def test_page_fragments_keep_original_text_and_levels() -> None:
    page = ClinrekyPage(
        url="https://clinreky.ru/nosology/J44/",
        title="ХОБЛ (КР603)",
        sections=[{"title": "Диагностика", "text": "Спирометрия обязательна."}],
        recommendations=[
            Recommendation("Рекомендуется спирометрия для подтверждения диагноза.", "УУР A, УДД 1"),
            Recommendation("коротко", None),  # короче 20 знаков — не рекомендация
        ],
    )
    fragments = page_fragments(page, document_id=9, book_id=3, subject="взрослые", author="Минздрав России")
    assert len(fragments) == 2
    assert fragments[0]["id"] == evidence_id(9, 0)
    assert fragments[0]["section"] == "Диагностика"
    assert fragments[1]["id"] == evidence_id(9, 1)
    assert "УУР A" in fragments[1]["section"]
    assert "Рекомендуется спирометрия" in fragments[1]["content"]


def test_select_fragments_puts_relevant_first_and_dedupes() -> None:
    page = ClinrekyPage(
        url="u",
        title="ХОБЛ",
        sections=[
            {"title": "Лечение", "text": "Бронходилататоры длительного действия — базисная терапия."},
            {"title": "Реабилитация", "text": "Легочная реабилитация улучшает переносимость нагрузки."},
        ],
        recommendations=[
            Recommendation("При обострении показаны ингаляционные бронходилататоры.", None),
            Recommendation("При обострении показаны ингаляционные бронходилататоры.", None),
        ],
    )
    fragments = page_fragments(page, document_id=1, book_id=1, subject="взрослые", author="Минздрав")
    picked = select_fragments(fragments, "лечение обострения ХОБЛ бронходилататоры", 3)
    assert len(picked) == 3
    assert len({item["content"] for item in picked}) == 3
    assert "бронходилататоры" in picked[0]["content"].lower()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
