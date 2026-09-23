"""Закрытая Admin Panel (раздел 2 дополнения к ТЗ).

Рабочее место владельца AI-системы. На этапе 1 работают три раздела: Dashboard
(расход и нагрузка), Models (TaskModelMap и профили моделей) и загрузка источников.
Остальные пункты меню перечислены с указанием этапа, на котором появятся.

Живёт в том же процессе и том же FastAPI-приложении, что бот и `/v1`: эмбеддер и
реранкер занимают 4.5 ГБ, второй копии в памяти не будет.
"""

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.admin.stats import dashboard_stats
from app.llm.registry import model_registry
from app.llm.task_map import task_model_map
from app.observability.context import request_context
from app.security.audit import audit
from app.security.auth import (
    ADMIN_COOKIE,
    check_admin_password,
    is_admin,
    issue_admin_session,
)
from config import settings
from constants import SOURCE_CLINREK, SOURCE_TEXTBOOK, SUBJECT_LABELS
from db.crud import delete_book, list_books, update_book
from db.models import IngestJob
from db.session import async_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Ссылки на фоновые задачи обработки источников. Без этого asyncio может
# собрать fire-and-forget задачу сборщиком мусора до её завершения — задача
# просто исчезает без единой строки в логе (это документированная ловушка
# asyncio.create_task, а не гипотетическая: "Task was destroyed but it is
# pending!"). Загрузка учебника занимает минуты — самое подходящее окно для
# такого молчаливого обрыва, особенно при нагрузке на память от ML-моделей.
_ingest_tasks: set[asyncio.Task] = set()

# Расширения, которые умеет разбирать rag/processor.py.
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}


def _login_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form(...)):
    if not check_admin_password(password):
        await audit("admin_login_failed", actor="unknown")
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный пароль"}, status_code=401
        )
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE,
        issue_admin_session(),
        httponly=True,
        samesite="lax",
        # На Timeweb приложение отдаётся по HTTPS; локально по HTTP кука тоже нужна,
        # поэтому secure включается только вне отладки.
        secure=not settings.ADMIN_SESSION_SECRET.startswith("dev-"),
        max_age=24 * 60 * 60,
    )
    await audit("admin_login")
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not is_admin(request):
        return _login_redirect()
    stats = await dashboard_stats()
    return templates.TemplateResponse(request, "dashboard.html", {"stats": stats})


@router.get("/models", response_class=HTMLResponse)
async def models(request: Request):
    if not is_admin(request):
        return _login_redirect()
    return templates.TemplateResponse(
        request,
        "models.html",
        {
            "registry": model_registry(),
            "task_map": {task.value: provider for task, provider in task_model_map().items()},
        },
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources(request: Request):
    if not is_admin(request):
        return _login_redirect()
    async with async_session() as session:
        books = await list_books(session)
        jobs = (
            (await session.execute(select(IngestJob).order_by(IngestJob.id.desc()).limit(20)))
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "books": books,
            "jobs": jobs,
            "subjects": SUBJECT_LABELS,
            "source_textbook": SOURCE_TEXTBOOK,
            "source_clinrek": SOURCE_CLINREK,
            "max_upload_mb": settings.MAX_UPLOAD_MB,
        },
    )


@router.post("/sources/upload")
async def upload_source(
    request: Request,
    file: UploadFile = File(...),
    source_type: str = Form(SOURCE_TEXTBOOK),
    subject: str = Form(...),
    author: str = Form(""),
    title: str = Form(""),
):
    """Приём файла источника и запуск обработки в фоне.

    Через сайт учебник заливается целиком: ограничение Telegram Bot API в 20 МБ
    здесь не действует. Файл пишется на диск потоком, чтобы не держать сотни
    мегабайт в памяти процесса, где уже живут две модели.
    """
    if not is_admin(request):
        return _login_redirect()

    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        return RedirectResponse(url="/admin/sources?error=extension", status_code=303)

    limit_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    fd, tmp_path = tempfile.mkstemp(suffix=extension)
    size = 0
    with os.fdopen(fd, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit_bytes:
                out.close()
                os.remove(tmp_path)
                return RedirectResponse(url="/admin/sources?error=too_large", status_code=303)
            out.write(chunk)

    effective_title = title.strip() or Path(file.filename or "источник").stem

    async with async_session() as session:
        job = IngestJob(
            filename=file.filename or "",
            title=effective_title,
            author=author.strip(),
            subject=subject,
            source_type=source_type,
            size_bytes=size,
            status="pending",
            stage="upload",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    await audit("source_upload", target=effective_title, details=f"{size} bytes, subject={subject}")
    # Обработка учебника занимает минуты — отвечаем сразу, прогресс виден в списке.
    # Ссылку на задачу сохраняем в _ingest_tasks (см. комментарий там) и убираем
    # из множества по завершении — иначе накопленные ссылки на завершённые
    # задачи держались бы в памяти вечно.
    task = asyncio.create_task(
        _run_ingest(job_id, tmp_path, effective_title, author.strip(), subject, source_type)
    )
    _ingest_tasks.add(task)
    task.add_done_callback(_ingest_tasks.discard)
    return RedirectResponse(url="/admin/sources", status_code=303)


async def _run_ingest(
    job_id: int,
    file_path: str,
    title: str,
    author: str,
    subject: str,
    source_type: str,
) -> None:
    """Фоновая обработка источника через существующий конвейер загрузки.

    Переиспользуется `scripts.load_books.load_book` — та же функция, которой грузит
    бот, поэтому у сайта и Telegram один и тот же чанкинг и эмбеддинги.
    """
    from scripts.load_books import load_book

    with request_context(channel="admin", workflow="INGEST"):
        # Весь путь целиком в try/except: если упадёт даже самая первая отметка
        # статуса (например, БД моргнула на секунду), задача не должна тихо
        # исчезнуть, оставив запись висеть в "pending" без объяснений.
        try:
            await _set_job(job_id, status="running", stage="parsing")
            chunks = await load_book(file_path, subject, author, title, source_type=source_type)
            await _set_job(
                job_id,
                status="done",
                stage="ready",
                chunks_count=chunks,
                finished_at=datetime.now(timezone.utc),
            )
            logger.info("Источник «%s» загружен: %d чанков", title, chunks)
        except Exception as exc:
            logger.exception("Не удалось загрузить источник «%s»", title)
            await _set_job(
                job_id,
                status="error",
                stage="failed",
                error=str(exc),
                finished_at=datetime.now(timezone.utc),
            )
        finally:
            # Исходник больше не нужен: в БД лежат чанки и эмбеддинги. Хранение
            # оригинала в S3 появится на этапе 2 (нужно для показа фрагмента).
            if os.path.exists(file_path):
                os.remove(file_path)


async def _set_job(job_id: int, **fields) -> None:
    async with async_session() as session:
        job = await session.get(IngestJob, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        await session.commit()


@router.post("/sources/{book_id}/delete")
async def delete_source(request: Request, book_id: int):
    if not is_admin(request):
        return _login_redirect()
    async with async_session() as session:
        title = await delete_book(session, book_id)
        # Без commit() удаление откатывается при закрытии сессии — книга
        # и её чанки оставались бы в БД, хотя из списка пропадали до перезагрузки
        # страницы (пока лежат в identity map этой же сессии).
        await session.commit()
    await audit("source_delete", target=title or str(book_id))
    return RedirectResponse(url="/admin/sources", status_code=303)


@router.post("/sources/{book_id}/edit")
async def edit_source(
    request: Request,
    book_id: int,
    title: str = Form(""),
    author: str = Form(""),
    subject: str = Form(""),
):
    """Правка названия/автора/предмета без переиндексации: чанки и эмбеддинги
    не трогаем, только метаданные (и их денормализованные копии в BookChunk,
    см. db.crud.update_book)."""
    if not is_admin(request):
        return _login_redirect()
    async with async_session() as session:
        book = await update_book(session, book_id, title=title, author=author, subject=subject)
        if book is None:
            await session.commit()
            return RedirectResponse(url="/admin/sources?error=not_found", status_code=303)
        new_title = book.title
        await session.commit()
    await audit("source_edit", target=new_title, details=f"book_id={book_id}")
    return RedirectResponse(url="/admin/sources", status_code=303)
