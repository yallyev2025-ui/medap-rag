"""TaskModelMap (§56.5 ТЗ): за каждой атомарной AI-задачей закреплён провайдер.

Принцип §66: модель НЕ выбирается заново на каждый запрос «умным» LLM-роутером.
Соответствие задача → провайдер детерминировано и меняется конфигурацией
(`TASK_MODEL_MAP_OVERRIDES`), а не правкой кода — производственная смена проходит
benchmark по §60/§62.
"""

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum

from sqlalchemy import select

from app.llm.registry import DEEPSEEK, OPENAI, model_registry
from config import settings
from db.models import TaskModelOverride
from db.session import async_session

logger = logging.getLogger(__name__)


class Task(str, Enum):
    """Атомарные AI-задачи. Имена совпадают с ТЗ, чтобы маппинг читался буквально."""

    # Grounded-генерация и работа с текстом — DeepSeek (§56.2).
    GROUNDED_QA = "GROUNDED_QA"
    EXPLAIN = "EXPLAIN"
    CLASS_QUICK = "CLASS_QUICK"
    TEST_SOLVE_TEXT = "TEST_SOLVE_TEXT"
    DOCUMENT_QA = "DOCUMENT_QA"
    TARGETED_REPAIR = "TARGETED_REPAIR"
    # Web Research (§19, §34 ТЗ, этап 4A.6) — заземлённый ответ по тексту ОДНОЙ
    # загруженной веб-страницы, та же группа, что GROUNDED_QA/DOCUMENT_QA.
    WEB_RESEARCH = "WEB_RESEARCH"
    # Настоящий поиск по интернету (Yandex Search API, §19 ТЗ, батч 8) — отдельно от WEB_RESEARCH
    # (там одна заданная страница, здесь несколько результатов поиска по запросу).
    WEB_SEARCH = "WEB_SEARCH"
    # PubMed (сверх исходного ТЗ, батч 8) — заземлённый ответ по абстрактам статей.
    PUBMED_SEARCH = "PUBMED_SEARCH"
    # Quick Outline (раздел "Quick Outline" дополнения к ТЗ) — заземлённая генерация
    # строго типизированной схемы по учебникам для сайта владельца, не студенческий Q&A.
    QUICK_OUTLINE = "QUICK_OUTLINE"
    # Content Studio (§31 ТЗ, этап 4B) — черновики учебного контента для сайта
    # владельца по проверенным источникам: recall-вопросы, тесты, клинические кейсы.
    CONTENT_RECALL = "CONTENT_RECALL"
    CONTENT_TEST = "CONTENT_TEST"
    CONTENT_CASE = "CONTENT_CASE"
    # Восприятие и оценка студента — GPT-5.4 Mini (§56.2).
    VISION_EXTRACT = "VISION_EXTRACT"
    RECALL_EVALUATE = "RECALL_EVALUATE"
    FREE_RECALL_EVALUATE = "FREE_RECALL_EVALUATE"
    ORAL_EVALUATE = "ORAL_EVALUATE"
    ERROR_DIAGNOSIS = "ERROR_DIAGNOSIS"
    CLAIM_EVIDENCE_CHECK = "CLAIM_EVIDENCE_CHECK"
    # Служебные дешёвые задачи: роутинг интента и переписывание запроса.
    INTENT_ROUTER = "INTENT_ROUTER"
    QUERY_REWRITE = "QUERY_REWRITE"


DEFAULT_TASK_MODEL_MAP: dict[Task, str] = {
    Task.GROUNDED_QA: DEEPSEEK,
    Task.EXPLAIN: DEEPSEEK,
    Task.CLASS_QUICK: DEEPSEEK,
    Task.TEST_SOLVE_TEXT: DEEPSEEK,
    Task.DOCUMENT_QA: DEEPSEEK,
    Task.TARGETED_REPAIR: DEEPSEEK,
    Task.WEB_RESEARCH: DEEPSEEK,
    Task.WEB_SEARCH: DEEPSEEK,
    Task.PUBMED_SEARCH: DEEPSEEK,
    Task.QUICK_OUTLINE: DEEPSEEK,
    Task.CONTENT_RECALL: DEEPSEEK,
    Task.CONTENT_TEST: DEEPSEEK,
    Task.CONTENT_CASE: DEEPSEEK,
    Task.VISION_EXTRACT: OPENAI,
    Task.RECALL_EVALUATE: OPENAI,
    Task.FREE_RECALL_EVALUATE: OPENAI,
    Task.ORAL_EVALUATE: OPENAI,
    Task.ERROR_DIAGNOSIS: OPENAI,
    Task.CLAIM_EVIDENCE_CHECK: OPENAI,
    Task.INTENT_ROUTER: DEEPSEEK,
    Task.QUERY_REWRITE: DEEPSEEK,
}


def _overrides() -> dict[Task, str]:
    raw = settings.TASK_MODEL_MAP_OVERRIDES.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("TASK_MODEL_MAP_OVERRIDES — невалидный JSON, использую маппинг по умолчанию")
        return {}

    result: dict[Task, str] = {}
    registry = model_registry()
    for key, value in data.items():
        try:
            task = Task(key)
        except ValueError:
            logger.error("TASK_MODEL_MAP_OVERRIDES: неизвестная задача %s — пропускаю", key)
            continue
        if value not in registry:
            logger.error("TASK_MODEL_MAP_OVERRIDES: неизвестный провайдер %s — пропускаю", value)
            continue
        result[task] = value
    return result


def task_model_map() -> dict[Task, str]:
    """Маппинг из кода + TASK_MODEL_MAP_OVERRIDES окружения (без оверрайдов из админки)."""
    return {**DEFAULT_TASK_MODEL_MAP, **_overrides()}


# --- Benchmark (этап 4B, §60): принудительный провайдер только внутри прогона ---
# ContextVar, а не глобальная переменная: параллельные запросы студентов во время
# прогона benchmark продолжают идти по production-маппингу.
_forced_providers: ContextVar[dict[Task, str] | None] = ContextVar("forced_providers", default=None)


@contextmanager
def force_providers(mapping: dict[Task, str]) -> Iterator[None]:
    token = _forced_providers.set(dict(mapping))
    try:
        yield
    finally:
        _forced_providers.reset(token)


# --- Production-маппинг из админки (TaskModelOverride) с лёгким кэшем -----------
# Тот же паттерн, что app/llm/prompts.py: один процесс, изменение из админки
# сбрасывает кэш сразу (clear_override_cache), TTL — страховка от запроса к БД
# на каждый вызов модели.
_OVERRIDE_CACHE_TTL_SECONDS = 30
_override_cache: tuple[dict[Task, str], float] | None = None


async def admin_overrides() -> dict[Task, str]:
    global _override_cache
    now = time.monotonic()
    if _override_cache is not None and _override_cache[1] > now:
        return _override_cache[0]

    try:
        async with async_session() as session:
            rows = (
                await session.execute(
                    select(TaskModelOverride.task, TaskModelOverride.provider)
                    .where(TaskModelOverride.active.is_(True))
                    .order_by(TaskModelOverride.id)
                )
            ).all()
    except Exception as exc:
        # БД недоступна — работаем по маппингу из кода/окружения, ответ не роняем.
        logger.warning("Не удалось прочитать оверрайды TaskModelMap из БД: %s", exc)
        rows = []

    registry = model_registry()
    result: dict[Task, str] = {}
    for task_key, provider in rows:
        try:
            task = Task(task_key)
        except ValueError:
            continue
        if provider in registry:
            result[task] = provider
    _override_cache = (result, now + _OVERRIDE_CACHE_TTL_SECONDS)
    return result


def clear_override_cache() -> None:
    global _override_cache
    _override_cache = None


async def effective_task_model_map() -> dict[Task, tuple[str, str]]:
    """Задача → (провайдер, откуда): "код", "окружение" или "админка" — для страницы Models."""
    env = _overrides()
    admin = await admin_overrides()
    result: dict[Task, tuple[str, str]] = {}
    for task, provider in DEFAULT_TASK_MODEL_MAP.items():
        if task in admin:
            result[task] = (admin[task], "админка")
        elif task in env:
            result[task] = (env[task], "окружение")
        else:
            result[task] = (provider, "код")
    return result


async def resolve_provider(task: Task) -> tuple[str, str | None]:
    """Провайдер для вызова: принудительный (только в benchmark) → админка →
    окружение → код; затем та же подмена недоступного провайдера, что в provider_for."""
    forced = _forced_providers.get()
    if forced and task in forced:
        return _with_fallback(task, forced[task])
    admin = await admin_overrides()
    return _with_fallback(task, admin.get(task) or task_model_map()[task])


def provider_for(task: Task) -> tuple[str, str | None]:
    """Синхронный вариант без оверрайдов из админки (см. resolve_provider)."""
    return _with_fallback(task, task_model_map()[task])


def _with_fallback(task: Task, chosen: str) -> tuple[str, str | None]:
    """Возвращает (провайдер, провайдер-источник фолбэка).

    Если закреплённый за задачей провайдер не сконфигурирован (нет ключа), берётся
    любой доступный, а исходный возвращается вторым элементом — он попадёт в
    `fallbackFrom` записи о расходе (§59), чтобы подмена не осталась незамеченной.
    """
    registry = model_registry()
    if registry[chosen].enabled:
        return chosen, None

    for key, prof in registry.items():
        if prof.enabled:
            logger.warning(
                "Провайдер %s для задачи %s не сконфигурирован, временно использую %s",
                chosen,
                task.value,
                key,
            )
            return key, chosen

    raise RuntimeError(
        "Не сконфигурирован ни один LLM-провайдер: задайте DEEPSEEK_API_KEY и/или OPENAI_API_KEY"
    )
