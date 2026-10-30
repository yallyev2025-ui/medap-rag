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

            headings = detect_headings(lines, body_font_size(lines))
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
