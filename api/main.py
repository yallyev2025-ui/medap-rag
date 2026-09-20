"""HTTP-обёртка над `rag.retriever.retrieve()` для внешних потребителей (например,
сценариста рилсов в репозитории `medap`). Бот (`bot/main.py`) — основной потребитель
для людей через Telegram; этот API — для сервисов.

Запускается В ТОМ ЖЕ процессе, что и бот (см. `bot/main.py`), а не отдельным
Railway-сервисом — чтобы не грузить эмбеддер (~2.2 ГБ) и реранкер (~2.3 ГБ) дважды.
Из-за этого сервис в Railway теперь должен быть проброшен как web (слушает $PORT),
а не как чистый polling-воркер — см. README про переменные окружения.

Авторизация — простой статический ключ в заголовке `X-API-Key` (сверяется с
`RAG_API_KEY` из конфига). Этого достаточно для внутреннего сервис-сервис вызова
(сценарист medap → этот API); если понадобится публичный доступ — усилить отдельно.
"""

import logging

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.admin.routes import router as admin_router
from app.api.v1 import router as v1_router

from config import settings
from constants import SOURCE_TEXTBOOK, clinrek_label, subject_label
from db.models import Book
from db.session import async_session
from rag.generator import NO_CONTEXT_ANSWER, generate_answer, relevant_chunks
from rag.retriever import ChunkResult, retrieve

logger = logging.getLogger(__name__)

app = FastAPI(title="MedAP Student AI")

# /v1 — стабильный контракт для образовательного сайта MedAP (§27 ТЗ),
# /admin — закрытая панель управления (дополнение к ТЗ, раздел 2).
# Оба подключаются к ЭТОМУ приложению, а не поднимают свой процесс: эмбеддер и
# реранкер занимают ~4.5 ГБ и должны жить в памяти в единственном экземпляре.
app.include_router(v1_router)
app.include_router(admin_router)


def _source_str(c: ChunkResult) -> str:
    """[Автор, Название, стр. N] — тот же формат, что бот показывает пользователю."""
    head = f"{c.author}, {c.title}" if c.author else c.title
    if c.page_from is None:
        return head
    if c.page_from == c.page_to:
        return f"{head}, стр. {c.page_from}"
    return f"{head}, стр. {c.page_from}-{c.page_to}"


class SearchRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Поисковый запрос (тема/вопрос)")
    source_type: str | None = Field(
        None, description="'учебник' или 'клинрек'; None — без фильтра (не рекомендуется, базы разной природы)"
    )
    subject: str | None = Field(
        None,
        description="Предмет учебника (pathanatomy/pathphys/physiology/anatomy/biochemistry/pharmacology/other) "
        "или категория клинрека (взрослые/дети/взрослые_и_дети); None — без фильтра по предмету",
    )
    top_k: int | None = Field(None, ge=1, le=50, description="Сколько фрагментов вернуть после реранка; по умолчанию — настройка сервиса")
    focus_document: bool = Field(
        False, description="Только для клинреков: сфокусироваться на одной доминирующей рекомендации вместо смеси нескольких"
    )


class SearchResultItem(BaseModel):
    content: str
    subject: str
    author: str
    title: str
    page_from: int | None
    page_to: int | None
    distance: float
    rerank_score: float | None


def _check_api_key(x_api_key: str = Header(default="")) -> None:
    if not settings.RAG_API_KEY:
        logger.error("RAG_API_KEY не задан в конфиге сервиса — /search отключён")
        raise HTTPException(status_code=503, detail="RAG API не сконфигурирован (нет RAG_API_KEY на сервере)")
    if x_api_key != settings.RAG_API_KEY:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий X-API-Key")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


class SubjectItem(BaseModel):
    code: str  # значение subject как в БД — передавай обратно в /answer и /search
    label: str  # человекочитаемое название


@app.get("/subjects", response_model=list[SubjectItem])
async def subjects(source_type: str = SOURCE_TEXTBOOK, _: None = Depends(_check_api_key)) -> list[SubjectItem]:
    """Реально загруженные предметы (для учебников — динамически, что фактически
    есть в базе, а не статичный список; так внешний потребитель — сценарист рилсов
    в medap — не привязан к жёсткому набору дисциплин и подхватывает новые предметы
    без изменений кода). Для клинреков категории — фиксированная таксономия
    (взрослые/дети/взрослые_и_дети), это не то, что "загружено", а всегда одни и те
    же 3 значения — их проще жёстко задать на стороне потребителя (см. constants.CLINREK_CATEGORIES),
    но эндпоинт всё равно отвечает и на них для единообразия API.
    """
    stmt = select(Book.subject).where(Book.source_type == source_type).distinct().order_by(Book.subject)
    async with async_session() as session:
        result = await session.execute(stmt)
        codes = [row[0] for row in result.all()]

    label_fn = subject_label if source_type == SOURCE_TEXTBOOK else clinrek_label
    return [SubjectItem(code=c, label=label_fn(c)) for c in codes]


@app.post("/search", response_model=list[SearchResultItem])
async def search(req: SearchRequest, _: None = Depends(_check_api_key)) -> list[SearchResultItem]:
    kwargs = dict(source_type=req.source_type, subject=req.subject, focus_document=req.focus_document)
    if req.top_k is not None:
        kwargs["top_k"] = req.top_k

    chunks = await retrieve(req.question, **kwargs)
    return [
        SearchResultItem(
            content=c.content,
            subject=c.subject,
            author=c.author,
            title=c.title,
            page_from=c.page_from,
            page_to=c.page_to,
            distance=c.distance,
            rerank_score=c.rerank_score,
        )
        for c in chunks
    ]


class AnswerRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Тема/вопрос для ответа по материалам")
    source_type: str = Field(SOURCE_TEXTBOOK, description="'учебник' (по умолчанию) или 'клинрек'")
    subject: str | None = Field(
        None, description="Предмет учебника (pathanatomy/pathphys/physiology/...) или категория клинрека"
    )
    top_k: int | None = Field(None, ge=1, le=50)


class AnswerResponse(BaseModel):
    found: bool  # False = по теме в материалах ничего релевантного не нашлось
    answer: str  # готовый, заземлённый и проверенный ответ по учебникам (или "" если found=false)
    sources: list[str]  # ["Автор, Название, стр. N", ...]


@app.post("/answer", response_model=AnswerResponse)
async def answer(req: AnswerRequest, _: None = Depends(_check_api_key)) -> AnswerResponse:
    """Готовый ОТВЕТ бота по материалам (retrieve → generate_answer с проверкой
    заземления), а не сырые фрагменты. Это то, что потребитель (сценарист рилсов
    в medap) должен «переделывать под видео»: факты уже собраны, сверены с учебником
    и снабжены источниками.

    ВАЖНО: в отличие от бота, здесь при отсутствии материалов НЕ включается фолбэк
    на общие знания ИИ — возвращаем `found=false, answer=""`. Для генерации видео
    факты обязаны быть из учебников, поэтому «нет материала» = честный отказ, а
    решение (стоп/повтор) принимает вызывающая сторона.
    """
    kwargs = dict(source_type=req.source_type, subject=req.subject)
    if req.top_k is not None:
        kwargs["top_k"] = req.top_k

    chunks = await retrieve(req.question, **kwargs)
    relevant = relevant_chunks(chunks)
    if not relevant:
        return AnswerResponse(found=False, answer="", sources=[])

    text = await generate_answer(req.question, chunks, source_type=req.source_type)
    # generate_answer может всё равно отказать, если контекст лишь упоминает тему
    # без раскрытия (см. его системный промпт) — тогда возвращаем то же "не найдено".
    if text.strip() == NO_CONTEXT_ANSWER.strip():
        return AnswerResponse(found=False, answer="", sources=[])

    sources = [_source_str(c) for c in relevant]
    return AnswerResponse(found=True, answer=text, sources=sources)
