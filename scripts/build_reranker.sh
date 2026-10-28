#!/bin/sh
# Сборка сжатого реранкера внутри образа ОДНИМ шагом: скачать большую модель → сжать → подобрать порог → удалить большую.
# Большая модель (≈2,3 ГБ) в слой образа не попадает: она стирается в том же RUN (владелец 28.10.2026: «большая не должна лежать в образе»).
# KEEP_TORCH_RERANKER=1 — оставить её (откат на torch возможен); по умолчанию 0.
# Не получилось сжать и KEEP=0 — сборка падает: образ без реранкера хуже, чем остановленная сборка.
set -u
PRESET="${ONNX_PRESET:-avx2}"
OUT="${RERANKER_ONNX_DIR:-/opt/models/reranker-onnx-int8}"
KEEP="${KEEP_TORCH_RERANKER:-0}"
MODEL="${RERANKER_MODEL_NAME:-BAAI/bge-reranker-v2-m3}"
CACHE="${HF_HOME:-/opt/models}/hub/models--$(echo "$MODEL" | sed 's#/#--#')"
ok=1

timeout 600 pip install --retries 10 --timeout 60 "transformers==4.57.6" "optimum==2.1.0" "optimum-onnx[onnxruntime]==0.1.0" || ok=0
if [ "$ok" = 1 ]; then python /tmp/export_reranker_onnx.py --preset "$PRESET" || ok=0; fi
if [ "$ok" = 1 ]; then
    python /tmp/export_reranker_onnx.py --preset "$PRESET" --per-channel --out "${OUT}-pc" || echo "Вариант -pc (per-channel) не собран"
    python /tmp/calibrate_onnx_threshold.py --onnx-dir "$OUT" || echo "Порог автоматически не подобран: будет общий (или RERANK_SCORE_THRESHOLD_ONNX)"
fi

if [ "$ok" = 1 ] && [ "$KEEP" != 1 ]; then
    rm -rf "$CACHE"
    echo "Большая модель реранкера удалена из образа: $CACHE"
elif [ "$ok" != 1 ] && [ "$KEEP" != 1 ]; then
    echo "ОШИБКА: сжатый реранкер не собран, а большую в образе оставлять не просили (KEEP_TORCH_RERANKER=0)" >&2
    exit 1
fi
exit 0
