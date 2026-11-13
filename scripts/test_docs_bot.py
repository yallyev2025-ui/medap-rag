"""Документы и конспекты из видео в Telegram (13.11.2026): python -m scripts.test_docs_bot (без сети, базы и Telegram).

Проверяется: узнавание просьб и ссылок, память последнего ответа, подпись запроса ко второй двери сайта, весь путь видео (отказ без Premium,
ход, готовый конспект с кнопками), конспект по теме (остаток лимита уходит на сайт, лимит исчерпан — отказ), документ из последнего ответа,
файл по кнопке уходит документом.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

import httpx  # noqa: E402

import bot.handlers.docs as d  # noqa: E402
from app.integrations.slides_site import SiteClient  # noqa: E402
from bot.docs_intent import find_youtube_url, parse_doc_request  # noqa: E402
from bot.last_answer import last_answer, remember_last_answer  # noqa: E402

checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    assert cond, what
    checks += 1


# ——— узнавание ———
for u in ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtu.be/dQw4w9WgXcQ?t=5", "смотри https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "https://youtube.com/shorts/dQw4w9WgXcQ"]:
    ok(find_youtube_url(u) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ", u)
ok(find_youtube_url("https://example.com/watch?v=dQw4w9WgXcQ") is None and find_youtube_url("просто текст") is None, "чужие адреса не ссылка на видео")

r = parse_doc_request("Сделай конспект по теме ишемическая болезнь сердца в бордовых тонах")
ok(r is not None and r.topic == "ишемическая болезнь сердца" and not r.from_previous, "тема без слов о цвете")
r = parse_doc_request("собери документ из этого ответа в pdf")
ok(r is not None and r.topic is None and r.from_previous and r.fmt == "pdf", "документ из прошлого ответа, формат pdf")
r = parse_doc_request("сделай шпаргалку про гепарин в ворде")
ok(r is not None and r.topic == "гепарин" and r.fmt == "docx", "шпаргалка в Word")
for q in ["Что такое тропонин?", "как сделать конспект быстро", "объясни патогенез ИБС", "расскажи про гепарин"]:
    ok(parse_doc_request(q) is None, f"обычный вопрос не документ: {q}")

# ——— память последнего ответа ———
remember_last_answer(5, "коротко")
ok(last_answer(5) is None, "слишком короткое не запоминается")
remember_last_answer(5, "Гепарин — прямой антикоагулянт. " * 6)
ok(last_answer(5) is not None and last_answer(6) is None, "ответ помнится по чату")

# ——— подпись ко второй двери ———
seen: dict = {}


def handler(request: httpx.Request) -> httpx.Response:
    seen["url"] = str(request.url)
    seen["h"] = dict(request.headers)
    seen["body"] = request.content.decode()
    return httpx.Response(200, json={"ok": True})


async def sign_check() -> None:
    c = SiteClient("https://site.example", "s" * 20, transport=httpx.MockTransport(handler), path="/api/docs-bot")
    res = await c.call("version", telegram_id=7)
    ok(res["ok"] and seen["url"] == "https://site.example/api/docs-bot", "вторая дверь — /api/docs-bot")
    ts = seen["h"]["x-slides-timestamp"]
    expect = hmac.new(b"s" * 20, f"{ts}.{seen['body']}".encode(), hashlib.sha256).hexdigest()
    ok(seen["h"]["x-slides-signature"] == expect and json.loads(seen["body"])["telegramId"] == 7, "подпись и telegramId")
    c2 = SiteClient("https://site.example", "s" * 20, transport=httpx.MockTransport(handler))
    await c2.call("me", telegram_id=7)
    ok(seen["url"].endswith("/api/slides-bot"), "первая дверь не изменилась")


asyncio.run(sign_check())


# ——— пути в чате ———
class Msg:
    def __init__(self, text: str = "", uid: int = 1):
        self.text = text
        self.from_user = types.SimpleNamespace(id=uid)
        self.chat = types.SimpleNamespace(id=uid)
        self.sent: list = []

    async def answer(self, t, **k):
        m = Msg()
        m.text = t
        self.sent.append((t, k.get("reply_markup")))
        m.edits = []

        async def edit_text(x):
            m.edits.append(x)
            self.sent.append((x, None))

        m.edit_text = edit_text  # type: ignore[attr-defined]
        return m

    async def answer_document(self, doc, caption=None):
        self.sent.append(("DOC:" + doc.filename, caption))


class FakeSite:
    configured = True

    def __init__(self, script):
        self.script = script
        self.calls: list = []

    async def call(self, action, telegram_id=None, **p):
        self.calls.append((action, telegram_id, p))
        return self.script(action, p)


d.POLL_SECONDS = 0.0
user_free = types.SimpleNamespace(id=1, is_premium=False)
user_prem = types.SimpleNamespace(id=2, is_premium=True)


def texts(m: Msg) -> str:
    return "\n".join(t for t, _ in m.sent)


def has_buttons(m: Msg) -> bool:
    return any(k is not None and any(b.callback_data.startswith("dd:") for row in k.inline_keyboard for b in row) for _, k in m.sent)


async def flows() -> None:
    # видео без Premium — отказ, лимит запросов не тратится
    d.docs_site = FakeSite(lambda a, p: {"ok": True})
    m = Msg(uid=1)
    ctx = {"count": True}
    await d.run_video(m, user_free, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", ctx)
    ok("Premium" in texts(m) and not ctx["count"] and not d.docs_site.calls, "видео: бесплатному — отказ, на сайт не ходим")

    # видео Premium: старт → два опроса → готово
    state = {"n": 0}

    def script(a, p):
        if a == "video_start":
            return {"ok": True, "noteId": "n1"}
        state["n"] += 1
        if state["n"] < 3:
            return {"ok": True, "state": "pending", "steps": [f"Пишу конспект: часть {state['n']} из 2"]}
        return {"ok": True, "state": "done", "docId": "doc12345678", "title": "ОКС", "preview": "Главное\n\n- ОКС — спектр состояний", "warnings": ["Субтитры автоматические"]}

    d.docs_site = FakeSite(script)
    m = Msg(uid=2)
    await d.run_video(m, user_prem, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    ok(d.docs_site.calls[0][0] == "video_start" and d.docs_site.calls[0][2]["premium"] is True, "видео: Premium уходит на сайт")
    ok("часть 2 из 2" in texts(m) and "ОКС — спектр состояний" in texts(m) and "Субтитры автоматические" in texts(m), "видео: ход и конспект в чате")
    ok(has_buttons(m), "видео: под конспектом кнопки PDF и Word")

    # видео: ошибка сайта словами
    d.docs_site = FakeSite(lambda a, p: {"ok": False, "error": "У этого видео нет субтитров"} if a == "video_start" else {"ok": True})
    m = Msg(uid=2)
    await d.run_video(m, user_prem, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    ok("нет субтитров" in texts(m) and not has_buttons(m), "видео: причина словами, без кнопок")

    # конспект по теме: остаток лимита уходит на сайт
    async def left_ok(user):
        return 12.5, False

    d._budget_rub = left_ok  # type: ignore[assignment]

    def script2(a, p):
        if a == "doc_topic":
            return {"ok": True, "jobId": "j1"}
        return {"ok": True, "state": "done", "docId": "doc87654321", "title": "ИБС", "found": ["Определение", "Лечение"], "missing": ["Патогенез"], "images": 3, "preview": "Определение\n\nтекст"}

    d.docs_site = FakeSite(script2)
    m = Msg(uid=1)
    await d.run_doc(m, user_free, "ишемическая болезнь сердца", "both")
    c0 = d.docs_site.calls[0]
    ok(c0[0] == "doc_topic" and c0[2]["budgetRub"] == 12.5 and c0[2]["topic"] == "ишемическая болезнь сердца", "тема и остаток лимита уходят на сайт")
    ok("разделов — 2" in texts(m) and "рисунков — 3" in texts(m) and "Патогенез" in texts(m) and has_buttons(m), "тема: итог, пропуски и кнопки")

    # лимит исчерпан
    async def left_zero(user):
        return 0.0, True

    d._budget_rub = left_zero  # type: ignore[assignment]
    d.docs_site = FakeSite(script2)
    m = Msg(uid=1)
    ctx = {"count": True}
    await d.run_doc(m, user_free, "гепарин", "both", ctx)
    ok("лимит" in texts(m).lower() and not d.docs_site.calls and not ctx["count"], "лимит исчерпан — отказ без обращения к сайту")

    # документ из последнего ответа
    d._budget_rub = left_ok  # type: ignore[assignment]
    m = Msg(uid=9)
    await d.run_doc(m, user_free, None, "both")
    ok("задайте вопрос" in texts(m), "нет прошлого ответа — подсказка")
    remember_last_answer(9, "Гепарин — прямой антикоагулянт, контролируют по АЧТВ. " * 5)
    d.docs_site = FakeSite(lambda a, p: {"ok": True, "docId": "docabcdefgh", "title": "Гепарин"})
    m = Msg(uid=9)
    await d.run_doc(m, user_free, None, "both")
    c = d.docs_site.calls[0]
    ok(c[0] == "doc_text" and "Гепарин" in c[2]["text"] and c[2]["allowModel"] is True and has_buttons(m), "документ из последнего ответа: текст на сайт, кнопки в чате")

    # файл по кнопке
    d.docs_site = FakeSite(lambda a, p: {"ok": True, "fileName": "Гепарин.pdf", "base64": base64.b64encode(b"%PDF-1.4 x").decode()})
    sent: list = []

    class Cb:
        data = "dd:p:docabcdefgh"
        from_user = types.SimpleNamespace(id=9)

        class message:  # noqa: N801
            @staticmethod
            async def answer(t, **k):
                sent.append(t)

            @staticmethod
            async def answer_document(doc, caption=None):
                sent.append(("DOC", doc.filename, bytes(doc.data)[:5]))

        @staticmethod
        async def answer(*a, **k):
            return None

    await d.on_download(Cb)  # type: ignore[arg-type]
    ok(sent and sent[0] == ("DOC", "Гепарин.pdf", b"%PDF-") and d.docs_site.calls[0][2]["format"] == "pdf", "кнопка PDF отдаёт файл документом")


asyncio.run(flows())
print(f"OK: {checks} проверок")
