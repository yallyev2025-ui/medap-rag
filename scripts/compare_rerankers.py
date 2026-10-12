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

from scripts._tee import tee_output


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


def split_marked(questions: list[str]) -> tuple[list[str], list[str]]:
    """Строка с «!» в начале — вопрос, которого в учебниках быть не должно (проверка отказа): (обычные, помеченные)."""
    normal = [q for q in questions if not q.startswith("!")]
    marked = [q.lstrip("!").strip() for q in questions if q.startswith("!")]
    return normal, [q for q in marked if q]


def ranks(scores: list[float]) -> list[float]:
    """Место каждого элемента по убыванию оценки (при равных — среднее место)."""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    result = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for t in range(i, j + 1):
            result[order[t]] = (i + j) / 2
        i = j + 1
    return result


def spearman(a: list[float], b: list[float]) -> float:
    """Корреляция порядка двух оценок (1.0 — порядок один и тот же). Устойчивее, чем «доля общих в топ-10»,
    которая сильно колеблется на кусках с почти равной оценкой у границы."""
    if len(a) < 3:
        return 1.0
    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 1.0


def top(ids: list[int], scores: list[float], k: int) -> list[int]:
    order = sorted(range(len(ids)), key=lambda i: scores[i], reverse=True)[:k]
    return [ids[i] for i in order]


async def main() -> None:
    tee_output("eval/last_compare_rerankers.txt")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--candidates", type=int, default=settings.RETRIEVAL_CANDIDATES)
    parser.add_argument("--subject", default=None)
    parser.add_argument("--source-type", default=None)
    parser.add_argument("--no-negatives", action="store_true", help="не проверять вопросы вне материалов")
    args = parser.parse_args()

    questions, marked_negatives = split_marked(read_questions(args.questions))
    if not questions:
        raise SystemExit("В файле нет вопросов")

    all_torch: list[float] = []
    all_onnx: list[float] = []
    overlaps: list[float] = []
    top1_same: list[float] = []
    top3_common: list[float] = []
    correlations: list[float] = []
    torch_s: list[float] = []
    onnx_s: list[float] = []
    best_pos_t: list[float] = []  # лучший кусок каждого настоящего вопроса — по нему подбирается порог
    best_pos_o: list[float] = []
    # Для подбора числа кандидатов: какая доля лучших кусков (по оценке) лежит в первых N кандидатах поиска
    pool_sizes = (15, 20, 25, 30, 40)
    pool_hits: dict[int, list[float]] = {n: [] for n in pool_sizes}

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
        top1_same.append(1.0 if top(ids, a, 1) == top(ids, b, 1) else 0.0)
        k3 = min(3, len(ids))
        top3_common.append(len(set(top(ids, a, k3)) & set(top(ids, b, k3))) / k3)
        correlations.append(spearman(a, b))
        best_ids = top(ids, a, k)
        for n in pool_sizes:
            if n <= len(ids):
                pool_hits[n].append(len(set(best_ids) & set(ids[:n])) / k)
        best_pos_t.append(max(a))
        best_pos_o.append(max(b))
        all_torch.extend(a)
        all_onnx.extend(b)
        overlaps.append(common)
        torch_s.append(torch_time)
        onnx_s.append(onnx_time)
        print(f"{common:5.0%} общих в топ-{k} | torch {torch_time:5.1f} с | onnx {onnx_time:5.1f} с | "
              f"лучший кусок torch {max(a):.3f} onnx {max(b):.3f} | {question[:40]}")

    if overlaps:
        print()
        print(f"Вопросов: {len(overlaps)}; кандидатов на вопрос: {args.candidates}")
        print(f"Совпадение топ-{args.top_k}: в среднем {statistics.mean(overlaps):.0%}, худший случай {min(overlaps):.0%}")
        print(f"Лучший кусок один и тот же: в {statistics.mean(top1_same):.0%} вопросов; "
              f"общих в топ-3: {statistics.mean(top3_common):.0%}")
        print(f"Корреляция порядка всех кусков (1.00 — полностью одинаковый): в среднем {statistics.mean(correlations):.3f}, "
              f"худший вопрос {min(correlations):.3f}")
        print(f"Время: torch {statistics.mean(torch_s):.1f} с, onnx {statistics.mean(onnx_s):.1f} с "
              f"(ускорение ×{statistics.mean(torch_s) / max(statistics.mean(onnx_s), 1e-9):.1f})")

        print("Сколько нужных кусков остаётся, если реранкеру отдать только первые N кандидатов поиска "
              f"(доля от топ-{args.top_k} при {args.candidates} кандидатах):")
        for n in pool_sizes:
            if pool_hits[n]:
                print(f"  {n:>3} кандидатов: в среднем {statistics.mean(pool_hits[n]):.0%}, худший вопрос {min(pool_hits[n]):.0%}")

        # Шкала оценок у int8 слегка сдвигается, а порог релевантности — по ней. Сравниваем оценки одних и тех же кусков
        # и долю прошедших порог у каждого способа
        base = settings.RERANK_SCORE_THRESHOLD
        diff = statistics.mean(abs(x - y) for x, y in zip(all_torch, all_onnx))
        share_torch = sum(1 for v in all_torch if v >= base) / len(all_torch)
        share_onnx = sum(1 for v in all_onnx if v >= base) / len(all_onnx)
        print(f"Оценки одних и тех же кусков отличаются в среднем на {diff:.3f} (шкала 0..1)")
        print(f"Прошли порог {base}: torch {share_torch:.0%}, onnx {share_onnx:.0%} кусков")
        if 0.05 <= share_torch <= 0.95 and abs(share_torch - share_onnx) > 0.03:
            ordered = sorted(all_onnx)
            index = min(len(ordered) - 1, max(0, int(round((1 - share_torch) * (len(ordered) - 1)))))
            print(f"Тот же процент у onnx даёт порог ≈ {ordered[index]:.2f} — можно поставить RERANK_SCORE_THRESHOLD_ONNX")
        else:
            print("Порог для onnx менять не нужно: оставьте RERANK_SCORE_THRESHOLD_ONNX незаданным (будет общий "
                  f"{base}). Если все куски проходят порог, подобрать его по этим вопросам нельзя.")

    neg_t: list[float] = []
    neg_o: list[float] = []
    if not args.no_negatives:
        neg_t, neg_o = await check_negatives(args, marked_negatives)
    if best_pos_o:
        print_calibration(settings.RERANK_SCORE_THRESHOLD, best_pos_t, best_pos_o, neg_t, neg_o)


def print_calibration(base: float, pos_t: list[float], pos_o: list[float], neg_t: list[float], neg_o: list[float]) -> None:
    """Порог отвечает на вопрос «есть ли вообще подходящий кусок». Нужен такой, чтобы лучший кусок настоящих вопросов
    его проходил, а лучший кусок посторонних — нет. Поэтому подбираем его по лучшему куску каждого вопроса."""
    print()
    print("Подбор порога для onnx (по лучшему куску каждого вопроса):")
    print(f"  настоящие вопросы, лучший кусок: torch {min(pos_t):.3f}–{max(pos_t):.3f}, onnx {min(pos_o):.3f}–{max(pos_o):.3f}")
    lost_o = sum(1 for v in pos_o if v < base)
    lost_t = sum(1 for v in pos_t if v < base)
    print(f"  при общем пороге {base}: без единого подходящего куска остались бы {lost_o} из {len(pos_o)} вопросов "
          f"у onnx (у torch — {lost_t})")
    if lost_o > lost_t:
        print("  ВНИМАНИЕ: с onnx бот ответил бы «не нашёл» на вопросы, на которые torch отвечает. "
              "Включать onnx с общим порогом нельзя.")
    if not neg_o:
        return
    print(f"  посторонние вопросы, лучший кусок: torch до {max(neg_t):.3f}, onnx до {max(neg_o):.3f}")
    low, high = max(neg_o), min(pos_o)
    if low < high:
        tau = max(low + 0.01, min(high - 0.01, round((low + high) / 2, 2)))
        print(f"  Оценки разделяются: ставьте RERANK_SCORE_THRESHOLD_ONNX={tau:.2f} "
              f"(между {low:.3f} посторонних и {high:.3f} настоящих). Запас небольшой — на 5–10 вопросах это ориентир.")
    else:
        print("  Оценки настоящих и посторонних вопросов у onnx ПЕРЕСЕКАЮТСЯ: никаким порогом их не разделить. "
              "Эта сборка сжатой модели не годится; попробуйте вариант -pc (см. docs/RERANKER.md).")


# Вопросы, ответа на которые в учебниках быть не должно: порог обязан их отсекать (бот скажет «нет в материалах»)
NEGATIVES = [
    "Как приготовить борщ",
    "Курс доллара к рублю сегодня",
    "Кто выиграл чемпионат мира по футболу",
    "Как установить Windows на ноутбук",
]


async def check_negatives(args, extra: list[str] | None = None) -> tuple[list[float], list[float]]:
    base = settings.RERANK_SCORE_THRESHOLD
    print()
    print(f"Вопросы вне материалов (лучший кусок должен быть ниже порога {base}, иначе бот не откажет):")
    worst_torch = worst_onnx = 0.0
    bests_t: list[float] = []
    bests_o: list[float] = []
    for question in [*NEGATIVES, *(extra or [])]:
        chunks = await _fetch_candidates(question, args.candidates, args.source_type, args.subject)
        if not chunks:
            print(f"  — нет кандидатов: {question}")
            continue
        texts = [c.content for c in chunks]
        best_torch = max(_torch_scores(question, texts))
        best_onnx = max(_onnx_scores(question, texts))
        worst_torch = max(worst_torch, best_torch)
        worst_onnx = max(worst_onnx, best_onnx)
        bests_t.append(best_torch)
        bests_o.append(best_onnx)
        print(f"  лучший кусок: torch {best_torch:.3f} | onnx {best_onnx:.3f} | {question}")
    verdict = "порог отсекает" if worst_onnx < base else "ВНИМАНИЕ: порог пропускает чужие вопросы — onnx включать нельзя без подбора порога"
    print(f"Итог для onnx: выше всех {worst_onnx:.3f} (torch {worst_torch:.3f}) — {verdict}")
    return bests_t, bests_o

if __name__ == "__main__":
    asyncio.run(main())
