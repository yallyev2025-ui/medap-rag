"""Сводка расходов по каналам на настоящей Postgres (без pgvector): python -m scripts.test_usage_summary
Нужна переменная TEST_DATABASE_URL (postgresql+asyncpg://...); без неё проверка пропускается."""

import asyncio
import os
import sys
import types

try:
    import torch  # noqa: F401
except ImportError:
    sys.modules.setdefault("rag.embedder", types.SimpleNamespace(embed_query=None, embed_passages=None, limited_threads=None))

URL = os.environ.get("TEST_DATABASE_URL")


async def main() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from config import settings
    from db.crud import usage_summary
    from db.models import AIUsageEvent, Base, User

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=[AIUsageEvent.__table__, User.__table__]))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__, AIUsageEvent.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    free_limit = settings.FREE_MONTHLY_BUDGET_RUB
    async with Session() as s:
        s.add_all([User(id=1, is_premium=False), User(id=2, is_premium=True)])
        await s.flush()

        def ev(uid, channel, rub, n):
            return AIUsageEvent(user_id=uid, request_id=f"r{n}", workflow="W", task="T", provider="luna", model="m",
                                provider_cost_usd=rub / 100, provider_cost_rub=rub, channel=channel)

        s.add_all([
            ev("telegram:1", "telegram", free_limit, 1),      # упёрся в бесплатный лимит
            ev("telegram:2", "telegram", free_limit, 2),      # премиум: лимит выше, не упёрся
            ev("site-user", "api", 3.0, 3),
            ev("system", "ingest", 1.0, 4),
        ])
        await s.commit()
    async with Session() as s:
        r = await usage_summary(s, 30)
    by = {c["channel"]: c for c in r["channels"]}
    assert by["telegram"]["calls"] == 2 and by["telegram"]["users"] == 2 and by["telegram"]["rub"] == free_limit * 2, by
    assert by["api"]["rub"] == 3.0 and by["ingest"]["rub"] == 1.0
    assert r["telegramUsersThisMonth"] == 2 and r["telegramUsersAtLimit"] == 1, r
    await engine.dispose()
    print("ok usage_summary")


if __name__ == "__main__":
    if not URL:
        print("skip: нет TEST_DATABASE_URL")
    else:
        asyncio.run(main())
