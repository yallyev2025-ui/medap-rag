"""Создание расширения pgvector и всех таблиц. Запуск: python -m db.init_db"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import settings
from db.models import Base


async def init_db() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        await conn.run_sync(Base.metadata.create_all)
        # Миграция для таблиц, созданных до появления номеров страниц в чанках.
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS page_from INTEGER;"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS page_to INTEGER;"))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(init_db())
