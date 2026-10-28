"""Проверка переключателя реранкера без моделей: python -m scripts.test_reranker_switch"""

import logging

import rag.reranker as reranker
from config import rerank_threshold, settings


ORIGINAL = {name: getattr(reranker, name) for name in ("_torch_scores", "_onnx_scores", "_get_onnx", "_get_reranker")}


def reset(backend: str) -> None:
    """Состояние модуля между проверками: настоящие функции на месте, ONNX не помечен сломанным."""
    for name, fn in ORIGINAL.items():
        setattr(reranker, name, fn)
    settings.RERANKER_BACKEND = backend
    settings.RERANKER_TORCH_FALLBACK = True  # эти проверки про откат; запрет отката проверяет test_reranker_fallback
    settings.RERANKER_ONNX_DIR = "/nonexistent"  # без файла порога, подобранного при сборке
    import config

    config._calibrated_onnx_threshold.cache_clear()
    reranker._onnx_failed = False


def test_torch_is_default_and_used() -> None:
    reset("torch")
    reranker._torch_scores = lambda q, p: [0.1] * len(p)
    reranker._onnx_scores = lambda q, p: (_ for _ in ()).throw(AssertionError("onnx не должен вызываться"))
    assert reranker.rerank_scores("q", ["a", "b"]) == [0.1, 0.1]
    assert reranker.active_backend() == "torch"


def test_onnx_used_when_enabled() -> None:
    reset("onnx")
    reranker._torch_scores = lambda q, p: (_ for _ in ()).throw(AssertionError("torch не должен вызываться"))
    reranker._onnx_scores = lambda q, p: [0.9] * len(p)
    assert reranker.rerank_scores("q", ["a"]) == [0.9]
    assert reranker.active_backend() == "onnx"


def test_onnx_failure_falls_back_to_torch_once() -> None:
    reset("onnx")
    calls = {"onnx": 0}

    def broken(q, p):
        calls["onnx"] += 1
        raise FileNotFoundError("нет файла")

    reranker._onnx_scores = broken
    reranker._torch_scores = lambda q, p: [0.5] * len(p)
    logging.disable(logging.CRITICAL)
    try:
        assert reranker.rerank_scores("q", ["a"]) == [0.5]
        assert reranker.rerank_scores("q", ["a"]) == [0.5]
    finally:
        logging.disable(logging.NOTSET)
    # Сломанный ONNX больше не пробуем — иначе каждый вопрос платил бы за ошибку
    assert calls["onnx"] == 1
    assert reranker.active_backend() == "torch"


def test_empty_input() -> None:
    reset("onnx")
    assert reranker.rerank_scores("q", []) == []


def test_threshold_per_backend() -> None:
    settings.RERANK_SCORE_THRESHOLD = 0.3
    settings.RERANK_SCORE_THRESHOLD_ONNX = None
    reset("onnx")
    assert rerank_threshold() == 0.3  # свой порог не задан — общий
    settings.RERANK_SCORE_THRESHOLD_ONNX = 0.25
    assert rerank_threshold() == 0.25
    reset("torch")
    assert rerank_threshold() == 0.3  # на torch свой порог onnx не действует


def test_onnx_scores_keep_original_order_after_length_sorting() -> None:
    """Куски считаются по длине, а результат обязан вернуться в исходном порядке."""
    import numpy as np

    class FakeTokenizer:
        def __call__(self, queries, passages, **kwargs):
            # «токен» на каждый символ: длина куска → единственный признак для фальшивой модели
            lengths = np.array([[len(p)] for p in passages], dtype=np.float32)
            return {"input_ids": lengths, "unused": lengths}

    class FakeInput:
        name = "input_ids"

    class FakeSession:
        def get_inputs(self):
            return [FakeInput()]

        def run(self, _outputs, feed):
            # логит = длина куска: больше кусок — больше оценка
            return [feed["input_ids"]]

    reset("onnx")
    reranker._get_onnx = lambda: (FakeSession(), FakeTokenizer())
    settings.RERANK_BATCH_SIZE = 2

    passages = ["ccc", "a", "bbbbbb", "dd", "eeeeeeee"]
    scores = reranker._onnx_scores("q", passages)

    # Оценка растёт вместе с длиной куска, и i-му куску соответствует i-я оценка
    assert scores == [reranker._sigmoid(float(len(p))) for p in passages]


def test_sigmoid_is_stable_and_symmetric() -> None:
    # Прежняя форма 1/(1+exp(-x)) падала на очень отрицательных x
    assert reranker._sigmoid(-5000.0) == 0.0 or reranker._sigmoid(-5000.0) < 1e-300
    assert reranker._sigmoid(5000.0) == 1.0
    assert abs(reranker._sigmoid(0.0) - 0.5) < 1e-12
    assert abs(reranker._sigmoid(2.0) + reranker._sigmoid(-2.0) - 1.0) < 1e-12


def test_nan_from_onnx_falls_back_to_torch() -> None:
    """Испорченная модель, отдающая NaN, не должна молча превращать все куски в «нерелевантные»."""
    import numpy as np

    class Tokenizer:
        def __call__(self, queries, passages, **kwargs):
            return {"input_ids": np.zeros((len(passages), 1), dtype=np.float32)}

    class Input:
        name = "input_ids"

    class NanSession:
        def get_inputs(self):
            return [Input()]

        def run(self, _outputs, feed):
            return [np.full((feed["input_ids"].shape[0], 1), np.nan, dtype=np.float32)]

    reset("onnx")
    reranker._get_onnx = lambda: (NanSession(), Tokenizer())
    reranker._torch_scores = lambda q, p: [0.42] * len(p)
    logging.disable(logging.CRITICAL)
    try:
        assert reranker.rerank_scores("q", ["a", "b"]) == [0.42, 0.42]
    finally:
        logging.disable(logging.NOTSET)
    assert reranker.active_backend() == "torch"


def test_torch_scores_are_single_sigmoid_not_double() -> None:
    """Регрессия 13.10.2026: predict уже применяет сигмоиду, вторая сжимала оценки в 0.50–0.73."""
    import math
    import sys
    import types

    reset("torch")  # настоящая _torch_scores, а не подделка предыдущего теста

    class Identity:
        pass

    fake_torch = types.SimpleNamespace(nn=types.SimpleNamespace(Identity=Identity))
    saved = sys.modules.get("torch")
    sys.modules["torch"] = fake_torch  # type: ignore[assignment]
    try:
        logits = [-6.0, 0.0, 4.0]

        class NewModel:
            # как sentence-transformers ≥ 4: по умолчанию сигмоида, с Identity — сырые логиты
            def predict(self, pairs, batch_size=None, activation_fn=None):
                if isinstance(activation_fn, Identity):
                    return logits
                return [1 / (1 + math.exp(-x)) for x in logits]

        class OldModel:
            # как версии, где параметр называется activation_fct
            def predict(self, pairs, batch_size=None, activation_fct=None):
                return logits if isinstance(activation_fct, Identity) else [1 / (1 + math.exp(-x)) for x in logits]

        class NoActivationModel:
            # параметра нет, возвращает уже вероятности
            def predict(self, pairs, batch_size=None):
                return [1 / (1 + math.exp(-x)) for x in logits]

        for model in (NewModel(), OldModel(), NoActivationModel()):
            reranker._get_reranker = lambda m=model: m  # type: ignore[assignment]
            scores = reranker._torch_scores("q", ["a", "b", "c"])
            expected = [1 / (1 + math.exp(-x)) for x in logits]
            assert all(abs(a - b) < 1e-9 for a, b in zip(scores, expected)), scores
            # настоящий разброс: посторонний кусок почти 0, хороший почти 1 (раньше было 0.50 и 0.73)
            assert scores[0] < 0.01 and scores[2] > 0.98
    finally:
        if saved is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
