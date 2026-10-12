"""Точка входа Telegram-бота.

Запускает ДВЕ вещи в одном процессе/событийном цикле: long-polling бота и
HTTP-сервер `api/main.py` (`/search`, `/health`) для внешних потребителей
(сценарист рилсов в `medap`). Именно поэтому один процесс, не два разных
Railway-сервиса — эмбеддер и реранкер (~4.5 ГБ суммарно) грузятся в память один
раз и переиспользуются и ботом, и API, вместо двойной загрузки.

Из-за HTTP-сервера сервис в Railway теперь должен слушать $PORT (Railway
проставляет эту переменную сам для web-сервисов) — см. README.
"""

import asyncio
import logging
import os

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommandScopeDefault

from api.main import app as api_app
from bot.commands import USER_COMMANDS
from bot.handlers import admin, fragments, menu, panel, query, recall, start, user_documents, vision
from bot.middlewares.limits import LimitsMiddleware
from config import settings
from db.init_db import init_db

logger = logging.getLogger(__name__)


async def _run_bot() -> None:
    admin.cleanup_addbook_tmp()
    await init_db()

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    dp.message.middleware(LimitsMiddleware())

    dp.include_router(admin.router)
    dp.include_router(menu.router)
    # Нижняя панель: кнопки — обычные тексты, поэтому раньше общих текстовых хендлеров.
    dp.include_router(panel.router)
    dp.include_router(fragments.router)
    dp.include_router(start.router)
    # recall.router до query.router: RecallStates-фильтрованные хендлеры должны
    # перехватывать текст/голос раньше, чем сработает общий catch-all в query.py.
    dp.include_router(recall.router)
    dp.include_router(query.router)
    dp.include_router(vision.router)
    dp.include_router(user_documents.router)

    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())

    me = await bot.get_me()
    logger.info("Бот запущен: @%s", me.username)

    await dp.start_polling(bot)


async def _run_api() -> None:
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(api_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("HTTP API (/search, /health) слушает 0.0.0.0:%d", port)
    await server.serve()


async def _warm_up_models() -> None:
    """Подгружает модели поиска заранее, чтобы первый студент после деплоя не ждал их загрузки. Фоном: бот и
    /health отвечают сразу. Сбой не опасен — модели загрузятся при первом вопросе, как раньше."""
    try:
        from rag.embedder import embed_query
        from rag.reranker import warm_up

        await asyncio.to_thread(embed_query, "прогрев")
        backend = await asyncio.to_thread(warm_up)
        logger.info("Модели поиска загружены, реранкер: %s", backend)
    except Exception:
        logger.exception("Прогрев моделей не удался — загрузятся при первом вопросе")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    warm_up_task = asyncio.create_task(_warm_up_models())  # noqa: F841 — держим ссылку, фоновая задача
    # Если один из двух упадёт — падает весь процесс (Railway перезапустит по
    # restartPolicy), а не тихо остаётся половина функциональности недоступна.
    await asyncio.gather(_run_bot(), _run_api())


if __name__ == "__main__":
    asyncio.run(main())
