"""Структура страниц учебника без моделей: python -m scripts.test_layout
(нужны pdfplumber и reportlab; без них проверки по настоящему PDF пропускаются)"""

import io
import os
import sys
import tempfile
import types

try:
    import torch  # noqa: F401
except ImportError:
    sys.modules.setdefault(
        "rag.embedder",
        types.SimpleNamespace(embed_query=None, embed_passages=None, limited_threads=None, _get_model=None),
    )

from rag.layout import (
    Caption,
    FigureBlock,
    Heading,
    PageLayout,
    TableBlock,
    annotate_chunks,
    split_table_text,
    Line,
    body_font_size,
    clean_table,
    detect_headings,
    keep_figure,
    needs_review,
    ocr_confidence,
    parse_caption,
    printed_page_candidate,
    reconcile_printed_pages,
    section_path,
    table_is_real,
    table_to_text,
    update_section_stack,
)


def test_printed_page_from_footer() -> None:
    assert printed_page_candidate(["Текст страницы", "ещё текст", "— 45 —"]) == 45
    assert printed_page_candidate(["12", "Текст страницы"]) == 12
    # год внутри текста номером страницы не считается
    assert printed_page_candidate(["Это произошло в 2019 году", "и далее текст"]) is None
    assert printed_page_candidate([]) is None


def test_printed_pages_follow_one_offset_and_ignore_noise() -> None:
    # страницы PDF 1-3 — титул и оглавление, нумерация начинается с 1 на 4-й; на 6-й «шум» 2019
    candidates = [None, None, None, 1, 2, 2019, 4, None, 6, 7]
    result = reconcile_printed_pages(candidates)
    assert result[:3] == [None, None, None]
    assert result[3:] == [1, 2, 3, 4, 5, 6, 7]  # шум заменён, пропуск достроен
    assert reconcile_printed_pages([None, None]) == [None, None]


def test_captions() -> None:
    cap = parse_caption("Рис. 3.2. Схема развития отёка")
    assert (cap.kind, cap.number, cap.text) == ("figure", "3.2", "Схема развития отёка")
    assert parse_caption("Таблица 5 — Классификация").kind == "table"
    assert parse_caption("Схема 1: Патогенез").kind == "scheme"
    assert parse_caption("Рисунок показывает, что") is None
    assert parse_caption("Обычный текст") is None


def test_headings_by_font_size_and_section_path() -> None:
    lines = [
        Line("Глава 3. Нарушения водного обмена", 18, 40),
        Line("Отёк", 14, 80),
        Line("Отёк — патологическое накопление жидкости в тканях организма и полостях тела.", 10, 100),
        Line("Он возникает при нарушении баланса гидростатического и онкотического давления.", 10, 112),
        Line("Рис. 3.1. Механизм отёка", 14, 200),  # подпись не заголовок
    ]
    body = body_font_size(lines)
    assert body == 10
    headings = detect_headings(lines, body)
    assert [(h.level, h.text) for h in headings] == [(1, "Глава 3. Нарушения водного обмена"), (2, "Отёк")]
    stack = update_section_stack([], headings)
    assert section_path(stack) == "Глава 3. Нарушения водного обмена → Отёк"
    # новый заголовок 2-го уровня вытесняет прежний, глава остаётся
    stack = update_section_stack(stack, [Heading(2, "Асцит")])
    assert section_path(stack) == "Глава 3. Нарушения водного обмена → Асцит"
    # новая глава сбрасывает всё
    stack = update_section_stack(stack, [Heading(1, "Глава 4")])
    assert section_path(stack) == "Глава 4"


def test_table_cleaning_and_text() -> None:
    rows = clean_table([["Вид", "Причина", None], ["Транссудат", "сердечная\nнедостаточность", None], [None, None, None]])
    assert rows == [["Вид", "Причина"], ["Транссудат", "сердечная недостаточность"]]
    assert table_is_real(rows) is True  # 2 строки, 2 столбца, 4 заполненные ячейки — минимально допустимая таблица
    assert table_to_text(rows, "Таблица 1. Отёки") == "Таблица 1. Отёки\nВид | Причина\nТранссудат | сердечная недостаточность"
    assert table_is_real([["a"], ["b"]]) is False


def test_figure_filter() -> None:
    page = (595, 842)
    assert keep_figure(100, 200, 400, 450, *page) is True
    assert keep_figure(10, 10, 60, 60, *page) is False  # значок
    assert keep_figure(50, 5, 500, 60, *page) is False  # полоса колонтитула
    assert keep_figure(100, 100, 400, 140, *page) is False  # тонкая линия


def test_ocr_confidence() -> None:
    data = {"conf": ["95", "-1", "40", "60"], "text": ["слово", "", "плохо", "норм"]}
    assert round(ocr_confidence(data), 1) == 65.0
    assert needs_review(45.0) is True and needs_review(80.0) is False and needs_review(None) is False


def test_big_table_is_split_between_rows_with_repeated_header() -> None:
    rows = [["Вид", "Причина"]] + [[f"Строка {i}", "причина " * 20] for i in range(30)]
    pieces = split_table_text(rows, "Таблица 1. Отёки", max_chars=600)
    assert len(pieces) > 1
    for piece in pieces:
        assert piece.startswith("Таблица 1. Отёки\nВид | Причина")  # шапка в каждом куске
        assert len(piece) < 900
    assert sum(p.count("Строка") for p in pieces) == 30  # ни одна строка не потеряна
    assert split_table_text([]) == []
    assert split_table_text([["a", "b"]]) == ["a | b"]


def test_annotate_chunks_adds_structure_and_tables() -> None:
    from rag.processor import Chunk

    chunks = [
        Chunk("текст про отёк", page_from=2, page_to=2),
        Chunk("текст на стыке страниц", page_from=2, page_to=3),
        Chunk("без страницы", page_from=None, page_to=None),
    ]
    table = TableBlock(rows=[["Вид", "Причина"], ["Транссудат", "сердечная недостаточность"]], caption=Caption("table", "1", "Отёки"), bbox=(0, 0, 1, 1))
    layouts = [
        PageLayout(index=1, text="", printed_page=None),
        PageLayout(index=2, text="x", printed_page=1, section_path="Глава 1 → Отёк", tables=[table], headings=[Heading(2, "Отёк")]),
        PageLayout(index=3, text="y", printed_page=2, section_path="Глава 1 → Асцит"),
    ]
    result = annotate_chunks(chunks, layouts, low_confidence_pages={3})
    assert result[0].page_printed == 1 and result[0].section_path == "Глава 1 → Отёк" and result[0].needs_review is False
    assert result[1].needs_review is True  # захватывает страницу 3 с плохим OCR
    assert result[2].page_printed is None
    tables = [c for c in result if c.kind == "table"]
    assert len(tables) == 1 and "Таблица 1. Отёки" in tables[0].content and tables[0].page_printed == 1
    assert all(c.kind == "text" for c in result[:3])  # обычные фрагменты не тронуты


def test_figure_rows_skip_repeated_decoration() -> None:
    from PIL import Image
    from scripts.load_books import _figure_rows

    def png(color):
        buffer = io.BytesIO()
        Image.new("RGB", (200, 150), color).save(buffer, format="PNG")
        return buffer.getvalue()

    logo, schema = png((10, 10, 10)), png((200, 50, 50))
    layouts = [
        PageLayout(index=i, text="x", printed_page=i, figures=[FigureBlock(bbox=(0, 0, 200, 150), caption=None, png=logo)])
        for i in range(1, 6)
    ]
    layouts[2].figures.append(FigureBlock(bbox=(0, 200, 200, 350), caption=Caption("figure", "1.1", "Механизм"), png=schema))
    rows = _figure_rows(7, 2, layouts)
    kinds = [(f.page, f.number, f.caption) for f, _ in rows]
    assert (3, "1.1", "Механизм") in kinds
    assert sum(1 for f, _ in rows if f.page != 3) == 2  # эмблема на 5 страницах остаётся не больше двух раз
    assert all(f.s3_key.startswith("figures/7/v2/") and f.processing_version == 2 for f, _ in rows)


def test_real_pdf_structure() -> None:
    try:
        from PIL import Image
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib import colors
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        print("skip test_real_pdf_structure: нет reportlab/Pillow")
        return

    font = next(
        (p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf") if os.path.exists(p)),
        None,
    )
    if font is None:
        print("skip test_real_pdf_structure: нет шрифта с кириллицей")
        return
    pdfmetrics.registerFont(TTFont("DejaVu", font))

    from rag.layout import analyze_pdf

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "book.pdf")
        c = canvas.Canvas(path, pagesize=A4)
        w, h = A4

        # страница 1 — титул без номера
        c.setFont("DejaVu", 20)
        c.drawString(80, h - 120, "Патологическая физиология")
        c.showPage()

        # страница 2 — печатный номер 1: заголовок, текст, таблица, рисунок с подписью
        c.setFont("DejaVu", 18)
        c.drawString(60, h - 70, "Глава 1. Отёк")
        c.setFont("DejaVu", 10)
        for i in range(6):
            c.drawString(60, h - 110 - i * 14, "Отёк - патологическое накопление жидкости в межклеточном пространстве тканей.")
        table = Table([["Вид", "Причина"], ["Транссудат", "сердечная недостаточность"], ["Экссудат", "воспаление"]])
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONT", (0, 0), (-1, -1), "DejaVu", 10)]))
        table.wrapOn(c, w, h)
        table.drawOn(c, 60, h - 320)
        img = Image.new("RGB", (400, 300), (200, 80, 80))
        c.drawImage(ImageReader(img), 60, h - 700, width=300, height=225)
        c.setFont("DejaVu", 10)
        c.drawString(60, h - 715, "Рис. 1.1. Механизм отёка")
        c.drawString(w / 2, 30, "1")
        c.showPage()
        c.save()

        layouts = analyze_pdf(path, with_figure_images=True)

    assert len(layouts) == 2
    assert layouts[0].printed_page is None
    second = layouts[1]
    assert second.printed_page is None or second.printed_page == 1  # одна страница с номером — сдвиг не подтверждён вторым
    assert any(hd.text.startswith("Глава 1") for hd in second.headings)
    assert second.section_path and second.section_path.startswith("Глава 1")
    assert len(second.tables) == 1 and second.tables[0].rows[1] == ["Транссудат", "сердечная недостаточность"]
    assert len(second.figures) == 1
    figure = second.figures[0]
    assert figure.caption and figure.caption.number == "1.1" and figure.caption.text == "Механизм отёка"
    assert figure.png and figure.png[:8] == b"\x89PNG\r\n\x1a\n"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
