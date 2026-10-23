"""Сценарии бота презентаций: вход по почте, создание, черновик, правки, файлы, баланс, раздел владельца.

Всё, что касается денег, аккаунтов и самих презентаций, делает сайт (`app/integrations/slides_site.py`) — бот только спрашивает
человека и показывает ответы. Поэтому правила оплаты, стоп-лист тем и чужие презентации защищены в одном месте — на сайте.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from app.integrations import slides_site
from config import settings
from slides_bot import texts as T
from slides_bot import version as V

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
    setup = State()  # экран «Настроить»: всё выбирается кнопками, снимки присылаются в чат
    wishes = State()
    colors = State()
    title_value = State()
    plan = State()  # свой план или готовый текст по слайдам: приходит несколькими сообщениями
    audience = State()


SETUP_STATES = (Create.setup, Create.wishes, Create.colors, Create.title_value, Create.plan, Create.audience)
MAX_PLAN_CHARS = 40_000
MAX_PHOTO_BYTES = 8 * 1024 * 1024


class Edit(StatesGroup):
    phrase = State()


class Review(StatesGroup):
    text = State()  # человек хочет добавить пару слов к своей оценке


class Topup(StatesGroup):
    amount = State()


def site():
    """Клиент сайта берётся из модуля при каждом вызове — тесты подменяют `slides_site.site`."""
    return slides_site.site


async def _site_link(tg_id: int, *, run_id: str = "", path: str = "") -> tuple[str | None, bool]:
    """Ссылка на сайт: (адрес, одноразовая_ли). В боте человек вошёл, а на сайте сессии нет, поэтому берётся одноразовая ссылка входа
    (живёт 15 минут, срабатывает один раз) и создаётся ТОЛЬКО в момент нажатия кнопки. Не вышло — обычная ссылка (сайт попросит войти), и человеку об этом говорится."""
    base = settings.SLIDES_SITE_URL.rstrip("/")
    if not base:
        return None, False
    try:
        r = await site().call("open_link", tg_id, **({"runId": run_id} if run_id else {"path": path or "/slides"}))
        if r.get("ok") and r.get("url"):
            return str(r["url"]), True
    except Exception:
        logger.warning("Одноразовая ссылка не получена", exc_info=True)
    return (f"{base}/slides/{run_id}" if run_id else f"{base}{path or '/slides'}"), False


async def _send_site_link(msg: Message, tg_id: int, *, run_id: str = "", path: str = "", back: str = "m:home") -> None:
    url, once = await _site_link(tg_id, run_id=run_id, path=path)
    if not url:
        await msg.answer(T.NO_SITE)
        return
    text = T.SITE_LINK_ONCE if once else T.SITE_LINK_PLAIN
    await msg.answer(text, reply_markup=T.kb([[("⬅ Назад", back)]], [[("🖥 Открыть на сайте", url)]]))


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


async def version_report() -> str:
    """Версии бота и сайта человеческим языком (команда /version и сообщение владельцам при запуске)."""
    return V.compare(await site().call("version"))


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    await message.answer(await version_report())


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
    await state.update_data(total=int(call.data[2:]), o=T.default_opts(), wz=0)
    await _wz_show(call.message, state)


# ---------- «Настроить»: те же выборы, что в мастере на сайте ----------

_catalog: dict[int, tuple[float, dict[str, Any]]] = {}
CATALOG_TTL = 600.0


async def _options(tg: int) -> dict[str, Any] | None:
    """Каталог выборов с сайта (шаблоны, шрифты, цвета, предметы, поля титула); на несколько минут запоминается, чтобы не дёргать сайт на каждое нажатие."""
    now = time.monotonic()
    hit = _catalog.get(tg)
    if hit and now - hit[0] < CATALOG_TTL:
        return hit[1]
    r = await site().call("options", tg)
    if not r.get("ok"):
        return None
    _catalog[tg] = (now, r)
    return r


async def _options_or_say(msg: Message, tg: int) -> dict[str, Any] | None:
    """Каталог выборов или понятное объяснение, почему его нет: «сайт старый» (не выложен вместе с ботом) отличается от «связь не настроена»."""
    cat = await _options(tg)
    if cat:
        return cat
    r = await site().call("options", tg)
    err = str(r.get("error") or "")
    await msg.answer(T.SITE_OLD if "Неизвестное действие" in err else (err or T.NO_SITE))
    return None


async def _screen(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Показать экран на месте прошлого (без потока сообщений); если Telegram не даёт править — новым сообщением."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


async def _open_setup(message: Message, state: FSMContext, edit: bool = False, final: bool = False) -> None:
    await state.set_state(Create.setup)
    d = await state.get_data()
    o = await _opts(state)
    text = T.final_text(d) if final else T.setup_text(d)
    if edit:
        await _screen(message, text, T.setup_kb(o=o))
    else:
        await message.answer(text, reply_markup=T.setup_kb(o=o))


# ---------- мастер: вопросы по одному ----------

async def _wz_show(message: Message, state: FSMContext, edit: bool = False) -> None:
    """Показать текущий вопрос мастера. Шаги, не нужные в выбранном режиме, пропускаются; после последнего — итоговый экран."""
    d = await state.get_data()
    o = await _opts(state)
    steps = T.wz_steps(o)
    i = d.get("wz")
    if i is None:
        await _open_setup(message, state, edit=edit)
        return
    i = int(i)
    if i >= len(steps):
        await state.update_data(wz=None)
        await _open_setup(message, state, edit=edit, final=True)
        return
    step = steps[i]
    # какие сообщения человека ждём: на вопросах «для кого» и «пожелания» — текст, на остальных — нажатия и файлы
    await state.set_state({"aud": Create.audience, "wish": Create.wishes}.get(step, Create.setup))
    await state.update_data(wz=i)
    text, markup = T.wz_text(step, i, len(steps), o), T.wz_kb(step, o, first=i == 0)
    if edit:
        await _screen(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _wz_move(message: Message, state: FSMContext, delta: int, edit: bool) -> None:
    d = await state.get_data()
    if d.get("wz") is None:
        await _open_setup(message, state, edit=edit)
        return
    await state.update_data(wz=max(0, int(d["wz"]) + delta))
    await _wz_show(message, state, edit=edit)


async def _return(message: Message, state: FSMContext, edit: bool = False) -> None:
    """Подэкран закончен («Готово», ответ текстом): в мастере — следующий вопрос, в меню «Изменить» — итоговый экран."""
    d = await state.get_data()
    if d.get("wz") is None:
        await _open_setup(message, state, edit=edit)
    else:
        await _wz_move(message, state, 1, edit)


async def _opts(state: FSMContext) -> dict[str, Any]:
    d = await state.get_data()
    o = d.get("o")
    if not isinstance(o, dict):
        o = T.default_opts()
        await state.update_data(o=o)
    return o


async def _save(state: FSMContext, o: dict[str, Any]) -> None:
    await state.update_data(o=o)


@router.callback_query(F.data.startswith("o:"))
async def cb_opt(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await call.answer()
    tg = call.from_user.id
    d = await state.get_data()
    if not d.get("topic"):
        await call.message.answer("Начнём заново: нажмите /menu.")
        return
    o = await _opts(state)
    parts = call.data.split(":")  # o:<действие>[:аргументы]
    act = parts[1]
    arg = parts[2] if len(parts) > 2 else ""
    msg = call.message

    if act == "home":
        await _return(msg, state, edit=True)
    elif act == "wz":
        if arg == "b":
            await _wz_move(msg, state, -1, True)
        else:
            if _current_step(d, o) == "up" and not await _check_uploads(msg, state, tg, final=False):
                await _wz_show(msg, state)  # файл не разобрался: остаёмся на вопросе про файлы
                return
            await _wz_move(msg, state, 1, True)
    elif act == "cancel":
        await state.clear()
        await msg.answer("Отменил. Выберите, что дальше.", reply_markup=T.main_menu(await _owner_confirmed(tg)))
    elif act == "upx":
        o["uploadIds"] = []
        o["uploadNames"] = {}
        await _save(state, o)
        await _wz_show(msg, state, edit=True)
    elif act == "go":
        await _create(msg, state, bot, tg)
    elif act == "site":
        await _send_site_link(msg, tg, path="/slides/new", back="o:home")
    # --- с чего начинаем, для кого, свои материалы
    elif act == "mode":
        await state.set_state(Create.setup)
        await _screen(msg, T.MODE_HELP, T.mode_kb(o))
    elif act == "m":
        if arg in ("topic", "plan", "text"):
            o["mode"] = arg
            if arg == "topic":
                o["planText"] = ""
            if arg == "text":
                o.update(medical=False, textbooks=False, discipline="", disciplineName="", pmids=[], uploadIds=[], wishes="")
            await _save(state, o)
        if arg in ("plan", "text"):
            await state.set_state(Create.plan)
            await msg.answer(T.PLAN_ASK[arg], reply_markup=T.plan_kb(bool(o["planText"])))
        else:
            await _return(msg, state, edit=True)
    elif act == "pdone":
        if o["mode"] != "topic" and not o["planText"].strip():
            await msg.answer("Текст пока не получен — пришлите его сообщением или выберите «Только тема».")
            return
        await _return(msg, state)
    elif act == "pclear":
        o["planText"] = ""
        await _save(state, o)
        await msg.answer("Очистил. Пришлите план или текст заново.", reply_markup=T.plan_kb(False))
    elif act == "aud":
        await state.set_state(Create.audience)
        await msg.answer(T.AUD_ASK, reply_markup=T.kb([[("⬅ К настройкам", "o:home")]]))
    elif act == "up":
        cat = await _options_or_say(msg, tg)
        if not cat:
            return
        items = cat.get("uploads", [])
        o["uploadNames"] = {x["id"]: x["fileName"] for x in items}
        await _save(state, o)
        await _screen(msg, T.UPLOADS_HELP if items else T.UPLOADS_NONE, T.uploads_kb(items, o["uploadIds"]))
    elif act == "upt":
        cat = await _options_or_say(msg, tg)
        items = (cat or {}).get("uploads", [])
        if arg.isdigit() and int(arg) < len(items):
            uid = items[int(arg)]["id"]
            if uid in o["uploadIds"]:
                o["uploadIds"].remove(uid)
            elif len(o["uploadIds"]) >= 5:
                await msg.answer("Можно выбрать не больше 5 материалов.")
                return
            else:
                o["uploadIds"].append(uid)
            await _save(state, o)
        await _screen(msg, T.UPLOADS_HELP, T.uploads_kb(items, o["uploadIds"]))
    # --- оформление (необязательно)
    elif act == "des":
        await _screen(msg, T.DESIGN_HELP, T.design_kb())
    elif act == "num2":
        o["numbers"] = not o["numbers"]
        await _save(state, o)
        await _screen(msg, T.DESIGN_HELP + f"\nНомера слайдов в углу: {'да' if o['numbers'] else 'нет'}.", T.design_kb())
    # --- источники
    elif act == "src":
        await state.set_state(Create.setup)
        await _screen(msg, T.SOURCES_HELP, T.sources_kb(o))
    elif act == "med":
        o["medical"] = not o["medical"]
        await _save(state, o)
        await _screen(msg, T.SOURCES_HELP, T.sources_kb(o))
    elif act == "tb":
        if o["textbooks"]:
            o.update(textbooks=False, discipline="", disciplineName="")
            await _save(state, o)
            await _screen(msg, T.SOURCES_HELP, T.sources_kb(o))
        else:
            await _show_disciplines(msg, tg, 0)
    elif act == "dl":
        await _show_disciplines(msg, tg, int(arg or 0))
    elif act == "d":
        cat = await _options(tg)
        items = (cat or {}).get("disciplines", [])
        if arg == "none":
            o.update(textbooks=True, discipline="", disciplineName="")
        elif arg.isdigit() and int(arg) < len(items):
            o.update(textbooks=True, discipline=items[int(arg)]["code"], disciplineName=items[int(arg)]["title"])
        await _save(state, o)
        await _screen(msg, T.SOURCES_HELP, T.sources_kb(o))
    elif act in ("pm", "pmm", "p", "pmx"):
        await _pubmed(msg, state, tg, act, arg)
    # --- оформление, шрифты, цвета
    elif act == "sty":
        cat = await _options_or_say(msg, tg)
        if not cat:
            return
        await _screen(msg, "Выберите оформление. Смена бесплатна и без ИИ — потом можно поменять на сайте.", T.styles_page_kb(cat["templates"], int(arg or 0)))
    elif act == "st":
        if arg in ("auto", "plain"):
            o.update(styleId="" if arg == "auto" else "plain", styleName="")
        else:
            cat = await _options(tg)
            tpl = (cat or {}).get("templates", [])
            if arg.isdigit() and int(arg) < len(tpl):
                o.update(styleId=tpl[int(arg)]["id"], styleName=tpl[int(arg)]["name"])
        await _save(state, o)
        await _open_setup(msg, state, edit=True)
    elif act == "fnt":
        cat = await _options_or_say(msg, tg)
        if not cat:
            return
        which = arg if arg in ("h", "b") else "h"
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        title = "Шрифт заголовков:" if which == "h" else f"Шрифт текста (заголовки: {(o.get('fontsTmp') or '—')}):"
        await _screen(msg, f"{title}\nШрифты работают парой: выберите оба — или ни одного (тогда возьмутся шрифты оформления).", T.fonts_page_kb(cat["fonts"], page, which))
    elif act in ("fh", "fb"):
        cat = await _options(tg)
        fonts = (cat or {}).get("fonts", [])
        if not (arg.isdigit() and int(arg) < len(fonts)):
            return
        if act == "fh":
            o["fontsTmp"] = fonts[int(arg)]
            await _save(state, o)
            await _screen(msg, f"Заголовки: {fonts[int(arg)]}. Теперь шрифт текста:", T.fonts_page_kb(fonts, 0, "b"))
        else:
            o["fonts"] = {"heading": o.pop("fontsTmp", None) or fonts[int(arg)], "body": fonts[int(arg)]}
            await _save(state, o)
            await _open_setup(msg, state, edit=True)
    elif act == "fx":
        o["fonts"] = None
        o.pop("fontsTmp", None)
        await _save(state, o)
        await _open_setup(msg, state, edit=True)
    elif act == "col":
        cat = await _options_or_say(msg, tg)
        if not cat:
            return
        await _screen(msg, "Выберите набор цветов или пришлите свои. Из трёх цветов достроится вся палитра с проверкой читаемости.", T.colors_kb(cat["colorPresets"], bool(o["colors"])))
    elif act == "cp":
        cat = await _options(tg)
        presets = (cat or {}).get("colorPresets", [])
        if arg.isdigit() and int(arg) < len(presets):
            o["colors"] = list(presets[int(arg)]["colors"])
            await _save(state, o)
        await _open_setup(msg, state, edit=True)
    elif act == "cc":
        await state.set_state(Create.colors)
        await msg.answer(T.COLORS_HELP, reply_markup=T.kb([[("⬅ К настройкам", "o:home")]]))
    elif act == "cx":
        o["colors"] = []
        await _save(state, o)
        await _open_setup(msg, state, edit=True)
    # --- текст
    elif act == "txt":
        await _screen(msg, T.TEXT_HELP, T.text_kb(o))
    elif act == "n":
        o["notes"] = arg if arg in ("none", "brief", "detailed") else o["notes"]
        await _save(state, o)
        await _screen(msg, T.TEXT_HELP, T.text_kb(o))
    elif act == "dd":
        o["detail"] = arg if arg in ("brief", "detailed") else o["detail"]
        await _save(state, o)
        await _screen(msg, T.TEXT_HELP, T.text_kb(o))
    elif act == "num":
        o["numbers"] = not o["numbers"]
        await _save(state, o)
        await _screen(msg, T.TEXT_HELP, T.text_kb(o))
    # --- титул
    elif act == "ttl":
        await _show_title(msg, state, tg)
    elif act == "tf":
        cat = await _options(tg)
        fields = ((cat or {}).get("titleFields") or {}).get(d.get("kind", "talk"), [])
        if arg.isdigit() and int(arg) < len(fields):
            await state.set_state(Create.title_value)
            await state.update_data(titleKey=fields[int(arg)][0])
            await msg.answer(f"{fields[int(arg)][1]}: пришлите значение (знак «-» очищает поле).")
    elif act == "tr":
        o["title"] = {}
        await _save(state, o)
        await _show_title(msg, state, tg)
    # --- фото и пожелания
    elif act == "ph":
        await _screen(msg, T.PHOTOS_HELP + (f"\n\nЗагружено: {len(o['photoIds'])}" if o["photoIds"] else ""), T.photos_kb(len(o["photoIds"])))
    elif act == "phx":
        o["photoIds"] = []
        await _save(state, o)
        await _screen(msg, "Фото убраны. " + T.PHOTOS_HELP, T.photos_kb(0))
    elif act == "wsh":
        await state.set_state(Create.wishes)
        await msg.answer(T.ASK_WISHES, reply_markup=T.kb([[("Без пожеланий", "o:wno")], [("⬅ К настройкам", "o:home")]]))
    elif act == "wno":
        o["wishes"] = ""
        await _save(state, o)
        await _return(msg, state)


async def _show_disciplines(msg: Message, tg: int, page: int) -> None:
    cat = await _options_or_say(msg, tg)
    if not cat:
        return
    items = cat.get("disciplines", [])
    if not items:
        await _screen(msg, "Предметов для учебников пока нет.", T.kb([[("⬅ Назад", "o:src")]]))
        return
    await _screen(msg, "Предмет для учебников — по нему бот найдёт план и текст:", T.disciplines_kb(items, page))


async def _show_title(msg: Message, state: FSMContext, tg: int) -> None:
    cat = await _options_or_say(msg, tg)
    if not cat:
        return
    d = await state.get_data()
    o = await _opts(state)
    fields = (cat.get("titleFields") or {}).get(d.get("kind", "talk"), [])
    await _screen(msg, T.title_text(fields, o["title"], cat.get("profile", {})), T.title_kb(fields))


async def _pubmed(msg: Message, state: FSMContext, tg: int, act: str, arg: str) -> None:
    """Выбор статей PubMed: 10 лучших по ценности, «ещё 10» (если нет учебников), выбор не больше числа слайдов — как на сайте."""
    d = await state.get_data()
    o = await _opts(state)
    limit = T.max_pick(int(d.get("total", 10)))
    page = list(d.get("pmList") or [])
    if act == "pmx":
        o["pmids"] = []
        await _save(state, o)
    elif act == "p":
        if arg.isdigit() and int(arg) < len(page):
            pmid = page[int(arg)]["pmid"]
            if pmid in o["pmids"]:
                o["pmids"].remove(pmid)
            elif len(o["pmids"]) >= limit:
                await msg.answer(f"Выбрать можно не больше {limit} — по числу слайдов. Снимите лишнюю статью.")
                return
            else:
                o["pmids"].append(pmid)
            await _save(state, o)
    elif act == "pmm" or not page:
        offset = int(o["pmOffset"]) + 10 if act == "pmm" else 0
        r = await site().call("articles", tg, topic=d.get("topic", ""), offset=offset)
        if not r.get("ok"):
            await msg.answer(str(r.get("error") or "Не получилось подобрать статьи"))
            return
        page = list(r.get("articles", []))
        o["pmOffset"] = offset
        await _save(state, o)
        await state.update_data(pmList=page)
    more = not o.get("textbooks") and int(o["pmOffset"]) < 20
    markup = T.articles_kb(page, o["pmids"], more)
    if act in ("p", "pmx"):
        # отметка статьи — меняются только кнопки (✅ у номера), список не присылается заново
        try:
            await msg.edit_reply_markup(reply_markup=markup)
        except Exception:
            await msg.answer(f"Выбрано статей: {len(o['pmids'])} из {limit}.", reply_markup=markup)
        return
    chunks = T.articles_chunks(page, o["pmids"], limit)
    for part in chunks[:-1]:
        await msg.answer(part)
    await msg.answer(chunks[-1], reply_markup=markup)


@router.message(Create.wishes, F.text)
async def got_wishes(message: Message, state: FSMContext) -> None:
    o = await _opts(state)
    o["wishes"] = (message.text or "").strip()[:3000]
    await _save(state, o)
    await _return(message, state)


@router.message(Create.plan, F.text)
async def got_plan_text(message: Message, state: FSMContext) -> None:
    o = await _opts(state)
    chunk = (message.text or "").strip()
    if not chunk:
        return
    joined = (o["planText"] + "\n" + chunk).strip() if o["planText"] else chunk
    if len(joined) > MAX_PLAN_CHARS:
        await message.answer(f"Текст длиннее {MAX_PLAN_CHARS} знаков — сократите или разбейте презентацию на две.")
        return
    o["planText"] = joined
    await _save(state, o)
    await message.answer(f"Принято: {len(joined)} знаков. Можно прислать ещё или нажать «Готово».", reply_markup=T.plan_kb(True))


@router.message(Create.audience, F.text)
async def got_audience(message: Message, state: FSMContext) -> None:
    o = await _opts(state)
    v = (message.text or "").strip()
    o["audience"] = "" if v == "-" else v[:200]
    await _save(state, o)
    await _return(message, state)


@router.message(Create.colors, F.text)
async def got_colors(message: Message, state: FSMContext) -> None:
    codes = re.findall(r"#?[0-9a-fA-F]{6}\b", message.text or "")
    colors = ["#" + c.lstrip("#").lower() for c in codes][:3]
    if not colors:
        await message.answer("Не нашёл цветов. Пришлите коды вида #2f6f73 (до трёх через пробел).")
        return
    o = await _opts(state)
    o["colors"] = colors
    await _save(state, o)
    await _open_setup(message, state)


@router.message(Create.title_value, F.text)
async def got_title_value(message: Message, state: FSMContext) -> None:
    d = await state.get_data()
    key = d.get("titleKey")
    o = await _opts(state)
    if key:
        v = (message.text or "").strip()
        o["title"][key] = "" if v == "-" else v[:160]
        await _save(state, o)
    await state.set_state(Create.setup)
    await _show_title(message, state, message.from_user.id)


FILE_EXT = {".pdf", ".docx", ".pptx", ".txt", ".png", ".jpg", ".jpeg", ".webp"}
MIME_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
MAX_FILE_BYTES = T.FILE_MB * 1024 * 1024  # больше Telegram всё равно не отдаёт боту


def _current_step(d: dict[str, Any], o: dict[str, Any]) -> str | None:
    i = d.get("wz")
    steps = T.wz_steps(o)
    return steps[int(i)] if i is not None and int(i) < len(steps) else None


async def _check_uploads(msg: Message, state: FSMContext, tg: int, final: bool) -> bool:
    """Разобраны ли присланные файлы. Не разобравшиеся убираются с объяснением; при создании («final») ещё разбирающиеся ждём."""
    o = await _opts(state)
    ids = list(o.get("uploadIds") or [])
    if not ids:
        return True
    r = await site().call("upload_status", tg, uploadIds=ids)
    if not r.get("ok"):
        return True  # сайт старый или не ответил: создание само отфильтрует неготовое
    pending, bad = [], []
    for it in r.get("items", []):
        if it.get("status") == "failed":
            bad.append(it)
        elif it.get("status") != "ready":
            pending.append(it)
    if bad:
        o["uploadIds"] = [x for x in ids if x not in {b["id"] for b in bad}]
        await _save(state, o)
        for b in bad:
            await msg.answer(f"Файл «{b.get('fileName', 'без имени')}» не разобрался: {b.get('failureReason') or 'не удалось прочитать'}. Пришлите другой.")
        return False
    if pending and final:
        await msg.answer("Файлы ещё разбираются (обычно до минуты). Подождите немного и нажмите «Создать» ещё раз.")
        return False
    return True


async def _got_file(message: Message, state: FSMContext, bot: Bot, tg: int) -> None:
    """Файл для презентации: уходит на сайт, там разбирается, попадает в «Мои материалы» (можно удалить) и в векторную базу бота."""
    o = await _opts(state)
    if len(o["uploadIds"]) >= T.FILES_MAX:
        await message.answer(f"Уже {T.FILES_MAX} файлов — больше не бывает. Уберите лишние кнопкой «Убрать все файлы».")
        return
    doc = message.document
    name = getattr(doc, "file_name", None) or "файл"
    if os.path.splitext(name.lower())[1] not in FILE_EXT and MIME_EXT.get(str(getattr(doc, "mime_type", "") or "")):
        name += MIME_EXT[str(doc.mime_type)]  # Telegram иногда не присылает имя: тип берём из mime
    if os.path.splitext(name.lower())[1] not in FILE_EXT:
        await message.answer("Такой формат не разбираю. Подойдут PDF, Word (.docx), PowerPoint (.pptx), текст (.txt) и фото страниц (JPEG, PNG, WebP).")
        return
    if (doc.file_size or 0) > MAX_FILE_BYTES:
        await message.answer(f"Файл больше {T.FILE_MB} МБ — пришлите поменьше или разделите на части.")
        return
    buf = await bot.download(doc.file_id)
    data = buf.read() if buf is not None else b""
    if not data:
        await message.answer("Не получилось получить файл из Telegram. Пришлите ещё раз.")
        return
    r = await site().call("upload_file", tg, base64=base64.b64encode(data).decode(), fileName=name)
    if not r.get("ok"):
        await message.answer(str(r.get("error") or "Файл не принят"))
        return
    o["uploadIds"].append(str(r["uploadId"]))
    o["uploadNames"][str(r["uploadId"])] = name
    await _save(state, o)
    d = await state.get_data()
    nxt = [[("➡ Далее", "o:wz:n")]] if d.get("wz") is not None else [[("✅ Готово", "o:home")]]
    await message.answer(f"📎 Принял «{name[:60]}» ({len(o['uploadIds'])} из {T.FILES_MAX}). Разбираю — это до минуты. Можно прислать ещё или нажать «Далее».", reply_markup=T.kb(nxt))


@router.message(F.photo | F.document, StateFilter(*SETUP_STATES))
async def got_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    """Снимок из чата — как загрузка в мастере на сайте: ждёт создания и после черновика встаёт на подходящий слайд. Документ (PDF, Word, …) —
    материал, по которому делается презентация; фото-файл на шаге «Свои файлы» тоже материал (скан страницы), на других шагах — снимок."""
    o = await _opts(state)
    d = await state.get_data()
    is_image_doc = bool(message.document and str(message.document.mime_type or "").startswith("image/"))
    if message.document and (not is_image_doc or _current_step(d, o) == "up"):
        await _got_file(message, state, bot, message.from_user.id)
        return
    if len(o["photoIds"]) >= T.MAX_PHOTOS:
        await message.answer(f"Уже {T.MAX_PHOTOS} снимков — больше не бывает. Уберите лишние в «Фото».")
        return
    if message.photo:
        file_id, size = message.photo[-1].file_id, message.photo[-1].file_size or 0
    elif is_image_doc:
        file_id, size = message.document.file_id, message.document.file_size or 0
    else:
        await message.answer("Это не снимок. Пришлите JPEG, PNG или WebP.")
        return
    if size > MAX_PHOTO_BYTES:
        await message.answer("Снимок больше 8 МБ — пришлите поменьше.")
        return
    buf = await bot.download(file_id)
    data = buf.read() if buf is not None else b""
    if not data:
        await message.answer("Не получилось получить снимок из Telegram. Пришлите ещё раз.")
        return
    r = await site().call("photo_upload", message.from_user.id, base64=base64.b64encode(data).decode())
    if not r.get("ok"):
        await message.answer(str(r.get("error") or "Снимок не принят"))
        return
    o["photoIds"].append(str(r["photoId"]))
    await _save(state, o)
    nxt = [[("➡ Далее", "o:wz:n")]] if d.get("wz") is not None else [[("✅ Готово", "o:home")]]
    await message.answer(f"🖼 Снимок принят ({len(o['photoIds'])}). Можно прислать ещё или нажать «Далее».", reply_markup=T.kb(nxt))


@router.callback_query(F.data.startswith("st:") | F.data.startswith("w:"))
async def cb_stale(call: CallbackQuery) -> None:
    await call.answer("Эта кнопка устарела — нажмите /menu", show_alert=True)


def _create_payload(d: dict[str, Any]) -> dict[str, Any]:
    """Что отправить сайту: сначала обязательное, потом только то, что человек выбрал (остальное подберёт сайт)."""
    o = d.get("o") or T.default_opts()
    p: dict[str, Any] = {"topic": d["topic"], "kind": d.get("kind", "talk"), "total": d.get("total", 10), "styleId": o.get("styleId", ""), "wishes": o.get("wishes", "")}
    if o.get("mode") in ("plan", "text"):
        p["mode"] = o["mode"]
        p["text"] = o.get("planText", "")
    if o.get("audience"):
        p["audience"] = o["audience"]
    if o.get("uploadIds"):
        p["uploadIds"] = list(o["uploadIds"])
    if o.get("medical"):
        p["medical"] = True
    if o.get("textbooks"):
        p["textbooks"] = True
        if o.get("discipline"):
            p["discipline"] = o["discipline"]
    if o.get("pmids"):
        p["pmids"] = list(o["pmids"])
    if o.get("notes"):
        p["notes"] = o["notes"]
    if o.get("detail") == "brief":
        p["detail"] = "brief"
    if o.get("fonts"):
        p["fonts"] = o["fonts"]
    if o.get("colors"):
        p["colors"] = list(o["colors"])
    if o.get("numbers"):
        p["numbers"] = True
    if o.get("title"):
        p["titleFields"] = dict(o["title"])
    if o.get("photoIds"):
        p["photoIds"] = list(o["photoIds"])
    return p


async def _create(message: Message, state: FSMContext, bot: Bot, tg: int) -> None:
    data = await state.get_data()
    if not data.get("topic"):
        await state.clear()
        await message.answer("Начнём заново: нажмите /menu.")
        return
    o = data.get("o") or {}
    if o.get("mode") in ("plan", "text") and not str(o.get("planText", "")).strip():
        await message.answer("Выбран режим со своим планом или текстом, но текст не получен. Откройте «С чего начинаем» и пришлите его или выберите «Только тема».")
        await state.set_state(Create.setup)
        return
    if o.get("uploadIds"):
        if not await _check_uploads(message, state, tg, final=True):
            await state.set_state(Create.setup)
            return
        data = await state.get_data()  # неразобравшиеся файлы могли быть убраны
    await state.clear()
    r = await site().call("create", tg, **_create_payload(data))
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
            _watch_auto_photos(bot, chat_id, tg, run_id)
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
        await bot.send_message(chat_id, text + "\n\nПрезентация утверждена. Выберите файл:", reply_markup=T.files_kb(run_id))
    else:
        await bot.send_message(chat_id, text, reply_markup=T.draft_kb(d, run_id))


# ---------- подбор фото ИИ ----------

PHOTO_POLL_SECONDS = 4
PHOTO_POLL_LIMIT = 75  # ~5 минут


@router.callback_query(F.data.startswith("ph:"))
async def cb_photos(call: CallbackQuery, bot: Bot) -> None:
    """«Подобрать фото ИИ»: сайт делает подбор фоном, бот ждёт и показывает предложения с кнопками «Поставить» и «Убрать»."""
    await call.answer()
    run_id = call.data[3:]
    tg = call.from_user.id
    r = await site().call("suggest_photos", tg, runId=run_id)
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or "Не получилось начать подбор"))
        return
    await call.message.answer(T.PHOTO_WAIT)
    t = asyncio.create_task(_poll_photos(bot, call.message.chat.id, tg, run_id, str(r["jobId"])))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _poll_photos(bot: Bot, chat_id: int, tg: int, run_id: str, job_id: str) -> None:
    for _ in range(PHOTO_POLL_LIMIT):
        await asyncio.sleep(PHOTO_POLL_SECONDS)
        r = await site().call("suggest_status", tg, jobId=job_id)
        if not r.get("ok"):
            if r.get("network"):
                continue
            await bot.send_message(chat_id, str(r.get("error") or "Не получилось подобрать фото"))
            return
        if r.get("state") == "pending":
            continue
        notes = list(r.get("notes") or [])
        added = int(r.get("added") or 0)
        if not notes and not added:
            await bot.send_message(chat_id, T.PHOTO_NONE)
            return
        await bot.send_message(chat_id, T.photo_notes_text(notes, added))
        if added:
            await _send_suggestions(bot, chat_id, tg, run_id)
        return
    await bot.send_message(chat_id, "Подбор занимает слишком долго. Нажмите «Подобрать фото ИИ» ещё раз через минуту.")


async def _send_suggestions(bot: Bot, chat_id: int, tg: int, run_id: str) -> int:
    """Показать предложенные картинки (каждая с подписью и кнопками «Поставить»/«Убрать») и общие кнопки: поставить все, не нужны, предложить другие."""
    r = await site().call("suggestions", tg, runId=run_id)
    sent = 0
    for it in (r.get("items") or [])[:8]:
        pic = await site().call("photo_get", tg, photoId=it["id"])
        if not pic.get("ok"):
            continue
        await bot.send_photo(chat_id, BufferedInputFile(base64.b64decode(str(pic["base64"])), filename="photo.jpg"), caption=T.suggestion_caption(it), reply_markup=T.suggestion_kb(run_id, str(it["id"])))
        sent += 1
    if sent:
        await bot.send_message(chat_id, T.PHOTO_OFFER.format(n=sent), reply_markup=T.photo_group_kb(run_id, (r.get("left") or {}).get("runs", 0)))
    return sent


def _watch_auto_photos(bot: Bot, chat_id: int, tg: int, run_id: str) -> None:
    t = asyncio.create_task(_poll_auto_photos(bot, chat_id, tg, run_id))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _poll_auto_photos(bot: Bot, chat_id: int, tg: int, run_id: str) -> None:
    """Сайт сам подбирает картинки сразу после черновика (тот же автоподбор, что в редакторе). Бот ждёт его и показывает предложения на утверждение.
    Сайт старый, подбор отключён или ничего не идёт — молча ничего не делаем: человек всё равно может нажать «Подобрать фото ИИ»."""
    started = False
    for _ in range(PHOTO_POLL_LIMIT):
        r = await site().call("photo_auto", tg, runId=run_id)
        if not r.get("ok"):
            if r.get("network"):
                await asyncio.sleep(PHOTO_POLL_SECONDS)
                continue
            return
        if r.get("state") == "running":
            if not started:
                started = True
                await bot.send_message(chat_id, T.PHOTO_AUTO_WAIT)
            await asyncio.sleep(PHOTO_POLL_SECONDS)
            continue
        if r.get("state") == "none":
            return
        await _send_suggestions(bot, chat_id, tg, run_id)
        return


@router.callback_query(F.data.startswith("pa:"))
async def cb_photo_accept(call: CallbackQuery, bot: Bot) -> None:
    await call.answer()
    _, run_id, photo_id = call.data.split(":")
    tg = call.from_user.id
    r = await site().call("photo_accept", tg, runId=run_id, photoId=photo_id)
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or "Не получилось поставить фото"))
        return
    idx = int(r.get("slideIndex", 0))
    await call.message.answer(f"Поставил на слайд {idx + 1}.")
    await _send_preview(bot, call.message.chat.id, tg, run_id, idx)


@router.callback_query(F.data.startswith("pall:"))
async def cb_photo_accept_all(call: CallbackQuery, bot: Bot) -> None:
    """«Поставить все»: каждая предложенная картинка встаёт на свой слайд с подписью автора."""
    await call.answer()
    run_id = call.data[5:]
    tg = call.from_user.id
    items = (await site().call("suggestions", tg, runId=run_id)).get("items") or []
    placed: list[int] = []
    for it in items:
        r = await site().call("photo_accept", tg, runId=run_id, photoId=it["id"])
        if r.get("ok"):
            placed.append(int(r.get("slideIndex", 0)))
    if not placed:
        await call.message.answer("Нечего ставить: предложений уже нет.")
        return
    await call.message.answer(T.PHOTO_PLACED.format(n=len(placed)), reply_markup=T.kb([[("🖥 Открыть на сайте", f"u:{run_id}")]]))
    await _send_preview(bot, call.message.chat.id, tg, run_id, placed[0])


@router.callback_query(F.data.startswith("pnone:"))
async def cb_photo_dismiss_all(call: CallbackQuery) -> None:
    await call.answer()
    run_id = call.data[6:]
    tg = call.from_user.id
    items = (await site().call("suggestions", tg, runId=run_id)).get("items") or []
    for it in items:
        await site().call("photo_dismiss", tg, runId=run_id, photoId=it["id"])
    await call.message.answer("Убрал предложения. Свои снимки можно добавить на сайте кнопкой «+ Фото» на нужном слайде.", reply_markup=T.kb([[("🔁 Предложить другие", f"ph:{run_id}")], [("🖥 Открыть на сайте", f"u:{run_id}")]]))


@router.callback_query(F.data.startswith("pd:"))
async def cb_photo_dismiss(call: CallbackQuery) -> None:
    await call.answer()
    _, run_id, photo_id = call.data.split(":")
    r = await site().call("photo_dismiss", call.from_user.id, runId=run_id, photoId=photo_id)
    await call.message.answer("Убрал." if r.get("ok") else str(r.get("error") or "Не получилось убрать"))


@router.callback_query(F.data.startswith("u:"))
async def cb_open_site(call: CallbackQuery) -> None:
    await call.answer()
    await _send_site_link(call.message, call.from_user.id, run_id=call.data[2:], back=f"d:{call.data[2:]}")


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
    await call.message.answer("Утверждено. Какой файл прислать?", reply_markup=T.files_kb(run_id))


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
    await _ask_review(call.message, call.from_user.id, run_id)


async def _ask_review(msg: Message, tg: int, run_id: str) -> None:
    """После скачивания просим оценить: звёзды БЕЗ выбранного значения; сайт сам не даёт спросить дважды и считает отказы."""
    st = await site().call("review_state", tg, runId=run_id)
    if st.get("ok") and st.get("ask"):
        await msg.answer(T.REVIEW_ASK, reply_markup=T.review_kb(run_id))


@router.callback_query(F.data.startswith("rv:"))
async def cb_review(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    _, run_id, n = call.data.split(":")
    if n == "skip":
        await site().call("review_skip", call.from_user.id)
        await call.message.answer("Хорошо, не буду отвлекать.")
        return
    if n == "none":
        await state.clear()
        await call.message.answer("Спасибо! Оценка учтена.")
        return
    if not n.isdigit():
        return
    r = await site().call("review_save", call.from_user.id, runId=run_id, rating=int(n))
    if not r.get("ok"):
        await call.message.answer(str(r.get("error") or "Не получилось сохранить оценку"))
        return
    await state.set_state(Review.text)
    await state.update_data(review_run=run_id, review_rating=int(n))
    await call.message.answer(T.REVIEW_THANKS.format(n=n), reply_markup=T.kb([[("Без отзыва", "rv:" + run_id + ":none")]]))


@router.message(Review.text, F.text)
async def got_review_text(message: Message, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    r = await site().call("review_save", message.from_user.id, runId=d.get("review_run", ""), rating=int(d.get("review_rating", 0)), text=(message.text or "")[:800])
    await message.answer("Спасибо! Отзыв появится на главной сайта после проверки." if r.get("ok") else str(r.get("error") or "Не получилось сохранить отзыв"))


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
    await call.message.answer(T.owner_text(r, 30), reply_markup=T.kb([[("📊 Сводка сейчас", "ow:digest"), ("📥 Клиенты (CSV)", "ow:csv:clients")], [("📥 События за 30 дн. (CSV)", "ow:csv:events")], [("⬅ В меню", "m:home")]]))


@router.callback_query(F.data.startswith("ow:"))
async def cb_owner_tools(call: CallbackQuery) -> None:
    """Сводка CRM и выгрузка таблицы в Telegram — только подтверждённому владельцу."""
    await call.answer()
    if not await _owner_confirmed(call.from_user.id):
        return
    parts = call.data.split(":")
    if parts[1] == "digest":
        r = await site().call("digest", call.from_user.id, days=7)
        await call.message.answer(str(r.get("text") or r.get("error") or "Нет данных"))
    elif parts[1] == "csv":
        r = await site().call("export", call.from_user.id, kind="events" if parts[2:] == ["events"] else "clients", days=30)
        if not r.get("ok"):
            await call.message.answer(str(r.get("error") or "Не получилось собрать таблицу"))
            return
        await call.message.answer_document(BufferedInputFile(base64.b64decode(str(r["base64"])), filename=str(r["fileName"])), caption="Таблица для Excel (кодировка UTF-8, разделитель «;»).")


async def notify_owners(bot: Bot, text: str) -> None:
    """Сообщить владельцам (ADMIN_IDS_RAW) о сбое. Недоставка не должна ронять бота."""
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning("Не удалось сообщить владельцу %s", admin_id, exc_info=True)
