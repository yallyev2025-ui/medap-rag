"""Режим загрузки «щадящий / полная мощность» и отметка прерванных загрузок (владелец 08.11.2026).
Запуск: python scripts/test_ingest_mode.py
Часть с базой выполняется, только если задан TEST_DATABASE_URL (postgresql+asyncpg://…, нужны права создавать таблицы app_flags и ingest_jobs).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "postgresql+asyncpg://u:p@localhost/db"
if os.environ.get("TEST_DATABASE_URL"):
    os.environ.setdefault("DATABASE_SSL_MODE", "disable")
os.environ.setdefault("OPENAI_API_KEY", "x")

from app import ingest_mode  # noqa: E402
from config import settings  # noqa: E402

fails = 0


def check(cond: bool, msg: str) -> None:
    global fails
    print(("OK   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


# --- логика без базы ---
ingest_mode._state["full"] = False
check(not ingest_mode.is_full_power(), "по умолчанию — щадящий режим")
check(ingest_mode.ingest_threads() == settings.INGEST_THREADS, "щадящий: потоки эмбеддера как в настройках")
check(ingest_mode.ingest_pause() == settings.INGEST_PAUSE_S and settings.INGEST_PAUSE_S > 0, "щадящий: пауза между пачками есть")
check(ingest_mode.ocr_workers() == settings.OCR_WORKERS, "щадящий: распознавание по бюджету процессора")

ingest_mode._state["full"] = True
real = os.cpu_count
for cores in (1, 4, 11, 64):
    os.cpu_count = lambda c=cores: c  # type: ignore[assignment]
    check(ingest_mode.ingest_threads() == cores, f"полная мощность, {cores} ядер: эмбеддер на всех {cores}")
    check(ingest_mode.ingest_pause() == 0.0, f"полная мощность, {cores} ядер: пауз нет")
    w = ingest_mode.ocr_workers()
    check(w >= settings.OCR_WORKERS and w <= max(settings.OCR_WORKERS, ingest_mode.OCR_WORKERS_FULL_CAP), f"полная мощность, {cores} ядер: распознавание {w} потоков (не меньше щадящего, потолок по памяти)")
os.cpu_count = lambda: 11  # type: ignore[assignment]
check(ingest_mode.ocr_workers() == 11, "11 ядер → 11 потоков распознавания")
os.cpu_count = real  # type: ignore[assignment]
ingest_mode._state["full"] = False

# --- шаблон админки: кнопка и пояснения в обоих режимах ---
from jinja2 import Environment, FileSystemLoader  # noqa: E402

env = Environment(loader=FileSystemLoader(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "admin", "templates")), autoescape=True)
tpl_source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "admin", "templates", "sources.html"), encoding="utf-8").read()
start = tpl_source.index("<h1 style=\"font-size:18px\">Скорость загрузки</h1>")
block = "<section>\n  " + tpl_source[start : tpl_source.index("</section>", start) + len("</section>")]
for full in (False, True):
    html = env.from_string(block).render(ingest_full_power=full, ingest_cores=11)
    check(("ПОЛНАЯ МОЩНОСТЬ" in html) == full and ('name="full" value="%d"' % (0 if full else 1)) in html and "11" in html, f"админка: блок скорости при full_power={full}")

# --- таблица загрузок: «Движение» (нулевая нагрузка не должна выглядеть как работа) ---
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

start = tpl_source.index('<h1 style="font-size:18px">Обработка</h1>')
jobs_block = "<section>\n  " + tpl_source[start : tpl_source.index("</section>", start) + len("</section>")]
now = datetime.now(timezone.utc)


def job(status: str, minutes_ago: int, title: str) -> SimpleNamespace:
    return SimpleNamespace(id=1, filename=f"{title}.pdf", title=title, subject="x", status=status, stage="эмбеддинги 96/540", chunks_count=None, error=None, updated_at=now - timedelta(minutes=minutes_ago))


html = env.from_string(jobs_block).render(now=now, jobs=[job("running", 3, "живая"), job("running", 45, "зависшая"), job("done", 600, "готовая")])
check("обновлено 3 мин назад" in html, "живая загрузка: «обновлено 3 мин назад»")
check("нет движения 45 мин" in html and "зависла" in html, "зависшая: красное «нет движения 45 мин»")
check(html.count("нет движения") == 1 and "эмбеддинги 96/540" in html, "готовая не помечается, стадия с прогрессом видна")

# --- с базой ---
if os.environ.get("TEST_DATABASE_URL"):
    from sqlalchemy import select  # noqa: E402

    from app.workflows.book_admin import mark_stale_ingest_jobs_failed  # noqa: E402
    from db.models import AppFlag, IngestJob  # noqa: E402
    from db.session import async_session, engine  # noqa: E402

    async def db_part() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: IngestJob.__table__.create(c, checkfirst=True))
            await conn.run_sync(lambda c: AppFlag.__table__.create(c, checkfirst=True))
            await conn.exec_driver_sql("DELETE FROM ingest_jobs")
            await conn.exec_driver_sql("DELETE FROM app_flags")
        await ingest_mode.set_full_power(True)
        ingest_mode._state["full"] = False
        check(await ingest_mode.load_from_db() is True and ingest_mode.is_full_power(), "режим «полная мощность» сохраняется в БД и поднимается после перезапуска")
        await ingest_mode.set_full_power(False)
        check(await ingest_mode.load_from_db() is False, "выключение тоже сохраняется")
        async with async_session() as s:
            row = await s.scalar(select(AppFlag).where(AppFlag.key == ingest_mode.FLAG_KEY))
            check(row is not None and row.value == "0", "в таблице одна строка флага")
        async with async_session() as s:
            for status in ("pending", "running", "done", "error"):
                s.add(IngestJob(filename=f"{status}.pdf", title=status, subject="x", status=status, stage="parsing", error=("старая ошибка" if status == "error" else None)))
            await s.commit()
        n = await mark_stale_ingest_jobs_failed()
        check(n == 2, f"прерванных перезапуском помечено 2 (получилось {n})")
        async with async_session() as s:
            jobs = {j.title: j for j in (await s.execute(select(IngestJob))).scalars()}
        check(jobs["pending"].status == "error" and jobs["running"].status == "error" and "Прервано перезапуском" in (jobs["running"].error or ""), "pending и running стали ошибкой с понятным текстом")
        check(jobs["done"].status == "done" and jobs["error"].error == "старая ошибка", "готовые и уже ошибочные не тронуты")
        check(await mark_stale_ingest_jobs_failed() == 0, "повторная отметка ничего не меняет")
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DROP TABLE ingest_jobs")
            await conn.exec_driver_sql("DROP TABLE app_flags")
        await engine.dispose()

    asyncio.run(db_part())
else:
    print("SKIP часть с базой (нет TEST_DATABASE_URL)")

print("OK" if fails == 0 else f"ПРОВАЛ: {fails}")
sys.exit(1 if fails else 0)
