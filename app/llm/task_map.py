"""TaskModelMap (§56.5 ТЗ): за каждой атомарной AI-задачей закреплён провайдер.

Принцип §66: модель НЕ выбирается заново на каждый запрос «умным» LLM-роутером.
Соответствие задача → провайдер детерминировано и меняется конфигурацией
(`TASK_MODEL_MAP_OVERRIDES`), а не правкой кода — производственная смена проходит
benchmark по §60/§62.
"""

import json
import logging
from enum import Enum

from app.llm.registry import DEEPSEEK, OPENAI, model_registry
from config import settings

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
    # Quick Outline (раздел "Quick Outline" дополнения к ТЗ) — заземлённая генерация
    # строго типизированной схемы по учебникам для сайта владельца, не студенческий Q&A.
    QUICK_OUTLINE = "QUICK_OUTLINE"
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
    Task.QUICK_OUTLINE: DEEPSEEK,
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
    return {**DEFAULT_TASK_MODEL_MAP, **_overrides()}


def provider_for(task: Task) -> tuple[str, str | None]:
    """Возвращает (провайдер, провайдер-источник фолбэка).

    Если закреплённый за задачей провайдер не сконфигурирован (нет ключа), берётся
    любой доступный, а исходный возвращается вторым элементом — он попадёт в
    `fallbackFrom` записи о расходе (§59), чтобы подмена не осталась незамеченной.
    """
    registry = model_registry()
    chosen = task_model_map()[task]
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
