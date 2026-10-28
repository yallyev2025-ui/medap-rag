"""Автоматический подбор порога релевантности для сжатого (int8) реранкера при сборке образа.

Идея: сжатая модель должна принимать те же решения «кусок подходит / не подходит», что и большая, поэтому порог для неё подбирается так,
чтобы решения совпадали на наборе пар «вопрос — кусок» (внизу), где большая модель считается эталоном при общем пороге RERANK_SCORE_THRESHOLD.
Результат — файл threshold.json рядом с моделью; бот читает его сам, переменная окружения не нужна (она, если задана, главнее).
Порог никогда не выше общего (иначе у сжатой модели появились бы ложные «не нашёл») и не ниже 0,005.
Работает без базы и без config (при сборке образа их нет). Запуск: python scripts/calibrate_onnx_threshold.py [--onnx-dir DIR]
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

MODEL = os.environ.get("RERANKER_MODEL_NAME", "BAAI/bge-reranker-v2-m3")
ONNX_DIR = os.environ.get("RERANKER_ONNX_DIR", "/opt/models/reranker-onnx-int8")
ONNX_FILE = os.environ.get("RERANKER_ONNX_FILE", "model_quantized.onnx")
BASE = float(os.environ.get("RERANK_SCORE_THRESHOLD", "0.05"))

# (вопрос, фрагмент). Подходящие и посторонние пары вперемешку: чтобы порог разделял и те, и другие.
Q = {
    "воспаление": "Какие стадии у воспаления и что такое альтерация?",
    "инфаркт": "Что такое инфаркт миокарда и как он проявляется?",
    "гипертония": "Какие препараты назначают при артериальной гипертонии?",
    "диабет": "Как диагностируют сахарный диабет 2 типа?",
    "почка": "Как устроен нефрон и как фильтруется кровь?",
    "борщ": "Как сварить борщ со свёклой?",
    "синапс": "Как передаётся сигнал в синапсе?",
    "пневмония": "Какие признаки внебольничной пневмонии?",
}
P = {
    "воспаление": "Воспаление проходит стадии альтерации, экссудации и пролиферации. Альтерация — повреждение ткани, которое запускает выброс медиаторов.",
    "инфаркт": "Инфаркт миокарда — некроз участка сердечной мышцы из-за острой ишемии. Проявляется давящей болью за грудиной, одышкой, холодным потом.",
    "гипертония": "При артериальной гипертонии назначают ингибиторы АПФ, блокаторы рецепторов ангиотензина, бета-блокаторы и диуретики с учётом сопутствующих болезней.",
    "диабет": "Диагноз сахарного диабета 2 типа ставят по уровню глюкозы натощак, гликированному гемоглобину и глюкозотолерантному тесту.",
    "почка": "Нефрон состоит из почечного тельца и канальцев. В клубочке кровь фильтруется, формируя первичную мочу, затем в канальцах происходит реабсорбция.",
    "синапс": "В химическом синапсе нейромедиатор выделяется из пресинаптического окончания в щель и связывается с рецепторами постсинаптической мембраны.",
    "пневмония": "Для внебольничной пневмонии характерны лихорадка, кашель с мокротой, одышка, влажные хрипы и инфильтрат на рентгенограмме.",
    "борщ": "Борщ варят на мясном бульоне, добавляя свёклу, капусту, картофель и морковь; подают со сметаной.",
    "перелом": "Перелом шейки бедра чаще встречается у пожилых и требует оперативного лечения в первые сутки.",
    "астма": "Бронхиальная астма — хроническое воспаление дыхательных путей с приступами удушья; основа лечения — ингаляционные глюкокортикостероиды.",
    "футбол": "Футбольный матч состоит из двух таймов по сорок пять минут, судья назначает штрафные удары за нарушения правил.",
}
PAIRS = [(q, p) for q in Q.values() for p in P.values()]


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x)) if x >= 0 else math.exp(x) / (1.0 + math.exp(x))


def torch_scores() -> list[float]:
    import torch
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(MODEL)
    raw = model.predict(PAIRS, batch_size=8, activation_fn=torch.nn.Identity())
    return [sigmoid(float(v)) for v in raw]


def onnx_scores(directory: str) -> list[float]:
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer

    session = ort.InferenceSession(os.path.join(directory, ONNX_FILE), providers=["CPUExecutionProvider"])
    tokenizer = AutoTokenizer.from_pretrained(directory)
    wanted = {i.name for i in session.get_inputs()}
    out: list[float] = []
    for start in range(0, len(PAIRS), 8):
        chunk = PAIRS[start : start + 8]
        enc = tokenizer([a for a, _ in chunk], [b for _, b in chunk], padding=True, truncation="only_second", max_length=512, return_tensors="np")
        logits = session.run(None, {k: v for k, v in enc.items() if k in wanted})[0]
        out += [sigmoid(float(v)) for v in np.asarray(logits).reshape(len(chunk), -1)[:, 0]]
    return out


def choose(torch_s: list[float], onnx_s: list[float], base: float) -> tuple[float, float]:
    """Порог для int8: максимум совпадения решений с большой моделью (при равенстве — меньший порог, чтобы не терять ответы)"""
    truth = [t >= base for t in torch_s]
    values = sorted(set(onnx_s))
    candidates = [0.005] + [(a + b) / 2 for a, b in zip(values, values[1:])] + [base]
    best = (-1.0, base)
    for c in candidates:
        c = min(max(c, 0.005), base)
        agree = sum((o >= c) == t for o, t in zip(onnx_s, truth)) / len(truth)
        if agree > best[0] + 1e-9:
            best = (agree, c)
    return best[1], best[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onnx-dir", default=ONNX_DIR)
    args = parser.parse_args()
    t, o = torch_scores(), onnx_scores(args.onnx_dir)
    threshold, agreement = choose(t, o, BASE)
    Path(args.onnx_dir, "threshold.json").write_text(json.dumps({"threshold": round(threshold, 4), "agreement": round(agreement, 3), "pairs": len(PAIRS), "base": BASE}, ensure_ascii=False))
    print(f"Порог для сжатого реранкера: {threshold:.4f} (совпадение решений с большой моделью {agreement:.0%} на {len(PAIRS)} парах)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
