"""Общий асинхронный engine и фабрика сессий SQLAlchemy.

SSL для managed-БД настраивается здесь, а не параметром "sslmode" в самом
DATABASE_URL: asyncpg (в отличие от psycopg2/libpq) такого параметра не
принимает и падает с `TypeError: connect() got an unexpected keyword
argument 'sslmode'`, если он всё же попал в URL (config.py на всякий случай
дополнительно вырезает его при нормализации строки).
"""

import ssl

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings


def _connect_args() -> dict:
    mode = settings.DATABASE_SSL_MODE.lower()
    if mode == "disable":
        return {}
    if mode == "verify-full":
        # Проверка сертификата сервера по системному хранилищу доверенных
        # центров — включайте, если провайдер использует публично доверенный
        # сертификат (а не самоподписанный/внутренний).
        return {"ssl": True}
    # "require" (по умолчанию): шифруем соединение, но не проверяем цепочку
    # сертификата — безопасный дефолт для managed-БД без своего CA-бандла
    # в образе. Эквивалент sslmode=require из мира psycopg2/libpq.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return {"ssl": context}


engine = create_async_engine(settings.DATABASE_URL, connect_args=_connect_args())
async_session = async_sessionmaker(engine, expire_on_commit=False)
