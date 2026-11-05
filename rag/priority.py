"""Приоритет запроса: срочный (вопрос студента, презентация, звонок) или фоновый (конспекты, проверки, исправления).

Сайт и обработчик помечают фоновую работу заголовком `X-Medap-Priority: background` (владелец 05.11.2026: «если делаются конспекты,
а кто-то пишет вопрос или делает презентацию — замедления быть не должно»). Метка лежит в `ContextVar`: она доходит до реранкера
без передачи параметром через каждый слой и сама переходит в потоки `asyncio.to_thread`.
"""

from __future__ import annotations

from contextvars import ContextVar

from fastapi import Header

BACKGROUND = "background"
INTERACTIVE = "interactive"

_priority: ContextVar[str] = ContextVar("medap_request_priority", default=INTERACTIVE)


def current_priority() -> str:
    return _priority.get()


def is_background() -> bool:
    return _priority.get() == BACKGROUND


async def read_priority(x_medap_priority: str = Header(default="")) -> None:
    """Зависимость FastAPI: запоминает приоритет для всего запроса. Любое значение, кроме `background`, — срочный запрос."""
    _priority.set(BACKGROUND if x_medap_priority.strip().lower() == BACKGROUND else INTERACTIVE)
