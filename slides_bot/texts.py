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


# ---------- «Настроить» — те же выборы, что в мастере на сайте ----------

PAGE = 8  # кнопок в одном списке (шаблоны, шрифты)
MAX_PHOTOS = 12
NOTES_NAMES = {"": "не нужны", "brief": "кратко", "detailed": "подробно"}
DETAIL_NAMES = {"brief": "кратко", "detailed": "подробно"}


def default_opts() -> dict[str, Any]:
    """Выборы по умолчанию: ничего не выбрано — сайт подберёт всё сам."""
    return {"medical": False, "textbooks": False, "discipline": "", "disciplineName": "", "pmids": [], "pmOffset": 0, "styleId": "", "styleName": "", "fonts": None, "colors": [], "notes": "", "detail": "detailed", "numbers": False, "title": {}, "photoIds": [], "wishes": ""}


def max_pick(total: int) -> int:
    """Сколько статей PubMed можно выбрать: не больше, чем слайдов (потолок 12), как на сайте."""
    return max(1, min(12, int(total)))


def sources_note(o: dict[str, Any]) -> str:
    parts = []
    if o.get("medical"):
        parts.append("мед. тема")
    if o.get("textbooks"):
        parts.append("учебники" + (f" ({o['disciplineName']})" if o.get("disciplineName") else ""))
    if o.get("pmids"):
        parts.append(f"PubMed: {len(o['pmids'])}")
    return " · ".join(parts) or "общие знания"


def style_note(o: dict[str, Any]) -> str:
    sid = o.get("styleId")
    return "подберём сами" if not sid else "чистый лист" if sid == "plain" else str(o.get("styleName") or sid)


def fonts_note(o: dict[str, Any]) -> str:
    f = o.get("fonts")
    base = f"{f['heading']} + {f['body']}" if f else "как у оформления"
    return base + (", свои цвета" if o.get("colors") else "")


def setup_text(d: dict[str, Any]) -> str:
    o = d.get("o") or default_opts()
    lines = [
        "Настройте презентацию — всё необязательно, ничего не трогали — подберу сам.",
        "",
        f"Тема: {d.get('topic', '')}",
        f"Вид: {KIND_NAMES.get(d.get('kind', 'talk'), 'Доклад')} · слайдов до {d.get('total', 10)}",
        f"📚 Источники: {sources_note(o)}",
        f"🎨 Оформление: {style_note(o)}",
        f"🔤 Шрифты и цвета: {fonts_note(o)}",
        f"📝 Заметки докладчика: {'подробно (мед. тема)' if o.get('medical') and not o.get('notes') else NOTES_NAMES.get(o.get('notes', ''), 'не нужны')} · слайды: {DETAIL_NAMES.get(o.get('detail', 'detailed'), 'подробно')}",
        f"🧾 Титул: {'поправлено: ' + str(len(o['title'])) if o.get('title') else 'из вашего профиля'}",
        f"🖼 Фото: {len(o['photoIds'])} (присылайте снимки прямо в чат)" if o.get("photoIds") else "🖼 Фото: нет (присылайте снимки прямо в чат)",
    ]
    if o.get("wishes"):
        lines.append(f"💬 Пожелания: {o['wishes'][:120]}")
    return "\n".join(lines)


def setup_kb(site_button: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [("📚 Источники", "o:src"), ("🎨 Оформление", "o:sty:0")],
        [("🔤 Шрифты", "o:fnt:h:0"), ("🎨 Цвета", "o:col")],
        [("📝 Заметки и объём", "o:txt"), ("🧾 Титул", "o:ttl")],
        [("🖼 Фото", "o:ph"), ("💬 Пожелания", "o:wsh")],
    ]
    if site_button:
        rows.append([("🖥 Полная форма на сайте", "o:site")])
    rows.append([("✅ Создать презентацию", "o:go")])
    return kb(rows)


def back_row() -> list[tuple[str, str]]:
    return [("⬅ К настройкам", "o:home")]


def _mark(on: bool, text: str) -> str:
    return ("✅ " if on else "▫ ") + text


def sources_kb(o: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = [
        [(_mark(bool(o.get("medical")), "Медицинская тема"), "o:med")],
        [(_mark(bool(o.get("textbooks")), "Учебники бота" + (f": {o['disciplineName']}" if o.get("disciplineName") else "")), "o:tb")],
        [(_mark(bool(o.get("pmids")), "Статьи PubMed" + (f": {len(o['pmids'])}" if o.get("pmids") else "")), "o:pm")],
        back_row(),
    ]
    return kb(rows)


SOURCES_HELP = (
    "Откуда брать материал.\n"
    "• Медицинская тема — строже к фактам, заметки докладчика всегда.\n"
    "• Учебники бота — план и текст по учебнику выбранного предмета, а не из памяти.\n"
    "• Статьи PubMed — доказательства и свежие данные; это не основа, а усиление слайдов."
)


def disciplines_kb(items: list[dict[str, Any]], page: int = 0) -> InlineKeyboardMarkup:
    chunk = items[page * PAGE:(page + 1) * PAGE]
    rows = [[(str(x["title"])[:40], f"o:d:{page * PAGE + i}")] for i, x in enumerate(chunk)]
    nav = pager(page, len(items), "o:dl")
    if nav:
        rows.append(nav)
    rows.append([("Без предмета", "o:d:none"), ("⬅ Назад", "o:src")])
    return kb(rows)


def pager(page: int, count: int, prefix: str) -> list[tuple[str, str]]:
    row: list[tuple[str, str]] = []
    if page > 0:
        row.append(("◀", f"{prefix}:{page - 1}"))
    if (page + 1) * PAGE < count:
        row.append(("▶", f"{prefix}:{page + 1}"))
    return row


def articles_text(articles: list[dict[str, Any]], chosen: list[str], limit: int) -> str:
    lines = [f"Статьи PubMed по теме (выбрано {len(chosen)} из {limit}). Лучшие по ценности — сверху.", ""]
    for i, a in enumerate(articles, 1):
        mark = "✅ " if a["pmid"] in chosen else ""
        meta = " · ".join(x for x in [str(a.get("journal") or ""), str(a.get("year") or ""), str(a.get("design") or "")] if x)
        free = " 🔓" if a.get("openAccess") else ""
        lines.append(f"{mark}{i}. {a['title']}{free}")
        if meta:
            lines.append(f"   {meta}")
        ab = " ".join(str(a.get("abstract", "")).split())
        if ab:
            lines.append(f"   {ab[:130]}{'…' if len(ab) > 130 else ''}")
    lines.append("\n🔓 — полный текст открыт. Нажмите номер, чтобы выбрать или снять.")
    return "\n".join(lines)[:3900]


def articles_kb(articles: list[dict[str, Any]], chosen: list[str], more: bool) -> InlineKeyboardMarkup:
    nums = [((("✅" if a["pmid"] in chosen else "") + str(i)), f"o:p:{i - 1}") for i, a in enumerate(articles, 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    tail = []
    if more:
        tail.append(("Ещё 10 статей", "o:pmm"))
    tail.append(("Готово", "o:src"))
    rows.append(tail)
    if chosen:
        rows.append([("Снять все", "o:pmx")])
    return kb(rows)


def styles_page_kb(templates: list[dict[str, Any]], page: int) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if page == 0:
        rows.append([("✨ Подберите сами", "o:st:auto"), ("Чистый лист", "o:st:plain")])
    chunk = templates[page * PAGE:(page + 1) * PAGE]
    pair: list[tuple[str, str]] = []
    for i, t in enumerate(chunk):
        pair.append((str(t["name"])[:24], f"o:st:{page * PAGE + i}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    nav = pager(page, len(templates), "o:sty")
    if nav:
        rows.append(nav)
    rows.append(back_row())
    return kb(rows)


def fonts_page_kb(fonts: list[str], page: int, which: str) -> InlineKeyboardMarkup:
    chunk = fonts[page * PAGE:(page + 1) * PAGE]
    pick = "o:fh" if which == "h" else "o:fb"
    rows = [[(f, f"{pick}:{page * PAGE + i}")] for i, f in enumerate(chunk)]
    nav = pager(page, len(fonts), f"o:fnt:{which}")
    if nav:
        rows.append(nav)
    rows.append([("Как у оформления", "o:fx"), ("⬅ К настройкам", "o:home")])
    return kb(rows)


def colors_kb(presets: list[dict[str, Any]], has_colors: bool) -> InlineKeyboardMarkup:
    rows = [[(str(p["name"]), f"o:cp:{i}")] for i, p in enumerate(presets)]
    rows.append([("✍ Свои цвета (напишу коды)", "o:cc")])
    if has_colors:
        rows.append([("Сбросить цвета", "o:cx")])
    rows.append(back_row())
    return kb(rows)


COLORS_HELP = "Пришлите до трёх цветов кодами, например: #2f6f73 #d9822b #1d2b3a. Из них достроится вся палитра с проверкой читаемости."


def text_kb(o: dict[str, Any]) -> InlineKeyboardMarkup:
    notes = o.get("notes", "")
    detail = o.get("detail", "detailed")
    return kb([
        [(_mark(notes == "", "Заметки: не нужны"), "o:n:none")],
        [(_mark(notes == "brief", "Заметки: кратко"), "o:n:brief"), (_mark(notes == "detailed", "Заметки: подробно"), "o:n:detailed")],
        [(_mark(detail == "brief", "Слайды: кратко"), "o:dd:brief"), (_mark(detail == "detailed", "Слайды: подробно"), "o:dd:detailed")],
        [(_mark(bool(o.get("numbers")), "Номера слайдов в углу"), "o:num")],
        back_row(),
    ])


TEXT_HELP = "Слайды остаются компактными, а всё, что нужно сказать вслух, — в заметках докладчика к каждому слайду. У медицинской темы заметки всегда подробные."


def title_text(fields: list[list[str]], values: dict[str, str], profile: dict[str, str]) -> str:
    lines = ["Титульный лист. Значения взяты из вашего профиля; нажмите поле, чтобы изменить (знак «-» очищает).", ""]
    for key, label in fields:
        v = values.get(key, profile.get(key, ""))
        lines.append(f"{label}: {v or '—'}")
    return "\n".join(lines)


def title_kb(fields: list[list[str]]) -> InlineKeyboardMarkup:
    rows = [[(str(label), f"o:tf:{i}")] for i, (_k, label) in enumerate(fields)]
    rows.append([("Вернуть из профиля", "o:tr")])
    rows.append(back_row())
    return kb(rows)


PHOTOS_HELP = (
    f"Присылайте снимки прямо в этот чат (JPEG, PNG или WebP, до 8 МБ, не больше {MAX_PHOTOS}). ИИ рассмотрит каждый, построит презентацию с учётом них и поставит на подходящий слайд — целиком, не обрезая. Подписи не нужны.\n\n"
    "Не присылайте снимки пациентов и лица людей без их согласия: снимки рассматривает внешняя модель. Данные съёмки (место, время, телефон) стираются."
)


def photos_kb(count: int) -> InlineKeyboardMarkup:
    rows = []
    if count:
        rows.append([("Убрать все фото", "o:phx")])
    rows.append(back_row())
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
