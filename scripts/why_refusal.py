"""Почему бот отказал или ответил долго: python -m scripts.why_refusal [--last 10] [--match текст] [--run "вопрос"]

Без --run: берёт из журнала ответов (answer_log) последние ответы, показывает по каждому время по шагам,
сколько фрагментов нашлось и сколько признано подходящими, проверена ли генерация, и сразу помечает причину
отказа: «поиск не нашёл подходящих», «модель отказалась при найденных фрагментах» или «ответ не прошёл проверку».

С --run "вопрос": прогоняет ТОЛЬКО поиск (без обращения к платной модели, денег не тратит) и печатает
время каждого шага и лучшие фрагменты с оценкой реранкера и порогом. Так видно, не находит ли бот
нужное, или находит, но оценка ниже порога, или всё упирается во время.
"""

import argparse
import asyncio
import json
import time
from datetime import timezone

from sqlalchemy import select

from config import rerank_threshold, settings
from db.models import AnswerLog
from db.session import async_session
from constants import SOURCE_TEXTBOOK
from rag.generator import NO_CONTEXT_ANSWER


def _reason(answer: str, verified, stages: dict) -> str:
    retrieval = stages.get("retrieval", {}).get("details", {})
    if retrieval and retrieval.get("relevant") == 0:
        if retrieval.get("reranked") is False:
            return "поиск: реранкер не отработал (упал или недоступен), оценок нет"
        return "поиск: ни один фрагмент не прошёл порог релевантности"
    if answer.strip().startswith(NO_CONTEXT_ANSWER.splitlines()[0]):
        if verified is False:
            return "ответ не прошёл проверку (числа/факты не нашлись во фрагментах)"
        return f"модель отказалась сама при {retrieval.get('relevant', '?')} подходящих фрагментах"
    return "—"


async def show_log(last: int, match: str | None) -> None:
    statement = select(AnswerLog).order_by(AnswerLog.created_at.desc()).limit(last)
    if match:
        statement = (
            select(AnswerLog)
            .where(AnswerLog.question.ilike(f"%{match}%"))
            .order_by(AnswerLog.created_at.desc())
            .limit(last)
        )
    async with async_session() as session:
        rows = (await session.execute(statement)).scalars().all()
    if not rows:
        print("Записей нет.")
        return

    for row in rows:
        try:
            data = json.loads(row.diagnostics or "{}")
        except ValueError:
            data = {}
        stages = {s.get("name", "?"): s for s in data.get("stages", [])}
        when = row.created_at.astimezone(timezone.utc).strftime("%d.%m %H:%M:%S UTC") if row.created_at else "?"
        print("=" * 78)
        print(f"{when} | {row.channel} | всего {row.latency_ms / 1000:.1f} с | проверка: {row.verified}")
        print(f"Вопрос: {row.question[:140]}")
        print(f"Ответ:  {row.answer[:100].replace(chr(10), ' ')}")
        print(f"Причина: {_reason(row.answer, row.verified, stages)}")
        for name, stage in sorted(stages.items(), key=lambda kv: -float(kv[1].get("durationMs", 0))):
            details = stage.get("details", {})
            extra = ", ".join(f"{k}={v}" for k, v in details.items() if k in {
                "pool", "reranker", "candidates", "relevant", "reranked", "expanded", "found", "new",
                "answered", "rewritten", "hit", "verified", "chars", "applied",
            })
            print(f"   {name:<18}{float(stage.get('durationMs', 0)) / 1000:>7.1f} с   {extra}")


async def run_search(question: str, pool: int, top: int) -> None:
    # импорт здесь: подгружает модели, а разбор журнала в них не нуждается
    from rag.reranker import active_backend
    from rag.retriever import _fetch_candidates, _is_relevant, _rerank

    print(f"Реранкер: {active_backend()}, порог {rerank_threshold():.2f}, кандидатов {pool}")

    started = time.perf_counter()
    chunks = await _fetch_candidates(question, pool, SOURCE_TEXTBOOK, None)
    fetched = time.perf_counter() - started
    print(f"1) поиск кандидатов (вектор + слова): {fetched:.1f} с, найдено {len(chunks)}")
    if not chunks:
        print("   Кандидатов нет вообще — вопрос не находит ничего даже грубым поиском.")
        return

    # первый реранк включает загрузку модели в память, второй — рабочая скорость
    started = time.perf_counter()
    ranked = await _rerank(question, list(chunks))
    first = time.perf_counter() - started
    started = time.perf_counter()
    ranked = await _rerank(question, list(chunks))
    second = time.perf_counter() - started
    print(f"2) реранк: первый {first:.1f} с (с загрузкой модели), второй {second:.1f} с (рабочая скорость)")

    scored = [c for c in ranked if c.rerank_score is not None]
    if not scored:
        print("   У фрагментов нет оценок реранкера — он не отработал. Смотри ошибки в логе бота.")
    passing = [c for c in ranked if _is_relevant(c)]
    print(f"3) прошли порог: {len(passing)} из {len(ranked)}")
    print(f"   {'оценка':>7}  {'книга':<34} стр.     в тексте: начало")
    for chunk in ranked[:top]:
        score = "нет" if chunk.rerank_score is None else f"{chunk.rerank_score:.3f}"
        mark = "+" if _is_relevant(chunk) else " "
        pages = f"{chunk.page_from}-{chunk.page_to}"
        snippet = chunk.content[:60].replace("\n", " ")
        print(f" {mark} {score:>7}  {(chunk.title or '?')[:34]:<34} {pages:<8} {snippet}")
    print("   «+» — фрагмент прошёл порог и ушёл бы в ответ.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--last", type=int, default=8, help="сколько последних ответов показать")
    parser.add_argument("--match", default=None, help="показать только вопросы, где есть этот текст")
    parser.add_argument("--run", default=None, help="прогнать поиск по этому вопросу (без платных вызовов)")
    parser.add_argument("--pool", type=int, default=settings.TIER_FAST_CANDIDATES)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    if args.run:
        await run_search(args.run, args.pool, args.top)
    else:
        await show_log(args.last, args.match)


if __name__ == "__main__":
    asyncio.run(main())
