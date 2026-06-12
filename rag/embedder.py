"""Эмбеддинги через sentence-transformers (multilingual-e5-large)."""

from functools import lru_cache

from sentence_transformers import SentenceTransformer

from config import settings


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
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
