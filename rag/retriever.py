"""Поиск релевантных чанков через pgvector."""

from dataclasses import dataclass

from sqlalchemy import select

from config import settings
from db.models import BookChunk
from db.session import async_session
from rag.embedder import embed_query


@dataclass
class ChunkResult:
    content: str
    subject: str
    author: str
    title: str
    distance: float


async def retrieve(question: str, top_k: int = settings.RETRIEVAL_TOP_K) -> list[ChunkResult]:
    query_embedding = embed_query(question)

    distance = BookChunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(
            BookChunk.content,
            BookChunk.subject,
            BookChunk.author,
            BookChunk.title,
            distance,
        )
        .order_by(distance)
        .limit(top_k)
    )

    async with async_session() as session:
        result = await session.execute(stmt)
        rows = result.all()

    return [
        ChunkResult(
            content=row.content,
            subject=row.subject,
            author=row.author,
            title=row.title,
            distance=row.distance,
        )
        for row in rows
    ]
