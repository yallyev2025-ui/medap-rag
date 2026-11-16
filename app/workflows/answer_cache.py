"""Кеш ответов по смыслу (батч 31).

Студенты задают одно и то же разными словами: «что такое воспаление» и «воспаление:
определение, этиология». Поиск по учебнику стоит только процессора, а вот генерация —
денег. Поэтому после поиска и до генерации проверяем, не отвечали ли уже на такой вопрос.

Отдаём готовый ответ ТОЛЬКО когда сошлись оба условия:
  1) вопросы близки по смыслу (косинус эмбеддингов не ниже CACHE_MIN_SIMILARITY);
  2) поиск нашёл те же фрагменты (доля общих не ниже CACHE_MIN_OVERLAP).
Любое сомнение — ответ пишется заново. В кеш попадают только проверенные ответы по учебникам
в обычной подаче; вопросы с пожеланием формы («кратко», «на пальцах», «схемой»…), уточнения
в диалоге, личные документы, клинреки и несколько вопросов сразу в кеш не идут.
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from config import settings
from db.models import AnswerCache, PromptVersion
from db.session import async_session
from app.llm.registry import model_registry
from app.llm.task_map import effective_task_model_map

logger = logging.getLogger(__name__)


async def policy_version() -> str:
    """Отдельный раздел кеша при любой смене модели или политики проверки."""
    mapping = await effective_task_model_map()
    async with async_session() as session:
        rows = (await session.execute(select(PromptVersion.prompt_key, PromptVersion.id).where(
            PromptVersion.status == "production",
        ).order_by(PromptVersion.prompt_key, PromptVersion.id))).all()
    state = {
        "prompt": settings.PROMPT_VERSION,
        "retrieval": settings.RETRIEVAL_VERSION,
        "models": {key: profile.model_id for key, profile in model_registry().items()},
        "tasks": {key.value: value for key, value in mapping.items()},
        "published_prompts": [(key, version) for key, version in rows],
        "verify": settings.VERIFY_GROUNDING,
        "soft_fail": settings.VERIFY_SOFT_FAIL,
        "schema": 2,
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

# Пожелания к форме ответа: на них ответ зависит от формулировки, а не только от темы.
STYLE_PATTERN = re.compile(
    r"кратк|коротк|в двух словах|подробн|развёрнут|развернут|на пальцах|простыми словами|"
    r"понятн|для чайник|схем|таблиц|истори|мнемон|аналоги|запомн|сравни|конспект|"
    r"пошагов|по шагам|тест|вариант",
    re.IGNORECASE,
)


@dataclass
class CachedAnswer:
    id: int
    answer: str
    citations: list[dict]
    verified: bool | None


def cacheable_question(question: str) -> bool:
    """Подходит ли сам вопрос для кеша: без пожеланий к форме и не слишком короткий."""
    text = question.strip()
    return len(text) >= 6 and not STYLE_PATTERN.search(text)


def overlap(cached_ids: list[int], current_ids: list[int]) -> float:
    """Доля общих фрагментов — относительно меньшего из двух наборов."""
    a, b = set(cached_ids), set(current_ids)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def pick_match(
    candidates: list[tuple[AnswerCache, float]],
    current_ids: list[int],
    min_similarity: float,
    min_overlap: float,
):
    """Первый подходящий кандидат из упорядоченных по близости пар (запись, косинус)."""
    for entry, similarity in candidates:
        if similarity < min_similarity:
            continue
        # Удалённый или выключенный источник не должен остаться в ответе
        # только потому, что остальные фрагменты совпали.
        cited = [c.get("evidenceId") for c in json.loads(entry.citations)]
        if not cited or any(not str(cid).isdigit() or int(cid) not in current_ids for cid in cited):
            continue
        if overlap(json.loads(entry.chunk_ids), current_ids) >= min_overlap:
            return entry
    return None


async def _embed(question: str) -> list[float]:
    from rag.embedder import embed_query

    return await asyncio.to_thread(embed_query, question)


async def lookup(
    *, question: str, tier: str, source_type: str, subject: str | None, mode: str, kind: str,
    fmt: str | None, chunk_ids: list[int],
) -> CachedAnswer | None:
    """Ищет готовый ответ. Любой сбой кеша — просто промах: ответ не должен зависеть от него."""
    if not settings.CACHE_ENABLED:
        return None
    try:
        vector = await _embed(question)
        policy = await policy_version()
        oldest = datetime.now(timezone.utc) - timedelta(days=settings.CACHE_MAX_AGE_DAYS)
        distance = AnswerCache.q_embedding.cosine_distance(vector)
        stmt = (
            select(AnswerCache, distance)
            .where(
                AnswerCache.policy_version == policy,
                AnswerCache.verified.is_(True),
                AnswerCache.tier == tier,
                AnswerCache.source_type == source_type,
                AnswerCache.subject == subject if subject is not None else AnswerCache.subject.is_(None),
                AnswerCache.mode == mode,
                AnswerCache.kind == kind,
                AnswerCache.fmt == fmt if fmt is not None else AnswerCache.fmt.is_(None),
                AnswerCache.created_at >= oldest,
            )
            .order_by(distance)
            .limit(5)
        )
        async with async_session() as session:
            rows = (await session.execute(stmt)).all()
            entry = pick_match(
                [(row[0], 1.0 - float(row[1])) for row in rows],
                chunk_ids,
                settings.CACHE_MIN_SIMILARITY,
                settings.CACHE_MIN_OVERLAP,
            )
            if entry is None:
                return None
            await session.execute(
                update(AnswerCache)
                .where(AnswerCache.id == entry.id)
                .values(hits=AnswerCache.hits + 1, last_hit_at=datetime.now(timezone.utc))
            )
            await session.commit()
            return CachedAnswer(entry.id, entry.answer, json.loads(entry.citations), entry.verified)
    except Exception:
        logger.exception("Кеш ответов недоступен — отвечаю без него")
        return None


async def store(
    *, question: str, tier: str, source_type: str, subject: str | None, mode: str, kind: str,
    fmt: str | None, chunk_ids: list[int], answer: str, citations: list[dict], verified: bool | None,
) -> None:
    if not settings.CACHE_ENABLED:
        return
    if verified is not True:
        return
    try:
        vector = await _embed(question)
        policy = await policy_version()
        async with async_session() as session:
            session.add(
                AnswerCache(
                    policy_version=policy,
                    tier=tier, source_type=source_type, subject=subject, mode=mode, kind=kind, fmt=fmt,
                    question=question, q_embedding=vector,
                    chunk_ids=json.dumps(sorted(set(chunk_ids))), answer=answer,
                    citations=json.dumps(citations, ensure_ascii=False), verified=verified,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Не удалось сохранить ответ в кеш")
