"""Списки команд бота для меню Telegram (BotCommandScope)."""

from aiogram.types import BotCommand

USER_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="new", description="Новая тема (забыть контекст диалога)"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="limit", description="Сколько запросов осталось сегодня"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="admin", description="Меню администратора"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="addbook", description="Добавить учебник"),
    BotCommand(command="delbook", description="Удалить учебник"),
    BotCommand(command="broadcast", description="Рассылка всем пользователям"),
    BotCommand(command="ban", description="Заблокировать пользователя"),
    BotCommand(command="unban", description="Разблокировать пользователя"),
    BotCommand(command="premium", description="Выдать премиум"),
    BotCommand(command="unpremium", description="Убрать премиум"),
]
