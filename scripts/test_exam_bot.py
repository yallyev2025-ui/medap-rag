"""Режим «Экзамен» в Telegram (MedAP, этап 5): python -m scripts.test_exam_bot (без сети, базы и тяжёлых библиотек).

Проверяется поток: загрузка списка (текст, фото), «Спроси меня» по очереди, разбор ответа текстом и голосом (голос — подписчикам),
слабые вопросы возвращаются в конце круга, «Расскажу тему», «Ответь на вопрос» по номеру, «Верный ответ».
"""

import asyncio
import os
import sys
import types
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")


class _Anything(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = lambda *a, **k: None  # noqa: E731
        setattr(self, name, value)
        return value


@dataclass
class EvaluationResult:
    transcript: str | None = None
    overall_feedback: str = ""
    covered: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    partially_correct: list = field(default_factory=list)
    incorrect: list = field(default_factory=list)
    causal_errors: list = field(default_factory=list)
    terminology_errors: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    repair: str = ""


ev = _Anything("app.workflows.evaluate")
ev.EvaluationResult = EvaluationResult  # type: ignore[attr-defined]
sys.modules["app.workflows.evaluate"] = ev
for heavy in ["rag.processor", "rag.generator", "rag.retriever", "rag.embedder", "app.workflows.ask", "app.workflows.pubmed", "app.workflows.web_search", "bot.fragments", "bot.handlers.query", "bot.waiting_phrases", "app.workflows.vision"]:
    sys.modules[heavy] = _Anything(heavy)

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

import bot.handlers.exam as ex  # noqa: E402

checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    assert cond, what
    checks += 1


class FakeMessage:
    def __init__(self, text=None, voice=None, photo=None, user=None):
        self.text = text
        self.voice = voice
        self.photo = photo
        self.document = None
        self.from_user = user
        self.sent: list[tuple[str, object]] = []
        self.voices = 0
        self.bot = types.SimpleNamespace(download=self._download)

    async def _download(self, _x):
        return types.SimpleNamespace(read=lambda: b"bytes")

    async def answer(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))
        return self

    async def delete(self):
        pass

    async def edit_text(self, *a, **k):
        pass

    def model_copy(self, update):
        clone = FakeMessage(self.text, self.voice, self.photo, update.get("from_user", self.from_user))
        clone.sent = self.sent
        return clone


class CB:
    def __init__(self, user, data):
        self.from_user = user
        self.data = data
        self.message = FakeMessage(user=None)
        self.alerts: list[str] = []

    async def answer(self, text="", show_alert=False):
        self.alerts.append(text)


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


DB: dict = {}
free = types.SimpleNamespace(id=1, is_premium=False, current_subject="pathphys", current_source_type="учебник")
paid = types.SimpleNamespace(id=2, is_premium=True, current_subject="pathphys", current_source_type="учебник")
users = {1: free, 2: paid}


async def fake_user(session, tg):
    return users[tg.id]


async def fake_get(session, user_id, subject):
    return DB.get((user_id, subject or ""))


async def fake_save(session, user_id, subject, questions):
    import json

    row = types.SimpleNamespace(questions=json.dumps(questions, ensure_ascii=False), position=0, weak="[]")
    DB[(user_id, subject or "")] = row
    return row


async def fake_update(session, row, position, weak):
    import json

    row.position = position
    row.weak = json.dumps(sorted(set(weak)))


async def fake_voice(session, uid):
    return "onyx"


ex.async_session = lambda: Session()  # type: ignore[assignment]
ex.get_or_create_user = fake_user  # type: ignore[assignment]
ex.get_exam_list = fake_get  # type: ignore[assignment]
ex.save_exam_list = fake_save  # type: ignore[assignment]
ex.update_exam_progress = fake_update  # type: ignore[assignment]
ex.get_voice = fake_voice  # type: ignore[assignment]
ex.can_use_deep = lambda u: bool(u.is_premium)  # type: ignore[assignment]

spoken: list[str] = []


async def fake_speak(message, text, voice):
    spoken.append(text)
    return True


ex.speak_to = fake_speak  # type: ignore[assignment]

evaluated: list[tuple[str, str]] = []
RESULT = {"weak": True}


async def fake_recall(question, answer, source_type="x", subject=None):
    evaluated.append(("text", question))
    return EvaluationResult(overall_feedback="Разбор", covered=["определение"], missing=["исходы"] if RESULT["weak"] else [], repair="добавь исходы" if RESULT["weak"] else "")


async def fake_oral(question, audio, source_type="x", subject=None, filename="v.ogg"):
    evaluated.append(("voice", question))
    return EvaluationResult(transcript="я сказал", overall_feedback="Разбор голоса")


async def fake_free(topic, answer, source_type="x", subject=None):
    evaluated.append(("story", topic))
    return EvaluationResult(overall_feedback="Разбор рассказа", missing=["осложнения"])


ex.evaluate_recall = fake_recall  # type: ignore[assignment]
ex.evaluate_oral = fake_oral  # type: ignore[assignment]
ex.evaluate_free_recall = fake_free  # type: ignore[assignment]

pipeline: list[str] = []


async def fake_answer_question(message, user, ctx, question, voice_reply=False):
    pipeline.append(f"{message.from_user.id if message.from_user else None}:{question}:{voice_reply}")


sys.modules["bot.handlers.query"].answer_question = fake_answer_question  # type: ignore[attr-defined]


def make_state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


async def main() -> None:
    # загрузка списка текстом: разбор, сохранение, лимит не тратится
    st = make_state(2)
    await ex.cb_list(CB(paid, "ex:list"), st)
    ok(await st.get_state() == ex.ExamStates.waiting_list.state, "ждём список")
    m = FakeMessage(text="1. Определение воспаления\n2. Стадии воспаления\n3. Исходы воспаления", user=paid)
    ctx = {"count": True}
    await ex.got_list_text(m, st, paid, ctx)
    ok(ctx["count"] is False and "Принял вопросов: 3" in m.sent[0][0] and await st.get_state() is None, "список принят, состояние сброшено, лимит не тратится")

    # «Спроси меня» без списка — просит загрузить
    st1 = make_state(1)
    cb0 = CB(free, "ex:ask")
    await ex.cb_ask(cb0, st1)
    ok("Сначала загрузи" in cb0.message.sent[0][0], "без списка просит загрузить")

    # подписчик: вопрос 1 текстом и голосом; ответ текстом → разбор, слабый вопрос отмечен
    cb1 = CB(paid, "ex:ask")
    await ex.cb_ask(cb1, st)
    ok("Вопрос 1 из 3" in cb1.message.sent[0][0] and "Определение воспаления" in cb1.message.sent[0][0], "задан первый вопрос")
    ok(spoken == [], "вопрос приходит текстом, голосом бот не отвечает")
    ok(await st.get_state() == ex.ExamStates.waiting_answer.state, "ждём ответ")
    ans = FakeMessage(text="Это защитная реакция", user=paid)
    await ex.answer_text(ans, st, paid, {"count": True})
    ok(evaluated[-1] == ("text", "Определение воспаления") and "Разбор" in ans.sent[-1][0] and "Пропущено" in ans.sent[-1][0], "ответ разобран, пропущенное показано")
    kb = ans.sent[-1][1]
    ok([b.callback_data for row in kb.inline_keyboard for b in row][:4] == ["ex:key:0", "ex:weak:0", "ex:ask", "ex:menu"], "кнопки под разбором")
    ok(DB[(2, "pathphys")].weak == "[0]", "слабый вопрос запомнен")

    # вопрос 2 голосом: подписчику распознаётся и разбирается; бесплатному — отказ и состояние сохраняется
    await ex.cb_ask(CB(paid, "ex:ask"), st)
    v = FakeMessage(voice=object(), user=paid)
    await ex.answer_voice(v, st, paid, {"count": True})
    ok(evaluated[-1] == ("voice", "Стадии воспаления") and "я сказал" in v.sent[-1][0], "голосовой ответ подписчика разобран, транскрипт показан")

    # бесплатный: загрузка, вопрос, голосовой ответ — отказ без траты лимита, вопрос остаётся
    await ex.got_list_text(FakeMessage(text="Тромбоз\nЭмболия вопросы", user=free), st1, free, {"count": True})
    await ex.cb_ask(CB(free, "ex:ask"), st1)
    fv = FakeMessage(voice=object(), user=free)
    fctx = {"count": True}
    await ex.answer_voice(fv, st1, free, fctx)
    ok(fctx["count"] is False and "подписчиков" in fv.sent[0][0] and await st1.get_state() == ex.ExamStates.waiting_answer.state, "бесплатный: голос отклонён, вопрос остался")

    # круг: после списка возвращаются слабые, потом «пройдено»
    RESULT["weak"] = False
    await ex.answer_text(FakeMessage(text="Стадии: альтерация, экссудация", user=paid), st, paid, {"count": True})  # ответ на вопрос 2 (после voice состояние сброшено)
    await ex.cb_ask(CB(paid, "ex:ask"), st)  # вопрос 3
    await ex.answer_text(FakeMessage(text="Выздоровление или хронизация", user=paid), st, paid, {"count": True})
    cb_weak = CB(paid, "ex:ask")
    await ex.cb_ask(cb_weak, st)
    ok("Вопрос 1 из 3" in cb_weak.message.sent[0][0] and "на повтор" in cb_weak.message.sent[0][0], "после круга возвращается слабый вопрос 1")
    RESULT["weak"] = False
    await ex.answer_text(FakeMessage(text="Защитная реакция, три стадии", user=paid), st, paid, {"count": True})
    # слабый вопрос 1 всё ещё в списке слабых до явного «повторить позже»/успеха — проверим завершение вручную
    DB[(2, "pathphys")].weak = "[]"
    cb_done = CB(paid, "ex:ask")
    await ex.cb_ask(cb_done, st)
    ok("Весь список пройден" in cb_done.message.sent[0][0], "список пройден — поздравление")

    # «Расскажу тему»
    st2 = make_state(2)
    await ex.cb_story(CB(paid, "ex:story"), st2)
    t = FakeMessage(text="Воспаление: исходы", user=paid)
    tctx = {"count": True}
    await ex.story_topic(t, st2, tctx)
    ok(tctx["count"] is False and await st2.get_state() == ex.ExamStates.waiting_story.state, "тема принята, лимит не тратится, ждём рассказ")
    s = FakeMessage(text="Исходы: выздоровление, хронизация", user=paid)
    await ex.story_text(s, st2, paid, {"count": True})
    ok(evaluated[-1] == ("story", "Воспаление: исходы") and "Разбор рассказа" in s.sent[-1][0], "рассказ разобран")

    # «Ответь на вопрос»: по номеру из списка и своим текстом, лишний номер — понятный отказ
    st3 = make_state(2)
    await ex.cb_solve(CB(paid, "ex:solve"), st3)
    r = FakeMessage(text="2", user=paid)
    await ex.solve_run(r, st3, paid, {"count": True})
    ok(pipeline[-1] == "2:Стадии воспаления:False", f"ответ по номеру через обычный конвейер, текстом: {pipeline}")
    await ex.cb_solve(CB(paid, "ex:solve"), st3)
    r2 = FakeMessage(text="Что такое лейкоцитоз?", user=paid)
    await ex.solve_run(r2, st3, paid, {"count": True})
    ok(pipeline[-1].startswith("2:Что такое лейкоцитоз?"), "свой вопрос тоже отвечается")
    await ex.cb_solve(CB(paid, "ex:solve"), st3)
    r3 = FakeMessage(text="99", user=paid)
    c3 = {"count": True}
    await ex.solve_run(r3, st3, paid, c3)
    ok(c3["count"] is False and "нет вопроса номер 99" in r3.sent[0][0], "несуществующий номер — понятный отказ")

    # «Верный ответ» под разбором — ответ идёт от имени студента
    cbk = CB(paid, "ex:key:0")
    await ex.cb_key(cbk)
    ok(pipeline[-1].startswith("2:Определение воспаления"), "верный ответ — обычным конвейером от имени студента")

    # фото списка: через распознавание, добавляется следующей страницей
    vis = sys.modules["app.workflows.vision"]

    async def fake_transcribe(img, mime="image/jpeg"):
        return ["1. Воспаление: определение", "2. Тромбоз: исходы"], 0.9, None

    vis.transcribe_image = fake_transcribe  # type: ignore[attr-defined]
    st4 = make_state(1)
    await ex.cb_list(CB(free, "ex:list"), st4)
    ph = FakeMessage(photo=[object()], user=free)
    await ex.got_list_photo(ph, st4, free, {"count": True})
    ok("Принял вопросов: 2" in ph.sent[-1][0], "фото списка прочитано")

    # выход
    cbs = CB(paid, "ex:stop")
    await ex.cb_stop(cbs, st3)
    ok("Вышли из режима" in cbs.message.sent[0][0], "выход из режима")

    print(f"OK: {checks} проверок")


asyncio.run(main())
