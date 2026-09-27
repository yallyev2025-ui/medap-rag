"""Эмбеддинги через sentence-transformers (multilingual-e5-large)."""

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


def embed_passages(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    prefixed = [f"passage: {text}" for text in texts]
    embeddings = model.encode(prefixed, normalize_embeddings=True)
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    model = _get_model()
    embedding = model.encode(f"query: {text}", normalize_embeddings=True)
    return embedding.tolist()
