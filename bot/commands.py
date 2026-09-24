"""Списки команд бота для меню Telegram (BotCommandScope)."""

from aiogram.types import BotCommand

USER_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="new", description="Новая тема (забыть контекст диалога)"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="limit", description="Сколько запросов осталось сегодня"),
    BotCommand(command="exitdocument", description="Выйти из режима «свой документ»"),
    BotCommand(command="selfcheck", description="Проверить свой ответ (текст или голосом)"),
    BotCommand(command="websearch", description="Найти в интернете"),
    BotCommand(command="pubmed", description="Искать в PubMed"),
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
