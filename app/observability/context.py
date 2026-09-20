"""Контекст запроса: сквозной request_id, пользователь и текущий workflow.

Нужен по §35 ТЗ: каждая строка лога и каждая запись о расходе должны привязываться
к одному запросу, чтобы по request_id можно было восстановить весь путь — какие
стадии отработали, какая модель отвечала, какой версии были промпты и retrieval.

Используются contextvars, поэтому значение живёт внутри одной async-задачи и не
протекает между параллельными запросами.
"""

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

_ctx: ContextVar["RequestContext | None"] = ContextVar("medap_request_ctx", default=None)


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    # Глобальный user_id MedAP (§25) либо telegram:<id> для бота.
    user_id: str | None = None
    # Канал обращения: "api" (образовательный сайт), "telegram", "admin".
    channel: str = "api"
    workflow: str | None = None
    # Стадии пайплайна с длительностью — основа диагностики в Inspector (этап 3).
    stages: list[tuple[str, int]] = field(default_factory=list)


def new_request_id() -> str:
    return uuid.uuid4().hex


def current() -> RequestContext | None:
    return _ctx.get()


def current_request_id() -> str | None:
    ctx = _ctx.get()
    return ctx.request_id if ctx else None


@contextmanager
def request_context(
    user_id: str | None = None,
    channel: str = "api",
    workflow: str | None = None,
    request_id: str | None = None,
):
    """Открывает контекст на время обработки одного запроса."""
    ctx = RequestContext(
        request_id=request_id or new_request_id(),
        user_id=user_id,
        channel=channel,
        workflow=workflow,
    )
    token = _ctx.set(ctx)
    try:
        yield ctx
    finally:
        _ctx.reset(token)


def set_workflow(workflow: str) -> None:
    """Уточняет workflow уже после того, как его определил Orchestrator."""
    ctx = _ctx.get()
    if ctx is not None:
        _ctx.set(replace(ctx, workflow=workflow, stages=ctx.stages))
