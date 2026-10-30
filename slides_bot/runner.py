"""Запуск бота презентаций в одном процессе с основным ботом и HTTP-API (см. `bot/main.py`)."""

from __future__ import annotations

import asyncio
import os
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from app.integrations import slides_site
from config import settings
from slides_bot import handlers

logger = logging.getLogger(__name__)

COMMANDS = [BotCommand(command="start", description="Начать"), BotCommand(command="menu", description="Меню"), BotCommand(command="version", description="Версия бота и сайта")]


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


MSK = timezone(timedelta(hours=3))
DIGEST_HOUR = 9


def seconds_until_digest(now: datetime | None = None) -> float:
    """Сколько секунд до ближайших 9:00 по Москве (утренняя сводка владельцу)."""
    now = (now or datetime.now(MSK)).astimezone(MSK)
    nxt = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


async def _send_digests() -> None:
    """Сводка CRM каждому владельцу из ADMIN_IDS: сайт отвечает только тому, чей Telegram привязан к аккаунту владельца."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.ADMIN_IDS:
        return
    notifier = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        for admin_id in settings.ADMIN_IDS:
            try:
                r = await slides_site.site.call("digest", admin_id, days=1)
                if r.get("ok") and r.get("text"):
                    await notifier.send_message(admin_id, str(r["text"]))
            except Exception:
                logger.warning("Сводка владельцу %s не отправилась", admin_id, exc_info=True)
    finally:
        await notifier.session.close()


async def digest_loop() -> None:
    """Раз в сутки в 9:00 по Москве. Любой сбой — в журнал, цикл продолжается."""
    while True:
        await asyncio.sleep(seconds_until_digest())
        try:
            await _send_digests()
        except Exception:
            logger.exception("Утренняя сводка не отправилась")
        await asyncio.sleep(60)


NUDGE_HOURS = range(10, 20)  # напоминания только днём по Москве


async def _send_nudges(bot: Bot) -> int:
    """Напоминания по расписанию (дни 1, 3, 7, 14, 28, 42). Список и отметка «отправлено» — на сайте, чтобы сообщение не ушло дважды."""
    from slides_bot import texts as T

    if not settings.ADMIN_IDS:
        return 0
    r = await slides_site.site.call("nudges", settings.ADMIN_IDS[0])
    sent = 0
    for item in (r.get("due") or []) if r.get("ok") else []:
        try:
            step = int(item["step"])
            text, markup, card = T.nudge_text(step, bool(item.get("hasRun"))), T.nudge_kb(step), T.NUDGE_CARDS.get(step)
            if card:
                with open(os.path.join(os.path.dirname(__file__), "assets", "cards", f"{card}.png"), "rb") as fh:
                    await bot.send_photo(int(item["telegramId"]), BufferedInputFile(fh.read(), filename=f"{card}.png"), caption=text, reply_markup=markup)
            else:
                await bot.send_message(int(item["telegramId"]), text, reply_markup=markup)
            sent += 1
        except Exception:  # noqa: BLE001 — человек заблокировал бота: пропускаем
            pass
        await asyncio.sleep(0.05)
    return sent


async def nudge_loop(bot: Bot) -> None:
    """Раз в час проверяет, кому пора напомнить; ночью (до 10 и после 20 по Москве) молчит. Сбой — в журнал, цикл живёт."""
    while True:
        await asyncio.sleep(3600)
        try:
            if datetime.now(MSK).hour in NUDGE_HOURS:
                n = await _send_nudges(bot)
                if n:
                    logger.info("Отправлено напоминаний: %s", n)
        except Exception:
            logger.exception("Серия напоминаний не отработала")


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
        report = await handlers.version_report()
        await _tell_owners(f"Бот презентаций запущен: @{me.username}. Откройте его и нажмите «Начать».\n{report}" + ("".join(f"\n⚠ {p}" for p in problems)))
        digest_task = asyncio.create_task(digest_loop())
        nudge_task = asyncio.create_task(nudge_loop(bot))
        try:
            await build_dispatcher().start_polling(bot)
        finally:
            digest_task.cancel()
            nudge_task.cancel()
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
