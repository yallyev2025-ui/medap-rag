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
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, GetMe, SendMessage, SendPhoto  # noqa: E402
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
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"), text=getattr(method, "text", None) or "ok")
        if isinstance(method, SendPhoto):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"))
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"), text=getattr(method, "text", None) or "ok")
        if isinstance(method, SendPhoto):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"))
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


class FlowSite:
    """Поддельный сайт для сквозного прохода: запоминает, что бот просил."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def call(self, action, telegram_id=None, **p):
        self.calls.append((action, telegram_id, p))
        if action == "me":
            return {"ok": True, "balance": "0 ₽", "freeAvailable": True, "team": False, "owner": False}
        if action == "prices":
            return {"ok": True, "tiers": [{"id": "s", "label": "до 10 слайдов", "maxSlides": 10, "price": "30 ₽"}], "free": {"maxSlides": 7, "aiEdits": 2}}
        if action == "options":
            return {"ok": True, "templates": [{"id": f"t{i}", "name": f"Шаблон {i}"} for i in range(5)], "fonts": ["A", "B"], "colorPresets": [{"id": "c", "name": "Набор", "colors": ["#111111", "#222222", "#333333"]}], "disciplines": [{"code": "therapy", "title": "Терапия"}], "uploads": [], "titleFields": {"talk": [["student", "ФИО"]]}, "profile": {}}
        if action == "create":
            return {"ok": True, "runId": "run1"}
        if action == "status":
            return {"ok": True, "phase": "draft", "error": None, "slides": 3}
        if action == "draft":
            return {"ok": True, "phase": "draft", "approved": False, "topic": "Гипертония", "editsLeft": 2, "slides": [{"n": 1, "title": "Гипертония", "text": "", "fixed": True, "fill": None}]}
        if action == "open_link":
            return {"ok": True, "url": "https://site.test/t/ONCE"}
        if action == "version":
            return {"ok": False, "error": "Неизвестное действие"}
        return {"ok": True, "base64": "UE5H"}


def cbq(uid: int, data: str, n: int) -> Update:
    cb = CallbackQuery(id=str(n), from_user=User(id=uid, is_bot=False, first_name="Тест"), chat_instance="x", data=data, message=Message(message_id=n, date=datetime.now(), chat=Chat(id=uid, type="private"), text="экран"))
    return Update(update_id=n, callback_query=cb)


async def full_flow(dp) -> None:
    """Весь путь создания через НАСТОЯЩИЙ диспетчер: /start → создать → тема → вид → размер → «Настроить» → выборы → создать.
    Ловит то, чего не видят прямые вызовы обработчиков: фильтры, порядок обработчиков, состояние между обновлениями."""
    from slides_bot import handlers as H

    H.POLL_SECONDS = 0
    H._catalog.clear()
    site = FlowSite()
    slides_site.site = site
    session = FakeSession()
    bot = Bot(token="123456:TEST", session=session)
    uid, n = 321, 100

    async def press(data: str) -> list:
        nonlocal n
        n += 1
        before = len(session.sent)
        await dp.feed_update(bot, cbq(uid, data, n))
        return session.sent[before:]

    async def say(text: str) -> list:
        nonlocal n
        n += 1
        before = len(session.sent)
        await dp.feed_update(bot, update(uid, text, n))
        return session.sent[before:]

    def texts_in(items) -> str:
        return "\n".join(str(getattr(m, "text", "") or "") for m in items)

    def buttons(items) -> list[str]:
        out: list[str] = []
        for m in items:
            kb = getattr(m, "reply_markup", None)
            if kb is not None:
                out += [b.text for row in kb.inline_keyboard for b in row]
        return out

    await say("/start")
    out = await press("m:new")
    ok("О чём презентация" in texts_in(out), f"«Создать» → просят тему: {texts_in(out)}")
    out = await say("Артериальная гипертония")
    ok("вид" in texts_in(out).lower(), "после темы — вид презентации")
    out = await press("k:talk")
    ok("слайдов" in texts_in(out).lower(), "после вида — размер")
    out = await press("s:10")
    ok("Шаг 1 из 7" in texts_in(out) and "С чего начинаем" in texts_in(out), f"после размера — первый вопрос мастера: {texts_in(out)[:80]}")
    out = await press("o:m:topic")
    ok("Шаг 2 из 7" in texts_in(out) and "Для кого" in texts_in(out), "ответ кнопкой → следующий вопрос")
    out = await say("студенты 3 курса")
    ok("Шаг 3 из 7" in texts_in(out) and "Откуда брать материал" in texts_in(out), f"текстовый ответ на «Для кого» попал в нужный обработчик (состояние живёт между обновлениями) и мастер пошёл дальше: {texts_in(out)[:100]}")
    await press("o:med")
    out = await press("o:home")
    ok("Шаг 4 из 7" in texts_in(out) and "Свои файлы" in texts_in(out), "«Готово» на подэкране источников ведёт к следующему вопросу")
    out = await press("o:wz:b")
    ok("Шаг 3 из 7" in texts_in(out), "«Назад» через настоящий диспетчер")
    await press("o:wz:n")
    out = await press("o:wz:n")
    ok("Шаг 5 из 7" in texts_in(out) and "Фото" in texts_in(out), "пятый вопрос — фото")
    out = await press("o:wz:n")
    ok("Шаг 6 из 7" in texts_in(out), "шестой вопрос — пожелания")
    out = await say("поменьше текста")
    ok("Шаг 7 из 7" in texts_in(out) and "Титульный лист" in texts_in(out), f"ответ-текст на пожелания принят, мастер пошёл дальше: {texts_in(out)[:100]}")
    out = await press("o:wz:n")
    labels = buttons(out)
    ok("Всё спросил" in texts_in(out) and "Настройте презентацию" in texts_in(out), f"в конце — сводка: {texts_in(out)[:80]}")
    for want in ("С чего начинаем", "Источники", "Свои материалы", "Для кого", "Заметки", "Титул", "Фото", "Оформление", "Полная форма", "Создать"):
        ok(any(want in x for x in labels), f"через настоящий диспетчер доходит кнопка «{want}»: {labels}")
    out = await press("o:src")
    ok("Откуда брать материал" in texts_in(out), "после мастера «Источники» открываются из сводки")
    out = await press("o:home")
    ok("Настройте презентацию" in texts_in(out), "из сводки «Готово» возвращает в сводку, а не запускает мастер заново")
    out = await press("o:des")
    ok(any("Шрифты" in x for x in buttons(out)), "«Оформление» открывает шаблон/шрифты/цвета")
    out = await press("o:sty:0")
    ok("Шаблон 0" in str(buttons(out)), "список шаблонов приходит с сайта")
    await press("o:st:auto")
    out = await press("o:go")
    ok("Пишу презентацию" in texts_in(out), f"«Создать» запускает написание: {texts_in(out)}")
    create = [c for c in site.calls if c[0] == "create"][-1]
    ok(create[2]["topic"] == "Артериальная гипертония" and create[2]["medical"] is True and create[2]["audience"] == "студенты 3 курса" and create[2]["total"] == 10, f"на сайт ушло всё выбранное: {create[2]}")
    await asyncio.gather(*list(H._tasks))
    ok(any(isinstance(m, SendMessage) and "Черновик" in (m.text or "") for m in session.sent), "черновик пришёл сам")
    out = await press("u:run1")
    ok(any(x for m in out for row in (getattr(m, "reply_markup", None).inline_keyboard if getattr(m, "reply_markup", None) else []) for x in row if x.url == "https://site.test/t/ONCE"), "«Открыть на сайте» выдаёт ссылку входа по нажатию")
    out = await say("/version")
    ok("СТАРАЯ" in texts_in(out), f"/version при старом сайте говорит прямо: {texts_in(out)[:120]}")


class FlowSite:
    """Поддельный сайт для сквозного прохода: запоминает, что бот просил."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def call(self, action, telegram_id=None, **p):
        self.calls.append((action, telegram_id, p))
        if action == "me":
            return {"ok": True, "balance": "0 ₽", "freeAvailable": True, "team": False, "owner": False}
        if action == "prices":
            return {"ok": True, "tiers": [{"id": "s", "label": "до 10 слайдов", "maxSlides": 10, "price": "30 ₽"}], "free": {"maxSlides": 7, "aiEdits": 2}}
        if action == "options":
            return {"ok": True, "templates": [{"id": f"t{i}", "name": f"Шаблон {i}"} for i in range(5)], "fonts": ["A", "B"], "colorPresets": [{"id": "c", "name": "Набор", "colors": ["#111111", "#222222", "#333333"]}], "disciplines": [{"code": "therapy", "title": "Терапия"}], "uploads": [], "titleFields": {"talk": [["student", "ФИО"]]}, "profile": {}}
        if action == "create":
            return {"ok": True, "runId": "run1"}
        if action == "status":
            return {"ok": True, "phase": "draft", "error": None, "slides": 3}
        if action == "draft":
            return {"ok": True, "phase": "draft", "approved": False, "topic": "Гипертония", "editsLeft": 2, "slides": [{"n": 1, "title": "Гипертония", "text": "", "fixed": True, "fill": None}]}
        if action == "open_link":
            return {"ok": True, "url": "https://site.test/t/ONCE"}
        if action == "version":
            return {"ok": False, "error": "Неизвестное действие"}
        return {"ok": True, "base64": "UE5H"}


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
    await full_flow(dp)
    await full_flow(dp)
    print(f"OK: {checks} проверок")


asyncio.run(main())
