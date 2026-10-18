"""Проверка бота презентаций через НАСТОЯЩИЙ диспетчер aiogram с поддельным Telegram (без сети и токена).

Запуск: python scripts/test_slides_bot_dispatch.py
Зачем отдельно от test_slides_bot.py: тот вызывает обработчики напрямую и не видит, доходит ли обновление Telegram до нужного обработчика
(фильтры, порядок роутеров, внедрение аргументов). Здесь приходит настоящее обновление «/start» — и мы смотрим, что бот ответил.
"""
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User  # noqa: E402

from app.integrations import slides_site  # noqa: E402
from config import settings  # noqa: E402
from slides_bot.runner import build_dispatcher  # noqa: E402

checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


class FakeSession(BaseSession):
    """Вместо сети запоминает, что бот хотел отправить Telegram."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list = []

    async def close(self) -> None:  # noqa: D401
        return None

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text=method.text)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="bot", username="medap_slide_bot")
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""


class FakeSite:
    def __init__(self, linked: bool) -> None:
        self.linked = linked

    async def call(self, action, telegram_id=None, **p):
        if action == "me":
            return {"ok": True, "balance": "0 ₽", "freeAvailable": True, "team": False, "owner": False} if self.linked else {"ok": False, "error": "Сначала войдите", "needLogin": True}
        return {"ok": True}


def update(uid: int, text: str, n: int) -> Update:
    user = User(id=uid, is_bot=False, first_name="Тест", username="tester")
    entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
    msg = Message(message_id=n, date=datetime.now(), chat=Chat(id=uid, type="private"), from_user=user, text=text, entities=entities)
    return Update(update_id=n, message=msg)


async def main() -> None:
    settings.SLIDES_SITE_URL = "https://site.test"
    settings.ADMIN_IDS_RAW = "999"

    # 1. человек без привязки: /start → просим почту (а не молчим)
    slides_site.site = FakeSite(linked=False)
    session = FakeSession()
    bot = Bot(token="123456:TEST", session=session)
    dp = build_dispatcher()  # роутер цепляется к одному диспетчеру — как в бою, поэтому он один на весь прогон
    await dp.feed_update(bot, update(111, "/start", 1))
    texts = [m.text for m in session.sent if isinstance(m, SendMessage)]
    ok(len(texts) >= 2, f"на /start бот ответил (сообщений: {len(texts)})")
    ok(any("почту" in t for t in texts), f"бот просит почту: {texts}")
    await dp.feed_update(bot, update(111, "a@b.ru", 2))
    ok(any("не отправился" in (m.text or "") or "код" in (m.text or "").lower() for m in session.sent if isinstance(m, SendMessage)), "после почты бот отвечает про код")

    # 2. сайт не настроен (нет секрета): бот отвечает понятной причиной, а не тишиной
    slides_site.site = slides_site.SiteClient("", "")
    session2 = FakeSession()
    bot2 = Bot(token="123456:TEST", session=session2)
    await dp.feed_update(bot2, update(112, "/start", 3))
    texts2 = [m.text for m in session2.sent if isinstance(m, SendMessage)]
    ok(texts2 and "не настроена" in texts2[0], f"без настроек сайта бот так и говорит: {texts2}")

    # 3. человек уже вошёл: /start → меню
    slides_site.site = FakeSite(linked=True)
    session3 = FakeSession()
    bot3 = Bot(token="123456:TEST", session=session3)
    await dp.feed_update(bot3, update(113, "/start", 4))
    menu = [m for m in session3.sent if isinstance(m, SendMessage)]
    ok(menu and menu[-1].reply_markup is not None, "вошедший получает меню с кнопками")

    # 4. нажатие кнопки меню проходит до обработчика
    cb = CallbackQuery(id="1", from_user=User(id=113, is_bot=False, first_name="Тест"), chat_instance="x", data="m:home", message=Message(message_id=9, date=datetime.now(), chat=Chat(id=113, type="private"), text="меню"))
    before = len(session3.sent)
    await dp.feed_update(bot3, Update(update_id=5, callback_query=cb))
    ok(len(session3.sent) > before, "кнопка меню обработана (бот что-то ответил)")
    print(f"OK: {checks} проверок")


asyncio.run(main())
