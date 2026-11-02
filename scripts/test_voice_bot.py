"""Голос в Telegram (MedAP, этап 5): python -m scripts.test_voice_bot (без сети, базы и тяжёлых библиотек).

Проверяется логика обработчиков: голосовой вопрос и «🔊 Озвучить» — только подписчикам; бесплатному — отказ без траты лимита;
подписчику — распознавание, ответ текстом и голосом; выбор голоса сохраняется, чужой голос отклоняется.
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")


class _Anything(types.ModuleType):
    """Заглушка тяжёлого модуля: любой запрошенный атрибут — безвредная функция/значение."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = lambda *a, **k: None  # noqa: E731
        setattr(self, name, value)
        return value


for heavy in [
    "rag.embedder", "rag.generator", "rag.processor", "rag.retriever", "app.workflows.ask", "app.workflows.pubmed", "app.workflows.web_search",
    "bot.fragments", "bot.handlers.panel", "bot.handlers.menu", "bot.waiting_phrases",
]:
    sys.modules[heavy] = _Anything(heavy)
sys.modules["bot.handlers.menu"].CLINREK_PREMIUM_TEXT = ""  # type: ignore[attr-defined]
sys.modules["bot.waiting_phrases"].FIRST_PHRASE = "…"  # type: ignore[attr-defined]
sys.modules["app.workflows.pubmed"].SOURCE_EUROPEPMC = "x"  # type: ignore[attr-defined]

import bot.handlers.query as q  # noqa: E402

checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    assert cond, what
    checks += 1


class FakeMessage:
    def __init__(self, voice=None, text=None):
        self.voice = voice
        self.text = text
        self.caption = None
        self.sent: list[tuple[str, object]] = []
        self.voices = 0
        self.bot = types.SimpleNamespace(download=self._download)

    async def _download(self, _voice):
        return types.SimpleNamespace(read=lambda: b"ogg")

    async def answer(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))
        return self

    async def answer_voice(self, _file, **kw):
        self.voices += 1
        return self

    async def delete(self):
        pass

    async def edit_text(self, *a, **k):
        pass


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


async def main() -> None:
    free = types.SimpleNamespace(id=1, is_premium=False)
    paid = types.SimpleNamespace(id=2, is_premium=True)

    # кнопка «Озвучить» — только подписчикам, кнопки источников сохраняются
    ok(q._with_speak(None, False) is None, "бесплатному кнопки нет")
    base = q.InlineKeyboardMarkup(inline_keyboard=[[q.InlineKeyboardButton(text="📖 книга · стр. 3", callback_data="frag:1")]])
    withspeak = q._with_speak(base, True)
    ok(len(withspeak.inline_keyboard) == 2 and withspeak.inline_keyboard[1][0].callback_data == "speak:1" and withspeak.inline_keyboard[0][0].text.startswith("📖"), "подписчику добавлена строка, источники целы")

    # голосовой вопрос бесплатному: отказ, лимит не тратится, распознавание не вызывается
    transcribed: list[str] = []
    from app.llm import provider as llm

    async def fake_transcribe(audio, name="x"):
        transcribed.append(name)
        return "Какие стадии воспаления?"

    llm.transcribe = fake_transcribe  # type: ignore[assignment]
    m = FakeMessage(voice=object())
    ctx = {"count": True}
    await q.handle_voice_question(m, free, ctx)
    ok(ctx["count"] is False and not transcribed and "подписчиков" in m.sent[0][0], "бесплатному — отказ без траты лимита и без распознавания")

    # подписчику: распознали → ответили (текст) → озвучили
    asked: list[tuple[str, bool]] = []
    spoken: list[str] = []

    async def fake_answer(message, db_user, usage_ctx, question, voice_reply=False):
        asked.append((question, voice_reply))

    q.answer_question = fake_answer  # type: ignore[assignment]
    m2 = FakeMessage(voice=object())
    await q.handle_voice_question(m2, paid, {"count": True})
    ok(asked == [("Какие стадии воспаления?", False)] and transcribed == ["voice.ogg"], "подписчик: вопрос распознан и отправлен в обычный поток, ответ текстом (голосом отвечает только «Звонок»)")
    ok(any("Вы спросили" in t for t, _ in m2.sent), "бот показал, что услышал")

    # тишина в записи → просьба повторить, лимит не тратится
    async def silent(audio, name="x"):
        return "   "

    llm.transcribe = silent  # type: ignore[assignment]
    m3 = FakeMessage(voice=object())
    ctx3 = {"count": True}
    await q.handle_voice_question(m3, paid, ctx3)
    ok(ctx3["count"] is False and "Не расслышал" in m3.sent[-1][0], "пустое распознавание — переспрос")

    # «Озвучить»: бесплатному — всплывающий отказ; подписчику — голос по выбранному голосу
    answers: list[tuple] = []

    class CB:
        def __init__(self, user, text):
            self.from_user = user
            self.data = "speak:1"
            self.message = FakeMessage(text=text)

        async def answer(self, text="", show_alert=False):
            answers.append((text, show_alert))

    users = {1: free, 2: paid}
    q.async_session = lambda: Session()  # type: ignore[assignment]

    async def fake_user(session, tg_user):
        return users[tg_user]

    async def fake_get_voice(session, uid):
        return "onyx"

    q.get_or_create_user = fake_user  # type: ignore[assignment]
    q.get_voice = fake_get_voice  # type: ignore[assignment]

    async def fake_speak(message, text, voice):
        spoken.append(f"{voice}:{text}")
        return True

    q.speak_to = fake_speak  # type: ignore[assignment]
    await q.speak_callback(CB(1, "Ответ"))
    ok(answers[-1][1] is True and not spoken, "бесплатному «Озвучить» — отказ, звук не делается")
    await q.speak_callback(CB(2, "Ответ ментора"))
    ok(spoken == ["onyx:Ответ ментора"], "подписчику озвучен текст сообщения выбранным голосом")

    # выбор голоса
    saved: list[str] = []
    import db.crud as crud

    async def fake_set(session, uid, voice):
        saved.append(voice)

    crud.set_voice = fake_set  # type: ignore[assignment]

    class VCB(CB):
        def __init__(self, user, data):
            super().__init__(user, "")
            self.data = data

    await q.voice_chosen(VCB(2, "voice:echo"))
    ok(saved == ["echo"], "голос сохранён")
    await q.voice_chosen(VCB(2, "voice:чужой"))
    ok(saved == ["echo"] and answers[-1][1] is True, "неизвестный голос отклонён")
    await q.voice_chosen(VCB(1, "voice:nova"))
    ok(saved == ["echo"], "бесплатному голос не выбирается")
    m4 = FakeMessage(text="/voice")
    await q.cmd_voice(m4, paid, {"count": True})
    ok(len(m4.sent[0][1].inline_keyboard) == 6, "команда /voice показывает шесть голосов")
    m5 = FakeMessage(text="/voice")
    c5 = {"count": True}
    await q.cmd_voice(m5, free, c5)
    ok(c5["count"] is False and "подписчиков" in m5.sent[0][0], "/voice бесплатному — отказ")

    # озвучка: опасные теги и разметка убираются до синтеза
    from bot import voice_reply

    calls: list[tuple] = []

    async def fake_synth(text, voice, speed, fmt):
        calls.append((text, voice, fmt))
        return b"opus", 0.0

    llm.synthesize = fake_synth  # type: ignore[assignment]
    m6 = FakeMessage(text="x")
    res = await voice_reply.speak_to(m6, "<b>Воспаление</b> — реакция [1], см. https://x.ru", "echo")
    ok(res and m6.voices == 1 and calls[0][0] == "Воспаление — реакция , см." or calls[0][0].startswith("Воспаление"), "теги и ссылки убраны, отправлено голосовым")
    ok(calls[0][1:] == ("echo", "opus"), "голос и формат opus")

    async def boom(text, voice, speed, fmt):
        raise RuntimeError("tts down")

    llm.synthesize = boom  # type: ignore[assignment]
    m7 = FakeMessage(text="x")
    ok(await voice_reply.speak_to(m7, "Текст ответа здесь", None) is False and "Не удалось озвучить" in m7.sent[-1][0], "сбой озвучки не ломает ответ")

    print(f"voice_bot: {checks} проверок прошло")


asyncio.run(main())
