"""Вопрос, ожидающий согласия, хранится в базе и переживает перезапуск: python -m scripts.test_pending_question
Нужна TEST_DATABASE_URL (postgresql+asyncpg://...); без неё проверка пропускается."""

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

try:
    import torch  # noqa: F401
except ImportError:
    sys.modules.setdefault("rag.embedder", types.SimpleNamespace(embed_query=None, embed_passages=None, limited_threads=None))

URL = os.environ.get("TEST_DATABASE_URL")


async def main() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.crud import save_pending_question, take_pending_question
    from db.models import Base, PendingQuestion

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=[PendingQuestion.__table__]))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[PendingQuestion.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    checks = 0

    async with Session() as s:
        await save_pending_question(s, 7, "Стадии воспаления?")
        await s.commit()
    # «перезапуск бота»: новая сессия, памяти процесса нет — вопрос на месте
    async with Session() as s:
        assert await take_pending_question(s, 7) == "Стадии воспаления?"
        await s.commit()
        checks += 1
    async with Session() as s:
        assert await take_pending_question(s, 7) is None  # забран один раз
        checks += 1

    async with Session() as s:
        await save_pending_question(s, 8, "первый")
        await save_pending_question(s, 8, "второй")  # новый вопрос заменяет прежний
        await s.commit()
    async with Session() as s:
        assert await take_pending_question(s, 8) == "второй"
        await s.commit()
        checks += 1

    async with Session() as s:  # просроченный считается устаревшим
        s.add(PendingQuestion(user_id=9, question="старый", created_at=datetime.now(timezone.utc) - timedelta(hours=7)))
        await s.commit()
    async with Session() as s:
        assert await take_pending_question(s, 9) is None
        checks += 1

    await engine.dispose()
    print(f"OK: {checks} проверок")


if __name__ == "__main__":
    if not URL:
        print("TEST_DATABASE_URL не задана — пропуск")
    else:
        asyncio.run(main())
