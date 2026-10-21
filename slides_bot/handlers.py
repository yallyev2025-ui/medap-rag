"""Сценарии бота презентаций: вход по почте, создание, черновик, правки, файлы, баланс, раздел владельца.

Всё, что касается денег, аккаунтов и самих презентаций, делает сайт (`app/integrations/slides_site.py`) — бот только спрашивает
человека и показывает ответы. Поэтому правила оплаты, стоп-лист тем и чужие презентации защищены в одном месте — на сайте.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.integrations import slides_site
from config import settings
from slides_bot import texts as T

logger = logging.getLogger(__name__)
router = Router()

POLL_SECONDS = 6
POLL_LIMIT = 150  # ~15 минут; дальше человек сам нажмёт «Мои презентации»


class Login(StatesGroup):
    email = State()
    consent = State()
    code = State()


class Create(StatesGroup):
    topic = State()
    wishes = State()


class Edit(StatesGroup):
    phrase = State()


class Topup(StatesGroup):
    amount = State()


def site():
    """Клиент сайта берётся из модуля при каждом вызове — тесты подменяют `slides_site.site`."""
    return slides_site.site


async def _site_url(tg_id: int, run_id: str) -> str | None:
    """Ссылка на презентацию на сайте. В боте человек вошёл, а на сайте сессии нет, поэтому берётся одноразовая ссылка входа
    (действует 5 минут, один раз); не вышло — обычная ссылка (сайт попросит войти)."""
    base = settings.SLIDES_SITE_URL.rstrip("/")
    if not base:
        return None
    try:
        r = await site().call("open_link", tg_id, runId=run_id)
        if r.get("ok") and r.get("url"):
            return str(r["url"])
    except Exception:
        logger.warning("Одноразовая ссылка не получена", exc_info=True)
    return f"{base}/slides/{run_id}"


def _is_owner(tg_id: int) -> bool:
    return tg_id in settings.ADMIN_IDS


async def _owner_confirmed(tg_id: int) -> bool:
    """Раздел «Владелец» видит только тот, у кого и Telegram-id в списке владельцев, и аккаунт на сайте с ролью владельца или поддержки.
    Обычному человеку не показываем ни кнопку, ни слова про «команду» и «владельца»."""
    if not _is_owner(tg_id):
        return False
    me = await site().call("me", tg_id)
    return bool(me.get("ok") and me.get("owner"))


async def _menu(target: Message, tg_id: int, text: str = "Что дальше?") -> None:
    await target.answer(text, reply_markup=T.main_menu(await _owner_confirmed(tg_id)))


async def _ensure_login(message: Message, state: FSMContext, tg_id: int) -> dict[str, Any] | None:
    """Профиль вошедшего или запрос почты. Возвращает None, если человека нужно сначала впустить."""
    me = await site().call("me", tg_id)
    if me.get("ok"):
        return me
    if me.get("needLogin"):
        await state.set_state(Login.email)
        await message.answer(T.WELCOME)
        await message.answer(T.ASK_EMAIL)
        return None
    await message.answer(str(me.get("error") or T.NO_SITE))
    return None


# ---------- старт и вход ----------

@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    tg = message.from_user.id
    me = await _ensure_login(message, state, tg)
    if me is None:
        return
    arg = (command.args or "").strip()
    if arg.startswith("paid_"):
        await _after_payment(message, state, bot, tg, arg[5:])
        return
    if arg == "paid":
        await message.answer(f"Оплата получена. Баланс: {me.get('balance', '—')}.")
    await _menu(message, tg, f"Здравствуйте! Баланс: {me.get('balance', '—')}." + (" Бесплатная презентация ещё доступна." if me.get("freeAvailable") else ""))


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await _ensure_login(message, state, message.from_user.id):
        await _menu(message, message.from_user.id)


@router.message(Login.email, F.text)
async def got_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if "@" not in email or " " in email or len(email) > 200 or "." not in email.split("@")[-1]:
        await message.answer("Проверьте адрес почты и пришлите его ещё раз.")
        return
    await state.update_data(email=email)
    await state.set_state(Login.consent)
    base = settings.SLIDES_SITE_URL.rstrip("/")
    links = [[("Политика конфиденциальности", f"{base}/legal/privacy"), ("Согласие на обработку данных", f"{base}/legal/consent")]] if base else []
    await message.answer(T.ASK_CONSENT.format(email=email), reply_markup=T.kb([[("✅ Согласен(на), прислать код", "c:ok")], [("✖ Другая почта", "c:no")]], links))


@router.callback_query(Login.consent, F.data == "c:ok")
async def cb_consent_ok(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    r = await site().call("login_request", call.from_user.id, email=data.get("email", ""), consent=True)
    if not r.get("ok"):
        await state.set_state(Login.email)
        await call.message.answer(str(r.get("error") or "Не получилось отправить код") + "\nПришлите почту ещё раз.")
        return
    await state.set_state(Login.code)
    await call.message.answer(T.ASK_CODE)


@router.callback_query(Login.consent, F.data == "c:no")
async def cb_consent_no(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Login.email)
    await call.message.answer("Хорошо, код не отправлен. Пришлите другую почту.")


@router.message(Login.code, F.text)
async def got_code(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    r = await site().call("login_verify", message.from_user.id, email=data.get("email", ""), code=(message.text or "").strip(), username=message.from_user.username or "")
    if not r.get("ok"):
        await message.answer(str(r.get("error") or "Код не подошёл") + "\nПришлите код ещё раз или нажмите /start, чтобы ввести другую почту.")
        return
    await state.clear()
    await message.answer(T.LOGGED_IN)
    await _menu(message, message.from_user.id)


# ---------- меню ----------

@router.callback_query(F.data == "m:home")
async def cb_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _menu(call.message, call.from_user.id)


@router.callback_query(F.data == "m:prices")
async def cb_prices(call: CallbackQuery) -> None:
    await call.answer()
    r = await site().call("prices")
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or T.NO_SITE))
        return
    lines = ["Цена зависит от размера презентации:"] + [f"• {t['label']} — {t['price']}" for t in r["tiers"]]
    lines.append(f"\nПервая презентация бесплатная: до {r['free']['maxSlides']} слайдов, {r['free']['aiEdits']} правки ИИ.")
    lines.append("Размер выбирается при оплате. Если понадобится больше слайдов — доплатите только разницу.")
    await call.message.answer("\n".join(lines), reply_markup=T.kb([[("⬅ В меню", "m:home")]]))


@router.callback_query(F.data == "m:out")
async def cb_out(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await site().call("unlink", call.from_user.id)
    await state.clear()
    await call.message.answer("Вы вышли. Чтобы войти снова, нажмите /start.")


# ---------- создание ----------

@router.callback_query(F.data == "m:new")
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    if await _ensure_login(call.message, state, call.from_user.id) is None:
        return
    await state.set_state(Create.topic)
    await call.message.answer(T.ASK_TOPIC)


@router.message(Create.topic, F.text)
async def got_topic(message: Message, state: FSMContext) -> None:
    topic = (message.text or "").strip()
    if len(topic) < 3:
        await message.answer(T.TOPIC_SHORT)
        return
    await state.update_data(topic=topic[:300])
    await message.answer("Какой вид презентации?", reply_markup=T.kinds_kb())


@router.callback_query(F.data.startswith("k:"))
async def got_kind(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    if not data.get("topic"):
        await call.message.answer("Начнём заново: нажмите «Создать презентацию».", reply_markup=T.main_menu(await _owner_confirmed(call.from_user.id)))
        return
    await state.update_data(kind=call.data[2:])
    prices, me = await asyncio.gather(site().call("prices"), site().call("me", call.from_user.id))
    if not prices.get("ok") or not me.get("ok"):
        await call.message.answer(str((prices if not prices.get("ok") else me).get("error") or T.NO_SITE))
        return
    free = bool(me.get("freeAvailable")) and not me.get("team")
    await call.message.answer(
        f"Сколько слайдов? Баланс: {me.get('balance', '—')}." + (" Оплата не нужна." if me.get("team") else ""),
        reply_markup=T.sizes_kb(prices["tiers"], free, int(prices["free"]["maxSlides"])),
    )


@router.callback_query(F.data.startswith("s:"))
async def got_size(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.update_data(total=int(call.data[2:]))
    r = await site().call("styles", call.from_user.id)
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or T.NO_SITE))
        return
    await call.message.answer("Выберите оформление:", reply_markup=T.styles_kb(r["templates"]))


@router.callback_query(F.data.startswith("st:"))
async def got_style(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    sid = call.data[3:]
    await state.update_data(styleId="" if sid == "auto" else sid)
    await state.set_state(Create.wishes)
    await call.message.answer(T.ASK_WISHES, reply_markup=T.kb([[("Без пожеланий", "w:none")]]))


@router.callback_query(Create.wishes, F.data == "w:none")
async def no_wishes(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    await _create(call.message, state, bot, call.from_user.id, "")


@router.message(Create.wishes, F.text)
async def got_wishes(message: Message, state: FSMContext, bot: Bot) -> None:
    await _create(message, state, bot, message.from_user.id, (message.text or "").strip()[:800])


async def _create(message: Message, state: FSMContext, bot: Bot, tg: int, wishes: str) -> None:
    data = await state.get_data()
    await state.clear()
    if not data.get("topic"):
        await message.answer("Начнём заново: нажмите /menu.")
        return
    r = await site().call("create", tg, topic=data["topic"], kind=data.get("kind", "talk"), total=data.get("total", 10), styleId=data.get("styleId", ""), wishes=wishes)
    if r.get("ok"):
        await message.answer(T.WORKING)
        _watch(bot, message.chat.id, tg, str(r["runId"]))
        return
    if r.get("payUrl"):
        need = T.money(int(r.get("shortfallKopecks", 0)))
        await message.answer(f"Для этой презентации не хватает {need}. Оплатите — и я сразу начну писать. Задание сохранено.", reply_markup=T.kb([], [[(f"💳 Оплатить {need}", str(r["payUrl"]))]]))
        return
    if r.get("closed"):
        await message.answer(str(r["error"]) + "\nСледите за новостями — как только откроем, бот сам напишет.")
        return
    await message.answer(str(r.get("error") or "Не получилось создать презентацию"))


async def _after_payment(message: Message, state: FSMContext, bot: Bot, tg: int, intent_id: str) -> None:
    r = await site().call("resume", tg, intentId=intent_id)
    if r.get("ok"):
        await message.answer("Оплата получена. " + T.WORKING)
        _watch(bot, message.chat.id, tg, str(r["runId"]))
    elif r.get("waiting"):
        await message.answer("Оплата ещё не дошла — это обычно занимает до минуты. Нажмите «Проверить оплату».", reply_markup=T.kb([[("🔄 Проверить оплату", f"p:{intent_id}")]]))
    else:
        await message.answer(str(r.get("error") or "Не получилось продолжить"))
        await _menu(message, tg)


@router.callback_query(F.data.startswith("p:"))
async def cb_check_pay(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    await _after_payment(call.message, state, bot, call.from_user.id, call.data[2:])


# ---------- ожидание и черновик ----------

_tasks: set[asyncio.Task] = set()


def _watch(bot: Bot, chat_id: int, tg: int, run_id: str) -> None:
    t = asyncio.create_task(_poll(bot, chat_id, tg, run_id))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _poll(bot: Bot, chat_id: int, tg: int, run_id: str) -> None:
    for _ in range(POLL_LIMIT):
        await asyncio.sleep(POLL_SECONDS)
        r = await site().call("status", tg, runId=run_id)
        if not r.get("ok"):
            if r.get("network"):
                continue
            await bot.send_message(chat_id, str(r.get("error") or "Не получилось узнать состояние"))
            return
        if r.get("phase") == "failed":
            await bot.send_message(chat_id, "Презентация не получилась" + (f": {r['error']}" if r.get("error") else "") + ".\nЕсли вы платили, деньги вернулись на баланс.", reply_markup=T.main_menu(await _owner_confirmed(tg)))
            return
        if r.get("phase") in ("draft", "approved"):
            await _send_draft(bot, chat_id, tg, run_id, preview=1)
            return


async def _send_preview(bot: Bot, chat_id: int, tg: int, run_id: str, index: int) -> None:
    """Картинка слайда перед текстом черновика. Сбой не страшен: человек всё равно получит текст и кнопки."""
    try:
        r = await site().call("preview", tg, runId=run_id, index=index)
        if r.get("ok"):
            await bot.send_photo(chat_id, BufferedInputFile(base64.b64decode(str(r["base64"])), filename="slide.png"), caption=f"Так выглядит слайд {index + 1}")
    except Exception:
        logger.warning("Превью слайда не отправилось", exc_info=True)


async def _send_draft(bot: Bot, chat_id: int, tg: int, run_id: str, edit_note: str = "", preview: int | None = None) -> None:
    if preview is not None:
        await _send_preview(bot, chat_id, tg, run_id, preview)
    d = await site().call("draft", tg, runId=run_id)
    if not d.get("ok"):
        await bot.send_message(chat_id, str(d.get("error") or "Не получилось открыть черновик"))
        return
    text = (edit_note + "\n\n" if edit_note else "") + T.draft_text(d)
    if d.get("approved"):
        await bot.send_message(chat_id, text + "\n\nПрезентация утверждена. Выберите файл:", reply_markup=T.files_kb(run_id, await _site_url(tg, run_id)))
    else:
        await bot.send_message(chat_id, text, reply_markup=T.draft_kb(d, run_id, await _site_url(tg, run_id)))


@router.callback_query(F.data.startswith("d:"))
async def cb_draft(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    await state.clear()
    await _send_draft(bot, call.message.chat.id, call.from_user.id, call.data[2:])


@router.callback_query(F.data == "m:runs")
async def cb_runs(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    if await _ensure_login(call.message, state, call.from_user.id) is None:
        return
    r = await site().call("runs", call.from_user.id)
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or T.NO_SITE))
        return
    if not r["runs"]:
        await call.message.answer("Презентаций пока нет.", reply_markup=T.main_menu(await _owner_confirmed(call.from_user.id)))
        return
    await call.message.answer("Ваши презентации:", reply_markup=T.runs_kb(r["runs"]))


# ---------- правки ----------

@router.callback_query(F.data.startswith("e:"))
async def cb_edit(call: CallbackQuery) -> None:
    await call.answer()
    run_id = call.data[2:]
    d = await site().call("draft", call.from_user.id, runId=run_id)
    if not d.get("ok"):
        await call.message.answer(str(d.get("error")))
        return
    await call.message.answer(f"Какой слайд поправить? (ИИ-правок осталось: {d.get('editsLeft', 0)})", reply_markup=T.slides_pick_kb(d, run_id))


@router.callback_query(F.data.startswith("es:"))
async def cb_edit_slide(call: CallbackQuery) -> None:
    await call.answer()
    _, run_id, idx = call.data.split(":")
    await call.message.answer(f"Слайд {int(idx) + 1}. Что сделать?", reply_markup=T.edit_actions_kb(run_id, int(idx)))


@router.callback_query(F.data.startswith("ea:"))
async def cb_edit_action(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    _, run_id, idx, kind = call.data.split(":")
    if kind == "own":
        await state.set_state(Edit.phrase)
        await state.update_data(runId=run_id, index=int(idx))
        await call.message.answer("Напишите своими словами, что изменить на этом слайде.")
        return
    await _do_edit(call.message, bot, call.from_user.id, run_id, int(idx), kind, "")


@router.message(Edit.phrase, F.text)
async def got_phrase(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await _do_edit(message, bot, message.from_user.id, data["runId"], int(data["index"]), "general", (message.text or "").strip()[:600])


async def _do_edit(message: Message, bot: Bot, tg: int, run_id: str, index: int, kind: str, phrase: str) -> None:
    await message.answer("Правлю слайд, это до минуты…")
    r = await site().call("edit", tg, runId=run_id, index=index, kind=kind, **({"phrase": phrase} if phrase else {}))
    if not r.get("ok"):
        await message.answer(str(r.get("error") or "Не получилось поправить"))
        await _send_draft(bot, message.chat.id, tg, run_id)
        return
    await _send_draft(bot, message.chat.id, tg, run_id, edit_note=f"Слайд {int(r.get('index', index)) + 1} исправлен." + (f" {r['note']}" if r.get("note") else ""), preview=int(r.get("index", index)))


@router.callback_query(F.data.startswith("a:"))
async def cb_approve(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    run_id = call.data[2:]
    r = await site().call("approve", call.from_user.id, runId=run_id)
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or "Не получилось утвердить"))
        return
    await call.message.answer("Утверждено. Какой файл прислать?", reply_markup=T.files_kb(run_id, await _site_url(call.from_user.id, run_id)))


@router.callback_query(F.data.startswith("f:"))
async def cb_file(call: CallbackQuery) -> None:
    await call.answer("Собираю файл…")
    _, run_id, kind = call.data.split(":")
    r = await site().call("file", call.from_user.id, runId=run_id, kind=kind)
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or "Не получилось собрать файл"))
        return
    data = base64.b64decode(str(r["base64"]))
    await call.message.answer_document(BufferedInputFile(data, filename=str(r["fileName"])), caption="Готово. Поправить текст и оформление можно на сайте.")


# ---------- баланс ----------

@router.callback_query(F.data == "m:bal")
async def cb_balance(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    me = await _ensure_login(call.message, state, call.from_user.id)
    if me is None:
        return
    await call.message.answer(f"Баланс: {me.get('balance', '—')}. Пополните — и платите за презентации с него.", reply_markup=T.topup_kb())


@router.callback_query(F.data.startswith("t:"))
async def cb_topup(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    arg = call.data[2:]
    if arg == "other":
        await state.set_state(Topup.amount)
        await call.message.answer("Напишите сумму в рублях (от 50 до 10 000).")
        return
    await _topup(call.message, call.from_user.id, int(arg))


@router.message(Topup.amount, F.text)
async def got_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").replace(" ", "").replace("₽", "").replace(",", ".")
    try:
        kopecks = round(float(raw) * 100)
    except ValueError:
        await message.answer("Напишите число, например 250.")
        return
    await state.clear()
    await _topup(message, message.from_user.id, kopecks)


async def _topup(message: Message, tg: int, kopecks: int) -> None:
    r = await site().call("topup", tg, kopecks=kopecks)
    if not r.get("ok"):
        await message.answer(str(r.get("error") or "Не получилось создать платёж"))
        return
    await message.answer(f"Пополнение на {T.money(kopecks)}. После оплаты вернитесь в бот — баланс обновится сам.", reply_markup=T.kb([], [[("💳 Оплатить", str(r["payUrl"]))]]))


# ---------- владелец ----------

@router.callback_query(F.data == "m:owner")
async def cb_owner(call: CallbackQuery) -> None:
    await call.answer()
    if not await _owner_confirmed(call.from_user.id):
        return
    r = await site().call("stats", call.from_user.id, days=30)
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or "Нет данных"))
        return
    await call.message.answer(T.owner_text(r, 30), reply_markup=T.kb([[("⬅ В меню", "m:home")]]))


async def notify_owners(bot: Bot, text: str) -> None:
    """Сообщить владельцам (ADMIN_IDS_RAW) о сбое. Недоставка не должна ронять бота."""
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning("Не удалось сообщить владельцу %s", admin_id, exc_info=True)
