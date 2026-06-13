"""Реранкер: cross-encoder переупорядочивает кандидатов retrieval и даёт честный
скор релевантности 0..1. Bi-encoder (e5) ищет грубо и «широко», cross-encoder
смотрит на пару (вопрос, фрагмент) целиком — это и точнее по порядку, и даёт
надёжный сигнал «релевантно/нет» для логики отказа.
"""

import math
from functools import lru_cache

from sentence_transformers import CrossEncoder

from config import settings


@lru_cache(maxsize=1)
def _get_reranker() -> CrossEncoder:
    return CrossEncoder(settings.RERANKER_MODEL_NAME)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def rerank_scores(query: str, passages: list[str]) -> list[float]:
    """Релевантность каждого passage к query в диапазоне 0..1 (sigmoid от логита)."""
    if not passages:
        return []
    model = _get_reranker()
    pairs = [(query, passage) for passage in passages]
    raw_scores = model.predict(pairs)
    return [_sigmoid(float(score)) for score in raw_scores]
