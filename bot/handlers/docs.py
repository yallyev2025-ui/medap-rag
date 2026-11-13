"""Документы и конспекты из видео в Telegram (13.11.2026, владелец: «чтобы работало через Telegram»).

Что умеет студент:
  • прислать ссылку на видео YouTube (или /video <ссылка>) → бот достаёт субтитры, пишет конспект и показывает его в чате; под ним кнопки
    «📄 PDF» и «📝 Word» (файл собирает сайт: фирменные цвета, логотип MedAP). Только Premium — как на сайте;
  • написать «сделай конспект по теме …» (или /doc <тема>) → полный конспект по разделам из учебников, с рисунками и источниками;
  • написать «собери документ из этого ответа» → документ из последнего ответа бота (бесплатно, без новых обращений к модели).
Тяжёлую работу делает сайт через дверь `/api/docs-bot` (подписанный запрос); бот только принимает просьбу, показывает ход и отдаёт файл.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.integrations.slides_site import docs_site
from bot.docs_intent import find_youtube_url, parse_doc_request
from bot.last_answer import last_answer
from bot.panel import is_panel_text
from db.crud import can_use_deep, monthly_budget_rub_for, month_spend_rub
from db.models import User
from db.session import async_session

logger = logging.getLogger(__name__)
router = Router()

POLL_SECONDS = 4.0
VIDEO_WAIT_SECONDS = 20 * 60
TOPIC_WAIT_SECONDS = 8 * 60
TELEGRAM_LIMIT = 3900

VIDEO_PREMIUM_TEXT = "🎬 Конспекты из видео — в MedAP Premium. Подписчикам достаточно прислать ссылку на видео YouTube."
NOT_READY_TEXT = "Документы в Telegram пока не подключены к сайту: нужны SLIDES_SITE_URL и SLIDES_SITE_SECRET. Напишите владельцу."
NO_PREVIOUS_TEXT = "Не из чего собирать документ: задайте вопрос по теме — и после ответа напишите «сделай документ из этого». Или сразу: «сделай конспект по теме …»."
LIMIT_TEXT = "Месячный лимит стоимости ответов исчерпан — конспект по теме собрать не получится. Лимит обновится 1 числа."

HELP_TEXT = (
    "📄 Документы и конспекты\n\n"
    "• Пришлите ссылку на видео YouTube — сделаю конспект (Premium).\n"
    "• «Сделай конспект по теме ишемическая болезнь сердца» — полный конспект по учебникам, с рисунками.\n"
    "• «Собери документ из этого ответа» — оформлю последний ответ в PDF или Word."
)


def _keyboard(doc_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="📄 PDF", callback_data=f"dd:p:{doc_id}"),
            InlineKeyboardButton(text="📝 Word", callback_data=f"dd:w:{doc_id}"),
        ]]
    )


def _chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет простой текст на сообщения Telegram по абзацам."""
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            parts.append(cur.strip())
            cur = ""
        while len(para) > limit:
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(para[:cut].strip())
            para = para[cut:].strip()
        cur += ("\n\n" if cur else "") + para
    if cur.strip():
        parts.append(cur.strip())
    return parts


async def _edit(status: Message | None, text: str) -> None:
    if status is None:
        return
    try:
        await status.edit_text(text[:4000])
    except Exception:  # то же сообщение или удалено — ход не важнее результата
        pass


async def _send_result(message: Message, preview: str, doc_id: str, head: str = "") -> None:
    """Прочитать можно прямо в чате; кнопки — под последней частью."""
    parts = _chunks(f"{head}\n\n{preview}".strip()) if preview else [head or "Готово."]
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await message.answer(part, reply_markup=_keyboard(doc_id) if last else None)


async def _poll(status: Message, payload: dict, wait_seconds: float, title: str) -> dict | None:
    """Ждёт готовности, показывая шаги; None — вышло время, словарь — итог (готово или ошибка)."""
    waited = 0.0
    last = ""
    while waited < wait_seconds:
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        r = await docs_site.call("status", **payload)
        if not r.get("ok"):
            return r
        if r.get("state") != "pending":
            return r
        steps = r.get("steps") or []
        text = f"{title}\n⏳ {steps[-1]}" if steps else title
        if text != last:
            last = text
            await _edit(status, text)
    return None


async def _budget_rub(user: User) -> tuple[float | None, bool]:
    """(остаток месячного лимита, ₽ — None у безлимитных; исчерпан ли лимит)."""
    limit = monthly_budget_rub_for(user)
    if limit is None:
        return None, False
    async with async_session() as session:
        spent = await month_spend_rub(session, user.id)
    left = max(0.0, limit - spent)
    return left, left <= 0.0


# ——— видео ———


async def run_video(message: Message, user: User, url: str, usage_ctx: dict | None = None) -> None:
    if not docs_site.configured:
        await message.answer(NOT_READY_TEXT)
        return
    if not can_use_deep(user):
        await message.answer(VIDEO_PREMIUM_TEXT)
        if usage_ctx is not None:
            usage_ctx["count"] = False
        return
    tg = message.from_user.id
    status = await message.answer("🎬 Принимаю ссылку, достаю субтитры видео…")
    r = await docs_site.call("video_start", telegram_id=tg, url=url, premium=True)
    if not r.get("ok"):
        await _edit(status, f"⚠️ {r.get('error') or 'Не получилось начать конспект'}")
        return
    done = await _poll(status, {"telegram_id": tg, "noteId": r["noteId"]}, VIDEO_WAIT_SECONDS, "🎬 Делаю конспект видео")
    if done is None:
        await _edit(status, "⏳ Конспект ещё пишется — это долгое видео. Пришлите ту же ссылку через несколько минут: готовое откроется сразу.")
        return
    if not done.get("ok") or done.get("state") != "done":
        await _edit(status, f"⚠️ {done.get('error') or 'Не получилось сделать конспект'}")
        return
    await _edit(status, f"✅ Конспект готов: {done.get('title') or 'видео'}")
    warn = "\n\n⚠️ " + "; ".join(done["warnings"]) if done.get("warnings") else ""
    await _send_result(message, (done.get("preview") or "") + warn, done["docId"])


@router.message(Command("video"))
async def cmd_video(message: Message, command: CommandObject, db_user: User, usage_ctx: dict) -> None:
    url = find_youtube_url(command.args)
    if not url:
        await message.answer("Пришлите ссылку на видео YouTube после команды: /video https://youtu.be/… — или просто ссылкой.")
        usage_ctx["count"] = False
        return
    await run_video(message, db_user, url, usage_ctx)


def _is_video_message(message: Message) -> bool:
    text = message.text or ""
    return bool(text) and not text.startswith("/") and not is_panel_text(text) and len(text) <= 300 and find_youtube_url(text) is not None


@router.message(F.text, _is_video_message)
async def on_video_link(message: Message, db_user: User, usage_ctx: dict) -> None:
    await run_video(message, db_user, find_youtube_url(message.text) or "", usage_ctx)


# ——— документы ———


async def run_doc(message: Message, user: User, topic: str | None, fmt: str, usage_ctx: dict | None = None) -> None:
    if not docs_site.configured:
        await message.answer(NOT_READY_TEXT)
        return
    tg = message.from_user.id
    if topic:
        left, exhausted = await _budget_rub(user)
        if exhausted:
            await message.answer(LIMIT_TEXT)
            if usage_ctx is not None:
                usage_ctx["count"] = False
            return
        status = await message.answer(f"📄 Собираю конспект «{topic}»: план, учебники по каждому разделу, рисунки…")
        r = await docs_site.call("doc_topic", telegram_id=tg, topic=topic, **({"budgetRub": left} if left is not None else {}))
        if not r.get("ok"):
            await _edit(status, f"⚠️ {r.get('error') or 'Не получилось начать конспект'}")
            return
        done = await _poll(status, {"telegram_id": tg, "jobId": r["jobId"]}, TOPIC_WAIT_SECONDS, f"📄 Конспект «{topic}»")
        if done is None:
            await _edit(status, "⏳ Конспект собирается дольше обычного. Напишите просьбу ещё раз чуть позже.")
            return
        if not done.get("ok") or done.get("state") != "done":
            await _edit(status, f"⚠️ {done.get('error') or 'Не получилось собрать конспект'}")
            return
        if done.get("empty"):
            await _edit(status, "В учебниках по этой теме ничего подходящего не нашёл. Попробуйте назвать тему иначе.")
            return
        found = len(done.get("found") or [])
        missing = done.get("missing") or []
        head = f"✅ Конспект «{done.get('title')}» готов: разделов — {found}" + (f", рисунков — {done.get('images')}" if done.get("images") else "") + (f". В учебниках не нашёл: {', '.join(missing)}" if missing else "") + "."
        await _edit(status, head)
        await _send_result(message, done.get("preview") or "", done["docId"])
        return

    text = last_answer(message.chat.id)
    if not text:
        await message.answer(NO_PREVIOUS_TEXT)
        if usage_ctx is not None:
            usage_ctx["count"] = False
        return
    status = await message.answer("📄 Оформляю последний ответ в документ…")
    left, exhausted = await _budget_rub(user)
    r = await docs_site.call("doc_text", telegram_id=tg, text=text, allowModel=not exhausted)
    if not r.get("ok"):
        await _edit(status, f"⚠️ {r.get('error') or 'Не получилось собрать документ'}")
        return
    await _edit(status, f"✅ Документ «{r.get('title')}» готов.")
    await message.answer("Выберите формат файла:", reply_markup=_keyboard(r["docId"]))


@router.message(Command("doc"))
async def cmd_doc(message: Message, command: CommandObject, db_user: User, usage_ctx: dict) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(HELP_TEXT)
        usage_ctx["count"] = False
        return
    await run_doc(message, db_user, arg if len(arg) >= 3 else None, "both", usage_ctx)


def _is_doc_request(message: Message) -> bool:
    text = message.text or ""
    return bool(text) and not text.startswith("/") and not is_panel_text(text) and parse_doc_request(text) is not None


@router.message(F.text, _is_doc_request)
async def on_doc_request(message: Message, db_user: User, usage_ctx: dict) -> None:
    req = parse_doc_request(message.text)
    assert req is not None
    await run_doc(message, db_user, req.topic, req.fmt, usage_ctx)


# ——— файл по кнопке ———


@router.callback_query(F.data.regexp(r"^dd:[pw]:[a-z0-9]{8,40}$"))
async def on_download(callback: CallbackQuery) -> None:
    _, kind, doc_id = (callback.data or "").split(":", 2)
    await callback.answer("Собираю файл…")
    r = await docs_site.call("doc_file", telegram_id=callback.from_user.id, docId=doc_id, format="pdf" if kind == "p" else "docx")
    if not r.get("ok"):
        await callback.message.answer(f"⚠️ {r.get('error') or 'Не получилось собрать файл'}")
        return
    data = base64.b64decode(r["base64"])
    name = re.sub(r"[\\/:*?\"<>|]", " ", r.get("fileName") or ("konspekt.pdf" if kind == "p" else "konspekt.docx"))
    await callback.message.answer_document(BufferedInputFile(data, filename=name), caption="MedAP · medapstudy.ru")
