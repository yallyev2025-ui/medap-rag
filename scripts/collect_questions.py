"""Собрать настоящие вопросы студентов для сравнения реранкеров:
python -m scripts.collect_questions [--limit 100] [--days 60] [--out eval/questions_real.txt]

Берёт из журнала ответов (answer_log) разные вопросы по учебникам, без повторов и без коротких
реплик («привет», «спасибо»). Файл потом идёт в scripts.compare_rerankers. ВОПРОСЫ СТУДЕНТОВ — это
их слова: файл остаётся на вашем сервере и в репозиторий не коммитится (eval/questions_real*.txt в
.gitignore); перед тем как кому-то показывать, просмотрите его глазами.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import AnswerLog
from db.session import async_session

# Рабочие вопросы по материалам: не болтовня и не служебные ветки
WORKFLOWS = ("GROUNDED_QA", "EXPLAIN")
MIN_CHARS = 15
MAX_CHARS = 400


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--out", default="eval/questions_real.txt")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    statement = (
        select(AnswerLog.question)
        .where(AnswerLog.created_at >= since, AnswerLog.workflow.in_(WORKFLOWS))
        .order_by(AnswerLog.created_at.desc())
        .limit(args.limit * 5)
    )

    async with async_session() as session:
        rows = (await session.execute(statement)).scalars().all()

    seen: set[str] = set()
    questions: list[str] = []
    for raw in rows:
        text = " ".join(raw.split())
        key = text.lower()
        if not (MIN_CHARS <= len(text) <= MAX_CHARS) or key in seen:
            continue
        seen.add(key)
        questions.append(text)
        if len(questions) >= args.limit:
            break

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(questions) + ("\n" if questions else ""))

    print(f"Записано вопросов: {len(questions)} → {args.out}")
    # Подсказка — только когда нашлось меньше, чем просили (сам попросил 5 — значит так и надо)
    if len(questions) < args.limit:
        print(f"Нашлось меньше, чем просили ({args.limit}): увеличьте --days. Это подсказка, файл записан.")


if __name__ == "__main__":
    asyncio.run(main())
