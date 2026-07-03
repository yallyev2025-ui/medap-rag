"""Лёгкая функция для процессов-воркеров: извлечение текста из PDF без torch.

Вынесено в отдельный модуль намеренно. Процесс-воркер (spawn) при старте
импортирует модуль, где определена вызываемая функция. Если бы это был
scripts.load_clinreks, воркер тянул бы torch (через rag.embedder) и подключение
к БД — по гигабайту памяти на каждый воркер. Здесь же импортируется только
rag.processor (pdfplumber), поэтому воркеры лёгкие.
"""

from rag.processor import extract_document


def extract_worker(path_str: str) -> tuple[list[str], bool]:
    return extract_document(path_str)
