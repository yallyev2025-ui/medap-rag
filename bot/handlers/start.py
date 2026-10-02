"""Хендлеры /start, /help, /limit."""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BotCommandScopeChat, Message

from bot.commands import ADMIN_COMMANDS
from bot.handlers.menu import send_main_menu
from config import settings
from db.crud import get_or_create_user, month_spend_rub, monthly_budget_rub_for
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

HELP_TEXT = """Я отвечаю строго по загруженным материалам MedAP: учебникам и клиническим рекомендациями.

Как пользоваться:
1. Кнопки внизу чата (их можно свернуть значком у поля ввода): 📚 Учебники — выбрать предмет, 📄 Мои документы — загрузить свой файл и включить его ✅, 🔀 Откуда отвечать — только мои документы / только учебники / мои и учебники вместе.
2. Задавайте вопросы — я ищу ответ в выбранном источнике и указываю его (📄 документ, 📚 учебник).
3. Можно прислать фото/скрин теста — отвечу на все вопросы на нём. Или написать несколько вопросов сразу.
4. /sources — открыть панель, если кнопки свёрнуты.

Я помню последние сообщения — можно задавать уточняющие вопросы («а какие дозы?»). Чтобы начать с чистого листа — команда /new (Новая тема).

В клин. рекомендациях есть режим «🩺 Разбор по симптомам»: опишите жалобы — дам вероятные версии.

Если ответа в загруженных материалах нет — я честно предупреждаю об этом и, если могу, отвечаю из общих знаний с пометкой «не из материалов».

Форматы вопроса:
- Вопрос: "Что такое инфаркт миокарда?"
- Конспект: "Сделай конспект по теме инфаркт миокарда"
- Объяснение простыми словами: "Объясни простыми словами, что такое инфаркт"

Команды:
/sources — панель источников
/new — новая тема (забыть контекст)
/help — это сообщение
/limit — сколько запросов осталось сегодня

⚠️ Я даю информацию для справки, а не медицинское назначение. Решение и ответственность — на пользователе."""

UNLIMITED_TEXT = "У тебя безлимитный доступ ✅"


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.from_user.id in settings.ADMIN_IDS:
        try:
            await message.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=message.chat.id)
            )
        except Exception:
            logger.warning("Не удалось задать меню команд для админа %s", message.from_user.id, exc_info=True)

    await send_main_menu(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("limit"))
async def cmd_limit(message: Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await session.commit()

        budget = monthly_budget_rub_for(user)
        if budget is None:
            await message.answer(UNLIMITED_TEXT)
            return

        spent = await month_spend_rub(session, user.id)

    await message.answer(f"В этом месяце потрачено: {spent:.0f}₽ из {budget:.0f}₽. Обновится 1 числа.")
