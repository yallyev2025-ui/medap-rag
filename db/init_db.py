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
        # Миграция v2: разделение на учебники/клинреки + состояние выбора у пользователя.
        # DEFAULT 'учебник' проставит существующим ~12.4K чанкам корректный тип автоматически.
        await conn.execute(
            text("ALTER TABLE books ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) NOT NULL DEFAULT 'учебник';")
        )
        await conn.execute(
            text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) NOT NULL DEFAULT 'учебник';")
        )
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_source_type VARCHAR(20);"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_subject VARCHAR(100);"))
        # Индекс под фильтрацию поиска по режиму/предмету.
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_book_chunks_source_subject ON book_chunks (source_type, subject);")
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(init_db())
