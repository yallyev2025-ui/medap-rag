"""Сохранение хода исследования и кеш планов (Mentor 2.0, этап 1).

Две разные вещи с разными правилами:

* **`research_runs`** — журнал: что Ментор считал обязательным, по каким фразам искал и что
  нашлось. Без него сравнение «до/после» остаётся мнением. Пишется один раз на вопрос и
  никогда не читается при ответе.
* **`research_cache`** — кеш планов: тот же вопрос не должен второй раз стоить вызова
  модели. Хранится только разбор вопроса (пункты и фразы), без данных студента; история
  диалога в ключ не входит, иначе уточняющий вопрос получил бы чужой план.

Любая ошибка базы здесь не имеет права испортить ответ: кеш — ускорение, журнал — учёт.
Поэтому все функции ловят свои исключения и молча возвращают «ничего нет».
"""

import asyncio
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.research.planner import (
    ResearchPlan,
    looks_complex,
    normalize_question,
    plan_from_dict,
    plan_research,
    plan_to_dict,
)
from db.models import ResearchCache, ResearchRun
from db.session import async_session

logger = logging.getLogger(__name__)

# Версия политики плана: при правке PLAN_SYSTEM или правил разбора поднимается, старый кеш
# перестаёт попадать (вопрос тот же — план другой).
POLICY_VERSION = "mentor2-research-1"


# Single-flight: одинаковые одновременные вопросы ждут один план, а не зовут модель дважды.
_inflight: dict[str, asyncio.Future] = {}


async def _read_plan(key: str) -> ResearchPlan | None:
    try:
        async with async_session() as session:
            row = (await session.execute(
                select(ResearchCache).where(
                    ResearchCache.policy_version == POLICY_VERSION,
                    ResearchCache.question_key == key,
                )
            )).scalar_one_or_none()
            if row is None:
                return None
            await session.execute(
                update(ResearchCache).where(ResearchCache.id == row.id).values(hits=ResearchCache.hits + 1)
            )
            await session.commit()
            return plan_from_dict(json.loads(row.plan))
    except Exception:
        logger.warning("Кеш планов недоступен — план соберётся заново", exc_info=False)
        return None


async def _write_plan(key: str, plan: ResearchPlan) -> None:
    try:
        async with async_session() as session:
            await session.execute(
                pg_insert(ResearchCache)
                .values(
                    policy_version=POLICY_VERSION,
                    question_key=key,
                    plan=json.dumps(plan_to_dict(plan), ensure_ascii=False),
                )
                .on_conflict_do_nothing(index_elements=["policy_version", "question_key"])
            )
            await session.commit()
    except Exception:
        logger.warning("Не удалось сохранить план в кеш", exc_info=False)


async def plan_with_cache(question: str, turns: list[tuple[str, str]] | None = None) -> ResearchPlan:
    """План вопроса. Простой вопрос модель не зовёт; повторный — берёт из кеша."""
    if turns or not looks_complex(question):
        return await plan_research(question, turns)

    key = normalize_question(question)
    if not key:
        return await plan_research(question, turns)

    running = _inflight.get(key)
    if running is not None:
        # Ждём чужой план, но не наследуем чужую ошибку: если первый запрос упал,
        # второй просто строит план сам.
        try:
            return await asyncio.shield(running)
        except Exception:
            return await plan_research(question, turns)

    cached = await _read_plan(key)
    if cached is not None:
        return cached

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _inflight[key] = future
    try:
        plan = await plan_research(question, turns)
        if plan.origin == "model":
            await _write_plan(key, plan)
        if not future.done():
            future.set_result(plan)
        return plan
    except BaseException:
        if not future.done():
            future.set_exception(RuntimeError("план не собран"))
            future.exception()  # помечаем прочитанным, чтобы не сыпалось «exception never retrieved»
        raise
    finally:
        _inflight.pop(key, None)


@dataclass(frozen=True)
class RunRecord:
    """Что пишем в журнал исследования. Текст ответа сюда не входит — он в `answer_log`."""

    question: str
    request_id: str | None = None
    user_id: str | None = None
    channel: str = ""
    status: str = "done"
    error: str | None = None


async def log_research_run(outcome, record: RunRecord) -> None:
    """Записать ход исследования. Ошибка записи не влияет на ответ студенту."""
    try:
        data = outcome.as_dict()
        async with async_session() as session:
            session.add(
                ResearchRun(
                    request_id=record.request_id,
                    user_id=record.user_id,
                    channel=record.channel,
                    question=record.question[:2000],
                    kind=data.get("kind", ""),
                    goal=data.get("goal", "")[:2000],
                    plan=json.dumps(data, ensure_ascii=False),
                    rounds=int(data.get("rounds") or 1),
                    covered=len(data.get("covered") or []),
                    aspects=len(data.get("aspects") or []),
                    status=record.status,
                    error=record.error,
                )
            )
            await session.commit()
    except Exception:
        logger.warning("Не удалось записать ход исследования", exc_info=False)
