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
OCR_WORKERS_FULL_CAP = 8  # каждый поток распознавания держит отрендеренную страницу скана (~25 МБ) и процесс tesseract; выше 8 память не выдерживает (потолок решения 07.11)
PER_WORKER_MB = 600  # запас памяти на один поток распознавания
RESERVE_MB = 1000  # память, которую не трогаем (модели, бот, база)

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


def available_memory_mb() -> int | None:
    """Свободная память контейнера, МБ: по лимиту cgroup (v2, затем v1), иначе MemAvailable. None — узнать не удалось."""
    mb = 1024 * 1024
    try:
        limit = open("/sys/fs/cgroup/memory.max").read().strip()
        if limit != "max":
            return (int(limit) - int(open("/sys/fs/cgroup/memory.current").read())) // mb
    except Exception:  # noqa: BLE001 — нет cgroup v2
        pass
    try:
        limit_v1 = int(open("/sys/fs/cgroup/memory/memory.limit_in_bytes").read())
        if limit_v1 < (1 << 60):
            return (limit_v1 - int(open("/sys/fs/cgroup/memory/memory.usage_in_bytes").read())) // mb
    except Exception:  # noqa: BLE001 — нет cgroup v1
        pass
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        pass
    return None


def safe_workers(requested: int, available_mb: int | None) -> int:
    """Потоков распознавания не больше, чем позволяет свободная память (по PER_WORKER_MB на поток после резерва RESERVE_MB); не меньше одного.
    Нехватка памяти убивала процесс, и загрузка оставалась «running» с нулевой нагрузкой (владелец 08.11.2026)."""
    if available_mb is None:
        return max(1, requested)
    return max(1, min(requested, (available_mb - RESERVE_MB) // PER_WORKER_MB))


def ocr_workers() -> int:
    """Потоков распознавания сканов: в полной мощности — до 8 (все ядра, но не больше), иначе по бюджету процессора; в обоих режимах
    не больше, чем выдержит свободная память (проверяется перед каждой пачкой страниц)."""
    requested = max(settings.OCR_WORKERS, min(OCR_WORKERS_FULL_CAP, cores())) if _state["full"] else settings.OCR_WORKERS
    workers = safe_workers(requested, available_memory_mb())
    if workers < requested:
        logger.warning("Распознавание: потоков %d вместо %d — мало свободной памяти (%s МБ)", workers, requested, available_memory_mb())
    return workers


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
