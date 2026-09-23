"""Замер качества RAG на наборе вопросов из eval/dataset.jsonl.

Зачем: тюнить порог реранкера (RERANK_SCORE_THRESHOLD), проверять, что бот
отказывается отвечать на то, чего нет в учебниках (главная защита от выдумок),
и не теряет ответы на то, что в учебниках есть.

Запуск (нужны настроенные DATABASE_URL с загруженными книгами и OPENAI_API_KEY):
    python -m scripts.eval_rag
    python -m scripts.eval_rag --dataset eval/dataset.jsonl

Формат строки датасета (JSON на строку):
    {"question": "...",
     "answerable": true|false,         # есть ли ответ в загруженных учебниках
     "expect_keywords": ["...", ...],  # для answerable=true: слова, что ДОЛЖНЫ быть в ответе
     "note": "..."}                    # пояснение (необязательно)

Метрики:
    - answerable=false → ОК, если бот отказался (выдал NO_CONTEXT_ANSWER).
      Ответ вместо отказа = ВЫДУМКА (hallucination) — самый важный провал.
    - answerable=true  → ОК, если бот НЕ отказался, все expect_keywords присутствуют
      и в ответе есть ссылка-источник [Автор, ...].
"""

import argparse
import asyncio
import json

from rag.generator import NO_CONTEXT_ANSWER, generate_answer
from rag.retriever import retrieve


def _load_dataset(path: str) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _has_source(answer: str) -> bool:
    return "[" in answer and "]" in answer


def _evaluate(item: dict, answer: str) -> tuple[bool, str]:
    abstained = answer.strip() == NO_CONTEXT_ANSWER.strip()

    if not item.get("answerable", True):
        if abstained:
            return True, "корректно отказался"
        return False, "ВЫДУМКА: ответил на то, чего нет в учебниках"

    if abstained:
        return False, "пропуск: отказался, хотя ответ должен быть в учебниках"

    missing = [
        kw for kw in item.get("expect_keywords", []) if kw.lower() not in answer.lower()
    ]
    if missing:
        return False, f"нет ключевых слов: {', '.join(missing)}"
    if not _has_source(answer):
        return False, "нет ссылки-источника"
    return True, "ответ корректен и со ссылкой"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Замер качества RAG")
    parser.add_argument("--dataset", default="eval/dataset.jsonl")
    parser.add_argument("--show-answers", action="store_true", help="печатать тексты ответов")
    args = parser.parse_args()

    items = _load_dataset(args.dataset)
    passed = 0
    hallucinations = 0

    for i, item in enumerate(items, start=1):
        question = item["question"]
        chunks = await retrieve(question)
        answer = (await generate_answer(question, chunks)).text
        ok, reason = _evaluate(item, answer)

        passed += ok
        if not ok and not item.get("answerable", True):
            hallucinations += 1

        mark = "OK " if ok else "FAIL"
        print(f"[{mark}] {i}. {question}")
        print(f"       → {reason}")
        if args.show_answers:
            print(f"       ответ: {answer!r}")

    total = len(items)
    print("\n" + "=" * 50)
    print(f"Пройдено: {passed}/{total}")
    print(f"Выдумок (ответ вместо отказа): {hallucinations}")


if __name__ == "__main__":
    asyncio.run(main())
