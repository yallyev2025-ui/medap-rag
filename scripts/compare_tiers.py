"""Сравнить «быстрый» и «глубокий» режимы на ваших вопросах:
python -m scripts.compare_tiers --questions eval/questions_real.txt [--limit 5] [--channel telegram]

Каждый вопрос проходит ОБА режима по очереди, как настоящий запрос (поиск → реранкер → модель → проверка).
Печатает по каждому: время всего и по шагам, сколько фрагментов дошло до модели, длину ответа, сколько
источников процитировано, стоимость в рублях и чем кончилось (ответ / ответ с пометкой / отказ). В конце —
средние цифры и насколько совпали процитированные страницы. Полные тексты ответов обоих режимов
записываются в eval/tier_compare.md — их стоит прочитать глазами: по цифрам качество не оценить.

ВНИМАНИЕ: это настоящие обращения к моделям, они стоят денег (на 5 вопросов — порядка 10–30 рублей, у глубокого
режима дороже) и пишутся в журнал ответов и расходов с user_id «tier-compare». Кеш ответов на время замера
выключен, иначе повторный вопрос вернулся бы мгновенно.
"""

import argparse
import asyncio
import logging
import statistics
import time
from pathlib import Path

from app.llm.usage import usage_for_request
from app.observability.context import request_context
from app.workflows.ask import ask
from config import settings
from constants import SOURCE_TEXTBOOK
from rag.generator import NO_CONTEXT_ANSWER, SOFT_FAIL_NOTE
from rag.reranker import warm_up

TIERS = ("fast", "deep")
OUT_FILE = "eval/tier_compare.md"


class _Issues(logging.Handler):
    """Собирает из журнала бота причины, по которым проверка ответа не прошла («Проблемы: …»), чтобы показать их
    рядом с ответом: без этого видно только «с пометкой», а почему — нет."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "Проблемы" in message or "не прошёл проверку" in message:
            self.lines.append(message)


ISSUES = _Issues()


def read_questions(path: str, limit: int | None) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip() and not line.startswith("{")]
    return lines[:limit] if limit else lines


def stage_seconds(diagnostics: dict, name: str) -> float:
    return sum(
        float(s.get("durationMs", 0)) for s in diagnostics.get("stages", []) if s.get("name") == name
    ) / 1000


def outcome(result) -> str:
    text = (result.answer or "").strip()
    if result.answer is None or not text:
        return "не нашёл"
    if text.startswith(NO_CONTEXT_ANSWER.splitlines()[0].strip()):
        return "отказ"
    if SOFT_FAIL_NOTE.strip() in text:
        return "с пометкой"
    return "ответ"


def cited_pages(result) -> set[tuple[str, int | None]]:
    return {(c.get("sourceTitle", ""), c.get("page")) for c in result.citations}


async def run_one(question: str, tier: str, channel: str) -> dict:
    ISSUES.lines.clear()
    started = time.perf_counter()
    with request_context(user_id="tier-compare", channel=channel):
        result = await ask(question, tier=tier, source_type=SOURCE_TEXTBOOK)
    elapsed = time.perf_counter() - started
    usage = await usage_for_request(result.request_id) if result.request_id else {}
    retrieval = next(
        (s.get("details", {}) for s in result.diagnostics.get("stages", []) if s.get("name") == "retrieval"), {}
    )
    return {
        "tier": tier,
        "result": result,
        "seconds": elapsed,
        "retrieval": stage_seconds(result.diagnostics, "retrieval"),
        "generation": stage_seconds(result.diagnostics, "generation"),
        "retry": stage_seconds(result.diagnostics, "retry_expanded"),
        "pool": retrieval.get("pool"),
        "relevant": retrieval.get("relevant"),
        "chars": len(result.answer or ""),
        "cited": len(result.citations),
        "rub": float(usage.get("costRub", 0.0)),
        "outcome": outcome(result),
        "issues": list(ISSUES.lines),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--channel", default="telegram", help="telegram (диалог, как в боте) или api (как сайт)")
    args = parser.parse_args()

    questions = read_questions(args.questions, args.limit)
    if not questions:
        raise SystemExit("В файле нет вопросов")

    settings.CACHE_ENABLED = False
    logging.getLogger("rag.generator").setLevel(logging.INFO)
    logging.getLogger("rag.generator").addHandler(ISSUES)
    print(f"Режимы: быстрый — {settings.TIER_FAST_PROVIDER}, {settings.TIER_FAST_TOP_K} фрагментов, "
          f"{settings.TIER_FAST_CANDIDATES} кандидатов; глубокий — {settings.TIER_DEEP_PROVIDER}, "
          f"{settings.TIER_DEEP_TOP_K} фрагментов, {settings.TIER_DEEP_CANDIDATES} кандидатов; "
          f"реранкер: {await asyncio.to_thread(warm_up)}; канал: {args.channel}\n")

    rows: dict[str, list[dict]] = {tier: [] for tier in TIERS}
    overlaps: list[float] = []
    report = ["# Быстрый и глубокий режим на одних вопросах\n"]

    for number, question in enumerate(questions, 1):
        print(f"[{number}/{len(questions)}] {question[:90]}")
        pair = {}
        for tier in TIERS:
            row = await run_one(question, tier, args.channel)
            rows[tier].append(row)
            pair[tier] = row
            print(f"   {tier:<5} {row['seconds']:6.1f} с (поиск {row['retrieval']:.1f}, ответ {row['generation']:.1f}"
                  f"{', второй заход ' + format(row['retry'], '.1f') if row['retry'] else ''}) | "
                  f"кандидатов {row['pool']}, подошло {row['relevant']} | {row['chars']} симв., "
                  f"источников {row['cited']} | {row['rub']:.2f} ₽ | {row['outcome']}")

        a, b = cited_pages(pair["fast"]["result"]), cited_pages(pair["deep"]["result"])
        if a or b:
            overlaps.append(len(a & b) / len(a | b))

        report.append(f"\n## {number}. {question}\n")
        for tier in TIERS:
            row = pair[tier]
            report.append(f"### {tier} — {row['seconds']:.0f} с, {row['rub']:.2f} ₽, {row['outcome']}\n")
            report.append((row["result"].answer or "(нет ответа)") + "\n")
            if row["issues"]:
                report.append("\n> Что нашла проверка:\n> " + "\n> ".join(row["issues"]).replace("\n", "\n> ") + "\n")

    print()
    print("Итого (среднее по вопросам):")
    print(f"  {'режим':<8}{'время, с':>10}{'симв.':>8}{'источн.':>9}{'₽/ответ':>9}   исходы")
    for tier in TIERS:
        data = rows[tier]
        outcomes: dict[str, int] = {}
        for row in data:
            outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
        print(f"  {tier:<8}{statistics.mean(r['seconds'] for r in data):>10.1f}"
              f"{statistics.mean(r['chars'] for r in data):>8.0f}{statistics.mean(r['cited'] for r in data):>9.1f}"
              f"{statistics.mean(r['rub'] for r in data):>9.2f}   "
              + ", ".join(f"{k} — {v}" for k, v in outcomes.items()))
    if overlaps:
        print(f"  Процитированные страницы совпали на {statistics.mean(overlaps):.0%} (доля общих среди всех названных)")

    Path(OUT_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(OUT_FILE).write_text("\n".join(report), encoding="utf-8")
    print(f"\nТексты ответов обоих режимов: {OUT_FILE} (откройте и сравните глазами)")


if __name__ == "__main__":
    asyncio.run(main())
