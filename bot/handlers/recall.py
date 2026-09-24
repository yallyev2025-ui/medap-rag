"""Самопроверка ответа в Telegram (батч 8): /selfcheck — короткий линейный визард,
та же механика, что у /addbook (aiogram FSM, MemoryStorage по умолчанию — потеря
состояния при рестарте бота не критична для 2-3-шагового диалога).

Реюзает уже готовые app/workflows/evaluate.py::evaluate_recall/evaluate_free_recall
(текст) и evaluate_oral (голос) — то, что раньше было доступно только через API
для сайта, теперь и в Telegram: "должно работать как обычный ИИ" (запрос пользователя)."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.observability.context import request_context
from app.workflows.evaluate import EvaluationResult, evaluate_free_recall, evaluate_oral, evaluate_recall
from bot.formatting import split_for_telegram, to_telegram_html
from db.models import User

logger = logging.getLogger(__name__)

router = Router()

ERROR_TEXT = "⚠️ Не получилось проверить ответ, попробуй ещё раз через минуту."

MODE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Конкретный вопрос", callback_data="recall_mode:recall"),
            InlineKeyboardButton(text="Пересказ всей темы", callback_data="recall_mode:free"),
        ]
    ]
)


class RecallStates(StatesGroup):
    waiting_topic = State()
    waiting_mode = State()
    waiting_answer = State()


def _format_evaluation(result: EvaluationResult) -> str:
    parts: list[str] = []
    if result.transcript:
        parts.append(f"🎤 Распознано: {result.transcript}")
    if result.overall_feedback:
        parts.append(result.overall_feedback)
    sections = [
        ("✅ Покрыто верно", result.covered),
        ("❌ Пропущено", result.missing),
        ("⚠️ Частично верно", result.partially_correct),
        ("❗ Неверно", result.incorrect),
        ("🔗 Ошибки причинно-следственных связей", result.causal_errors),
        ("📖 Ошибки терминологии", result.terminology_errors),
        ("⛔ Противоречия материалу", result.contradictions),
    ]
    for label, items in sections:
        if items:
            parts.append(f"{label}:\n" + "\n".join(f"— {item}" for item in items))
    if result.repair:
        parts.append(f"💡 Адресная коррекция: {result.repair}")
    return "\n\n".join(parts) if parts else "Оценка пуста — попробуй переформулировать ответ."


async def _send_evaluation(message: Message, result: EvaluationResult) -> None:
    text = _format_evaluation(result)
    for part in split_for_telegram(to_telegram_html(text)):
        await message.answer(part)


@router.message(Command("selfcheck"))
async def cmd_selfcheck(message: Message, state: FSMContext) -> None:
    await state.set_state(RecallStates.waiting_topic)
    await message.answer("Напиши вопрос или тему, по которой хочешь проверить свой ответ.")


@router.message(RecallStates.waiting_topic, F.text)
async def recall_got_topic(message: Message, state: FSMContext) -> None:
    await state.update_data(topic=message.text)
    await state.set_state(RecallStates.waiting_mode)
    await message.answer(
        "Это ответ на конкретный вопрос или пересказ всей темы своими словами?",
        reply_markup=MODE_KEYBOARD,
    )


@router.message(RecallStates.waiting_topic)
async def recall_topic_wrong_type(message: Message) -> None:
    await message.answer("Напиши вопрос или тему текстом.")


@router.callback_query(RecallStates.waiting_mode, F.data.startswith("recall_mode:"))
async def recall_choose_mode(callback: CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.split(":", 1)[1]
    await callback.answer()
    try:
        await callback.message.edit_reply_markup()
    except Exception:
        pass
    await state.update_data(mode=mode)
    await state.set_state(RecallStates.waiting_answer)
    await callback.message.answer("Теперь напиши или запиши голосом свой ответ.")


@router.message(RecallStates.waiting_answer, F.text)
async def recall_answer_text(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    data = await state.get_data()
    topic = data.get("topic", "")
    mode = data.get("mode", "recall")
    await state.clear()

    status = await message.answer("🧠 Проверяю ответ…")
    try:
        with request_context(
            user_id=f"telegram:{db_user.id}", channel="telegram",
            workflow="FREE_RECALL_EVALUATION" if mode == "free" else "RECALL_EVALUATION",
        ):
            if mode == "free":
                result = await evaluate_free_recall(
                    topic, message.text, source_type=db_user.current_source_type, subject=db_user.current_subject
                )
            else:
                result = await evaluate_recall(
                    topic, message.text, source_type=db_user.current_source_type, subject=db_user.current_subject
                )
    except Exception:
        logger.exception("Ошибка при оценке текстового ответа")
        try:
            await status.edit_text(ERROR_TEXT)
        except Exception:
            pass
        usage_ctx["count"] = False
        return

    try:
        await status.delete()
    except Exception:
        pass
    await _send_evaluation(message, result)


@router.message(RecallStates.waiting_answer, F.voice)
async def recall_answer_voice(message: Message, state: FSMContext, db_user: User, usage_ctx: dict) -> None:
    data = await state.get_data()
    topic = data.get("topic", "")
    await state.clear()

    status = await message.answer("🎤 Распознаю голосовой ответ…")
    try:
        buffer = await message.bot.download(message.voice)
        audio_bytes = buffer.read()
        with request_context(user_id=f"telegram:{db_user.id}", channel="telegram", workflow="ORAL_EVALUATE"):
            result = await evaluate_oral(
                topic, audio_bytes, source_type=db_user.current_source_type, subject=db_user.current_subject
            )
    except Exception:
        logger.exception("Ошибка при оценке устного ответа")
        try:
            await status.edit_text(ERROR_TEXT)
        except Exception:
            pass
        usage_ctx["count"] = False
        return

    try:
        await status.delete()
    except Exception:
        pass
    await _send_evaluation(message, result)


@router.message(RecallStates.waiting_answer)
async def recall_answer_wrong_type(message: Message) -> None:
    await message.answer("Ответь текстом или голосовым сообщением.")
