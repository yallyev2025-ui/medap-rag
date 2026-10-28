"""Эмбеддинги через sentence-transformers (multilingual-e5-large)."""

from contextlib import contextmanager
from functools import lru_cache

import torch
from sentence_transformers import SentenceTransformer

from config import settings


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    # Батч 16: переменных окружения (OMP_NUM_THREADS и т.п., выставлены в
    # config.py) не всегда достаточно — если torch успел проинициализировать
    # свой пул потоков раньше, они не подхватятся. Явный вызов — вторая,
    # надёжная точка контроля; выполнится ровно один раз (lru_cache).
    torch.set_num_threads(settings.CPU_THREAD_LIMIT)
    return SentenceTransformer(settings.EMBEDDING_MODEL_NAME)


@contextmanager
def limited_threads(n: int):
    """На время загрузки источника эмбеддер берёт не больше n ядер; после — прежнее число (ответы людям не должны ждать загрузку)"""
    if n <= 0:  # 0 — без ограничения (загрузка на отдельной машине)
        yield
        return
    prev = torch.get_num_threads()
    torch.set_num_threads(max(1, n))
    try:
        yield
    finally:
        torch.set_num_threads(prev)


def embed_passages(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    prefixed = [f"passage: {text}" for text in texts]
    embeddings = model.encode(prefixed, normalize_embeddings=True)
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    model = _get_model()
    embedding = model.encode(f"query: {text}", normalize_embeddings=True)
    return embedding.tolist()
