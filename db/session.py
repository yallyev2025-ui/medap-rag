"""Общий асинхронный engine и фабрика сессий SQLAlchemy.

SSL для managed-БД настраивается здесь, а не параметром "sslmode" в самом
DATABASE_URL: asyncpg (в отличие от psycopg2/libpq) такого параметра не
принимает и падает с `TypeError: connect() got an unexpected keyword
argument 'sslmode'`, если он всё же попал в URL (config.py на всякий случай
дополнительно вырезает его при нормализации строки).

pool_pre_ping/pool_recycle — защита от другой особенности managed-БД: сервер
(или прокси перед ним) сам закрывает простаивающие соединения по таймауту.
Если между запросами к БД проходит долгая пауза без единого обращения к
базе — например, извлечение текста из большого PDF занимает минуты, пока
чекнутое из пула соединение просто лежит без дела, — следующий запрос падает
с `asyncpg.exceptions.InterfaceError: connection is closed`, хотя сам код
абсолютно корректен. pool_pre_ping перед реальным запросом дёшево проверяет
живо ли соединение и незаметно для вызывающего кода берёт новое, если старое
уже мёртво; pool_recycle на всякий случай принудительно обновляет соединения
старше получаса, не дожидаясь, когда их прибьёт сервер.
"""

import ssl

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings


def _verified_context() -> ssl.SSLContext:
    """Контекст с проверкой цепочки; CA берём из DATABASE_SSL_CA, если задан.

    Timeweb managed PostgreSQL отдаёт самоподписанную цепочку, которой нет в
    системном хранилище. Поэтому verify-full без CA провайдера гарантированно
    падает с CERTIFICATE_VERIFY_FAILED, а не защищает соединение.
    """
    ca = settings.DATABASE_SSL_CA.strip()
    if ca.startswith("-----BEGIN"):
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=ca)
    elif ca:
        context = ssl.create_default_context(cafile=ca)
    else:
        context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _connect_args() -> dict:
    mode = settings.DATABASE_SSL_MODE.lower()
    if mode == "disable":
        return {}
    if mode in ("verify-full", "verify-ca"):
        # Полная проверка сертификата. Для самоподписанной БД Timeweb нужен
        # DATABASE_SSL_CA (путь к сертификату или PEM). "verify-ca" оставлен
        # для совместимости: проверяем цепочку, но не имя хоста.
        context = _verified_context()
        if mode == "verify-ca":
            context.check_hostname = False
        return {"ssl": context}
    # "require" (по умолчанию): шифруем соединение, но не проверяем цепочку
    # сертификата — так работали все успешные выкладки на managed-БД Timeweb,
    # пока не был добавлен CA провайдера. Эквивалент sslmode=require из мира
    # psycopg2/libpq; трафик при этом остаётся зашифрованным.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return {"ssl": context}


engine = create_async_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args(),
    pool_pre_ping=True,
    pool_recycle=1800,
)
async_session = async_sessionmaker(engine, expire_on_commit=False)
