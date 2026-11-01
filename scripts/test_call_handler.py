"""Кнопка «Звонок» в Telegram (MedAP, этап 5): python -m scripts.test_call_handler (без сети и базы).

Бесплатному — отказ без ссылки; без настройки сервера — понятное сообщение; подписчику — кнопка мини-приложения с подписанной ссылкой.
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

import bot.handlers.call as c  # noqa: E402

checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    assert cond, what
    checks += 1


class M:
    def __init__(self, uid: int):
        self.sent: list = []
        self.from_user = types.SimpleNamespace(id=uid)

    async def answer(self, t, **k):
        self.sent.append((t, k.get("reply_markup")))


class S:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


c.async_session = lambda: S()  # type: ignore[assignment]
users = {1: types.SimpleNamespace(id=1, is_premium=False, current_subject=None), 2: types.SimpleNamespace(id=2, is_premium=True, current_subject="Патфиз")}


async def gu(s, tg):
    return users[tg.id]


c.get_or_create_user = gu  # type: ignore[assignment]
c.can_use_deep = lambda u: u.is_premium  # type: ignore[assignment]


async def main() -> None:
    m = M(1)
    await c.cmd_call(m)
    ok("подписчиков" in m.sent[0][0] and m.sent[0][1] is None, "бесплатному — отказ без кнопки")
    c.settings.SLIDES_SITE_URL = ""
    m = M(2)
    await c.cmd_call(m)
    ok("не настроен" in m.sent[0][0], "сервер не настроен — понятное сообщение")
    c.settings.SLIDES_SITE_URL = "https://site.ru"
    c.settings.SERVICE_TOKEN = "секрет"
    m = M(2)
    await c.cmd_call(m)
    btn = m.sent[0][1].inline_keyboard[0][0]
    ok(btn.web_app.url.startswith("https://site.ru/call?t=") and "." in btn.web_app.url.split("t=")[1], "подписчику — кнопка мини-приложения с подписанной ссылкой")
    print(f"OK: {checks} проверок")


asyncio.run(main())
