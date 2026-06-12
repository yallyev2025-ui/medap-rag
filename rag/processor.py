"""Извлечение текста из PDF и чанкинг."""

import pdfplumber

from config import settings
from rag.embedder import _get_model

MIN_CHUNK_TOKENS = 20


def extract_text(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages)


def chunk_text(
    text: str,
    chunk_size: int = settings.CHUNK_SIZE_TOKENS,
    overlap: int = settings.CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    tokenizer = _get_model().tokenizer
    token_ids = tokenizer.encode(text, add_special_tokens=False)

    chunks = []
    step = chunk_size - overlap
    for start in range(0, len(token_ids), step):
        chunk_ids = token_ids[start : start + chunk_size]
        if len(chunk_ids) < MIN_CHUNK_TOKENS:
            break
        chunks.append(tokenizer.decode(chunk_ids))
        if start + chunk_size >= len(token_ids):
            break
    return chunks
