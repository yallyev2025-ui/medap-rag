"""Реранкер: cross-encoder переупорядочивает кандидатов retrieval и даёт честный
скор релевантности 0..1. Bi-encoder (e5) ищет грубо и «широко», cross-encoder
смотрит на пару (вопрос, фрагмент) целиком — это и точнее по порядку, и даёт
надёжный сигнал «релевантно/нет» для логики отказа.

Два способа считать ОДНУ И ТУ ЖЕ модель (bge-reranker-v2-m3) — переключатель
`RERANKER_BACKEND` (12.10.2026):
  — "torch" (по умолчанию): sentence-transformers, 32-битные веса, как было;
  — "onnx": та же модель, сжатая в int8 и запущенная через onnxruntime — быстрее на
    процессоре и вчетверо меньше по памяти (см. scripts/export_reranker_onnx.py).
Откат на прежний путь — вернуть RERANKER_BACKEND=torch и перезапустить. Если ONNX не
загрузился или упал, бот сам продолжает на torch и пишет причину в лог: сломанное
сжатие не должно оставлять поиск без реранкера.
"""

import logging
import math
import os
import threading
from functools import lru_cache

from config import settings

logger = logging.getLogger(__name__)

# ONNX сломан (нет файлов, не загрузился) — больше не пробуем и работаем на torch
_onnx_failed = False
_onnx_lock = threading.Lock()


@lru_cache(maxsize=1)
def _get_reranker():
    # Импорт здесь, а не наверху модуля: при ONNX-пути torch и sentence-transformers
    # могли бы вообще не понадобиться, и модуль должен импортироваться без них
    from sentence_transformers import CrossEncoder

    return CrossEncoder(settings.RERANKER_MODEL_NAME)


def _sigmoid(x: float) -> float:
    # Устойчивая форма: при очень отрицательном x обычная exp(-x) переполняется
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _torch_scores(query: str, passages: list[str]) -> list[float]:
    """Оценки 0..1: сигмоида от сырого логита модели — ровно один раз.

    ВАЖНО (замер 13.10.2026). В sentence-transformers у модели с одним выходом (bge-reranker) `predict` уже сам
    применяет сигмоиду. Прежний код брал сигмоиду от этого результата ещё раз, и все оценки сжимались в диапазон
    0.50–0.73: «борщ» получал те же 0.50, что и мусор, лучший настоящий кусок не поднимался выше 0.73, а порог
    релевантности 0.3 не отсекал вообще ничего — «подходящими» были всегда все куски. Здесь у `predict` просим сырые
    логиты (тождественная активация) и берём сигмоиду сами."""
    import torch

    model = _get_reranker()
    pairs = [(query, passage) for passage in passages]
    batch = settings.RERANK_BATCH_SIZE
    identity = torch.nn.Identity()

    # Имя параметра менялось между версиями sentence-transformers (activation_fn / activation_fct)
    for name in ("activation_fn", "activation_fct"):
        try:
            raw = model.predict(pairs, batch_size=batch, **{name: identity})
        except TypeError:
            continue
        return [_sigmoid(float(score)) for score in raw]

    # Параметра нет: оценки либо уже вероятности (0..1) — берём как есть, либо сырые логиты — тогда сигмоида
    values = [float(score) for score in model.predict(pairs, batch_size=batch)]
    if all(0.0 <= value <= 1.0 for value in values):
        return values
    return [_sigmoid(value) for value in values]


@lru_cache(maxsize=1)
def _get_onnx():
    """Сессия onnxruntime и токенизатор из папки RERANKER_ONNX_DIR."""
    import onnxruntime as ort
    from transformers import AutoTokenizer

    directory = settings.RERANKER_ONNX_DIR
    model_path = os.path.join(directory, settings.RERANKER_ONNX_FILE)
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"нет файла сжатой модели: {model_path}")

    options = ort.SessionOptions()
    options.intra_op_num_threads = settings.CPU_THREAD_LIMIT
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
    tokenizer = AutoTokenizer.from_pretrained(directory)
    return session, tokenizer


def _onnx_scores(query: str, passages: list[str]) -> list[float]:
    import numpy as np

    session, tokenizer = _get_onnx()
    wanted = {i.name for i in session.get_inputs()}

    # Сначала короткие, потом длинные: в батче выравнивание (padding) идёт по самому длинному,
    # и смешивать короткие с длинными значит считать пустоту. Порядок результатов восстанавливаем
    order = sorted(range(len(passages)), key=lambda i: len(passages[i]))
    scores: list[float] = [0.0] * len(passages)

    batch = max(1, settings.RERANK_BATCH_SIZE)
    for start in range(0, len(order), batch):
        indexes = order[start : start + batch]
        encoded = tokenizer(
            [query] * len(indexes),
            [passages[i] for i in indexes],
            padding=True,
            truncation="only_second",
            max_length=512,
            return_tensors="np",
        )
        feed = {name: value for name, value in encoded.items() if name in wanted}
        logits = session.run(None, feed)[0]
        flat = np.asarray(logits).reshape(len(indexes), -1)[:, 0]
        for i, logit in zip(indexes, flat):
            value = float(logit)
            # Молчаливо испорченная модель (NaN/inf) сделала бы все куски «нерелевантными» и бот отвечал бы
            # отказами. Лучше громко упасть: rerank_scores перейдёт на torch и запишет причину в лог
            if not math.isfinite(value):
                raise ValueError("ONNX-реранкер вернул нечисловую оценку (NaN/inf)")
            scores[i] = _sigmoid(value)

    return scores


def rerank_scores(query: str, passages: list[str]) -> list[float]:
    """Релевантность каждого passage к query в диапазоне 0..1 (sigmoid от логита).

    Реранк идёт небольшими батчами (RERANK_BATCH_SIZE), чтобы ограничить пик RAM —
    это критично для тяжёлого bge-reranker-v2-m3 поверх e5-large на лимите 8 ГБ.
    """
    global _onnx_failed

    if not passages:
        return []

    if settings.RERANKER_BACKEND == "onnx" and not _onnx_failed:
        try:
            return _onnx_scores(query, passages)
        except Exception:
            with _onnx_lock:
                if not _onnx_failed:
                    _onnx_failed = True
                    logger.exception(
                        "ONNX-реранкер не работает — продолжаю на torch. "
                        "Проверь RERANKER_ONNX_DIR и scripts/export_reranker_onnx.py"
                    )

    return _torch_scores(query, passages)


def warm_up() -> str:
    """Загружает реранкер в память заранее (при старте бота), а не на первом вопросе студента.
    Возвращает, какой путь работает; сбой ONNX здесь же переводит на torch."""
    rerank_scores("прогрев", ["прогрев"])
    backend = active_backend()
    if backend == "onnx" and settings.RERANK_SCORE_THRESHOLD_ONNX is None:
        # Оценки сжатой модели ниже: общий порог мог бы отсекать подходящие куски, и бот отвечал бы «не нашёл»
        # (замер 12.10.2026: у 2 вопросов из 5). Порог подбирает scripts.compare_rerankers («Подбор порога»)
        logger.warning(
            "ONNX включён без своего порога RERANK_SCORE_THRESHOLD_ONNX: возможны ложные «не нашёл». "
            "Подберите порог: python -m scripts.compare_rerankers"
        )
    return backend


def active_backend() -> str:
    """Какой путь реально работает сейчас (для логов и диагностики): с учётом отката на torch."""
    if settings.RERANKER_BACKEND == "onnx" and not _onnx_failed:
        return "onnx"
    return "torch"
