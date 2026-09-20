"""Журнал критичных операций (§33 ТЗ: audit log для критических AI workflows).

Сюда пишутся действия, меняющие поведение системы или состав знаний: вход в
админку, загрузка и удаление источника, смена маппинга моделей. Сбой записи в
журнал не должен ломать саму операцию — логируем и продолжаем.
"""

import logging

from app.observability.context import current_request_id
from db.models import AuditLog
from db.session import async_session

logger = logging.getLogger(__name__)


async def audit(action: str, *, actor: str = "admin", target: str | None = None, details: str | None = None) -> None:
    try:
        async with async_session() as session:
            session.add(
                AuditLog(
                    actor=actor,
                    action=action,
                    target=target,
                    details=details,
                    request_id=current_request_id(),
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Не удалось записать audit log: action=%s target=%s", action, target)
