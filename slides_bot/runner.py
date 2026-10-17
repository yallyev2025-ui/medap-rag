"""Запуск бота презентаций в одном процессе с основным ботом и HTTP-API (см. `bot/main.py`)."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from config import settings
from slides_bot import handlers

logger = logging.getLogger(__name__)

COMMANDS = [BotCommand(command="start", description="Начать"), BotCommand(command="menu", description="Меню")]


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers.router)
    return dp


async def run_slides_bot() -> None:
    """Long polling второго бота. Без SLIDES_BOT_TOKEN тихо пропускается — основной бот не страдает."""
    if not settings.SLIDES_BOT_TOKEN:
        logger.info("SLIDES_BOT_TOKEN не задан — бот презентаций не запускается")
        return
    bot = Bot(token=settings.SLIDES_BOT_TOKEN)
    try:
        await bot.set_my_commands(COMMANDS, scope=BotCommandScopeDefault())
        me = await bot.get_me()
        logger.info("Бот презентаций запущен: @%s", me.username)
        await build_dispatcher().start_polling(bot)
    except Exception:
        # Сбой второго бота не должен останавливать основной: asyncio.gather в main.py иначе уронил бы весь процесс
        logger.exception("Бот презентаций остановился")
        await handlers.notify_owners(bot, "Бот презентаций остановился из-за ошибки, смотрите журнал сервиса.")
    finally:
        await bot.session.close()
