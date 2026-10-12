"""Сравнить реранкеры на ваших вопросах: python -m scripts.compare_rerankers --questions FILE [--top-k 10]

Для каждого вопроса берёт одних и тех же кандидатов поиска и считает их ДВУМЯ способами — torch (как
сейчас) и onnx int8 — затем печатает: сколько секунд заняло у каждого и насколько совпал верх выдачи
(доля общих кусков в топ-K). Если совпадение высокое (скажем, 90%+), сжатие почти не меняет то, что
увидит модель. Нужны DATABASE_URL с загруженными источниками и собранная папка RERANKER_ONNX_DIR.

FILE — текст по вопросу в строке или jsonl с полем "question" (как eval/dataset.jsonl).
"""

import argparse
import asyncio
import json
import statistics
import time

from config import settings
from rag.reranker import _onnx_scores, _torch_scores
from rag.retriever import _fetch_candidates


def read_questions(path: str) -> list[str]:
    questions: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                item = json.loads(line)
                if item.get("question"):
                    questions.append(str(item["question"]))
            else:
                questions.append(line)
    return questions


def top(ids: list[int], scores: list[float], k: int) -> list[int]:
    order = sorted(range(len(ids)), key=lambda i: scores[i], reverse=True)[:k]
    return [ids[i] for i in order]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--candidates", type=int, default=settings.RETRIEVAL_CANDIDATES)
    parser.add_argument("--subject", default=None)
    parser.add_argument("--source-type", default=None)
    args = parser.parse_args()

    questions = read_questions(args.questions)
    if not questions:
        raise SystemExit("В файле нет вопросов")

    overlaps: list[float] = []
    torch_s: list[float] = []
    onnx_s: list[float] = []

    # Первый вызов каждого способа загружает модель — в замер времени он не входит
    warm = ["прогрев"]
    _torch_scores("прогрев", warm)
    _onnx_scores("прогрев", warm)

    for question in questions:
        chunks = await _fetch_candidates(question, args.candidates, args.source_type, args.subject)
        if not chunks:
            print(f"— нет кандидатов: {question[:60]}")
            continue
        texts = [c.content for c in chunks]
        ids = [c.id for c in chunks]

        started = time.perf_counter()
        a = _torch_scores(question, texts)
        torch_time = time.perf_counter() - started

        started = time.perf_counter()
        b = _onnx_scores(question, texts)
        onnx_time = time.perf_counter() - started

        k = min(args.top_k, len(ids))
        common = len(set(top(ids, a, k)) & set(top(ids, b, k))) / k
        overlaps.append(common)
        torch_s.append(torch_time)
        onnx_s.append(onnx_time)
        print(f"{common:5.0%} общих в топ-{k} | torch {torch_time:5.1f} с | onnx {onnx_time:5.1f} с | {question[:50]}")

    if overlaps:
        print()
        print(f"Вопросов: {len(overlaps)}; кандидатов на вопрос: {args.candidates}")
        print(f"Совпадение топ-{args.top_k}: в среднем {statistics.mean(overlaps):.0%}, худший случай {min(overlaps):.0%}")
        print(f"Время: torch {statistics.mean(torch_s):.1f} с, onnx {statistics.mean(onnx_s):.1f} с "
              f"(ускорение ×{statistics.mean(torch_s) / max(statistics.mean(onnx_s), 1e-9):.1f})")


if __name__ == "__main__":
    asyncio.run(main())
