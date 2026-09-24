"""Создание расширения pgvector и всех таблиц. Запуск: python -m db.init_db"""

import asyncio

from sqlalchemy import text

from db.models import Base
# Общий engine с уже настроенным SSL для asyncpg (см. db/session.py) — свой
# отдельный create_async_engine() здесь не заводим, чтобы не дублировать и не
# рассинхронизировать настройку SSL с остальным приложением.
from db.session import engine


async def init_db() -> None:
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
        # Режим «Разбор по симптомам» + лёгкая память диалога (v2.1).
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS clinrek_symptom_mode BOOLEAN NOT NULL DEFAULT FALSE;")
        )
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS chat_started_at TIMESTAMPTZ;"))
        # Индекс под фильтрацию поиска по режиму/предмету.
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_book_chunks_source_subject ON book_chunks (source_type, subject);")
        )

        # Этап 2: метаданные и provenance источника (§8–§9 ТЗ).
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS section VARCHAR(255);"))
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS topic VARCHAR(255);"))
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS edition VARCHAR(100);"))
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS year INTEGER;"))
        await conn.execute(
            text("ALTER TABLE books ADD COLUMN IF NOT EXISTS authority_level VARCHAR(32) NOT NULL DEFAULT 'primary_textbook';")
        )
        await conn.execute(
            text("ALTER TABLE books ADD COLUMN IF NOT EXISTS verification_status VARCHAR(20) NOT NULL DEFAULT 'unverified';")
        )
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS language VARCHAR(8) NOT NULL DEFAULT 'ru';"))
        await conn.execute(
            text("ALTER TABLE books ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'production';")
        )
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS file_path VARCHAR(512);"))
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS pages_path VARCHAR(512);"))
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS checksum VARCHAR(64);"))
        await conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS reindexed_at TIMESTAMPTZ;"))

        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS section VARCHAR(255);"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS topic VARCHAR(255);"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS knowledge_unit_ids TEXT;"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS edition VARCHAR(100);"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS year INTEGER;"))
        await conn.execute(
            text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS authority_level VARCHAR(32) NOT NULL DEFAULT 'primary_textbook';")
        )
        await conn.execute(
            text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS verification_status VARCHAR(20) NOT NULL DEFAULT 'unverified';")
        )
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS language VARCHAR(8) NOT NULL DEFAULT 'ru';"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS user_id VARCHAR(64);"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS exam_id VARCHAR(64);"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS char_start INTEGER;"))
        await conn.execute(text("ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS char_end INTEGER;"))

        # Этап 4A.5: режим «спрашиваю по своему документу» у пользователя Telegram (§18 ТЗ).
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_document_id VARCHAR(64);"))

        # Гибридный retrieval (§7 ТЗ): лексический поиск через встроенный
        # полнотекстовый индекс Postgres — отдельный поисковый движок не нужен.
        # GENERATED ALWAYS AS ... STORED сам пересчитывает колонку для уже
        # существующих строк при первом ALTER и на каждой вставке дальше.
        await conn.execute(
            text(
                "ALTER TABLE book_chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector "
                "GENERATED ALWAYS AS (to_tsvector('russian', content)) STORED;"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_book_chunks_content_tsv ON book_chunks USING GIN (content_tsv);")
        )

        # Отключённые/архивные источники не должны участвовать в retrieval —
        # индекс под фильтр по статусу вместе с режимом/предметом.
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_books_status ON books (status);")
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(init_db())
