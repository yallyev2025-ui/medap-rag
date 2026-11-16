"""Повторяемое удаление личных документов и TTL разового разбора."""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from app.storage import s3
from constants import SOURCE_USER_DOCUMENT
from db.crud import delete_book
from db.models import AIUsageEvent, AnswerLog, AuditLog, Book, BookChunk
from db.session import async_session

logger = logging.getLogger(__name__)


def _remove_objects(book_id: int) -> None:
    if not s3.is_configured():
        raise RuntimeError("Хранилище оригиналов недоступно: удаление нужно повторить")
    client = s3._client()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=s3.settings.S3_BUCKET, Prefix=f"sources/{book_id}/"):
        objects = [{"Key": row["Key"]} for row in page.get("Contents", [])]
        if objects:
            result = client.delete_objects(Bucket=s3.settings.S3_BUCKET, Delete={"Objects": objects})
            if result.get("Errors"):
                raise RuntimeError("Не все объекты удалены: удаление нужно повторить")


async def remove_private_book(session, book_id: int) -> None:
    book = await session.get(Book, book_id)
    if book is None:
        return
    if book.source_type != SOURCE_USER_DOCUMENT:
        raise ValueError("Удаление личных данных не может затрагивать общие учебники")
    if book.file_path or book.pages_path:
        await asyncio.to_thread(_remove_objects, book_id)
    await delete_book(session, book_id)


async def erase_user_data(user_id: str) -> int:
    async with async_session() as session:
        ids = list((await session.execute(
            select(BookChunk.book_id).where(BookChunk.user_id == user_id, BookChunk.source_type == SOURCE_USER_DOCUMENT).distinct()
        )).scalars())
        for book_id in ids:
            await remove_private_book(session, book_id)
        await session.execute(delete(AnswerLog).where(AnswerLog.user_id == user_id))
        await session.execute(delete(AuditLog).where(AuditLog.actor == f"user:{user_id}"))
        # Суммы расхода сохраняются без связи с удалённым аккаунтом.
        await session.execute(update(AIUsageEvent).where(AIUsageEvent.user_id == user_id).values(user_id=None))
        await session.commit()
    return len(ids)


async def cleanup_expired_documents() -> int:
    async with async_session() as session:
        ids = list((await session.execute(select(Book.id).where(
            Book.source_type == SOURCE_USER_DOCUMENT, Book.expires_at <= datetime.now(timezone.utc),
        ))).scalars())
        for book_id in ids:
            await remove_private_book(session, book_id)
        await session.commit()
    return len(ids)


async def cleanup_loop() -> None:
    while True:
        try:
            await cleanup_expired_documents()
        except Exception:
            logger.exception("Очистка временных документов не завершена; повтор через 10 минут")
        await asyncio.sleep(600)
