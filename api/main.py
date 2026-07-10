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

from config import settings
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

app = FastAPI(title="medap-rag search API")


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
