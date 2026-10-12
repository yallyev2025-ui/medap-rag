"""Насколько ответы с реранкером torch и onnx похожи: python -m scripts.compare_backends --questions FILE [--limit 5]

Для каждого вопроса в БЫСТРОМ режиме (как в боте) получает три ответа: torch, ещё раз torch и onnx.
Второй torch нужен как «линейка шума»: языковая модель пишет с долей случайности, поэтому два ответа с
ОДНИМ И ТЕМ ЖЕ реранкером тоже не совпадают на 100%. Если torch↔onnx похожи примерно так же, как
torch↔torch, сжатие реранкера ответы не меняет.

Меряется четыре вещи:
  1. Фрагменты, ушедшие в модель (топ-K): доля общих. Это прямое действие реранкера, оно детерминировано,
     у torch↔torch тут всегда 100%.
  2. Процитированные страницы: доля общих среди всех названных.
  3. Смысл ответов: косинус эмбеддингов (e5), в процентах; длинный ответ режется на куски.
  4. Числа в ответах (дозы, сроки, проценты): доля общих. Важно для медицины.
Платные вызовы: ~3 быстрых ответа на вопрос (копейки у DeepSeek); кеш ответов на время замера выключен.
"""

import argparse
import asyncio
import logging
import re
import statistics
import time
from pathlib import Path

from app.llm.usage import usage_for_request
from app.observability.context import request_context
from app.verification.numeric import extract_numeric_claims
from app.workflows.ask import ask
from config import settings
from constants import SOURCE_TEXTBOOK
from rag import reranker

RUNS = (("torch", "torch"), ("torch2", "torch"), ("onnx", "onnx"))


def read_questions(path: str, limit: int | None) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip() and not line.startswith("{")]
    return lines[:limit] if limit else lines


def share(a: set, b: set) -> float:
    """Доля общих среди всех названных (Жаккар). Пусто у обоих — полное совпадение."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def numbers_of(text: str) -> set:
    return extract_numeric_claims(text)


def split_pieces(text: str, size: int = 1200) -> list[str]:
    """Длинный ответ режется по абзацам на куски: эмбеддинг читает только первые ~512 токенов."""
    pieces: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        if current and len(current) + len(paragraph) > size:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}".strip()
    if current:
        pieces.append(current)
    return pieces or [text]


def meaning(a: str, b: str, embed) -> float:
    """Косинус средних эмбеддингов двух ответов (0..1). embed(list[str]) -> list[list[float]], векторы нормированы."""

    if not a.strip() and not b.strip():
        return 1.0
    if not a.strip() or not b.strip():
        return 0.0  # у одного ответа нет вовсе — это не «похоже», а полное расхождение

    def mean_vector(text: str) -> list[float]:
        vectors = embed(split_pieces(text))
        size = len(vectors[0])
        mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(size)]
        norm = sum(x * x for x in mean) ** 0.5 or 1.0
        return [x / norm for x in mean]

    va, vb = mean_vector(a), mean_vector(b)
    return sum(x * y for x, y in zip(va, vb))


def chunk_ids(result) -> set:
    return {c.id for c in result.chunks}


def cited(result) -> set:
    return {(c.get("sourceTitle", ""), c.get("page")) for c in result.citations}


async def run(question: str, backend: str, channel: str) -> dict:
    settings.RERANKER_BACKEND = backend
    reranker._onnx_failed = False
    started = time.perf_counter()
    with request_context(user_id="backend-compare", channel=channel):
        result = await ask(question, tier="fast", source_type=SOURCE_TEXTBOOK)
    seconds = time.perf_counter() - started
    usage = await usage_for_request(result.request_id) if result.request_id else {}
    return {
        "result": result,
        "seconds": seconds,
        "rub": float(usage.get("costRub", 0.0)),
        "actual": reranker.active_backend(),
    }


def fmt(values: list[float]) -> str:
    return f"{statistics.mean(values):4.0%}" if values else "  — "


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--channel", default="telegram")
    parser.add_argument("--out", default="eval/backend_compare.md")
    args = parser.parse_args()

    questions = read_questions(args.questions, args.limit)
    if not questions:
        raise SystemExit("В файле нет вопросов")

    from rag.embedder import embed_passages

    settings.CACHE_ENABLED = False
    logging.getLogger("rag.reranker").setLevel(logging.ERROR)
    # Обе модели грузятся заранее: загрузка не должна попасть в замер времени
    reranker._torch_scores("прогрев", ["прогрев"])
    reranker._onnx_scores("прогрев", ["прогрев"])
    print(f"Быстрый режим: {settings.TIER_FAST_TOP_K} фрагментов в модель, {settings.TIER_FAST_CANDIDATES} кандидатов; "
          f"вопросов: {len(questions)}\n")

    names = ("torch↔torch (шум)", "torch↔onnx")
    metrics: dict[str, dict[str, list[float]]] = {n: {"chunks": [], "pages": [], "meaning": [], "numbers": []} for n in names}
    seconds = {"torch": [], "onnx": []}
    empty = {"torch": 0, "torch2": 0, "onnx": 0}
    report = ["# torch и onnx: ответы быстрого режима\n"]

    for number, question in enumerate(questions, 1):
        print(f"[{number}/{len(questions)}] {question[:90]}")
        got = {}
        for label, backend in RUNS:
            got[label] = await run(question, backend, args.channel)
            row = got[label]
            if backend == "onnx" and row["actual"] != "onnx":
                raise SystemExit("ONNX не работает (упал на torch): см. журнал бота и scripts/export_reranker_onnx.py")
            if label != "torch2":
                seconds[backend].append(row["seconds"])
            if not (row["result"].answer or "").strip():
                empty[label] += 1
            print(f"   {label:<7} {row['seconds']:5.1f} с | {len(row['result'].answer or '')} симв. | "
                  f"источников {len(row['result'].citations)} | {row['rub']:.2f} ₽")

        base = got["torch"]["result"]
        for name, other in zip(names, ("torch2", "onnx")):
            r = got[other]["result"]
            m = metrics[name]
            m["chunks"].append(share(chunk_ids(base), chunk_ids(r)))
            m["pages"].append(share(cited(base), cited(r)))
            m["meaning"].append(meaning(base.answer or "", r.answer or "", embed_passages))
            m["numbers"].append(share(numbers_of(base.answer or ""), numbers_of(r.answer or "")))
        print("   сходство torch↔onnx: фрагменты {:.0%}, страницы {:.0%}, смысл {:.0%}, числа {:.0%}".format(
            *(metrics[names[1]][k][-1] for k in ("chunks", "pages", "meaning", "numbers"))))

        report.append(f"\n## {number}. {question}\n")
        for label, _ in RUNS:
            report.append(f"### {label}\n\n{got[label]['result'].answer or '(нет ответа)'}\n")

    print("\nИТОГО (среднее по вопросам):")
    print(f"  {'':<22}{'фрагменты':>11}{'страницы':>10}{'смысл':>8}{'числа':>8}")
    for name in names:
        m = metrics[name]
        print(f"  {name:<22}{fmt(m['chunks']):>11}{fmt(m['pages']):>10}{fmt(m['meaning']):>8}{fmt(m['numbers']):>8}")
    print(f"\nВопросов БЕЗ ответа вообще (поиск не нашёл подходящих кусков): torch {empty['torch']}, "
          f"torch повторно {empty['torch2']}, onnx {empty['onnx']} из {len(questions)}")
    if empty["onnx"] > empty["torch"]:
        print("  ВНИМАНИЕ: с onnx бот не нашёл материал там, где torch нашёл. Это сдвиг оценок реранкера относительно "
              "порога: сначала scripts.compare_rerankers (блок «Подбор порога»), потом снова этот замер.")
    print(f"\nВремя ответа: torch {statistics.mean(seconds['torch']):.1f} с, onnx {statistics.mean(seconds['onnx']):.1f} с "
          f"(ускорение ×{statistics.mean(seconds['torch']) / max(statistics.mean(seconds['onnx']), 1e-9):.1f})")
    print("Как читать: если строка torch↔onnx близка к строке «шум» (torch↔torch), сжатие ответы не меняет; "
          "заметно ниже — реранкеры отдают модели разный материал.")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(report), encoding="utf-8")
    print(f"Тексты всех трёх ответов по каждому вопросу: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
