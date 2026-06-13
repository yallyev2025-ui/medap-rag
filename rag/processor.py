"""Извлечение текста из PDF и чанкинг."""

from dataclasses import dataclass

import pdfplumber

from config import settings
from rag.embedder import _get_model

MIN_CHUNK_TOKENS = 20


@dataclass
class Chunk:
    content: str
    page_from: int
    page_to: int


def extract_pages(pdf_path: str) -> list[str]:
    with pdfplumber.open(pdf_path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def chunk_text(
    pages: list[str],
    chunk_size: int = settings.CHUNK_SIZE_TOKENS,
    overlap: int = settings.CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    tokenizer = _get_model().tokenizer

    token_ids: list[int] = []
    token_pages: list[int] = []
    for page_number, page_text in enumerate(pages, start=1):
        page_token_ids = tokenizer.encode(page_text, add_special_tokens=False)
        token_ids.extend(page_token_ids)
        token_pages.extend([page_number] * len(page_token_ids))

    chunks = []
    step = chunk_size - overlap
    for start in range(0, len(token_ids), step):
        chunk_ids = token_ids[start : start + chunk_size]
        if len(chunk_ids) < MIN_CHUNK_TOKENS:
            break
        chunk_pages = token_pages[start : start + chunk_size]
        chunks.append(
            Chunk(
                content=tokenizer.decode(chunk_ids),
                page_from=chunk_pages[0],
                page_to=chunk_pages[-1],
            )
        )
        if start + chunk_size >= len(token_ids):
            break
    return chunks
