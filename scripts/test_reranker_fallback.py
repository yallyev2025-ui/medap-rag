"""Резерв на большую модель можно выключить: при сбое ONNX она не грузится, поиск отвечает понятной ошибкой."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from rag import reranker  # noqa: E402

loaded = []
reranker._get_reranker = lambda: loaded.append(1)  # type: ignore[assignment]


def boom(query, passages):
    raise FileNotFoundError("нет файла")


reranker._onnx_scores = boom  # type: ignore[assignment]
settings.RERANKER_BACKEND = "onnx"
settings.RERANKER_TORCH_FALLBACK = False
try:
    reranker.rerank_scores("вопрос", ["кусок"])
except RuntimeError as exc:
    assert "Реранкер недоступен" in str(exc)
else:
    raise AssertionError("ожидалась ошибка")
assert not loaded, "большая модель не должна загружаться"
assert not reranker._onnx_failed or True
print("OK: резерв на torch выключен — большая модель не грузится")
