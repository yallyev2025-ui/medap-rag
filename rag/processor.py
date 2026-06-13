"""Извлечение текста из учебников (PDF/Word/txt) с OCR сканов, отсев служебных
страниц и чанкинг по предложениям."""

import logging
import os
import re
from dataclasses import dataclass

import pdfplumber

from config import settings
from rag.embedder import _get_model

logger = logging.getLogger(__name__)

# OCR для сканированных PDF (страницы-картинки без текстового слоя). Зависимости
# системные (tesseract + poppler) — если их нет (локально), мягко деградируем:
# текстовые PDF/docx/txt всё равно работают, а сканы просто не распознаются.
try:
    import pytesseract
    from pdf2image import convert_from_path

    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False

# python-docx для .docx
try:
    import docx as _docx

    _DOCX_AVAILABLE = True
except ImportError:
    _DOCX_AVAILABLE = False

MIN_CHUNK_TOKENS = 20

# Ниже этого числа символов на странице считаем, что текстового слоя нет (скан) —
# и пробуем распознать страницу через OCR.
OCR_MIN_CHARS = 20
OCR_LANG = "rus+eng"
OCR_DPI = 300

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt")

# Точечные лидеры оглавления: "Тема ........ 42"
_DOTTED_LEADER = re.compile(r"\.\s?\.\s?\.\s?\.")
# Строка вида "...текст... 123" (заканчивается номером страницы)
_LINE_ENDS_WITH_PAGENO = re.compile(r"\S\s+\d{1,4}\s*$")
# Разбивка на предложения: после .!?… и пробела.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


@dataclass
class Chunk:
    content: str
    # None — если у формата нет осмысленных номеров страниц (docx, txt без разметки).
    page_from: int | None
    page_to: int | None


def _ocr_pdf_page(pdf_path: str, page_number: int) -> str:
    """Распознаёт одну страницу PDF как картинку (1-based номер). '' при ошибке."""
    images = convert_from_path(pdf_path, dpi=OCR_DPI, first_page=page_number, last_page=page_number)
    if not images:
        return ""
    return pytesseract.image_to_string(images[0], lang=OCR_LANG)


def extract_pages(pdf_path: str) -> list[str]:
    """Текст PDF по страницам. Страницы без текстового слоя (сканы) добиваются OCR."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]

    if not _OCR_AVAILABLE:
        return pages

    for i, text in enumerate(pages):
        if len(text.strip()) >= OCR_MIN_CHARS:
            continue
        try:
            recognized = _ocr_pdf_page(pdf_path, i + 1)
        except Exception:
            logger.exception("OCR страницы %d не удался", i + 1)
            continue
        if len(recognized.strip()) > len(text.strip()):
            pages[i] = recognized

    return pages


def extract_docx(docx_path: str) -> str:
    document = _docx.Document(docx_path)
    return "\n".join(p.text for p in document.paragraphs if p.text.strip())


def extract_txt(txt_path: str) -> str:
    with open(txt_path, encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_document(path: str) -> tuple[list[str], bool]:
    """Извлекает текст из файла учебника по расширению.

    Возвращает (страницы, paged), где paged=True означает, что номера страниц
    осмысленны (PDF; txt-sidecar от ocrmypdf с разделителем страниц \\f).
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        return extract_pages(path), True

    if ext == ".txt":
        raw = extract_txt(path)
        # ocrmypdf --sidecar разделяет страницы символом перевода страницы \f —
        # тогда номера страниц восстановимы.
        if "\f" in raw:
            return raw.split("\f"), True
        return [raw], False

    if ext == ".docx":
        if not _DOCX_AVAILABLE:
            raise ValueError("Поддержка .docx недоступна: не установлен python-docx")
        return [extract_docx(path)], False

    raise ValueError(f"Неподдерживаемый формат файла: {ext}")


def is_service_page(page_text: str) -> bool:
    """Эвристика: страница оглавления/индекса/списка (а не связный учебный текст).

    Такие страницы плотно совпадают с короткими запросами-терминами в эмбеддинг-
    пространстве и систематически «протаскиваются» в retrieval, провоцируя выдумки.
    Отсеиваем их до чанкинга.
    """
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return False

    dotted = sum(1 for ln in lines if _DOTTED_LEADER.search(ln))
    if dotted / len(lines) >= 0.3:
        return True

    ends_with_page = sum(1 for ln in lines if _LINE_ENDS_WITH_PAGENO.search(ln))
    if len(lines) >= 6 and ends_with_page / len(lines) >= 0.5:
        return True

    return False


def _page_sentences(page_text: str) -> list[str]:
    """Склеивает визуальные переносы строк PDF в связный текст и режет на предложения."""
    normalized = re.sub(r"\s+", " ", page_text).strip()
    if not normalized:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(normalized) if s.strip()]


def _hard_split(text: str, page: int | None, chunk_size: int, tokenizer) -> list[Chunk]:
    """Дробит сверхдлинное предложение (таблица, формула без пунктуации) по токенам."""
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    out = []
    for start in range(0, len(token_ids), chunk_size):
        piece = token_ids[start : start + chunk_size]
        if len(piece) < MIN_CHUNK_TOKENS:
            break
        out.append(Chunk(content=tokenizer.decode(piece), page_from=page, page_to=page))
    return out


def chunk_text(
    pages: list[str],
    chunk_size: int = settings.CHUNK_SIZE_TOKENS,
    overlap: int = settings.CHUNK_OVERLAP_TOKENS,
    paged: bool = True,
) -> list[Chunk]:
    """Режет текст на чанки. paged=False — у формата нет номеров страниц
    (docx, txt без разметки), у чанков page_from/page_to будут None."""
    tokenizer = _get_model().tokenizer

    # (предложение, номер страницы, число токенов) по всем НЕслужебным страницам.
    sentences: list[tuple[str, int, int]] = []
    for page_number, page_text in enumerate(pages, start=1):
        if is_service_page(page_text):
            continue
        for sentence in _page_sentences(page_text):
            token_len = len(tokenizer.encode(sentence, add_special_tokens=False))
            sentences.append((sentence, page_number, token_len))

    chunks: list[Chunk] = []
    current: list[tuple[str, int, int]] = []

    def emit() -> None:
        if not current or sum(s[2] for s in current) < MIN_CHUNK_TOKENS:
            return
        pages_in = [s[1] for s in current]
        chunks.append(
            Chunk(
                content=" ".join(s[0] for s in current),
                page_from=min(pages_in) if paged else None,
                page_to=max(pages_in) if paged else None,
            )
        )

    def overlap_tail() -> list[tuple[str, int, int]]:
        """Последние предложения текущего чанка в пределах overlap токенов — для переноса."""
        tail: list[tuple[str, int, int]] = []
        tail_tokens = 0
        for sentence in reversed(current):
            if tail_tokens + sentence[2] > overlap:
                break
            tail.insert(0, sentence)
            tail_tokens += sentence[2]
        return tail

    for sentence, page_number, token_len in sentences:
        item = (sentence, page_number, token_len)

        if token_len > chunk_size:
            emit()
            current = []
            chunks.extend(_hard_split(sentence, page_number if paged else None, chunk_size, tokenizer))
            continue

        current_tokens = sum(s[2] for s in current)
        if current and current_tokens + token_len > chunk_size:
            emit()
            current = overlap_tail()

        current.append(item)

    emit()
    return chunks

