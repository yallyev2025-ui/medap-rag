"""Режим «Экзамен» в Telegram (MedAP, этап 5; решения 41–42).

Студент загружает свой список экзаменационных вопросов (текстом, файлом Word/PDF/txt или фото) и дальше:
• «❓ Спроси меня» — бот задаёт вопросы списка по очереди (подписчикам — ещё и голосом), студент отвечает текстом или голосом,
  получает разбор: что верно, что пропущено, что добавить; слабые вопросы возвращаются в конце круга;
• «🎤 Расскажу тему» — студент называет тему и рассказывает её, бот разбирает рассказ;
• «📖 Ответь на вопрос» — бот сам отвечает на вопрос списка (по номеру) или любой другой, по учебникам, с источниками.
Голос (вопросы и ответы голосом) — только подписчикам; текст — всем.
"""

from __future__ import annotations

import logging
import os
import tempfile

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.exam.logic import load_list, load_weak, next_index, parse_questions, progress_line, question_by_number
from app.observability.context import request_context
from app.workflows.evaluate import EvaluationResult, evaluate_free_recall, evaluate_oral, evaluate_recall
from bot.formatting import split_for_telegram, to_telegram_html
from bot.panel import BTN_EXAM
from bot.voice_reply import VOICE_PREMIUM_TEXT
from constants import SOURCE_TEXTBOOK
from db.crud import can_use_deep, get_exam_list, get_or_create_user, save_exam_list, update_exam_progress
from db.models import User
from db.session import async_session

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "⚠️ Не получилось, попробуй ещё раз через минуту."
MAX_FILE_BYTES = 15 * 1024 * 1024

MENU_TEXT = (
    "🎓 Режим «Экзамен»\n\n"
    "Загрузи свой список экзаменационных вопросов, и я потренирую тебя по нему: буду спрашивать, разбирать ответы и объяснять, "
    "что пропущено. Можно и просто рассказать мне тему — я скажу, что верно, а что добавить."
)


class ExamStates(StatesGroup):
    waiting_list = State()
    waiting_answer = State()
    waiting_story_topic = State()
    waiting_story = State()
    waiting_solve = State()


def menu_keyboard(total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❓ Спроси меня" + (f" ({total})" if total else ""), callback_data="ex:ask")],
            [InlineKeyboardButton(text="🎤 Расскажу тему", callback_data="ex:story")],
            [InlineKeyboardButton(text="📖 Ответь на вопрос", callback_data="ex:solve")],
            [InlineKeyboardButton(text="🎙 Устный экзамен (звонок)", callback_data="ex:oral")],
            [InlineKeyboardButton(text="📱 Подготовка в приложении", callback_data="ex:app")],
            [InlineKeyboardButton(text="📋 Мой список вопросов" + (f" ({total})" if total else " — загрузить"), callback_data="ex:list")],
            [InlineKeyboardButton(text="⏹ Выйти из режима", callback_data="ex:stop")],
        ]
    )


def after_answer_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💡 Верный ответ", callback_data=f"ex:key:{index}"), InlineKeyboardButton(text="🔁 Повторить позже", callback_data=f"ex:weak:{index}")],
            [InlineKeyboardButton(text="➡️ Следующий", callback_data="ex:ask"), InlineKeyboardButton(text="🎓 Меню", callback_data="ex:menu")],
        ]
    )


def story_after_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎤 Ещё тема", callback_data="ex:story"), InlineKeyboardButton(text="🎓 Меню", callback_data="ex:menu")]]
    )


def format_evaluation(result: EvaluationResult) -> str:
    """Тот же разбор, что у /selfcheck: что покрыто, что пропущено, что неверно."""
    parts: list[str] = []
    if result.transcript:
        parts.append(f"🎤 Распознано: {result.transcript}")
    if result.overall_feedback:
        parts.append(result.overall_feedback)
    for label, items in [
        ("✅ Покрыто верно", result.covered),
        ("❌ Пропущено", result.missing),
        ("⚠️ Частично верно", result.partially_correct),
        ("❗ Неверно", result.incorrect),
        ("🔗 Ошибки причинно-следственных связей", result.causal_errors),
        ("📖 Ошибки терминологии", result.terminology_errors),
        ("⛔ Противоречия материалу", result.contradictions),
    ]:
        if items:
            parts.append(f"{label}:\n" + "\n".join(f"— {i}" for i in items))
    if result.repair:
        parts.append(f"💡 Что добавить: {result.repair}")
    return "\n\n".join(parts) if parts else "Оценка пуста — попробуй ответить подробнее."


def is_weak(result: EvaluationResult) -> bool:
    """Вопрос возвращается на повтор, если что-то пропущено, неверно или противоречит материалу."""
    return bool(result.missing or result.incorrect or result.contradictions)


async def _load_user(tg_user) -> User:
    async with async_session() as session:
        user = await get_or_create_user(session, tg_user)
        await session.commit()
        return user


async def _send(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    parts = split_for_telegram(to_telegram_html(text))
    for i, part in enumerate(parts):
        await message.answer(part, reply_markup=markup if i == len(parts) - 1 else None)


async def show_menu(message: Message, user: User) -> None:
    async with async_session() as session:
        row = await get_exam_list(session, user.id, user.current_subject)
    total = len(load_list(row.questions)) if row else 0
    await message.answer(MENU_TEXT, reply_markup=menu_keyboard(total))


# ----------------------------------------------------------------- вход и меню


@router.message(Command("exam"))
@router.message(F.text == BTN_EXAM)
async def cmd_exam(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_menu(message, await _load_user(message.from_user))


@router.callback_query(F.data == "ex:menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await show_menu(callback.message, await _load_user(callback.from_user))


@router.callback_query(F.data == "ex:stop")
async def cb_stop(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Режим экзамена выключен")
    await state.clear()
    await callback.message.answer("Вышли из режима «Экзамен». Вопросы по-прежнему можно задавать как обычно. Вернуться — /exam.")


# ----------------------------------------------------------------- список вопросов


@router.callback_query(F.data == "ex:list")
async def cb_list(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ExamStates.waiting_list)
    await callback.message.answer(
        "Пришли свои вопросы: текстом (каждый с новой строки, номера можно оставить), файлом Word, PDF или txt либо фото списка. "
        "Новый список заменит прежний. Передумал — /exam."
    )


async def _save_questions(message: Message, state: FSMContext, user: User, questions: list[str]) -> None:
    if len(questions) < 1:
        await message.answer("Не нашёл ни одного вопроса. Пришли список ещё раз: каждый вопрос с новой строки.")
        return
    async with async_session() as session:
        await save_exam_list(session, user.id, user.current_subject, questions)
        await session.commit()
    await state.clear()
    preview = "\n".join(f"{i + 1}. {q[:90]}" for i, q in enumerate(questions[:5]))
    more = f"\n…и ещё {len(questions) - 5}" if len(questions) > 5 else ""
    await message.answer(f"Принял вопросов: {len(questions)}.\n{preview}{more}", reply_markup=menu_keyboard(len(questions)))


@router.message(ExamStates.waiting_list, F.text & ~F.text.startswith("/"))
async def got_list_text(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    usage_ctx["count"] = False
    await _save_questions(message, state, db_user, parse_questions(message.text))


@router.message(ExamStates.waiting_list, F.document)
async def got_list_file(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    usage_ctx["count"] = False
    doc = message.document
    ext = os.path.splitext(doc.file_name or "")[1].lower()
    if ext not in {".docx", ".pdf", ".txt"}:
        await message.answer("Подойдёт Word (.docx), PDF с текстом или обычный текст. Остальное пришли фото или вставь текстом.")
        return
    if (doc.file_size or 0) > MAX_FILE_BYTES:
        await message.answer("Файл больше 15 МБ — раздели на части или пришли текстом.")
        return
    status = await message.answer("📄 Читаю файл…")
    try:
        from rag.processor import extract_document

        buffer = await message.bot.download(doc)
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(buffer.read())
            pages, _paged = extract_document(path)
        finally:
            os.remove(path)
        text = "\n".join(pages)
    except Exception:
        logger.exception("Не удалось прочитать файл со списком вопросов")
        await status.edit_text("Не получилось прочитать файл. Сохрани его как .docx или вставь вопросы текстом.")
        return
    try:
        await status.delete()
    except Exception:
        pass
    if not text.strip():
        await message.answer("В файле нет текста (похоже, это скан). Пришли фото страниц — я их прочитаю.")
        return
    await _save_questions(message, state, db_user, parse_questions(text))


@router.message(ExamStates.waiting_list, F.photo)
async def got_list_photo(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    """Фото списка вопросов: текст читает та же модель, что читает фото тестов; несколько фото — по одному, они добавляются."""
    from app.workflows.vision import transcribe_image

    status = await message.answer("🖼 Читаю фото…")
    try:
        buffer = await message.bot.download(message.photo[-1])
        with request_context(user_id=f"telegram:{db_user.id}", channel="telegram", workflow="VISION_EXTRACT"):
            lines, confidence, issue = await transcribe_image(buffer.read(), "image/jpeg")
    except Exception:
        logger.exception("Не удалось прочитать фото списка вопросов")
        await _set(status, ERROR_TEXT)
        return
    if not lines or confidence < 0.5:
        await _set(status, f"Не получилось прочитать фото{': ' + issue if issue else ''}. Переснимите ровнее и крупнее или вставьте текст.")
        return
    try:
        await status.delete()
    except Exception:
        pass
    async with async_session() as session:
        row = await get_exam_list(session, db_user.id, db_user.current_subject)
    previous = load_list(row.questions) if row else []
    # Фото-список дописывается к уже принятому, если он был загружен только что (следующая страница)
    data = await state.get_data()
    base = previous if data.get("photo_session") else []
    await state.update_data(photo_session=True)
    await _save_questions_keep_state(message, state, db_user, parse_questions(base + lines))


async def _save_questions_keep_state(message: Message, state: FSMContext, user: User, questions: list[str]) -> None:
    async with async_session() as session:
        await save_exam_list(session, user.id, user.current_subject, questions)
        await session.commit()
    await message.answer(f"Принял вопросов: {len(questions)}. Есть ещё страница — пришли следующее фото; всё — /exam.", reply_markup=menu_keyboard(len(questions)))


async def _set(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except Exception:
        pass


# ----------------------------------------------------------------- «Спроси меня»


@router.callback_query(F.data == "ex:ask")
async def cb_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    user = await _load_user(callback.from_user)
    async with async_session() as session:
        row = await get_exam_list(session, user.id, user.current_subject)
        questions = load_list(row.questions) if row else []
        if not questions:
            await callback.message.answer("Сначала загрузи список вопросов.", reply_markup=menu_keyboard(0))
            return
        weak = load_weak(row.weak, len(questions))
        index, position = next_index(len(questions), row.position, weak)
        if index is None:
            await update_exam_progress(session, row, 0, [])
            await session.commit()
            await state.clear()
            await callback.message.answer(f"🎉 Весь список пройден ({len(questions)} вопросов), слабых мест не осталось. Хочешь ещё круг — нажми «Спроси меня».", reply_markup=menu_keyboard(len(questions)))
            return
        await update_exam_progress(session, row, position, weak)
        await session.commit()
    await state.set_state(ExamStates.waiting_answer)
    await state.update_data(index=index)
    text = f"{progress_line(index, len(questions), len(weak))}\n\n{questions[index]}\n\nОтвечай текстом или голосом (голос — для подписчиков)."
    await callback.message.answer(text)


async def _evaluate_and_show(message: Message, state: FSMContext, db_user: User, usage_ctx: dict, question: str, index: int, *, audio: bytes | None, text: str | None) -> None:
    status = await message.answer("🧠 Разбираю ответ…")
    try:
        with request_context(user_id=f"telegram:{db_user.id}", channel="telegram", workflow="RECALL_EVALUATION" if audio is None else "ORAL_EVALUATE"):
            if audio is not None:
                result = await evaluate_oral(question, audio, source_type=db_user.current_source_type or SOURCE_TEXTBOOK, subject=db_user.current_subject)
            else:
                result = await evaluate_recall(question, text or "", source_type=db_user.current_source_type or SOURCE_TEXTBOOK, subject=db_user.current_subject)
    except Exception:
        logger.exception("Ошибка при оценке ответа в режиме экзамена")
        await _set(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return
    try:
        await status.delete()
    except Exception:
        pass
    await state.clear()
    if is_weak(result):
        async with async_session() as session:
            row = await get_exam_list(session, db_user.id, db_user.current_subject)
            if row:
                total = len(load_list(row.questions))
                await update_exam_progress(session, row, row.position, load_weak(row.weak, total) + [index])
                await session.commit()
    await _send(message, format_evaluation(result), after_answer_keyboard(index))


@router.message(ExamStates.waiting_answer, F.text & ~F.text.startswith("/"))
async def answer_text(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    data = await state.get_data()
    index = int(data.get("index", 0))
    async with async_session() as session:
        row = await get_exam_list(session, db_user.id, db_user.current_subject)
    questions = load_list(row.questions) if row else []
    if not (0 <= index < len(questions)):
        await state.clear()
        await message.answer("Список изменился — начни заново: /exam.")
        usage_ctx["count"] = False
        return
    await _evaluate_and_show(message, state, db_user, usage_ctx, questions[index], index, audio=None, text=message.text)


@router.message(ExamStates.waiting_answer, F.voice)
async def answer_voice(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    if not can_use_deep(db_user):
        await message.answer(VOICE_PREMIUM_TEXT + " Ответь текстом — я разберу так же.")
        usage_ctx["count"] = False
        return
    data = await state.get_data()
    index = int(data.get("index", 0))
    async with async_session() as session:
        row = await get_exam_list(session, db_user.id, db_user.current_subject)
    questions = load_list(row.questions) if row else []
    if not (0 <= index < len(questions)):
        await state.clear()
        await message.answer("Список изменился — начни заново: /exam.")
        usage_ctx["count"] = False
        return
    try:
        buffer = await message.bot.download(message.voice)
        audio = buffer.read()
    except Exception:
        logger.exception("Не удалось скачать голосовой ответ")
        await message.answer(ERROR_TEXT)
        usage_ctx["count"] = False
        return
    await _evaluate_and_show(message, state, db_user, usage_ctx, questions[index], index, audio=audio, text=None)


@router.message(ExamStates.waiting_answer)
async def answer_wrong_type(message: Message) -> None:
    await message.answer("Ответь текстом или голосовым сообщением. Выйти — /exam.")


@router.callback_query(F.data.startswith("ex:weak:"))
async def cb_weak(callback: CallbackQuery) -> None:
    index = int(callback.data.rsplit(":", 1)[1])
    user = await _load_user(callback.from_user)
    async with async_session() as session:
        row = await get_exam_list(session, user.id, user.current_subject)
        if row:
            total = len(load_list(row.questions))
            await update_exam_progress(session, row, row.position, load_weak(row.weak, total) + [index])
            await session.commit()
    await callback.answer("Верну этот вопрос в конце круга")


@router.callback_query(F.data.startswith("ex:key:"))
async def cb_key(callback: CallbackQuery) -> None:
    """«💡 Верный ответ»: бот отвечает на вопрос списка по учебникам, с источниками (тот же конвейер, что обычные вопросы)."""
    await callback.answer()
    index = int(callback.data.rsplit(":", 1)[1])
    user = await _load_user(callback.from_user)
    async with async_session() as session:
        row = await get_exam_list(session, user.id, user.current_subject)
    questions = load_list(row.questions) if row else []
    if not (0 <= index < len(questions)):
        await callback.message.answer("Не нашёл этот вопрос в списке.")
        return
    await _answer_via_main_pipeline(callback.message, callback.from_user, user, questions[index])


# ----------------------------------------------------------------- «Ответь на вопрос»


@router.callback_query(F.data == "ex:solve")
async def cb_solve(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ExamStates.waiting_solve)
    await callback.message.answer("Напиши номер вопроса из своего списка (например, 7) или сам вопрос — отвечу по учебникам, с источниками.")


@router.message(ExamStates.waiting_solve, F.text & ~F.text.startswith("/"))
async def solve_run(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    await state.clear()
    async with async_session() as session:
        row = await get_exam_list(session, db_user.id, db_user.current_subject)
    questions = load_list(row.questions) if row else []
    picked = question_by_number(questions, message.text)
    if picked is None and message.text.strip().isdigit():
        await message.answer(f"В списке нет вопроса номер {message.text.strip()} (всего {len(questions)}).", reply_markup=menu_keyboard(len(questions)))
        usage_ctx["count"] = False
        return
    question = picked[1] if picked else message.text
    await message.answer(f"Вопрос: {question}")
    await _answer_via_main_pipeline(message, message.from_user, db_user, question, usage_ctx)


async def _answer_via_main_pipeline(message: Message, tg_user, user: User, question: str, usage_ctx: dict | None = None) -> None:
    """Ответ тем же путём, что обычный вопрос: учебники, источники, общие знания с пометкой, PubMed."""
    from bot.handlers.query import answer_question

    # Для нажатия кнопки message — сообщение бота; подставляем автора, чтобы лимит и запись шли на студента
    proxy = message.model_copy(update={"from_user": tg_user})
    await answer_question(proxy, user, usage_ctx if usage_ctx is not None else {"count": False}, question)
    await message.answer("Дальше?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data="ex:ask"), InlineKeyboardButton(text="🎓 Меню", callback_data="ex:menu")]]))


# ----------------------------------------------------------------- «Расскажу тему»


@router.callback_query(F.data == "ex:story")
async def cb_story(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ExamStates.waiting_story_topic)
    await callback.message.answer("Какую тему расскажешь? Напиши название (например, «Воспаление: стадии и исходы»).")


@router.message(ExamStates.waiting_story_topic, F.text & ~F.text.startswith("/"))
async def story_topic(message: Message, state: FSMContext, usage_ctx: dict) -> None:
    usage_ctx["count"] = False
    await state.update_data(topic=message.text.strip()[:200])
    await state.set_state(ExamStates.waiting_story)
    await message.answer("Рассказывай — текстом или голосовым (голос — для подписчиков). Я скажу, что верно, что пропущено и что добавить.")


@router.message(ExamStates.waiting_story, F.text & ~F.text.startswith("/"))
async def story_text(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    await _evaluate_story(message, state, db_user, usage_ctx, audio=None, text=message.text)


@router.message(ExamStates.waiting_story, F.voice)
async def story_voice(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    if not can_use_deep(db_user):
        await message.answer(VOICE_PREMIUM_TEXT + " Расскажи текстом — разберу так же.")
        usage_ctx["count"] = False
        return
    try:
        buffer = await message.bot.download(message.voice)
        audio = buffer.read()
    except Exception:
        logger.exception("Не удалось скачать голосовой рассказ")
        await message.answer(ERROR_TEXT)
        usage_ctx["count"] = False
        return
    await _evaluate_story(message, state, db_user, usage_ctx, audio=audio, text=None)


async def _evaluate_story(message: Message, state: FSMContext, db_user: User, usage_ctx: dict, *, audio: bytes | None, text: str | None) -> None:
    data = await state.get_data()
    topic = data.get("topic", "")
    status = await message.answer("🧠 Разбираю рассказ…")
    try:
        with request_context(user_id=f"telegram:{db_user.id}", channel="telegram", workflow="FREE_RECALL_EVALUATION" if audio is None else "ORAL_EVALUATE"):
            if audio is not None:
                result = await evaluate_oral(topic, audio, source_type=db_user.current_source_type or SOURCE_TEXTBOOK, subject=db_user.current_subject)
            else:
                result = await evaluate_free_recall(topic, text or "", source_type=db_user.current_source_type or SOURCE_TEXTBOOK, subject=db_user.current_subject)
    except Exception:
        logger.exception("Ошибка при оценке рассказа в режиме экзамена")
        await _set(status, ERROR_TEXT)
        usage_ctx["count"] = False
        return
    try:
        await status.delete()
    except Exception:
        pass
    await state.clear()
    await _send(message, format_evaluation(result), story_after_keyboard())


@router.message(ExamStates.waiting_story_topic)
@router.message(ExamStates.waiting_story)
@router.message(ExamStates.waiting_solve)
@router.message(ExamStates.waiting_list)
async def wrong_type(message: Message) -> None:
    await message.answer("Не понял, что это. Пришли текст (или голос там, где можно). Выйти — /exam.")


ORAL_TEXT = (
    "🎙 Устный экзамен. Откроется окно звонка с твоими экзаменами с сайта: выбери вопрос, расскажи ответ голосом — "
    "экзаменатор задаст точные вопросы по тому, что ты пропустил, и в конце разберёт ответ. Результат попадёт в план подготовки на сайте. "
    "Режим работает по оплаченному предмету, Telegram должен быть привязан к аккаунту на сайте (Профиль → Telegram)."
)


@router.callback_query(F.data == "ex:oral")
async def cb_oral(callback: CallbackQuery) -> None:
    from app.voice.call_link import call_url, sign_call_token
    from config import settings

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await session.commit()
    await callback.answer()
    if not can_use_deep(user):
        await callback.message.answer("🎙 Голосовой экзамен — для подписчиков. Текстовый «Экзамен» работает как обычно.")
        return
    if not settings.SLIDES_SITE_URL or not settings.SERVICE_TOKEN:
        await callback.message.answer("Звонок пока не настроен на сервере. Напиши администратору.")
        return
    token = sign_call_token(settings.SERVICE_TOKEN, user.id, user.current_subject)
    await callback.message.answer(
        ORAL_TEXT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎙 Начать устный экзамен", web_app=WebAppInfo(url=call_url(settings.SLIDES_SITE_URL, token, "exam")))]]),
    )


APP_TEXT = (
    "📱 Подготовка в приложении — то же, что на сайте, прямо в Telegram: «Подготовиться к паре» (краткий конспект, схема, карточки, тесты), "
    "«Экзамен» (план по дням, цикл на вопрос: читаем → вспоминаем → перечитываем → вспоминаем) и «Звонок». "
    "Нужен аккаунт на сайте с привязанным Telegram (Профиль → Telegram); экзамен открывается по купленному предмету."
)


@router.callback_query(F.data == "ex:app")
async def cb_app(callback: CallbackQuery) -> None:
    from app.voice.call_link import call_url, enter_url, sign_call_token
    from config import settings

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await session.commit()
    await callback.answer()
    if not settings.SLIDES_SITE_URL or not settings.SERVICE_TOKEN:
        await callback.message.answer("Приложение пока не настроено на сервере. Напиши администратору.")
        return
    base = settings.SLIDES_SITE_URL
    token = sign_call_token(settings.SERVICE_TOKEN, user.id, user.current_subject)

    def button(text: str, url: str) -> list[InlineKeyboardButton]:
        return [InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]

    rows = [
        button("📖 Подготовиться к паре", enter_url(base, token, "prepare")),
        button("🎓 Экзамен: план и цикл", enter_url(base, token, "exams")),
    ]
    if can_use_deep(user):
        rows.append(button("📞 Звонок", call_url(base, token)))
        rows.append(button("🎙 Устный экзамен", call_url(base, token, "exam")))
    await callback.message.answer(APP_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
