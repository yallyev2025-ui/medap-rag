"""Запуск бота презентаций в одном процессе с основным ботом и HTTP-API (см. `bot/main.py`)."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from app.integrations import slides_site
from config import settings
from slides_bot import handlers

logger = logging.getLogger(__name__)

COMMANDS = [BotCommand(command="start", description="Начать"), BotCommand(command="menu", description="Меню")]


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers.router)
    return dp


def config_problems(cfg=settings, site_configured: bool | None = None, username: str | None = None) -> list[str]:
    """Что не так с настройками — человеческими словами (уходит владельцу в Telegram при каждом запуске)."""
    problems: list[str] = []
    configured = slides_site.site.configured if site_configured is None else site_configured
    if not configured:
        problems.append("связь с сайтом не настроена: впишите SLIDES_SITE_URL и SLIDES_SITE_SECRET (бот будет отвечать «связь с сайтом не настроена»)")
    if username and cfg.SLIDES_BOT_USERNAME and cfg.SLIDES_BOT_USERNAME.lstrip("@").lower() != username.lower():
        problems.append(f"в SLIDES_BOT_USERNAME указано «{cfg.SLIDES_BOT_USERNAME}», а токен принадлежит боту @{username} — на сайте и в боте должно стоять имя @{username} без @")
    return problems


async def _tell_owners(text: str) -> None:
    """Сообщить владельцам (ADMIN_IDS_RAW) через ОСНОВНОГО бота: это единственный способ узнать, что бот презентаций не поднялся, не заходя в журналы."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.ADMIN_IDS:
        return
    notifier = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        for admin_id in settings.ADMIN_IDS:
            try:
                await notifier.send_message(admin_id, text)
            except Exception:
                logger.warning("Не удалось сообщить владельцу %s", admin_id, exc_info=True)
    finally:
        await notifier.session.close()


async def run_slides_bot() -> None:
    """Long polling второго бота. Любой сбой превращается в понятное сообщение владельцу и не останавливает основной бот."""
    if not settings.SLIDES_BOT_TOKEN:
        logger.info("SLIDES_BOT_TOKEN не задан — бот презентаций не запускается")
        await _tell_owners("Бот презентаций не запущен: у этого приложения не задана переменная SLIDES_BOT_TOKEN (токен от @BotFather). Впишите её в переменные именно этого приложения (бота), а не сайта, и перезапустите.")
        return
    if settings.SLIDES_BOT_TOKEN == settings.TELEGRAM_BOT_TOKEN:
        logger.error("SLIDES_BOT_TOKEN совпадает с TELEGRAM_BOT_TOKEN — бот презентаций не запускается")
        await _tell_owners("Бот презентаций не запущен: SLIDES_BOT_TOKEN совпадает с токеном основного бота. Нужен отдельный бот: создайте нового в @BotFather и впишите его токен.")
        return
    bot = Bot(token=settings.SLIDES_BOT_TOKEN)
    try:
        me = await bot.get_me()
        await bot.set_my_commands(COMMANDS, scope=BotCommandScopeDefault())
        problems = config_problems(username=me.username)
        logger.info("Бот презентаций запущен: @%s", me.username)
        await _tell_owners(f"Бот презентаций запущен: @{me.username}. Откройте его и нажмите «Начать»." + ("".join(f"\n⚠ {p}" for p in problems)))
        await build_dispatcher().start_polling(bot)
    except TelegramUnauthorizedError:
        logger.error("Telegram не принял SLIDES_BOT_TOKEN")
        await _tell_owners("Бот презентаций не запущен: Telegram не принял токен (SLIDES_BOT_TOKEN). Скопируйте токен из @BotFather целиком, без пробелов и кавычек.")
    except TelegramConflictError:
        logger.error("Токен бота презентаций уже опрашивает другой процесс")
        await _tell_owners("Бот презентаций не запущен: этот токен уже используется другим запущенным приложением (например, старой копией бота). Остановите лишнюю копию.")
    except Exception as exc:
        # Сбой второго бота не должен останавливать основной: asyncio.gather в main.py иначе уронил бы весь процесс
        logger.exception("Бот презентаций остановился")
        await _tell_owners(f"Бот презентаций остановился из-за ошибки ({type(exc).__name__}). Подробности — в журнале приложения бота.")
    finally:
        await bot.session.close()
