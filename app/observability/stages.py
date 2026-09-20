"""Замер стадий пайплайна и структурные логи по ним (§35 ТЗ).

Диагностика копится в объекте `StageLog`, который workflow возвращает вместе с
ответом: на этапе 1 он попадает в поле `diagnostics` ответа `/v1`, на этапе 2-3 —
в Retrieval Inspector и Answer Inspector админки.
"""

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.observability.context import current_request_id

logger = logging.getLogger(__name__)


@dataclass
class Stage:
    name: str
    duration_ms: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageLog:
    stages: list[Stage] = field(default_factory=list)

    @contextmanager
    def measure(self, name: str, **details: Any):
        """Замеряет стадию и пишет её в лог с request_id."""
        started = time.monotonic()
        collected: dict[str, Any] = dict(details)
        try:
            yield collected
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.stages.append(Stage(name=name, duration_ms=duration_ms, details=collected))
            logger.info(
                "stage=%s duration_ms=%d request_id=%s %s",
                name,
                duration_ms,
                current_request_id(),
                collected or "",
            )

    def note(self, name: str, **details: Any) -> None:
        """Отмечает мгновенное событие стадии (без замера длительности)."""
        self.stages.append(Stage(name=name, duration_ms=0, details=dict(details)))
        logger.info("stage=%s request_id=%s %s", name, current_request_id(), details or "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "stages": [
                {"name": s.name, "durationMs": s.duration_ms, **({"details": s.details} if s.details else {})}
                for s in self.stages
            ],
            "totalMs": sum(s.duration_ms for s in self.stages),
        }
