"""Разбор цитат вида «[Автор, Название, стр. N(-M)]» из текста ответа и их
сопоставление с реально найденными чанками (§13 ТЗ).

В citations должны попадать только источники, которые модель ДЕЙСТВИТЕЛЬНО
указала в ответе, а не все «релевантные» чанки, которые были в контексте, но не
факт что использованы (модель уже обязана указывать источник ровно в этом
формате — см. rag/generator.py:_format_source и системные промпты).
"""

import re

from rag.retriever import ChunkResult

_BRACKET = re.compile(r"\[([^\[\]]+)\]")
_PAGE = re.compile(r"стр\.?\s*(\d+)\s*(?:[-–—]\s*(\d+))?", re.IGNORECASE)


def _matches(bracket_text: str, chunk: ChunkResult) -> bool:
    text = bracket_text.lower()
    if chunk.title.lower() not in text:
        return False
    if chunk.page_from is None:
        # Источник без пагинации (docx/txt) — совпадения по названию достаточно.
        return True

    page_match = _PAGE.search(bracket_text)
    if page_match is None:
        return True

    p1 = int(page_match.group(1))
    p2 = int(page_match.group(2)) if page_match.group(2) else p1
    chunk_to = chunk.page_to if chunk.page_to is not None else chunk.page_from
    # Пересечение диапазона страниц в скобке с диапазоном страниц чанка.
    return p1 <= chunk_to and chunk.page_from <= p2


def extract_cited_chunks(answer: str, chunks: list[ChunkResult]) -> list[ChunkResult]:
    """Чанки, реально процитированные в тексте ответа, в порядке первого
    упоминания. Если разбор ничего не нашёл (модель не указала источник или
    указала то, что не совпало ни с одним чанком) — возвращаем пустой список,
    а не все переданные чанки: приписывать источник claim'у только потому, что
    он в принципе был в контексте — запрещённый "citation laundering"."""
    brackets = _BRACKET.findall(answer)
    if not brackets:
        return []

    cited: list[ChunkResult] = []
    seen_ids: set[int] = set()
    for bracket_text in brackets:
        for chunk in chunks:
            if chunk.id in seen_ids:
                continue
            if _matches(bracket_text, chunk):
                cited.append(chunk)
                seen_ids.add(chunk.id)

    return cited
