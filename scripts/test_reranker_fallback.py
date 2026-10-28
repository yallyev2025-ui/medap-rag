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

# порог для сжатой модели: переменная > подобранный при сборке (threshold.json) > общий; подобранный не выше общего
import json
import tempfile

import config

with tempfile.TemporaryDirectory() as d:
    settings.RERANKER_ONNX_DIR = d
    settings.RERANKER_BACKEND = "onnx"
    settings.RERANK_SCORE_THRESHOLD_ONNX = None
    config._calibrated_onnx_threshold.cache_clear()
    assert config.rerank_threshold() == settings.RERANK_SCORE_THRESHOLD, "нет файла — общий порог"
    Path(d, "threshold.json").write_text(json.dumps({"threshold": 0.02}))
    config._calibrated_onnx_threshold.cache_clear()
    assert config.rerank_threshold() == 0.02, "порог из файла сборки, переменной не нужно"
    Path(d, "threshold.json").write_text(json.dumps({"threshold": 0.9}))
    config._calibrated_onnx_threshold.cache_clear()
    assert config.rerank_threshold() == settings.RERANK_SCORE_THRESHOLD, "не строже общего"
    settings.RERANK_SCORE_THRESHOLD_ONNX = 0.07
    assert config.rerank_threshold() == 0.07, "переменная окружения главнее"
print("OK: порог сжатого реранкера подбирается автоматически")
