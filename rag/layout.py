"""Структура страниц учебника: печатные номера страниц, заголовки по размеру шрифта,
таблицы со строками и столбцами, рисунки с подписями (MedAP, этап 1, часть Б).

Модуль не трогает действующий разбор (`rag/processor.py`): тот продолжает отвечать на
поиск как раньше. Этот слой строит рядом новую версию обработки книги
(`processing_version`), переключение — решение владельца по отчёту.

Не импортирует torch и эмбеддер: работает в процессах-воркерах разбора PDF.
Чистые функции (без PDF) вынесены отдельно, чтобы проверяться без файлов.
"""

from __future__ import annotations

import io
import re
import statistics
from dataclasses import dataclass, field

# --- Печатные номера страниц ---------------------------------------------------

# Строка колонтитула, состоящая только из номера: "45", "— 45 —", "- 45 -", "45."
_PAGE_NUMBER_LINE = re.compile(r"^[\s\-–—.]*([0-9]{1,4})[\s\-–—.]*$")
# Допуск: номер отличается от индекса страницы PDF не более чем на столько
# (титул, оглавление и вклейки сдвигают нумерацию, но не на сотни страниц)
MAX_PRINTED_OFFSET = 80


def printed_page_candidate(lines: list[str]) -> int | None:
    """Номер страницы из колонтитула: ищем строку-цифру среди первых и последних двух
    непустых строк страницы. Номер внутри текста ("в 2019 году") не считается."""
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    if not nonempty:
        return None
    for line in nonempty[:2] + nonempty[-2:]:
        match = _PAGE_NUMBER_LINE.match(line)
        if match:
            return int(match.group(1))
    return None


def reconcile_printed_pages(candidates: list[int | None]) -> list[int | None]:
    """Печатные номера по всем страницам книги.

    Кандидаты шумят (год в колонтитуле, номер таблицы). Нумерация учебника идёт с
    постоянным сдвигом относительно индекса PDF, поэтому берём самый частый сдвиг и
    оставляем только кандидатов с этим сдвигом; пропущенные страницы внутри
    пронумерованной части достраиваем. До первой пронумерованной страницы
    (титул, оглавление) номера нет — `None`.
    """
    offsets = [
        index - number
        for index, number in enumerate(candidates, start=1)
        if number is not None and abs(index - number) <= MAX_PRINTED_OFFSET
    ]
    if not offsets:
        return [None] * len(candidates)

    offset = statistics.mode(offsets)
    confirmed = [
        index
        for index, number in enumerate(candidates, start=1)
        if number is not None and index - number == offset
    ]
    first, last = min(confirmed), max(confirmed)

    result: list[int | None] = []
    for index in range(1, len(candidates) + 1):
        value = index - offset
        result.append(value if first <= index <= last and value >= 1 else None)
    return result


# --- Подписи рисунков и таблиц -------------------------------------------------

_CAPTION = re.compile(
    r"^\s*(?P<kind>рис(?:унок|\.)?|табл(?:ица|\.)?|схема)\s*(?P<num>\d+(?:[.\-–]\d+)*)\s*[.:\-–—]?\s*(?P<text>.*)$",
    re.IGNORECASE,
)


@dataclass
class Caption:
    kind: str  # figure | table | scheme
    number: str
    text: str

    @property
    def kind_label(self) -> str:
        return {"figure": "Рис.", "table": "Таблица", "scheme": "Схема"}[self.kind]


def parse_caption(line: str) -> Caption | None:
    """«Рис. 3.2. Схема развития отёка» → Caption(figure, '3.2', 'Схема развития отёка')."""
    match = _CAPTION.match(line)
    if not match:
        return None
    raw = match.group("kind").lower()
    kind = "figure" if raw.startswith("рис") else "table" if raw.startswith("табл") else "scheme"
    return Caption(kind=kind, number=match.group("num").replace("–", "-"), text=match.group("text").strip())


# --- Заголовки по размеру шрифта -----------------------------------------------

HEADING_MIN_RATIO = 1.12  # заголовок крупнее основного текста минимум на 12 %
HEADING_MAX_CHARS = 140
MAX_HEADING_LEVELS = 3


@dataclass
class Line:
    text: str
    size: float  # средний размер шрифта строки
    top: float


@dataclass
class Heading:
    level: int
    text: str


def body_font_size(lines: list[Line]) -> float | None:
    """Размер основного текста: наиболее частый размер, взвешенный числом знаков."""
    weights: dict[float, int] = {}
    for line in lines:
        key = round(line.size * 2) / 2
        weights[key] = weights.get(key, 0) + len(line.text)
    return max(weights, key=weights.get) if weights else None


def detect_headings(lines: list[Line], body_size: float | None) -> list[Heading]:
    """Строки заметно крупнее основного текста, короткие и без точки в конце — заголовки.
    Уровень — по убыванию размера (самый крупный — 1)."""
    if not body_size:
        return []
    candidates = [
        line
        for line in lines
        if line.size >= body_size * HEADING_MIN_RATIO
        and 2 <= len(line.text) <= HEADING_MAX_CHARS
        and line.text[-1] not in ".,;:"
        and parse_caption(line.text) is None
    ]
    sizes = sorted({round(line.size * 2) / 2 for line in candidates}, reverse=True)[:MAX_HEADING_LEVELS]
    level_of = {size: level for level, size in enumerate(sizes, start=1)}
    return [
        Heading(level=level_of[round(line.size * 2) / 2], text=line.text)
        for line in candidates
        if round(line.size * 2) / 2 in level_of
    ]


def update_section_stack(stack: list[Heading], found: list[Heading]) -> list[Heading]:
    """Путь «Глава → раздел → подраздел»: новый заголовок уровня N вытесняет всё, что
    на уровне N и глубже."""
    for heading in found:
        stack = [h for h in stack if h.level < heading.level]
        stack.append(heading)
    return stack


def section_path(stack: list[Heading]) -> str | None:
    return " → ".join(h.text for h in stack) or None


# --- Сноски и формулы ------------------------------------------------------------

FOOTNOTE_ZONE_SHARE = 0.80  # сноски стоят в нижних 20 % страницы
FOOTNOTE_SIZE_RATIO = 0.90  # и набраны мельче основного текста
_FOOTNOTE_MARK = re.compile(r"^\s*(?:\d{1,2}|[*†‡§¹²³⁴⁵⁶⁷⁸⁹])\s*[).:]?\s+\S")

_MATH_CHARS = set("=+−×÷±≈≠≤≥<>∑∫√∞∂∆∇^_")
_GREEK = set("αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΘΛΞΠΣΦΨΩ")
FORMULA_MAX_CHARS = 160


def detect_footnotes(lines: list[Line], body_size: float | None, page_height: float) -> list[str]:
    """Сноски: строки в нижней зоне страницы, мельче основного текста, начинающиеся с номера или
    значка (`1 Текст`, `* Текст`). Продолжение сноски (строка мельче без значка сразу под ней) приклеивается."""
    if not body_size:
        return []
    notes: list[str] = []
    for line in lines:
        small = line.size <= body_size * FOOTNOTE_SIZE_RATIO
        in_zone = line.top >= page_height * FOOTNOTE_ZONE_SHARE
        if not (small and in_zone):
            continue
        if _FOOTNOTE_MARK.match(line.text):
            notes.append(line.text)
        elif notes:
            notes[-1] = f"{notes[-1]} {line.text}"
    return notes


def is_formula_line(text: str) -> bool:
    """Короткая строка, где много математических знаков и мало слов: `pH = pK + lg([A⁻]/[HA])`.
    Обычное предложение со знаком «=» формулой не считается."""
    stripped = text.strip()
    if not stripped or len(stripped) > FORMULA_MAX_CHARS:
        return False
    symbols = sum(1 for ch in stripped if ch in _MATH_CHARS or ch in _GREEK)
    words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", stripped)]
    has_equals = "=" in stripped
    return symbols >= 2 and len(words) <= 3 and (has_equals or symbols >= 3)


def collect_formulas(lines: list[Line]) -> list[str]:
    """Формулы страницы: подряд идущие формульные строки склеиваются в одну."""
    formulas: list[str] = []
    previous_was_formula = False
    for line in lines:
        if is_formula_line(line.text):
            if previous_was_formula:
                formulas[-1] = f"{formulas[-1]} {line.text.strip()}"
            else:
                formulas.append(line.text.strip())
            previous_was_formula = True
        else:
            previous_was_formula = False
    return formulas


# --- Таблицы ------------------------------------------------------------------


def clean_table(rows: list[list[str | None]]) -> list[list[str]]:
    """Убирает пустые строки и столбцы, склеивает переносы внутри ячеек."""
    cleaned = [[re.sub(r"\s+", " ", (cell or "")).strip() for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    keep = [i for i in range(width) if any(row[i] for row in cleaned)]
    return [[row[i] for i in keep] for row in cleaned]


def table_is_real(rows: list[list[str]]) -> bool:
    """Отсев ложных таблиц (рамки, колонтитулы): минимум 2 строки и 2 столбца с текстом."""
    return len(rows) >= 2 and len(rows[0]) >= 2 and sum(1 for row in rows for c in row if c) >= 4


def table_to_text(rows: list[list[str]], caption: str | None = None) -> str:
    """Текст таблицы для поиска и для модели: строки через перевод строки, ячейки через « | »."""
    body = "\n".join(" | ".join(row) for row in rows)
    return f"{caption}\n{body}" if caption else body


TABLE_PIECE_MAX_CHARS = 1400  # окно эмбеддера ~512 токенов; большая таблица режется по строкам


def split_table_text(rows: list[list[str]], caption: str | None = None, max_chars: int = TABLE_PIECE_MAX_CHARS) -> list[str]:
    """Таблица кусками для поиска. Режем только между строками, шапка повторяется в каждом
    куске, чтобы фрагмент без шапки не терял смысл столбцов. Одна сверхдлинная строка остаётся целой."""
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    head_text = ((caption + "\n") if caption else "") + " | ".join(header)
    if not body:
        return [head_text]
    pieces: list[str] = []
    current: list[str] = []
    size = len(head_text)
    for row in body:
        line = " | ".join(row)
        if current and size + len(line) + 1 > max_chars:
            pieces.append(head_text + "\n" + "\n".join(current))
            current, size = [], len(head_text)
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append(head_text + "\n" + "\n".join(current))
    return pieces


def annotate_chunks(chunks: list, layouts: list["PageLayout"], low_confidence_pages: set[int] | None = None) -> list:
    """Дополняет фрагменты текста структурой (печатная страница, путь раздела, «плохо читается»)
    и добавляет фрагменты-таблицы. Возвращает новый список: сначала текст, затем таблицы."""
    from rag.processor import Chunk  # ленивый импорт: processor не знает про layout

    low = low_confidence_pages or set()
    by_index = {layout.index: layout for layout in layouts}

    for chunk in chunks:
        if chunk.page_from is None:
            continue
        layout = by_index.get(chunk.page_from)
        if layout is not None:
            chunk.page_printed = layout.printed_page
            chunk.section_path = layout.section_path
        last = chunk.page_to if chunk.page_to is not None else chunk.page_from
        chunk.needs_review = any(page in low for page in range(chunk.page_from, last + 1))

    tables = []
    for layout in layouts:
        for table in layout.tables:
            caption = None
            if table.caption:
                caption = f"{table.caption.kind_label} {table.caption.number}. {table.caption.text}".strip()
            for piece in split_table_text(table.rows, caption):
                tables.append(
                    Chunk(
                        content=piece,
                        page_from=layout.index,
                        page_to=layout.index,
                        section=layout.headings[-1].text if layout.headings else None,
                        kind="table",
                        page_printed=layout.printed_page,
                        section_path=layout.section_path,
                        needs_review=layout.index in low,
                    )
                )
    extras = []
    for layout in layouts:
        for kind, items in (("footnote", layout.footnotes), ("formula", layout.formulas)):
            for text in items:
                if len(text) < 6:
                    continue
                extras.append(
                    Chunk(
                        content=text,
                        page_from=layout.index,
                        page_to=layout.index,
                        section=layout.headings[-1].text if layout.headings else None,
                        kind=kind,
                        page_printed=layout.printed_page,
                        section_path=layout.section_path,
                        needs_review=layout.index in low,
                    )
                )
    return list(chunks) + tables + extras


def ocr_page_confidences(pdf_path: str, page_numbers: list[int], dpi: int = 150) -> dict[int, float]:
    """Средняя уверенность Tesseract по страницам-сканам (1-based). Без OCR в окружении — пусто.
    Отдельный проход на меньшем разрешении: действующий OCR (`processor._ocr_pdf_pages`) не трогаем."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        return {}
    result: dict[int, float] = {}
    for page in page_numbers:
        try:
            image = convert_from_path(pdf_path, dpi=dpi, first_page=page, last_page=page)[0]
            data = pytesseract.image_to_data(image, lang="rus+eng", output_type=pytesseract.Output.DICT)
        except Exception:
            continue
        confidence = ocr_confidence(data)
        if confidence is not None:
            result[page] = confidence
    return result


# --- Рисунки ------------------------------------------------------------------

MIN_FIGURE_SIDE_PT = 80  # меньше — значок или логотип
MIN_FIGURE_AREA_SHARE = 0.015  # меньше 1,5 % площади страницы — мусор
EDGE_BAND_SHARE = 0.06  # полоса у верхнего/нижнего края, где живут колонтитулы


def keep_figure(x0: float, top: float, x1: float, bottom: float, page_w: float, page_h: float) -> bool:
    """Отсев мусорных картинок: значки, логотипы, линии, колонтитулы."""
    width, height = x1 - x0, bottom - top
    if width < MIN_FIGURE_SIDE_PT or height < MIN_FIGURE_SIDE_PT:
        return False
    if width * height < page_w * page_h * MIN_FIGURE_AREA_SHARE:
        return False
    in_edge_band = top < page_h * EDGE_BAND_SHARE or bottom > page_h * (1 - EDGE_BAND_SHARE)
    if in_edge_band and height < page_h * 0.15:
        return False
    return True


# --- Уверенность OCR ----------------------------------------------------------

NEEDS_REVIEW_CONFIDENCE = 60.0


def ocr_confidence(data: dict) -> float | None:
    """Средняя уверенность Tesseract по словам из `image_to_data(..., output_type=DICT)`;
    служебные значения (-1) пропускаются. `None` — слов нет."""
    confs = [float(c) for c, t in zip(data.get("conf", []), data.get("text", [])) if str(t).strip() and float(c) >= 0]
    return sum(confs) / len(confs) if confs else None


def needs_review(confidence: float | None) -> bool:
    return confidence is not None and confidence < NEEDS_REVIEW_CONFIDENCE


# --- Разбор PDF ---------------------------------------------------------------


@dataclass
class TableBlock:
    rows: list[list[str]]
    caption: Caption | None
    bbox: tuple[float, float, float, float]

    @property
    def text(self) -> str:
        caption = f"{self.caption.kind_label} {self.caption.number}. {self.caption.text}".strip() if self.caption else None
        return table_to_text(self.rows, caption)


@dataclass
class FigureBlock:
    bbox: tuple[float, float, float, float]
    caption: Caption | None
    png: bytes | None = None  # заполняется, если запрошена вырезка


@dataclass
class PageLayout:
    index: int  # 1-based индекс страницы PDF
    text: str
    printed_page: int | None = None
    headings: list[Heading] = field(default_factory=list)
    section_path: str | None = None
    tables: list[TableBlock] = field(default_factory=list)
    figures: list[FigureBlock] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    formulas: list[str] = field(default_factory=list)


def _group_lines(words: list[dict]) -> list[Line]:
    """Слова pdfplumber (с `size`) → строки с текстом, средним размером и позицией."""
    rows: dict[int, list[dict]] = {}
    for word in words:
        rows.setdefault(round(word["top"] / 3), []).append(word)
    lines = []
    for _, row in sorted(rows.items()):
        row.sort(key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in row).strip()
        if text:
            lines.append(Line(text=text, size=statistics.mean(w["size"] for w in row), top=row[0]["top"]))
    return lines


def _caption_near(lines: list[Line], bbox: tuple[float, float, float, float], max_gap: float = 70.0) -> Caption | None:
    """Подпись к объекту: ближайшая строка-подпись под ним, иначе над ним (в пределах max_gap pt)."""
    _, top, _, bottom = bbox
    below = [(line.top - bottom, line) for line in lines if 0 <= line.top - bottom <= max_gap]
    above = [(top - line.top, line) for line in lines if 0 < top - line.top <= max_gap]
    for _, line in sorted(below, key=lambda t: t[0]) + sorted(above, key=lambda t: t[0]):
        caption = parse_caption(line.text)
        if caption:
            return caption
    return None


def analyze_pdf(pdf_path: str, with_figure_images: bool = False, figure_dpi: int = 150) -> list[PageLayout]:
    """Структура всех страниц PDF. Текст страниц здесь только для привязки (номера, заголовки);
    полный текст для поиска по-прежнему берёт `processor.extract_pages` (с OCR)."""
    import pdfplumber  # импорт здесь: модуль остаётся лёгким для чистых функций

    pages: list[PageLayout] = []
    candidates: list[int | None] = []
    stack: list[Heading] = []

    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            words = page.extract_words(extra_attrs=["size"], keep_blank_chars=False, use_text_flow=False)
            lines = _group_lines(words)

            candidates.append(printed_page_candidate([line.text for line in lines]))

            body_size = body_font_size(lines)
            headings = detect_headings(lines, body_size)
            stack = update_section_stack(stack, headings)

            tables: list[TableBlock] = []
            for table in page.find_tables():
                rows = clean_table(table.extract())
                if table_is_real(rows):
                    tables.append(TableBlock(rows=rows, caption=_caption_near(lines, table.bbox), bbox=table.bbox))

            figures: list[FigureBlock] = []
            for image in page.images:
                bbox = (image["x0"], image["top"], image["x1"], image["bottom"])
                if keep_figure(*bbox, page.width, page.height):
                    block = FigureBlock(bbox=bbox, caption=_caption_near(lines, bbox))
                    if with_figure_images:
                        block.png = render_region(page, bbox, figure_dpi)
                    figures.append(block)

            pages.append(
                PageLayout(
                    index=index,
                    text=text,
                    headings=headings,
                    section_path=section_path(stack),
                    tables=tables,
                    figures=figures,
                    footnotes=detect_footnotes(lines, body_size, page.height),
                    formulas=collect_formulas(lines),
                )
            )
            page.flush_cache()

    for layout, printed in zip(pages, reconcile_printed_pages(candidates)):
        layout.printed_page = printed
    return pages


def render_region(page, bbox: tuple[float, float, float, float], dpi: int = 150) -> bytes:
    """Вырезка области страницы PNG-байтами (рисунок, схема или фрагмент страницы)."""
    x0, top, x1, bottom = bbox
    pad = 4
    box = (max(0, x0 - pad), max(0, top - pad), min(page.width, x1 + pad), min(page.height, bottom + pad))
    image = page.crop(box).to_image(resolution=dpi).original
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
