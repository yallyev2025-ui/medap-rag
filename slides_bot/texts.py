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
ASK_WISHES = "Напишите техническое задание и пожелания — что должно быть на слайдах. Пишите конкретно, например: «на 5 слайде таблица сравнения», «где про классификацию — фото 2» (фото нумеруются в порядке присылки), «больше сравнений и инфографики», «поменьше текста». Что напишете, то сделаю и покажу, что выполнено. Или нажмите «Без пожеланий»."
TOPIC_SHORT = "Тема слишком короткая — напишите хотя бы пару слов."
WORKING = "Пишу презентацию. Обычно это занимает 1–3 минуты. Пришлю черновик, когда будет готов."
SITE_LINK_ONCE = "Ссылка для входа на сайт без регистрации. Она действует 15 минут и срабатывает один раз — откройте её сейчас."
SITE_LINK_PLAIN = "Не удалось создать ссылку мгновенного входа, поэтому сайт попросит войти по почте (той же, что в боте). Откройте сайт:"
NO_SITE = "Связь с сайтом не настроена. Напишите владельцу."
SITE_OLD = "Сайт ещё не обновлён до новой версии: выбор источников, оформления, шрифтов и цветов заработает, когда сайт выложат вместе с ботом. Напишите владельцу."


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
NOTES_NAMES = {"": "подробно", "none": "не нужны", "brief": "кратко", "detailed": "подробно"}
DETAIL_NAMES = {"brief": "кратко", "detailed": "подробно"}


def default_opts() -> dict[str, Any]:
    """Выборы по умолчанию: ничего не выбрано — сайт подберёт всё сам."""
    return {"medical": False, "textbooks": False, "discipline": "", "disciplineName": "", "pmids": [], "pmOffset": 0, "styleId": "", "styleName": "", "fonts": None, "colors": [], "notes": "", "detail": "detailed", "numbers": False, "title": {}, "photoIds": [], "wishes": "", "mode": "topic", "planText": "", "audience": "", "uploadIds": [], "uploadNames": {}}


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


MODE_NAMES = {"topic": "только тема", "plan": "тема и свой план", "text": "готовый текст по слайдам"}


def setup_text(d: dict[str, Any]) -> str:
    """Экран «Настроить». Сверху — то, что после создания на сайте НЕ поменять (поэтому спрашиваем здесь); ниже — оформление, его можно изменить на сайте потом, бесплатно."""
    o = d.get("o") or default_opts()
    text_mode = o.get("mode") == "text"
    notes = NOTES_NAMES.get(o.get("notes", ""), "подробно")
    lines = [
        "Настройте презентацию.",
        f"Тема: {d.get('topic', '')}",
        f"Вид: {KIND_NAMES.get(d.get('kind', 'talk'), 'Доклад')} · слайдов до {d.get('total', 10)}",
        "",
        "Выберите то, что нельзя поменять после создания (всё необязательно — ничего не трогали, подберу сам):",
        f"🧩 С чего начинаем: {MODE_NAMES.get(o.get('mode', 'topic'), 'только тема')}" + (f" ({len(o['planText'])} знаков)" if o.get("planText") else ""),
    ]
    if not text_mode:
        lines.append(f"📚 Источники: {sources_note(o)}")
        lines.append(f"📎 Свои материалы: {len(o['uploadIds'])}" if o.get("uploadIds") else "📎 Свои материалы: нет")
    lines.append(f"👥 Для кого: {o['audience']}" if o.get("audience") else "👥 Для кого: не указано")
    lines.append(f"📝 Заметки докладчика: {notes} · слайды: {DETAIL_NAMES.get(o.get('detail', 'detailed'), 'подробно')}")
    lines.append(f"🧾 Титул: {'поправлено: ' + str(len(o['title'])) if o.get('title') else 'из вашего профиля'}")
    lines.append(f"🖼 Фото: {len(o['photoIds'])} (присылайте снимки прямо в чат)" if o.get("photoIds") else "🖼 Фото: нет (присылайте снимки прямо в чат)")
    if o.get("wishes") and not text_mode:
        lines.append(f"💬 Пожелания: {o['wishes'][:120]}")
    lines += ["", f"🎨 Оформление: {style_note(o)}; шрифты и цвета: {fonts_note(o)} — необязательно, это можно изменить на сайте потом."]
    return "\n".join(lines)


def setup_kb(site_button: bool = True, o: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    text_mode = bool(o and o.get("mode") == "text")
    rows: list[list[tuple[str, str]]] = [[("🧩 С чего начинаем", "o:mode")]]
    if not text_mode:
        rows.append([("📚 Источники", "o:src"), ("📎 Свои материалы", "o:up")])
    rows += [
        [("👥 Для кого", "o:aud"), ("📝 Заметки и объём", "o:txt")],
        [("🧾 Титул", "o:ttl"), ("🖼 Фото", "o:ph")],
    ]
    if not text_mode:
        rows.append([("💬 Пожелания", "o:wsh")])
    rows.append([("🎨 Оформление, шрифты, цвета (необязательно)", "o:des")])
    if site_button:
        rows.append([("🖥 Полная форма на сайте", "o:site")])
    rows.append([("✅ Создать презентацию", "o:go")])
    return kb(rows)


def design_kb() -> InlineKeyboardMarkup:
    return kb([
        [("🎨 Шаблон", "o:sty:0"), ("🔤 Шрифты", "o:fnt:h:0")],
        [("🎨 Цвета", "o:col"), ("🔢 Номера слайдов", "o:num2")],
        back_row(),
    ])


DESIGN_HELP = "Оформление необязательно: ничего не выберете — подберу сам под вас, а потом на сайте можно сменить шаблон, шрифты и цвета бесплатно и сколько угодно раз."


def mode_kb(o: dict[str, Any]) -> InlineKeyboardMarkup:
    m = o.get("mode", "topic")
    return kb([
        [(_mark(m == "topic", "Только тема — напишу сам"), "o:m:topic")],
        [(_mark(m == "plan", "Тема и свой план"), "o:m:plan")],
        [(_mark(m == "text", "Готовый текст по слайдам (не меняю формулировки)"), "o:m:text")],
        back_row(),
    ])


MODE_HELP = "С чего начинаем. «Свой план» — вы присылаете пункты, я пишу по ним слайды. «Готовый текст» — вы присылаете текст каждого слайда, я его оформляю, формулировки не меняю (источники и пожелания в этом режиме не нужны)."
PLAN_ASK = {
    "plan": "Пришлите план по пунктам (можно несколькими сообщениями), как вам нужно. Когда всё отправили — нажмите «Готово».",
    "text": "Пришлите текст по слайдам, например:\nСлайд 1. Заголовок\nТекст…\n\nСлайд 2. …\nМожно несколькими сообщениями. Когда всё отправили — нажмите «Готово».",
}


def plan_kb(has_text: bool) -> InlineKeyboardMarkup:
    rows = [[("✅ Готово", "o:pdone")]]
    if has_text:
        rows.append([("Очистить и начать заново", "o:pclear")])
    rows.append(back_row())
    return kb(rows)


def uploads_kb(items: list[dict[str, Any]], chosen: list[str]) -> InlineKeyboardMarkup:
    rows = [[(_mark(x["id"] in chosen, f"{str(x['fileName'])[:30]} · {str(x.get('discipline') or '')[:14]}"), f"o:upt:{i}")] for i, x in enumerate(items[:10])]
    rows.append(back_row())
    return kb(rows)


UPLOADS_HELP = "Свои материалы — файлы, которые вы загрузили на сайте (конспект, лекция, статья). По отмеченным (до 5) модель напишет слайды."
UPLOADS_NONE = "Своих материалов пока нет. Загрузить файл можно на сайте — кнопка «Полная форма на сайте» — и вернуться сюда."
AUD_ASK = "Для кого презентация? Например: «студенты 3 курса», «комиссия на защите», «школьники 9 класса». Пришлите одной фразой (или «-», чтобы убрать)."


def back_row() -> list[tuple[str, str]]:
    return [("✅ Готово", "o:home")]


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


def articles_chunks(articles: list[dict[str, Any]], chosen: list[str], limit: int, budget: int = 3700) -> list[str]:
    """Список статей одним или несколькими сообщениями: аннотация каждой статьи видна сразу и целиком (по-русски), а не за нажатием.
    Telegram режет сообщение на 4096 знаках, поэтому статьи раскладываются по сообщениям, ни одна не режется посередине."""
    head = f"Статьи PubMed по теме (выбрано {len(chosen)} из {limit}). Лучшие по ценности — сверху."
    tail = "🔓 — полный текст открыт. Нажмите номер под списком, чтобы выбрать или снять статью."
    blocks: list[str] = []
    for i, a in enumerate(articles, 1):
        mark = "✅ " if a["pmid"] in chosen else ""
        meta = " · ".join(x for x in [str(a.get("journal") or ""), str(a.get("year") or ""), str(a.get("design") or "")] if x)
        free = " 🔓" if a.get("openAccess") else ""
        lines = [f"{mark}{i}. {a['title']}{free}"]
        if meta:
            lines.append(f"   {meta}")
        ab = " ".join(str(a.get("abstract", "")).split())
        if ab:
            lines.append(f"   {ab}")
        blocks.append("\n".join(lines)[:1500])
    chunks: list[str] = []
    cur = head
    for blk in blocks:
        if len(cur) + len(blk) + 2 > budget:
            chunks.append(cur)
            cur = blk
        else:
            cur += "\n\n" + blk
    if len(cur) + len(tail) + 2 > budget:
        chunks.append(cur)
        cur = tail
    else:
        cur += "\n\n" + tail
    chunks.append(cur)
    return chunks


def articles_kb(articles: list[dict[str, Any]], chosen: list[str], more: bool) -> InlineKeyboardMarkup:
    nums = [((("✅" if a["pmid"] in chosen else "") + str(i)), f"o:p:{i - 1}") for i, a in enumerate(articles, 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    tail = []
    if more:
        tail.append(("Ещё 10 статей", "o:pmm"))
    tail.append((f"Готово ({len(chosen)})" if chosen else "Готово", "o:src"))
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
    rows.append([("Как у оформления", "o:fx"), ("✅ Готово", "o:home")])
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
        [(_mark(notes == "none", "Заметки: не нужны"), "o:n:none")],
        [(_mark(notes == "brief", "Заметки: кратко"), "o:n:brief"), (_mark(notes in ("", "detailed"), "Заметки: подробно"), "o:n:detailed")],
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


def draft_kb(d: dict[str, Any], run_id: str, site: bool = True) -> InlineKeyboardMarkup:
    """«Открыть на сайте» — обычная кнопка: ссылка входа создаётся в момент нажатия (она живёт недолго и срабатывает один раз; созданная заранее, при отправке
    сообщения, к нажатию «умирала», и сайт просил регистрироваться заново)."""
    rows = [[("✏️ Поправить слайд", f"e:{run_id}"), ("✅ Утвердить", f"a:{run_id}")], [("🖼 Подобрать фото ИИ", f"ph:{run_id}")]]
    if site:
        rows.append([("🖥 Открыть на сайте", f"u:{run_id}")])
    return kb(rows)


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


def files_kb(run_id: str, site: bool = True) -> InlineKeyboardMarkup:
    rows = [[("📄 PowerPoint", f"f:{run_id}:pptx"), ("📕 PDF", f"f:{run_id}:pdf")], [("🖼 Подобрать фото ИИ", f"ph:{run_id}")]]
    if site:
        rows.append([("🖥 Открыть на сайте (поправить текст и оформление)", f"u:{run_id}")])
    rows.append([("⬅ В меню", "m:home")])
    return kb(rows)


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


# ---------- подбор фото ИИ ----------

PHOTO_WAIT = "Подбираю фото: ИИ смотрит слайды, ищет картинки там, где они помогают, и сам отсеивает лишнее. Обычно до минуты, пришлю результат."
PHOTO_NONE = "ИИ посмотрел слайды и решил, что картинки им не нужны — это нормально: слайд без картинки лучше слайда с лишней."


def photo_notes_text(notes: list[dict[str, Any]], added: int) -> str:
    lines = [f"Готово: предложено картинок — {added}." if added else "Подходящих картинок не нашлось.", ""]
    for n in notes:
        lines.append(f"{'✓' if n.get('ok') else '—'} Слайд {n['slide']}" + (f" «{n['title']}»" if n.get("title") else "") + f": {n['text']}")
    return "\n".join(lines)[:3900]


def suggestion_caption(it: dict[str, Any]) -> str:
    where = f"Слайд {it['slide']}" if it.get("slide") else "Слайд подберу сам"
    origin = "Нарисовано ИИ" if it.get("origin") == "ai" else "Из интернета"
    lines = [f"{origin} · {where}", f"Зачем: {it.get('why') or '—'}", f"Подпись внизу слайда: {it.get('credit') or '—'}"]
    if it.get("sourceUrl"):
        lines.append(str(it["sourceUrl"]))
    return "\n".join(lines)[:1000]


PHOTO_AUTO_WAIT = "Пока вы смотрите черновик, ИИ подбирает к нему картинки. Пришлю предложения — вы решите, какие поставить."
PHOTO_OFFER = "Предложено картинок: {n}. Поставить все сразу или выберите по одной под каждой?"
PHOTO_PLACED = "Поставил картинок: {n}. Мало — добавьте свои на сайте кнопкой «+ Фото» на нужном слайде или попросите ИИ предложить другие."


def photo_group_kb(run_id: str, runs_left: int) -> InlineKeyboardMarkup:
    rows = [[("✅ Поставить все", f"pall:{run_id}"), ("🗑 Не нужны", f"pnone:{run_id}")]]
    if runs_left > 0:
        rows.append([(f"🔁 Предложить другие (осталось запусков: {runs_left})", f"ph:{run_id}")])
    return kb(rows)


def suggestion_kb(run_id: str, photo_id: str) -> InlineKeyboardMarkup:
    return kb([[("✅ Поставить на слайд", f"pa:{run_id}:{photo_id}"), ("🗑 Убрать", f"pd:{run_id}:{photo_id}")]])


# ---------- мастер: вопросы по одному, кнопки только там, где выбор из вариантов ----------

WZ_STEPS = ("mode", "aud", "src", "up", "ph", "wish", "ttl")
FILES_MAX = 5
FILE_MB = 20


def wz_skip(step: str, o: dict[str, Any]) -> bool:
    """В режиме «готовый текст» источники, файлы и пожелания не нужны: слова не меняются."""
    return o.get("mode") == "text" and step in ("src", "up", "wish")


def wz_steps(o: dict[str, Any]) -> list[str]:
    return [x for x in WZ_STEPS if not wz_skip(x, o)]


WZ_TEXT = {
    "mode": "С чего начинаем?\n• Только тема — напишу всё сам.\n• Тема и свой план — вы присылаете пункты, я пишу по ним.\n• Готовый текст по слайдам — оформлю, формулировки не меняю.",
    "aud": "Для кого презентация? Напишите одной фразой, например: «студенты 3 курса», «комиссия на защите», «школьники 9 класса». Или нажмите «Пропустить».",
    "src": "Откуда брать материал? Отметьте нужное и нажмите «Далее».\n" + SOURCES_HELP,
    "up": f"Свои файлы: пришлите в этот чат до {FILES_MAX} файлов (PDF, Word, PowerPoint, текст, фото страниц; до {FILE_MB} МБ каждый). Презентация будет сделана строго по ним, без добавлений от себя. Если файлов нет — нажмите «Далее».",
    "ph": f"Фото: присылайте снимки прямо в чат (JPEG, PNG или WebP, до 8 МБ, не больше {MAX_PHOTOS}). Остальное подберёт ИИ сам и покажет на утверждение. Нажмите «Далее», когда готово.",
    "wish": ASK_WISHES,
    "ttl": "Титульный лист: ФИО, группа и вуз берутся из вашего профиля. Их не отправляют в модель.",
}


def wz_text(step: str, index: int, total: int, o: dict[str, Any]) -> str:
    head = f"Шаг {index + 1} из {total}."
    body = WZ_TEXT[step]
    if step == "up":
        n = len(o.get("uploadIds") or [])
        if n:
            body += "\n\nПринято файлов: " + str(n) + " из " + str(FILES_MAX) + "."
    if step == "ph" and o.get("photoIds"):
        body += f"\n\nПринято снимков: {len(o['photoIds'])}."
    if step == "aud" and o.get("audience"):
        body += f"\n\nСейчас: {o['audience']}"
    return head + " " + body


def wz_nav(first: bool) -> list[tuple[str, str]]:
    row = [("⬅ Назад", "o:wz:b")] if not first else []
    row.append(("✖ Отмена", "o:cancel"))
    return row


def wz_kb(step: str, o: dict[str, Any], first: bool = False) -> InlineKeyboardMarkup:
    nxt = [("➡ Далее", "o:wz:n")]
    if step == "mode":
        m = o.get("mode", "topic")
        rows = [
            [(_mark(m == "topic", "Только тема"), "o:m:topic")],
            [(_mark(m == "plan", "Тема и свой план"), "o:m:plan")],
            [(_mark(m == "text", "Готовый текст по слайдам"), "o:m:text")],
        ]
    elif step == "aud":
        rows = [[("Пропустить", "o:wz:n")]]
    elif step == "src":
        rows = [
            [(_mark(bool(o.get("medical")), "Медицинская тема"), "o:med")],
            [(_mark(bool(o.get("textbooks")), "Учебники бота" + (f": {o['disciplineName']}" if o.get("disciplineName") else "")), "o:tb")],
            [(_mark(bool(o.get("pmids")), "Статьи PubMed" + (f": {len(o['pmids'])}" if o.get("pmids") else "")), "o:pm")],
            nxt,
        ]
    elif step == "up":
        rows = [[("📂 Выбрать из уже загруженных", "o:up")]]
        if o.get("uploadIds"):
            rows.append([("Убрать все файлы", "o:upx")])
        rows.append(nxt)
    elif step == "ph":
        rows = []
        if o.get("photoIds"):
            rows.append([("Убрать все фото", "o:phx")])
        rows.append(nxt)
    elif step == "wish":
        rows = [[("Без пожеланий", "o:wz:n")]]
    else:  # ttl
        rows = [[("✅ Оставить как в профиле", "o:wz:n")], [("✏️ Поправить поля", "o:ttl")]]
    rows.append(wz_nav(first))
    return kb(rows)


def final_text(d: dict[str, Any]) -> str:
    return "Всё спросил. Проверьте и нажмите «Создать» — или откройте нужный пункт, чтобы изменить.\n\n" + setup_text(d)


# ---------- оценка после скачивания (честно: ничего не выбрано заранее) ----------

REVIEW_ASK = "Как вам презентация? Поставьте оценку от 1 до 5 — это поможет улучшить сервис."
REVIEW_THANKS = "Оценка {n} из 5 сохранена, спасибо! Если хотите, напишите пару слов о презентации одним сообщением — они появятся на главной сайта после проверки."


def review_kb(run_id: str) -> InlineKeyboardMarkup:
    return kb([[(f"{n} ⭐" if n == 1 else f"{n}", f"rv:{run_id}:{n}") for n in range(1, 6)], [("Не сейчас", f"rv:{run_id}:skip")]])
