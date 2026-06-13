"""Извлечение текста из PDF, отсев служебных страниц и чанкинг по предложениям."""

import re
from dataclasses import dataclass

import pdfplumber

from config import settings
from rag.embedder import _get_model

MIN_CHUNK_TOKENS = 20

# Точечные лидеры оглавления: "Тема ........ 42"
_DOTTED_LEADER = re.compile(r"\.\s?\.\s?\.\s?\.")
# Строка вида "...текст... 123" (заканчивается номером страницы)
_LINE_ENDS_WITH_PAGENO = re.compile(r"\S\s+\d{1,4}\s*$")
# Разбивка на предложения: после .!?… и пробела.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


@dataclass
class Chunk:
    content: str
    page_from: int
    page_to: int


def extract_pages(pdf_path: str) -> list[str]:
    with pdfplumber.open(pdf_path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


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


def _hard_split(text: str, page: int, chunk_size: int, tokenizer) -> list[Chunk]:
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
) -> list[Chunk]:
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
                page_from=min(pages_in),
                page_to=max(pages_in),
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
            chunks.extend(_hard_split(sentence, page_number, chunk_size, tokenizer))
            continue

        current_tokens = sum(s[2] for s in current)
        if current and current_tokens + token_len > chunk_size:
            emit()
            current = overlap_tail()

        current.append(item)

    emit()
    return chunks

