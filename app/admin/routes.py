"""Закрытая Admin Panel (раздел 2 дополнения к ТЗ).

Рабочее место владельца AI-системы. На этапе 1 работают три раздела: Dashboard
(расход и нагрузка), Models (TaskModelMap и профили моделей) и загрузка источников.
Остальные пункты меню перечислены с указанием этапа, на котором появятся.

Живёт в том же процессе и том же FastAPI-приложении, что бот и `/v1`: эмбеддер и
реранкер занимают 4.5 ГБ, второй копии в памяти не будет.
"""

import asyncio
import json
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
from app.evidence.viewer import fetch_evidence
from app.llm.prompts import clear_cache
from app.llm.registry import model_registry
from app.llm.task_map import task_model_map
from app.llm.usage import usage_for_request
from app.observability.context import request_context
from app.security.audit import audit
from app.security.auth import (
    ADMIN_COOKIE,
    check_admin_password,
    is_admin,
    issue_admin_session,
)
from app.workflows.ask import ask
from config import settings
from constants import (
    AUTHORITY_LEVELS,
    DEFAULT_AUTHORITY_LEVEL,
    DEFAULT_VERIFICATION_STATUS,
    FEEDBACK_REASONS,
    SOURCE_CLINREK,
    SOURCE_STATUSES,
    SOURCE_TEXTBOOK,
    SUBJECT_LABELS,
    VERIFICATION_STATUSES,
)
from db.crud import (
    create_eval_case,
    current_prompt,
    delete_book,
    list_answer_logs,
    list_books,
    list_eval_cases,
    list_prompt_versions,
    publish_prompt,
    set_answer_feedback,
    update_book,
)
from db.models import IngestJob, PromptVersion
from db.session import async_session
from evals.run import run_eval
from rag.generator import PROMPT_DEFAULTS
from rag.retriever import retrieve_with_diagnostics

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

# job_id → задача, чтобы можно было отменить конкретную зависшую/долгую
# загрузку из админки (см. cancel_job) — обычного множества выше для этого
# недостаточно, там задачи не связаны с id.
_ingest_tasks_by_job: dict[int, asyncio.Task] = {}

# Расширения, которые умеет разбирать rag/processor.py.
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}

# Последние прогоны /admin/evals — для сравнения «до/после» в текущей сессии
# процесса. Не персистится на диск: файловая система Timeweb эфемерна между
# деплоями, а прогон и так можно повторить в любой момент.
_recent_eval_reports: list[dict] = []
_MAX_RECENT_REPORTS = 5


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


@router.get("/retrieval", response_class=HTMLResponse)
async def retrieval_inspector(
    request: Request,
    q: str = "",
    source_type: str = "",
    subject: str = "",
):
    """Минимальный Retrieval Inspector (раздел 5 дополнения к ТЗ): произвольный
    тестовый запрос → итоговые чанки после fusion+reranking со скорами. Без
    раздельного показа промежуточных стадий (BM25/вектор отдельно) — расширяется
    позже, если понадобится глубже диагностировать конкретный плохой ответ."""
    if not is_admin(request):
        return _login_redirect()

    result = None
    if q.strip():
        _, result = await retrieve_with_diagnostics(
            q.strip(),
            source_type=source_type.strip() or None,
            subject=subject.strip().lower() or None,
        )

    return templates.TemplateResponse(
        request,
        "retrieval.html",
        {
            "q": q,
            "source_type": source_type,
            "subject": subject,
            "result": result,
            "subjects": SUBJECT_LABELS,
            "source_textbook": SOURCE_TEXTBOOK,
            "source_clinrek": SOURCE_CLINREK,
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
            "authority_levels": AUTHORITY_LEVELS,
            "default_authority_level": DEFAULT_AUTHORITY_LEVEL,
            "verification_statuses": VERIFICATION_STATUSES,
            "default_verification_status": DEFAULT_VERIFICATION_STATUS,
            "source_statuses": SOURCE_STATUSES,
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
    section: str = Form(""),
    topic: str = Form(""),
    edition: str = Form(""),
    year: str = Form(""),
    authority_level: str = Form(DEFAULT_AUTHORITY_LEVEL),
    verification_status: str = Form(DEFAULT_VERIFICATION_STATUS),
    language: str = Form("ru"),
):
    """Приём файла источника и запуск обработки в фоне.

    Через сайт учебник заливается целиком: ограничение Telegram Bot API в 20 МБ
    здесь не действует. Файл пишется на диск потоком, чтобы не держать сотни
    мегабайт в памяти процесса, где уже живут две модели.
    """
    if not is_admin(request):
        return _login_redirect()

    # Мобильная клавиатура автоматически делает первую букву обычного текстового
    # поля заглавной ("pathphys" → "Pathphys") — глазами на планшете легко не
    # заметить. Коды предметов везде в системе (меню бота, фильтры поиска)
    # строго нижним регистром: несовпадение регистра значит "разные предметы",
    # и учебник молча выпадает из поиска. Нормализуем здесь, а не полагаемся на
    # то, что каждый администратор аккуратно вводит текст с любого устройства.
    subject = subject.strip().lower()

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
    year_value = int(year) if year.strip().isdigit() else None

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
        _run_ingest(
            job_id,
            tmp_path,
            effective_title,
            author.strip(),
            subject,
            source_type,
            section.strip() or None,
            topic.strip() or None,
            edition.strip() or None,
            year_value,
            authority_level,
            verification_status,
            language.strip() or "ru",
        )
    )
    _ingest_tasks.add(task)
    _ingest_tasks_by_job[job_id] = task
    task.add_done_callback(_ingest_tasks.discard)
    task.add_done_callback(lambda _t, jid=job_id: _ingest_tasks_by_job.pop(jid, None))
    return RedirectResponse(url="/admin/sources", status_code=303)


async def _run_ingest(
    job_id: int,
    file_path: str,
    title: str,
    author: str,
    subject: str,
    source_type: str,
    section: str | None = None,
    topic: str | None = None,
    edition: str | None = None,
    year: int | None = None,
    authority_level: str = DEFAULT_AUTHORITY_LEVEL,
    verification_status: str = DEFAULT_VERIFICATION_STATUS,
    language: str = "ru",
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
            chunks = await load_book(
                file_path,
                subject,
                author,
                title,
                source_type=source_type,
                section=section,
                topic=topic,
                edition=edition,
                year=year,
                authority_level=authority_level,
                verification_status=verification_status,
                language=language,
            )
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


@router.post("/jobs/{job_id}/delete")
async def cancel_job(request: Request, job_id: int):
    """Отменяет зависшую/долгую фоновую загрузку и убирает её из списка.

    Пока задача висит в pending/running/error, у неё ещё нет строки в таблице
    Book (при ошибке load_book делает rollback ДО commit — книга и чанки не
    создаются вовсе), поэтому обычное удаление источника здесь не применимо —
    нужен отдельный путь именно для записи в IngestJob.
    """
    if not is_admin(request):
        return _login_redirect()

    task = _ingest_tasks_by_job.get(job_id)
    if task is not None and not task.done():
        task.cancel()

    async with async_session() as session:
        job = await session.get(IngestJob, job_id)
        title = job.title if job is not None else str(job_id)
        if job is not None:
            await session.delete(job)
            await session.commit()

    await audit("source_ingest_cancelled", target=title, details=f"job_id={job_id}")
    return RedirectResponse(url="/admin/sources", status_code=303)


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
    section: str = Form(""),
    topic: str = Form(""),
    edition: str = Form(""),
    year: str = Form(""),
    authority_level: str = Form(""),
    verification_status: str = Form(""),
    language: str = Form(""),
):
    """Правка метаданных источника без переиндексации: чанки и эмбеддинги
    не трогаем, только метаданные (и их денормализованные копии в BookChunk,
    см. db.crud.update_book)."""
    if not is_admin(request):
        return _login_redirect()
    year_value = int(year) if year.strip().isdigit() else None
    async with async_session() as session:
        book = await update_book(
            session,
            book_id,
            title=title,
            author=author,
            subject=subject,
            section=section,
            topic=topic,
            edition=edition,
            year=year_value,
            authority_level=authority_level,
            verification_status=verification_status,
            language=language,
        )
        if book is None:
            await session.commit()
            return RedirectResponse(url="/admin/sources?error=not_found", status_code=303)
        new_title = book.title
        await session.commit()
    await audit("source_edit", target=new_title, details=f"book_id={book_id}")
    return RedirectResponse(url="/admin/sources", status_code=303)


@router.post("/sources/{book_id}/status")
async def set_source_status(request: Request, book_id: int, status: str = Form(...)):
    """Включение/отключение/архивация источника без удаления чанков и эмбеддингов
    (§8 ТЗ, раздел 3 дополнения): retrieval фильтрует по этому статусу через JOIN
    на books (rag/retriever.py), переиндексация при возврате не нужна."""
    if not is_admin(request):
        return _login_redirect()
    if status not in dict(SOURCE_STATUSES):
        return RedirectResponse(url="/admin/sources?error=bad_status", status_code=303)
    async with async_session() as session:
        book = await update_book(session, book_id, status=status)
        if book is None:
            await session.commit()
            return RedirectResponse(url="/admin/sources?error=not_found", status_code=303)
        title = book.title
        await session.commit()
    await audit("source_status", target=title, details=f"book_id={book_id}, status={status}")
    return RedirectResponse(url="/admin/sources", status_code=303)


# --- AI Playground (раздел 4 дополнения к ТЗ) ------------------------------------


@router.get("/playground", response_class=HTMLResponse)
async def playground_form(request: Request):
    if not is_admin(request):
        return _login_redirect()
    return templates.TemplateResponse(
        request,
        "playground.html",
        {
            "subjects": SUBJECT_LABELS,
            "source_textbook": SOURCE_TEXTBOOK,
            "source_clinrek": SOURCE_CLINREK,
            "result": None,
            "usage": None,
            "evidence_by_id": {},
            "question": "",
            "subject": "",
            "source_type": SOURCE_TEXTBOOK,
        },
    )


@router.post("/playground", response_class=HTMLResponse)
async def playground_run(
    request: Request,
    question: str = Form(...),
    source_type: str = Form(SOURCE_TEXTBOOK),
    subject: str = Form(""),
):
    """Проверка Student AI прямо из админки, без образовательного сайта (раздел
    4 дополнения к ТЗ): пользовательский вид ответа + техпанель одновременно."""
    if not is_admin(request):
        return _login_redirect()

    subject_value = subject.strip().lower() or None
    with request_context(user_id="admin", channel="admin", workflow="PLAYGROUND"):
        result = await ask(question, source_type=source_type, subject=subject_value)

    usage = (
        await usage_for_request(result.request_id)
        if result.request_id
        else {"calls": 0, "inputTokens": 0, "outputTokens": 0, "costRub": 0.0}
    )

    # «Открыть оригинал» под каждой цитатой (presigned S3, если источник его
    # предоставляет — см. app/evidence/viewer.py). Тот же lookup, что и у
    # /v1/evidence/{id}, не дублируем DB-запрос и presign.
    evidence_by_id = {}
    for c in result.citations:
        detail = await fetch_evidence(int(c["evidenceId"]))
        if detail is not None:
            evidence_by_id[c["evidenceId"]] = detail

    return templates.TemplateResponse(
        request,
        "playground.html",
        {
            "subjects": SUBJECT_LABELS,
            "source_textbook": SOURCE_TEXTBOOK,
            "source_clinrek": SOURCE_CLINREK,
            "result": result,
            "usage": usage,
            "evidence_by_id": evidence_by_id,
            "question": question,
            "subject": subject,
            "source_type": source_type,
        },
    )


# --- Answer Inspector (раздел 8 дополнения к ТЗ) ---------------------------------


def _answer_view(log) -> dict:
    """AnswerLog с распарсенными JSON-полями — для шаблонов (см. db.models.AnswerLog)."""
    return {
        "id": log.id,
        "request_id": log.request_id,
        "channel": log.channel,
        "user_id": log.user_id,
        "question": log.question,
        "answer": log.answer,
        "subject": log.subject,
        "workflow": log.workflow,
        "intent": log.intent,
        "verified": log.verified,
        "latency_ms": log.latency_ms,
        "created_at": log.created_at,
        "citations": json.loads(log.citations),
        "conflicts": json.loads(log.conflicts),
        "diagnostics": json.loads(log.diagnostics),
        "feedback_reason": log.feedback_reason,
        "feedback_note": log.feedback_note,
    }


@router.get("/answers", response_class=HTMLResponse)
async def answers_list(request: Request, verified: str = "", flagged: str = ""):
    if not is_admin(request):
        return _login_redirect()
    verified_filter = {"true": True, "false": False}.get(verified)
    async with async_session() as session:
        logs = await list_answer_logs(session, verified=verified_filter, only_flagged=bool(flagged))
    return templates.TemplateResponse(
        request,
        "answers.html",
        {
            "logs": [_answer_view(log) for log in logs],
            "verified_filter": verified,
            "flagged_filter": flagged,
            "feedback_reasons": FEEDBACK_REASONS,
        },
    )


@router.post("/answers/{answer_id}/feedback")
async def answers_feedback(
    request: Request, answer_id: int, reason: str = Form(""), note: str = Form("")
):
    """Разбор плохого ответа (раздел 8/9 дополнения к ТЗ) — причина видна в
    списке, дальше по ней можно добавить кейс в Evals (раздел 13)."""
    if not is_admin(request):
        return _login_redirect()
    async with async_session() as session:
        log = await set_answer_feedback(session, answer_id, reason, note)
        if log is None:
            await session.commit()
            return RedirectResponse(url="/admin/answers?error=not_found", status_code=303)
        await session.commit()
    await audit("answer_feedback", target=str(answer_id), details=f"reason={reason}")
    return RedirectResponse(url="/admin/answers", status_code=303)


# --- Prompts & Policies (раздел 10 дополнения к ТЗ, упрощённая версия) -----------


@router.get("/prompts", response_class=HTMLResponse)
async def prompts_list(request: Request):
    """Редактирование → публикация → откат, без автоматического eval-гейта перед
    публикацией (сознательное упрощение полного DRAFT→TEST→EVAL→PUBLISH из ТЗ —
    см. plans/medap-ai/03_evidence.md). Константы в rag/generator.py остаются
    дефолтом, пока для ключа нет production-версии в БД."""
    if not is_admin(request):
        return _login_redirect()
    rows = {}
    async with async_session() as session:
        for key, default in PROMPT_DEFAULTS.items():
            active = await current_prompt(session, key)
            versions = await list_prompt_versions(session, key)
            rows[key] = {
                "content": active.content if active else default,
                "is_default": active is None,
                "versions": versions,
            }
    return templates.TemplateResponse(request, "prompts.html", {"rows": rows})


@router.post("/prompts/{key}/publish")
async def prompts_publish(request: Request, key: str, content: str = Form(...)):
    if not is_admin(request):
        return _login_redirect()
    if key not in PROMPT_DEFAULTS:
        return RedirectResponse(url="/admin/prompts?error=bad_key", status_code=303)
    async with async_session() as session:
        await publish_prompt(session, key, content, created_by="admin")
        await session.commit()
    clear_cache(key)
    await audit("prompt_publish", target=key)
    return RedirectResponse(url="/admin/prompts", status_code=303)


@router.post("/prompts/{key}/rollback/{version_id}")
async def prompts_rollback(request: Request, key: str, version_id: int):
    """Откатывает к тексту старой версии, публикуя его КАК НОВУЮ версию — история
    не переписывается, видно, что и когда откатывали."""
    if not is_admin(request):
        return _login_redirect()
    async with async_session() as session:
        old = await session.get(PromptVersion, version_id)
        if old is None or old.prompt_key != key:
            await session.commit()
            return RedirectResponse(url="/admin/prompts?error=not_found", status_code=303)
        await publish_prompt(session, key, old.content, created_by="admin(rollback)")
        await session.commit()
    clear_cache(key)
    await audit("prompt_rollback", target=key, details=f"from version_id={version_id}")
    return RedirectResponse(url="/admin/prompts", status_code=303)


# --- Evals (раздел 13 дополнения к ТЗ) -------------------------------------------


@router.get("/evals", response_class=HTMLResponse)
async def evals_page(request: Request, prefill_question: str = "", prefill_subject: str = ""):
    if not is_admin(request):
        return _login_redirect()
    async with async_session() as session:
        db_cases = await list_eval_cases(session)
    return templates.TemplateResponse(
        request,
        "evals.html",
        {
            "db_cases": db_cases,
            "reports": list(reversed(_recent_eval_reports)),
            "source_textbook": SOURCE_TEXTBOOK,
            "source_clinrek": SOURCE_CLINREK,
            "prefill_question": prefill_question,
            "prefill_subject": prefill_subject,
        },
    )


@router.post("/evals/add")
async def evals_add(
    request: Request,
    question: str = Form(...),
    category: str = Form("uncategorized"),
    answerable: str = Form("true"),
    source_type: str = Form(SOURCE_TEXTBOOK),
    subject: str = Form(""),
    expect_source: str = Form(""),
    expect_page: str = Form(""),
    expect_keywords: str = Form(""),
    note: str = Form(""),
):
    """Кнопка «Добавить в Eval Dataset» (раздел 9/13 дополнения к ТЗ) — можно
    предзаполнить из строки Answer Inspector query-параметрами."""
    if not is_admin(request):
        return _login_redirect()
    keywords = [k.strip() for k in expect_keywords.split(",") if k.strip()] or None
    async with async_session() as session:
        await create_eval_case(
            session,
            question=question.strip(),
            category=category.strip() or "uncategorized",
            answerable=answerable == "true",
            source_type=source_type,
            subject=subject.strip().lower() or None,
            expect_source=expect_source.strip() or None,
            expect_page=int(expect_page) if expect_page.strip().isdigit() else None,
            expect_keywords=json.dumps(keywords, ensure_ascii=False) if keywords else None,
            note=note.strip() or None,
        )
        await session.commit()
    await audit("eval_case_add", target=question[:80])
    return RedirectResponse(url="/admin/evals", status_code=303)


@router.post("/evals/run")
async def evals_run(request: Request, retrieval_only: str = Form("")):
    """Синхронный прогон (в рамках одного HTTP-запроса — датасет пока небольшой;
    если вырастет, потребуется фоновая задача по аналогии с _run_ingest)."""
    if not is_admin(request):
        return _login_redirect()
    report = await run_eval(retrieval_only=bool(retrieval_only))
    _recent_eval_reports.append(report)
    del _recent_eval_reports[:-_MAX_RECENT_REPORTS]
    await audit("eval_run", details=f"cases={report['summary']['cases']}")
    return RedirectResponse(url="/admin/evals", status_code=303)
