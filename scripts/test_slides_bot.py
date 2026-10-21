"""Проверка бота презентаций без сети, Telegram и настоящего сайта.

Запуск: python scripts/test_slides_bot.py
Проверяется: подпись запросов (тот же ответ, что у сайта на Node), заголовки и тело запроса к сайту, сетевой сбой как понятный
отказ, вход по почте и коду, создание → оплата → возврат из оплаты → черновик, правка слайда, файл документом, баланс,
раздел владельца только для ADMIN_IDS, тексты и клавиатуры.
"""
import asyncio
import base64
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

import httpx  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from app.integrations import slides_site  # noqa: E402
from config import settings  # noqa: E402
from slides_bot import handlers as H  # noqa: E402
from slides_bot import texts as T  # noqa: E402

SECRET = "s" * 32
checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


# ---------- подпись и клиент ----------

ok(slides_site.sign_body(SECRET, 1700000000, '{"action":"prices"}') == "20654c0a7299b3fb70717a165987fb9e72bc2ef22253072c1b8411c932cbf64b", "подпись совпадает с сайтом (Node)")

seen: dict = {}


def handler(request: httpx.Request) -> httpx.Response:
    body = request.content.decode("utf-8")
    seen["headers"] = request.headers
    seen["body"] = json.loads(body)
    seen["raw"] = body
    seen["url"] = str(request.url)
    return httpx.Response(200, json={"ok": True, "echo": seen["body"]["action"]})


async def client_checks() -> None:
    c = slides_site.SiteClient("https://site.test/", SECRET, httpx.MockTransport(handler))
    r = await c.call("me", 42, extra="Привет")
    ok(r == {"ok": True, "echo": "me"}, "ответ сайта проходит как есть")
    ok(seen["url"] == "https://site.test/api/slides-bot", "адрес без двойного слэша")
    ok(seen["body"] == {"action": "me", "telegramId": 42, "extra": "Привет"}, "тело: действие, Telegram-id, поля")
    ts = int(seen["headers"]["x-slides-timestamp"])
    ok(seen["headers"]["x-slides-signature"] == slides_site.sign_body(SECRET, ts, seen["raw"]), "заголовок подписи верен")
    ok(abs(ts - __import__("time").time()) < 5, "время подписи — сейчас")

    ok((await slides_site.SiteClient("", "").call("me", 1))["ok"] is False, "без настроек — понятный отказ, не падение")
    ok((await slides_site.SiteClient("https://x.test", "короткий").call("me", 1))["ok"] is False, "короткий секрет не принимается")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи")

    r = await slides_site.SiteClient("https://x.test", SECRET, httpx.MockTransport(boom)).call("me", 1)
    ok(r["ok"] is False and r.get("network") is True and "не отвечает" in r["error"], "сбой сети — ok:false с понятным текстом")
    r = await slides_site.SiteClient("https://x.test", SECRET, httpx.MockTransport(lambda req: httpx.Response(502, text="Bad Gateway"))).call("me", 1)
    ok(r["ok"] is False and r.get("network") is True, "не-JSON ответ — отказ")
    r = await slides_site.SiteClient("https://x.test", SECRET, httpx.MockTransport(lambda req: httpx.Response(401, json={"ok": False, "error": "подпись неверна"}))).call("me", 1)
    ok(r == {"ok": False, "error": "подпись неверна"}, "отказ сайта с JSON доходит до человека")


# ---------- поддельный сайт и поддельный Telegram ----------

class FakeSite:
    def __init__(self) -> None:
        self.linked = False
        self.calls: list[tuple] = []
        self.pay_needed = False
        self.phase_seq = ["choosing", "draft"]
        self.balance = "0 ₽"

    async def call(self, action: str, telegram_id: int | None = None, **p):
        self.calls.append((action, telegram_id, p))
        if action == "prices":
            return {"ok": True, "tiers": [{"id": "s", "label": "до 10 слайдов", "maxSlides": 10, "price": "30 ₽"}, {"id": "m", "label": "до 15 слайдов", "maxSlides": 15, "price": "40 ₽"}, {"id": "l", "label": "до 20 слайдов", "maxSlides": 20, "price": "50 ₽"}], "free": {"maxSlides": 7, "aiEdits": 2}}
        if action == "login_request":
            if not p.get("consent"):
                return {"ok": False, "error": "Нужно согласие на обработку персональных данных", "needConsent": True}
            return {"ok": True} if "@" in p["email"] else {"ok": False, "error": "Проверьте адрес почты"}
        if action == "login_verify":
            if p["code"] == "123456":
                self.linked = True
                return {"ok": True}
            return {"ok": False, "error": "Код неверный или устарел"}
        if not self.linked or telegram_id is None:
            # как настоящий сайт: без Telegram-id аккаунт не определить (раньше бот так «забывал» вошедшего на шаге выбора стиля)
            return {"ok": False, "error": "Сначала войдите", "needLogin": True}
        if action == "open_link":
            return {"ok": True, "url": f"https://site.test/t/ONETIME-{p.get('runId', '')}"}
        if action == "me":
            return {"ok": True, "balance": self.balance, "freeAvailable": True, "team": False, "owner": telegram_id == 999}
        if action == "options":
            return {
                "ok": True,
                "templates": [{"id": f"t{i}", "name": f"Шаблон {i}"} for i in range(12)],
                "fonts": [f"Шрифт{i}" for i in range(10)],
                "colorPresets": [{"id": "teal", "name": "Бирюза и янтарь", "colors": ["#2f6f73", "#d9822b", "#1d2b3a"]}],
                "disciplines": [{"code": "therapy", "title": "Терапия"}, {"code": "surgery", "title": "Хирургия"}],
                "titleFields": {"talk": [["university", "Вуз"], ["student", "Докладчик (ФИО)"], ["year", "Год"]]},
                "profile": {"student": "Иван Петров", "university": "ВолгГМУ", "year": "2026"},
                "maxPhotos": 12,
            }
        if action == "articles":
            off = p.get("offset", 0)
            return {"ok": True, "articles": [{"pmid": f"{off + i}00", "title": f"Статья {off + i}", "journal": "Lancet", "year": "2024", "design": "обзор", "abstract": "аннотация " * 30, "openAccess": i % 2 == 0} for i in range(1, 11)]}
        if action == "photo_upload":
            self.photos = getattr(self, "photos", 0) + 1
            return {"ok": True, "photoId": f"photo-{self.photos:012d}", "width": 400, "height": 300}
        if action == "styles":
            return {"ok": True, "templates": [{"id": "cobalt", "name": "Кобальт"}, {"id": "mint", "name": "Мята"}]}
        if action == "create":
            if getattr(self, "closed", False):
                return {"ok": False, "error": "Презентации пока открыты только для команды Medap.", "closed": True}
            if self.pay_needed:
                return {"ok": False, "error": "Не хватает денег", "payUrl": "https://pay.test/x", "shortfallKopecks": 3000}
            return {"ok": True, "runId": "run1"}
        if action == "resume":
            return {"ok": True, "runId": "run1"}
        if action == "status":
            return {"ok": True, "phase": self.phase_seq.pop(0) if len(self.phase_seq) > 1 else self.phase_seq[0], "error": None, "slides": 3}
        if action == "draft":
            return {"ok": True, "phase": "draft", "approved": False, "topic": "Гипертония", "editsLeft": 2, "slides": [{"n": 1, "title": "Гипертония", "text": "", "fixed": True, "fill": None}, {"n": 2, "title": "Причины", "text": "Первая строка", "fixed": False, "fill": None}]}
        if action == "edit":
            return {"ok": True, "index": p["index"], "note": None}
        if action == "preview":
            return {"ok": True, "base64": base64.b64encode(b"\x89PNG-test").decode()}
        if action == "approve":
            return {"ok": True}
        if action == "file":
            return {"ok": True, "fileName": "Gipertoniya.pptx", "base64": base64.b64encode(b"PK-test").decode()}
        if action == "topup":
            return {"ok": True, "payUrl": "https://pay.test/topup"}
        if action == "stats":
            return {"ok": True, "runs": 5, "free": 2, "failed": 1, "paidRuns": 2, "costRub": 12.5, "topupRub": 300}
        return {"ok": True}


class FakeMessage:
    def __init__(self, tg: int = 111, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=tg, username="stud")
        self.chat = SimpleNamespace(id=tg)
        self.text = text
        self.sent: list[tuple[str, object]] = []
        self.docs: list[tuple[str, bytes]] = []
        self.photo = None
        self.document = None

    async def answer(self, text: str, reply_markup=None, **_):
        self.sent.append((text, reply_markup))

    async def edit_text(self, text: str, reply_markup=None, **_):
        self.sent.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None, **_):
        self.sent.append(("(кнопки обновлены)", reply_markup))

    async def answer_document(self, document, caption=None, **_):
        self.docs.append((document.filename, document.data))


class FakeCall:
    def __init__(self, data: str, tg: int = 111, message: FakeMessage | None = None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=tg)
        self.message = message or FakeMessage(tg)
        self.answered = 0

    async def answer(self, *_a, **_k):
        self.answered += 1


def fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.download = AsyncMock(side_effect=lambda _id: __import__("io").BytesIO(b"\x89PNG-from-telegram"))
    return bot


def texts_of(msg: FakeMessage) -> str:
    return "\n".join(t for t, _ in msg.sent)


async def flow_checks() -> None:
    fs = FakeSite()
    slides_site.site = fs  # хендлеры берут клиента из модуля при каждом вызове
    H.POLL_SECONDS = 0
    settings.SLIDES_SITE_URL = "https://site.test"
    settings.ADMIN_IDS_RAW = "999"
    storage = MemoryStorage()

    def ctx(tg: int = 111) -> FSMContext:
        return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=tg, user_id=tg))

    bot = fake_bot()

    # старт без входа → просим почту
    m = FakeMessage(text="/start")
    await H.cmd_start(m, SimpleNamespace(args=None), ctx(), bot)
    ok("почту" in texts_of(m), "новичок: просим почту")
    ok(await ctx().get_state() == H.Login.email.state, "ждём почту")

    # плохая почта, хорошая, плохой код, хороший код
    m = FakeMessage(text="не почта")
    await H.got_email(m, ctx())
    ok("Проверьте адрес" in texts_of(m) and await ctx().get_state() == H.Login.email.state, "плохая почта: остаёмся на шаге")
    m = FakeMessage(text="a@b.ru")
    await H.got_email(m, ctx())
    ok(await ctx().get_state() == H.Login.consent.state and "Согласен" in str(m.sent), "почта принята → просим согласие кнопкой")
    ok(not [c for c in fs.calls if c[0] == "login_request"], "без нажатия «Согласен» код не запрашивается")
    call = FakeCall("c:no")
    await H.cb_consent_no(call, ctx())
    ok(await ctx().get_state() == H.Login.email.state and not [c for c in fs.calls if c[0] == "login_request"], "«Другая почта»: код не отправлен")
    m = FakeMessage(text="a@b.ru")
    await H.got_email(m, ctx())
    call = FakeCall("c:ok")
    await H.cb_consent_ok(call, ctx())
    req = [c for c in fs.calls if c[0] == "login_request"][-1]
    ok(req[2]["consent"] is True and req[1] == 111, "«Согласен» → код запрошен с согласием и Telegram-id")
    ok(await ctx().get_state() == H.Login.code.state, "согласие дано → ждём код")
    m = FakeMessage(text="000000")
    await H.got_code(m, ctx())
    ok("не подошёл" in texts_of(m) or "неверный" in texts_of(m), "неверный код: сообщение")
    verify = [c for c in fs.calls if c[0] == "login_verify"][-1]
    ok(verify[2]["email"] == "a@b.ru" and verify[1] == 111, "код проверяется для той же почты и Telegram-id")
    m = FakeMessage(text="123456")
    await H.got_code(m, ctx())
    ok(fs.linked and "вы вошли" in texts_of(m), "вошли")

    # создание: тема → вид → размер → стиль → пожелания → создано
    call = FakeCall("m:new")
    await H.cb_new(call, ctx())
    ok(await ctx().get_state() == H.Create.topic.state, "ждём тему")
    m = FakeMessage(text="ab")
    await H.got_topic(m, ctx())
    ok(T.TOPIC_SHORT in texts_of(m), "короткая тема отклонена")
    m = FakeMessage(text="Артериальная гипертония")
    await H.got_topic(m, ctx())
    call = FakeCall("k:talk")
    await H.got_kind(call, ctx())
    buttons = [b.text for row in call.message.sent[0][1].inline_keyboard for b in row]
    ok(any("Бесплатная" in b for b in buttons) and any("30 ₽" in b for b in buttons) and any("50 ₽" in b for b in buttons), "в размерах — бесплатная и цены")
    call = FakeCall("s:10")
    await H.got_size(call, ctx())
    ok("Настройте презентацию" in texts_of(call.message) and "войдите" not in texts_of(call.message), f"после размера — экран «Настроить», а не просьба войти: {texts_of(call.message)}")
    ok(await ctx().get_state() == H.Create.setup.state, "ждём выборов")
    labels = [b.text for row in call.message.sent[0][1].inline_keyboard for b in row]
    for want in ("Источники", "Оформление", "Шрифты", "Цвета", "Заметки", "Титул", "Фото", "Пожелания", "Полная форма на сайте", "Создать презентацию"):
        ok(any(want in x for x in labels), f"на экране «Настроить» есть «{want}»: {labels}")
    ok("Источники: общие знания" in texts_of(call.message) and "Оформление: подберём сами" in texts_of(call.message), "на экране видно, что выбрано сейчас («авто»)")

    # источники: медицина, учебники → предмет, PubMed → список 10, выбор, «ещё 10», предел по числу слайдов
    call = FakeCall("o:src")
    await H.cb_opt(call, ctx(), fake_bot())
    ok("Откуда брать материал" in texts_of(call.message), "экран источников")
    await H.cb_opt(FakeCall("o:med"), ctx(), fake_bot())
    call = FakeCall("o:tb")
    await H.cb_opt(call, ctx(), fake_bot())
    ok("Предмет для учебников" in texts_of(call.message) and "Терапия" in str(call.message.sent), "учебники → просим выбрать предмет")
    await H.cb_opt(FakeCall("o:d:0"), ctx(), fake_bot())
    call = FakeCall("o:pm")
    await H.cb_opt(call, ctx(), fake_bot())
    ok("Статья 1" in texts_of(call.message) and "Статья 10" in texts_of(call.message) and "🔓" in texts_of(call.message), "PubMed: список 10 статей с аннотациями и пометкой открытого текста")
    btns = [b.text for row in call.message.sent[0][1].inline_keyboard for b in row]
    ok("Ещё 10 статей" not in btns, "при включённых учебниках «ещё 10» не показывается (как на сайте)")
    await H.cb_opt(FakeCall("o:p:0"), ctx(), fake_bot())
    await H.cb_opt(FakeCall("o:p:2"), ctx(), fake_bot())
    o = (await ctx().get_data())["o"]
    ok(o["pmids"] == ["100", "300"] and o["textbooks"] and o["medical"] and o["discipline"] == "therapy", f"выбор источников запомнен: {o}")
    await H.cb_opt(FakeCall("o:p:0"), ctx(), fake_bot())
    ok((await ctx().get_data())["o"]["pmids"] == ["300"], "повторное нажатие снимает статью")
    await H.cb_opt(FakeCall("o:tb"), ctx(), fake_bot())  # снять учебники → «ещё 10» вернётся
    call = FakeCall("o:pm")
    await H.cb_opt(call, ctx(), fake_bot())
    btns = [b.text for row in call.message.sent[0][1].inline_keyboard for b in row]
    ok("Ещё 10 статей" in btns, "без учебников есть «Ещё 10 статей»")
    call = FakeCall("o:pmm")
    await H.cb_opt(call, ctx(), fake_bot())
    ok("Статья 11" in texts_of(call.message) and [c for c in fs.calls if c[0] == "articles"][-1][2]["offset"] == 10, "«Ещё 10» просит вторую страницу (offset 10)")
    await ctx().update_data(total=3)
    for k in range(10):
        await H.cb_opt(FakeCall(f"o:p:{k}"), ctx(), fake_bot())
    ok(len((await ctx().get_data())["o"]["pmids"]) == 3, "выбрать можно не больше, чем слайдов (3)")
    await ctx().update_data(total=10)
    await H.cb_opt(FakeCall("o:pmx"), ctx(), fake_bot())

    # длинные аннотации: список режется по сообщениям, каждая аннотация видна целиком и сразу, ни одно сообщение не длиннее лимита Telegram
    long = [{"pmid": f"{i}", "title": f"Статья {i}", "journal": "Lancet", "year": "2024", "design": "обзор", "abstract": ("Результаты: " + "предложение. " * 50).strip(), "openAccess": True} for i in range(1, 11)]
    chunks = T.articles_chunks(long, [], 10)
    ok(len(chunks) >= 2 and all(len(c) <= 4096 for c in chunks), f"длинный список статей — несколько сообщений, каждое в пределах лимита: {[len(c) for c in chunks]}")
    ok(all(x in "\n".join(chunks) for x in [f"{i}. Статья {i}" for i in range(1, 11)]) and "\n".join(chunks).count("Результаты:") == 10, "в списке все 10 статей и у каждой аннотация")
    ok(chunks[-1].rstrip().endswith("снять статью."), "подсказка про номера — в последнем сообщении, рядом с кнопками")

    # оформление, шрифты, цвета, заметки, титул
    call = FakeCall("o:sty:0")
    await H.cb_opt(call, ctx(), fake_bot())
    ok("Шаблон 0" in str(call.message.sent) and "Чистый лист" in str(call.message.sent), "список шаблонов с «чистым листом» и «подберите сами»")
    call = FakeCall("o:sty:1")
    await H.cb_opt(call, ctx(), fake_bot())
    ok("Шаблон 8" in str(call.message.sent) and "Шаблон 0" not in str(call.message.sent), "вторая страница шаблонов")
    await H.cb_opt(FakeCall("o:st:3"), ctx(), fake_bot())
    ok((await ctx().get_data())["o"]["styleId"] == "t3", "шаблон выбран")
    call = FakeCall("o:fnt:h:0")
    await H.cb_opt(call, ctx(), fake_bot())
    await H.cb_opt(FakeCall("o:fh:2"), ctx(), fake_bot())
    call = FakeCall("o:fb:5")
    await H.cb_opt(call, ctx(), fake_bot())
    ok((await ctx().get_data())["o"]["fonts"] == {"heading": "Шрифт2", "body": "Шрифт5"}, "шрифты — парой: заголовки и текст")
    await H.cb_opt(FakeCall("o:cp:0"), ctx(), fake_bot())
    ok((await ctx().get_data())["o"]["colors"] == ["#2f6f73", "#d9822b", "#1d2b3a"], "набор цветов выбран")
    await H.cb_opt(FakeCall("o:cc"), ctx(), fake_bot())
    ok(await ctx().get_state() == H.Create.colors.state, "свои цвета: ждём коды")
    m = FakeMessage(text="ерунда")
    await H.got_colors(m, ctx())
    ok("Не нашёл цветов" in texts_of(m) and await ctx().get_state() == H.Create.colors.state, "не цвета — просим ещё раз")
    m = FakeMessage(text="#112233 AABBCC 445566 ffffff")
    await H.got_colors(m, ctx())
    ok((await ctx().get_data())["o"]["colors"] == ["#112233", "#aabbcc", "#445566"] and await ctx().get_state() == H.Create.setup.state, "свои цвета: до трёх, нижний регистр, возврат к настройкам")
    await H.cb_opt(FakeCall("o:n:detailed"), ctx(), fake_bot())
    await H.cb_opt(FakeCall("o:dd:brief"), ctx(), fake_bot())
    await H.cb_opt(FakeCall("o:num"), ctx(), fake_bot())
    call = FakeCall("o:ttl")
    await H.cb_opt(call, ctx(), fake_bot())
    ok("Иван Петров" in texts_of(call.message) and "ВолгГМУ" not in texts_of(call.message) or "Вуз: ВолгГМУ" in texts_of(call.message), "титул показывает значения из профиля")
    await H.cb_opt(FakeCall("o:tf:0"), ctx(), fake_bot())
    ok(await ctx().get_state() == H.Create.title_value.state, "поле титула: ждём значение")
    m = FakeMessage(text="МГУ")
    await H.got_title_value(m, ctx())
    ok((await ctx().get_data())["o"]["title"] == {"university": "МГУ"} and "Вуз: МГУ" in texts_of(m), "значение поля титула сохранено и показано")
    await H.cb_opt(FakeCall("o:tf:1"), ctx(), fake_bot())
    await H.got_title_value(FakeMessage(text="-"), ctx())
    ok((await ctx().get_data())["o"]["title"]["student"] == "", "«-» очищает поле")

    # фото из чата: идут на сайт, запоминаются; не снимок и огромный файл отклоняются
    bot_ph = fake_bot()
    m = FakeMessage()
    m.photo = [SimpleNamespace(file_id="small", file_size=1000), SimpleNamespace(file_id="big", file_size=200000)]
    await H.got_photo(m, ctx(), bot_ph)
    bot_ph.download.assert_awaited_with("big")
    up = [c for c in fs.calls if c[0] == "photo_upload"][-1]
    ok(base64.b64decode(up[2]["base64"]) == b"\x89PNG-from-telegram" and up[1] == 111, "снимок из чата ушёл на сайт (самый крупный размер)")
    ok(len((await ctx().get_data())["o"]["photoIds"]) == 1 and "принят" in texts_of(m), "снимок принят и посчитан")
    m = FakeMessage()
    m.document = SimpleNamespace(file_id="doc", file_size=1000, mime_type="application/pdf")
    await H.got_photo(m, ctx(), bot_ph)
    ok("не снимок" in texts_of(m), "не снимок отклонён")
    m = FakeMessage()
    m.photo = [SimpleNamespace(file_id="huge", file_size=9 * 1024 * 1024)]
    await H.got_photo(m, ctx(), bot_ph)
    ok("больше 8 МБ" in texts_of(m), "слишком большой снимок отклонён до скачивания")
    await ctx().update_data(o={**(await ctx().get_data())["o"], "photoIds": [f"p{i:011d}" for i in range(12)]})
    m = FakeMessage()
    m.photo = [SimpleNamespace(file_id="x", file_size=10)]
    await H.got_photo(m, ctx(), bot_ph)
    ok("больше не бывает" in texts_of(m), "13-й снимок не принимается")
    await ctx().update_data(o={**(await ctx().get_data())["o"], "photoIds": ["photo-000000000001"]})
    await H.cb_opt(FakeCall("o:ph"), ctx(), fake_bot())

    # «Открыть полную форму на сайте» — одноразовая ссылка на /slides/new
    call = FakeCall("o:site")
    await H.cb_opt(call, ctx(), fake_bot())
    link_btn = [b for row in call.message.sent[0][1].inline_keyboard for b in row if b.url]
    ok(link_btn and link_btn[0].url == "https://site.test/t/ONETIME-" and [c for c in fs.calls if c[0] == "open_link"][-1][2] == {"path": "/slides/new"}, f"кнопка полной формы ведёт по одноразовой ссылке: {[b.url for b in link_btn]}")

    # пожелания
    await H.cb_opt(FakeCall("o:wsh"), ctx(), fake_bot())
    ok(await ctx().get_state() == H.Create.wishes.state, "ждём пожелания")
    m = FakeMessage(text="поменьше текста")
    await H.got_wishes(m, ctx())
    ok("Пожелания: поменьше текста" in texts_of(m) and await ctx().get_state() == H.Create.setup.state, "пожелания сохранены, вернулись к настройкам")

    # создание получает ровно то, что выбрано
    await H.cb_opt(FakeCall("o:med"), ctx(), fake_bot())  # medical был включён в начале → снять и снова включить, чтобы проверить переключатель
    await H.cb_opt(FakeCall("o:med"), ctx(), fake_bot())
    m = FakeMessage()
    await H.cb_opt(FakeCall("o:go", message=m), ctx(), bot)
    create = [c for c in fs.calls if c[0] == "create"][-1]
    ok(create[2] == {
        "topic": "Артериальная гипертония", "kind": "talk", "total": 10, "styleId": "t3", "wishes": "поменьше текста",
        "medical": True, "notes": "detailed", "detail": "brief", "fonts": {"heading": "Шрифт2", "body": "Шрифт5"},
        "colors": ["#112233", "#aabbcc", "#445566"], "numbers": True, "titleFields": {"university": "МГУ", "student": ""}, "photoIds": ["photo-000000000001"],
    }, f"создание получило всё выбранное: {create[2]}")
    ok(T.WORKING in texts_of(m) and await ctx().get_state() is None, "создано, состояние очищено")
    await asyncio.gather(*list(H._tasks))
    sent = bot.send_message.await_args_list
    ok(any("Черновик: Гипертония" in str(c.args[1]) for c in sent), "черновик пришёл сам после готовности")
    photos = bot.send_photo.await_args_list
    ok(len(photos) == 1 and "слайд 2" in photos[0].kwargs["caption"], "перед черновиком пришла картинка слайда")
    draft_msg = [c for c in sent if "Черновик: Гипертония" in str(c.args[1])][-1]
    site_btn = [b for row in draft_msg.kwargs["reply_markup"].inline_keyboard for b in row if b.url]
    ok(site_btn and site_btn[0].url == "https://site.test/t/ONETIME-run1", f"кнопка «Открыть на сайте» ведёт по одноразовой ссылке входа, а не на закрытую страницу: {[b.url for b in site_btn]}")
    ok([c for c in fs.calls if c[0] == "open_link"][-1][2] == {"runId": "run1"}, "одноразовая ссылка запрошена именно на эту презентацию")
    ok([c for c in fs.calls if c[0] == "preview"][-1][2] == {"runId": "run1", "index": 1}, "превью запрошено у сайта для первого слайда с содержанием")

    # ничего не выбрано → создание как раньше (минимум полей), всё остальное подберёт сайт
    ok(H._create_payload({"topic": "Т", "kind": "lecture", "total": 7}) == {"topic": "Т", "kind": "lecture", "total": 7, "styleId": "", "wishes": ""}, "без выборов — только тема, вид, размер")
    # сайт старой версии (не знает «options»): человек получает точное объяснение, а не «связь не настроена»
    real_call = fs.call

    async def old_site(action, telegram_id=None, **p):
        return {"ok": False, "error": "Неизвестное действие"} if action == "options" else await real_call(action, telegram_id, **p)

    fs.call = old_site
    H._catalog.clear()
    await ctx().update_data(topic="Тема", kind="talk", total=10, o=T.default_opts())
    m = FakeMessage()
    await H.cb_opt(FakeCall("o:sty:0", message=m), ctx(), fake_bot())
    ok("ещё не обновлён" in texts_of(m) and "не настроена" not in texts_of(m), f"старый сайт: понятное объяснение: {texts_of(m)}")
    fs.call = real_call
    H._catalog.clear()
    await ctx().clear()
    # потерянное состояние (бот перезапускался): понятное сообщение
    m = FakeMessage()
    await H.cb_opt(FakeCall("o:src", message=m), ctx(), fake_bot())
    ok("Начнём заново" in texts_of(m), "кнопка настроек без состояния — «начнём заново»")

    # не хватает денег → ссылка на оплату, задание сохранено; возврат из оплаты → пишется
    fs.pay_needed = True
    await ctx().update_data(topic="Тема", kind="talk", total=15, o=T.default_opts())
    await ctx().set_state(H.Create.setup)
    m = FakeMessage()
    await H.cb_opt(FakeCall("o:go", message=m), ctx(), bot)
    pay = [b for row in m.sent[0][1].inline_keyboard for b in row if b.url]
    ok(pay and pay[0].url == "https://pay.test/x" and "30 ₽" in pay[0].text, "кнопка оплаты на недостающую сумму")
    m = FakeMessage(text="/start paid_int1")
    bot2 = fake_bot()
    await H._after_payment(m, ctx(), bot2, 111, "int1")
    ok("Оплата получена" in texts_of(m), "после оплаты — продолжаем")
    await asyncio.gather(*list(H._tasks))
    ok(bot2.send_message.await_count >= 1, "после оплаты пришёл черновик")

    # правка слайда, утверждение, файл
    call = FakeCall("ea:run1:1:shorter")
    bot3 = fake_bot()
    await H.cb_edit_action(call, ctx(), bot3)
    edit = [c for c in fs.calls if c[0] == "edit"][-1]
    ok(edit[2] == {"runId": "run1", "index": 1, "kind": "shorter"}, "правка «короче» уходит на сайт")
    ok("Слайд 2 исправлен" in str(bot3.send_message.await_args_list[-1].args[1]), "после правки — обновлённый черновик")
    call = FakeCall("ea:run1:1:own")
    await H.cb_edit_action(call, ctx(), bot3)
    ok(await ctx().get_state() == H.Edit.phrase.state, "«своими словами» ждёт фразу")
    await H.got_phrase(FakeMessage(text="добавь пример"), ctx(), bot3)
    edit = [c for c in fs.calls if c[0] == "edit"][-1]
    ok(edit[2]["kind"] == "general" and edit[2]["phrase"] == "добавь пример", "правка фразой")
    call = FakeCall("a:run1")
    await H.cb_approve(call, bot3)
    ok("Утверждено" in texts_of(call.message), "утверждено")
    call = FakeCall("f:run1:pptx")
    await H.cb_file(call)
    ok(call.message.docs == [("Gipertoniya.pptx", b"PK-test")], "файл пришёл документом с исходным именем")

    # баланс и пополнение
    call = FakeCall("t:30000")
    await H.cb_topup(call, ctx())
    ok(any(b.url == "https://pay.test/topup" for row in call.message.sent[0][1].inline_keyboard for b in row), "ссылка на пополнение")
    topup = [c for c in fs.calls if c[0] == "topup"][-1]
    ok(topup[2] == {"kopecks": 30000}, "сумма пополнения в копейках")
    await ctx().set_state(H.Topup.amount)
    m = FakeMessage(text="250,5 ₽")
    await H.got_amount(m, ctx())
    ok([c for c in fs.calls if c[0] == "topup"][-1][2] == {"kopecks": 25050}, "своя сумма разобрана")
    m = FakeMessage(text="много")
    await ctx().set_state(H.Topup.amount)
    await H.got_amount(m, ctx())
    ok("число" in texts_of(m), "не число — просим число")

    # обычному человеку в меню нет «Владелец», владельцу (и Telegram-id в списке, и роль на сайте) — есть
    m = FakeMessage(tg=111)
    await H._menu(m, 111)
    ok(not any("Владелец" in b.text for row in m.sent[0][1].inline_keyboard for b in row), "обычному человеку кнопки «Владелец» нет")
    m = FakeMessage(tg=999)
    await H._menu(m, 999)
    ok(any("Владелец" in b.text for row in m.sent[0][1].inline_keyboard for b in row), "владельцу кнопка есть")

    # владелец: только по ADMIN_IDS_RAW
    call = FakeCall("m:owner", tg=111)
    await H.cb_owner(call)
    ok(not call.message.sent, "обычному человеку раздел владельца молчит")
    call = FakeCall("m:owner", tg=999)
    await H.cb_owner(call)
    ok("презентаций: 5" in texts_of(call.message) and "300 ₽" in texts_of(call.message), "владельцу — сводка")
    ok(any("Владелец" in b.text for row in T.main_menu(True).inline_keyboard for b in row) and not any("Владелец" in b.text for row in T.main_menu(False).inline_keyboard for b in row), "кнопка «Владелец» только в меню владельца")

    # кабинет закрыт для не-команды: человеку понятный текст, ни оплаты, ни ожидания
    fs.closed = True
    await ctx().update_data(topic="Тема", kind="talk", total=10, o=T.default_opts())
    await ctx().set_state(H.Create.setup)
    m = FakeMessage()
    await H.cb_opt(FakeCall("o:go", message=m), ctx(), fake_bot())
    ok("только для команды" in texts_of(m) and "Следите" in texts_of(m), "закрыто: понятное сообщение")
    fs.closed = False

    # сбой на сайте при ожидании: сообщение, не молчание
    fs.phase_seq = ["failed"]
    bot4 = fake_bot()
    H._watch(bot4, 5, 111, "run1")
    await asyncio.gather(*list(H._tasks))
    ok("не получилась" in str(bot4.send_message.await_args_list[-1].args[1]), "сбой генерации сообщён человеку")


def text_checks() -> None:
    ok(T.money(3000) == "30 ₽" and T.money(2550) == "25,50 ₽", "деньги")
    d = {"topic": "Т", "editsLeft": 1, "slides": [{"n": i, "title": f"З{i}", "text": "слово " * 80, "fixed": i == 1, "fill": None} for i in range(1, 21)]}
    ok(len(T.draft_text(d)) <= 3900, "черновик помещается в сообщение Telegram")
    pick = T.slides_pick_kb(d, "r")
    ok(sum(len(r) for r in pick.inline_keyboard[:-1]) == 19, "кнопки правки — без титульного слайда")
    for markup in (T.main_menu(True), T.kinds_kb(), T.edit_actions_kb("r" * 25, 19), T.files_kb("r" * 25, None), T.topup_kb()):
        for row in markup.inline_keyboard:
            for b in row:
                ok(b.callback_data is None or len(b.callback_data.encode()) <= 64, f"callback_data ≤ 64 байт: {b.callback_data}")


def runner_checks() -> None:
    from slides_bot import runner
    cfg = SimpleNamespace(SLIDES_BOT_USERNAME="Old_Name")
    ok(any("SLIDES_SITE_URL" in p for p in runner.config_problems(cfg, site_configured=False)), "без настроек сайта владельцу говорят, какие переменные вписать")
    ok(runner.config_problems(cfg, site_configured=True) == [], "настроено — предупреждений нет")
    ok(any("@real_bot" in p for p in runner.config_problems(cfg, site_configured=True, username="real_bot")), "имя в переменной не совпало с настоящим — предупреждение")
    ok(runner.config_problems(SimpleNamespace(SLIDES_BOT_USERNAME="@Real_Bot"), site_configured=True, username="real_bot") == [], "@ и регистр в имени не мешают")


async def main() -> None:
    text_checks()
    runner_checks()
    await client_checks()
    await flow_checks()
    print(f"OK: {checks} проверок")


asyncio.run(main())
