"""Orchestrator / Intent Router (§5 ТЗ).

Определяет, какой workflow запускать и нужен ли дорогой Evidence pipeline вообще.
По §2 обычный безопасный немедицинский запрос («привет», «спасибо») получает
короткий ответ и НЕ запускает retrieval: MedAP остаётся medical-first продуктом,
но не платит за поиск там, где искать нечего.

Решение детерминированное: по §5 там, где хватает обычного кода, лишняя LLM не
добавляется.
"""

import re
from dataclasses import dataclass
from enum import Enum

from app.llm.task_map import Task

# Короткие бытовые обращения без медицинского содержания.
_SMALL_TALK = re.compile(
    r"^\s*(привет\w*|здравствуй\w*|добрый\s+(день|вечер|утро)|хай|hello|hi|"
    r"спасибо\w*|благодар\w+|пока|до\s+свидания|ок|окей|ясно|понял\w*|"
    r"что\s+ты\s+умеешь|кто\s+ты|как\s+дела)[\s!.?)]*$",
    re.IGNORECASE,
)

# Просьба объяснить тему, а не ответить на конкретный вопрос.
_EXPLAIN_HINT = re.compile(
    r"\b(объясни|расскажи|разбер[иё]м|что такое|как работает|механизм|простыми словами)\b",
    re.IGNORECASE,
)


class Workflow(str, Enum):
    GROUNDED_QA = "GROUNDED_QA"
    EXPLAIN = "EXPLAIN"
    CLASS_QUICK = "CLASS_QUICK"
    SMALL_TALK = "SMALL_TALK"


@dataclass(frozen=True)
class RoutingDecision:
    workflow: Workflow
    task: Task
    needs_retrieval: bool
    reason: str


_TASK_BY_WORKFLOW = {
    Workflow.GROUNDED_QA: Task.GROUNDED_QA,
    Workflow.EXPLAIN: Task.EXPLAIN,
    Workflow.CLASS_QUICK: Task.CLASS_QUICK,
    Workflow.SMALL_TALK: Task.GROUNDED_QA,
}


def route(question: str, requested_workflow: str | None = None) -> RoutingDecision:
    """Выбирает минимально достаточный workflow (§4: не каждый запрос обязан
    проходить весь pipeline).

    `requested_workflow` — явное указание клиента (например, режим «на паре» с
    образовательного сайта); оно имеет приоритет над эвристикой.
    """
    if requested_workflow:
        try:
            workflow = Workflow(requested_workflow.upper())
        except ValueError:
            workflow = Workflow.GROUNDED_QA
        else:
            return RoutingDecision(
                workflow=workflow,
                task=_TASK_BY_WORKFLOW[workflow],
                needs_retrieval=workflow is not Workflow.SMALL_TALK,
                reason="явно запрошен клиентом",
            )

    if _SMALL_TALK.match(question):
        return RoutingDecision(
            workflow=Workflow.SMALL_TALK,
            task=Task.GROUNDED_QA,
            needs_retrieval=False,
            reason="немедицинское бытовое сообщение",
        )

    if _EXPLAIN_HINT.search(question):
        return RoutingDecision(
            workflow=Workflow.EXPLAIN,
            task=Task.EXPLAIN,
            needs_retrieval=True,
            reason="просьба объяснить тему",
        )

    return RoutingDecision(
        workflow=Workflow.GROUNDED_QA,
        task=Task.GROUNDED_QA,
        needs_retrieval=True,
        reason="медицинский вопрос по материалам",
    )
