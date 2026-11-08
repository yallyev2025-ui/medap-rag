"""Режим загрузки источников: «щадящий» (днём) и «полная мощность» (ночью), переключается кнопкой в админке (владелец 08.11.2026).

Щадящий — как с 28.10.2026: эмбеддер не больше INGEST_THREADS ядер, пауза INGEST_PAUSE_S между пачками, чтобы ответы и презентации не зависали.
Полная мощность — все ядра процессора и ни одной паузы: загрузка (эмбеддинги и распознавание сканов) идёт максимально быстро, ответы людям
в это время могут замедляться. Переключатель сохраняется в БД (переживает перезапуск) и действует на уже идущую загрузку: режим читается заново
перед каждой пачкой, поэтому «нажал — через несколько секунд ускорилась».

Бот, API и админка живут в одном процессе (bot/main.py), поэтому флаг в памяти виден всем; БД нужна, чтобы состояние пережило перезапуск.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select

from config import settings
from db.models import AppFlag
from db.session import async_session

logger = logging.getLogger(__name__)

FLAG_KEY = "ingest_full_power"
OCR_WORKERS_FULL_CAP = 16  # каждый поток распознавания держит отрендеренную страницу скана в памяти

_state = {"full": False}


def cores() -> int:
    return max(1, os.cpu_count() or 1)


def is_full_power() -> bool:
    return _state["full"]


def ingest_threads() -> int:
    """Потоков эмбеддера на время загрузки: все ядра в полной мощности, иначе щадящий лимит."""
    return cores() if _state["full"] else settings.INGEST_THREADS


def ingest_pause() -> float:
    """Пауза между пачками, секунды: нет в полной мощности."""
    return 0.0 if _state["full"] else settings.INGEST_PAUSE_S


def ocr_workers() -> int:
    """Потоков распознавания сканов: в полной мощности — все ядра (с потолком по памяти), иначе по бюджету процессора."""
    if _state["full"]:
        return max(settings.OCR_WORKERS, min(OCR_WORKERS_FULL_CAP, cores()))
    return settings.OCR_WORKERS


async def load_from_db() -> bool:
    """Прочитать сохранённое состояние (при старте процесса). Сбой БД — щадящий режим, не падаем."""
    try:
        async with async_session() as session:
            row = await session.scalar(select(AppFlag).where(AppFlag.key == FLAG_KEY))
        _state["full"] = bool(row and row.value == "1")
    except Exception:
        logger.exception("Не удалось прочитать режим загрузки — остаётся щадящий")
        _state["full"] = False
    return _state["full"]


async def set_full_power(on: bool) -> None:
    """Включить/выключить полную мощность и сохранить в БД."""
    async with async_session() as session:
        row = await session.scalar(select(AppFlag).where(AppFlag.key == FLAG_KEY))
        if row is None:
            session.add(AppFlag(key=FLAG_KEY, value="1" if on else "0"))
        else:
            row.value = "1" if on else "0"
        await session.commit()
    _state["full"] = on
    logger.info("Режим загрузки: %s", "ПОЛНАЯ МОЩНОСТЬ" if on else "щадящий")
