"""Тексты и клавиатуры бота презентаций. Без обращений к сети и базе — легко проверяются тестами."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

KIND_NAMES = {"talk": "Доклад", "defense": "Защита работы", "lecture": "Лекция", "business": "Деловая"}
PHASE_NAMES = {"choosing": "пишется", "draft": "черновик", "approved": "утверждена", "failed": "не получилась"}

WELCOME = (
    "Я делаю презентации Medap Slide: вы называете тему — я пишу слайды, подбираю оформление и присылаю файл PowerPoint или PDF.\n\n"
    "Первая презентация бесплатная (до 7 слайдов). Дальше — по размеру, оплата рублями с баланса."
)
ASK_CONSENT = (
    "Отправлю код на {email}.\n\nНажимая «Согласен(на)», вы соглашаетесь на обработку персональных данных (почта, Telegram-аккаунт, тексты ваших презентаций) "
    "и принимаете политику конфиденциальности — ссылки ниже. Без согласия код не отправляется."
)
ASK_EMAIL = "Чтобы начать, пришлите почту, которую вы используете на сайте Medap (или новую). Я отправлю на неё код из 6 цифр."
ASK_CODE = "Код отправлен. Пришлите 6 цифр из письма (письмо может прийти в «Спам»)."
LOGGED_IN = "Готово, вы вошли. Теперь можно создавать презентации."
ASK_TOPIC = "О чём презентация? Напишите тему одной-двумя фразами."
ASK_WISHES = "Есть пожелания? Например: «сделай упор на лечение», «поменьше текста», «добавь таблицу». Или нажмите «Без пожеланий»."
TOPIC_SHORT = "Тема слишком короткая — напишите хотя бы пару слов."
WORKING = "Пишу презентацию. Обычно это занимает 1–3 минуты. Пришлю черновик, когда будет готов."
NO_SITE = "Связь с сайтом не настроена. Напишите владельцу."


def money(kopecks: int) -> str:
    r = kopecks / 100
    return f"{int(r)} ₽" if float(r).is_integer() else f"{r:.2f}".replace(".", ",") + " ₽"


def kb(rows: list[list[tuple[str, str]]], url_rows: list[list[tuple[str, str]]] | None = None) -> InlineKeyboardMarkup:
    """Клавиатура из рядов (текст, callback_data); `url_rows` — ряды ссылок (текст, адрес), идут сверху."""
    buttons: list[list[InlineKeyboardButton]] = []
    for row in url_rows or []:
        buttons.append([InlineKeyboardButton(text=t, url=u) for t, u in row])
    for row in rows:
        buttons.append([InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu(owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [("➕ Создать презентацию", "m:new")],
        [("📂 Мои презентации", "m:runs"), ("💰 Баланс", "m:bal")],
        [("🏷 Цены", "m:prices"), ("🚪 Выйти", "m:out")],
    ]
    if owner:
        rows.append([("📊 Владелец", "m:owner")])
    return kb(rows)


def kinds_kb() -> InlineKeyboardMarkup:
    return kb([[("Доклад", "k:talk"), ("Защита работы", "k:defense")], [("Лекция", "k:lecture"), ("Деловая", "k:business")]])


def sizes_kb(tiers: list[dict[str, Any]], free_available: bool, free_max: int) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if free_available:
        rows.append([(f"🎁 Бесплатная, до {free_max} слайдов", f"s:{free_max}")])
    pair: list[tuple[str, str]] = []
    for t in tiers:
        pair.append((f"{t['label']} — {t['price']}", f"s:{t['maxSlides']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return kb(rows)


def styles_kb(templates: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [[("✨ Подберите сами", "st:auto")]]
    pair: list[tuple[str, str]] = []
    for t in templates[:10]:
        pair.append((str(t["name"])[:24], f"st:{t['id']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return kb(rows)


def summary_text(d: dict[str, Any]) -> str:
    parts = [f"Тема: {d.get('topic', '')}", f"Вид: {KIND_NAMES.get(d.get('kind', 'talk'), 'Доклад')}", f"Слайдов: до {d.get('total', 10)}"]
    if d.get("wishes"):
        parts.append(f"Пожелания: {d['wishes']}")
    return "\n".join(parts)


def draft_text(d: dict[str, Any]) -> str:
    """Черновик целиком: номера, заголовки и по паре строк текста. Длинный текст Telegram режет, поэтому слайды укорочены."""
    lines = [f"Черновик: {d.get('topic', '')}", f"ИИ-правок осталось: {d.get('editsLeft', 0)}", ""]
    for s in d.get("slides", []):
        text = " ".join(str(s.get("text", "")).split())
        text = text[:140] + "…" if len(text) > 140 else text
        mark = " ⚠" if s.get("fill") else ""
        lines.append(f"{s['n']}. {s.get('title') or 'без заголовка'}{mark}")
        if text and text != s.get("title"):
            lines.append(f"   {text}")
    return "\n".join(lines)[:3900]


def draft_kb(d: dict[str, Any], run_id: str, site_url: str | None) -> InlineKeyboardMarkup:
    rows = [[("✏️ Поправить слайд", f"e:{run_id}"), ("✅ Утвердить", f"a:{run_id}")]]
    url_rows = [[("🖥 Открыть на сайте", site_url)]] if site_url else None
    return kb(rows, url_rows)


def slides_pick_kb(d: dict[str, Any], run_id: str) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for s in d.get("slides", []):
        if s.get("fixed"):
            continue
        row.append((str(s["n"]), f"es:{run_id}:{s['n'] - 1}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("Отмена", f"d:{run_id}")])
    return kb(rows)


def edit_actions_kb(run_id: str, index: int) -> InlineKeyboardMarkup:
    return kb([
        [("Короче", f"ea:{run_id}:{index}:shorter"), ("Подробнее", f"ea:{run_id}:{index}:longer")],
        [("Переписать", f"ea:{run_id}:{index}:rewrite"), ("Исправить ошибки", f"ea:{run_id}:{index}:fix")],
        [("Своими словами…", f"ea:{run_id}:{index}:own"), ("Отмена", f"d:{run_id}")],
    ])


def files_kb(run_id: str, site_url: str | None) -> InlineKeyboardMarkup:
    rows = [[("📄 PowerPoint", f"f:{run_id}:pptx"), ("📕 PDF", f"f:{run_id}:pdf")], [("⬅ В меню", "m:home")]]
    url_rows = [[("🖥 Открыть на сайте", site_url)]] if site_url else None
    return kb(rows, url_rows)


def runs_kb(runs: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = [[(f"{str(r['topic'])[:34]} · {PHASE_NAMES.get(r['phase'], r['phase'])}", f"d:{r['id']}")] for r in runs[:10]]
    rows.append([("⬅ В меню", "m:home")])
    return kb(rows)


def topup_kb() -> InlineKeyboardMarkup:
    return kb([[("100 ₽", "t:10000"), ("300 ₽", "t:30000"), ("500 ₽", "t:50000")], [("Другая сумма", "t:other"), ("⬅ В меню", "m:home")]])


DENY_NAMES = {"email": "та же почта", "telegram": "тот же Telegram", "disposable": "временная почта", "ip": "много с одного адреса", "cap": "суточный потолок"}


def owner_text(s: dict[str, Any], days: int = 30) -> str:
    return (
        f"За {days} дн.:\n"
        f"презентаций: {s.get('runs', 0)} (платных {s.get('paidRuns', 0)}, бесплатных {s.get('free', 0)}, сбоев {s.get('failed', 0)})\n"
        f"пополнений: {s.get('topupRub', 0)} ₽\n"
        f"расход на модели: {s.get('costRub', 0)} ₽"
        + (("\nзащита отказала в бесплатной: " + ", ".join(f"{DENY_NAMES.get(k, k)} — {v}" for k, v in s["freeDenied"].items())) if s.get("freeDenied") else "")
    )
