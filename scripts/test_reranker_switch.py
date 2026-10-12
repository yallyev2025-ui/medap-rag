"""Проверка переключателя реранкера без моделей: python -m scripts.test_reranker_switch"""

import logging

import rag.reranker as reranker
from config import rerank_threshold, settings


ORIGINAL = {name: getattr(reranker, name) for name in ("_torch_scores", "_onnx_scores", "_get_onnx")}


def reset(backend: str) -> None:
    """Состояние модуля между проверками: настоящие функции на месте, ONNX не помечен сломанным."""
    for name, fn in ORIGINAL.items():
        setattr(reranker, name, fn)
    settings.RERANKER_BACKEND = backend
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
