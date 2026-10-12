"""Сжать реранкер в ONNX int8: python scripts/export_reranker_onnx.py [--preset avx2] [--out DIR]

Берёт ту же модель, что и обычный путь (RERANKER_MODEL_NAME, bge-reranker-v2-m3), переводит её в
ONNX и квантует веса в 8-битные целые. Результат — папка с model_quantized.onnx и токенизатором;
из неё грузит `RERANKER_BACKEND=onnx` (rag/reranker.py). Исходная модель не меняется.

Пресет зависит от процессора, на котором будет считаться:
  avx2         — работает почти везде (по умолчанию, самый совместимый);
  avx512_vnni  — быстрее на процессорах с VNNI (проверка: lscpu | grep avx512_vnni);
  arm64        — для ARM.
Нужны пакеты: pip install "optimum[onnxruntime]". Модель большая (~2,3 ГБ в fp32): нужно около 8 ГБ
оперативной памяти на время конвертации и несколько минут.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path



# Не `config.settings`: при сборке образа нет ни токена бота, ни базы, а весь config их ждёт
DEFAULT_MODEL = os.environ.get("RERANKER_MODEL_NAME", "BAAI/bge-reranker-v2-m3")
DEFAULT_OUT = os.environ.get("RERANKER_ONNX_DIR", "/opt/models/reranker-onnx-int8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preset", choices=["avx2", "avx512_vnni", "arm64"], default="avx2")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--per-channel",
        action="store_true",
        help="масштаб квантования на каждый выходной канал, а не на весь слой: обычно точнее; "
        "сначала проверьте compare_rerankers",
    )
    args = parser.parse_args()

    from optimum.onnxruntime import ORTModelForSequenceClassification, ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig
    from transformers import AutoTokenizer

    out = Path(args.out)
    raw = out / "_fp32"
    out.mkdir(parents=True, exist_ok=True)

    print(f"1/3 Перевожу {args.model} в ONNX…")
    model = ORTModelForSequenceClassification.from_pretrained(args.model, export=True)
    model.save_pretrained(raw)
    AutoTokenizer.from_pretrained(args.model).save_pretrained(out)

    print(f"2/3 Квантую в int8 (пресет {args.preset})…")
    config = getattr(AutoQuantizationConfig, args.preset)(is_static=False, per_channel=args.per_channel)
    quantizer = ORTQuantizer.from_pretrained(raw)
    quantizer.quantize(save_dir=out, quantization_config=config)

    produced = out / "model_quantized.onnx"
    if not produced.is_file():
        print("Квантованный файл не появился — смотри вывод выше.", file=sys.stderr)
        return 1

    # Промежуточная fp32-копия не нужна: в образе остаётся только сжатая модель
    shutil.rmtree(raw, ignore_errors=True)

    print(f"3/3 Готово: {produced} ({produced.stat().st_size / 1024**3:.2f} ГБ). Включение: RERANKER_BACKEND=onnx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
