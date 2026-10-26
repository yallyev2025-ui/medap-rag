"""Истории, лайфхаки и факты для экранов ожидания (владелец 26.10.2026: «фразы повторяются к 5–10-му вопросу»).

Содержимое лежит в `data/waiting_content.json` (одинаковый файл у сайта и у ботов). Здесь — выбор без повторов: каждому человеку
показывается то, чего он ещё не видел; когда всё показано, круг начинается заново в другом порядке. Истории идут по частям
(одна часть за один экран) и продолжаются, даже если ответ пришёл раньше: на следующем вопросе — следующая часть.
Бесплатно, без нейросети.
"""

from __future__ import annotations

import json
import random
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_PATH = Path(__file__).resolve().parent.parent / "data" / "waiting_content.json"
_MAX_USERS = 20000
# чередование видов: лайфхак → история → факт → лайфхак …
_ORDER = ("lifehack", "story", "fact")
_EMOJI = {"lifehack": "💡 Лайфхак: ", "fact": "🧠 Знаете ли вы? "}


@dataclass
class Item:
    id: str
    pool: str
    kind: str
    title: str
    parts: list[str]


@dataclass
class Shown:
    """Что показать сейчас и сколько секунд это читать"""

    text: str
    seconds: float


@dataclass
class _User:
    seen: set[str] = field(default_factory=set)
    cursor: dict[str, int] = field(default_factory=dict)  # история → номер следующей части
    step: int = 0
    last_story: bool = False


_items: list[Item] | None = None
_users: "OrderedDict[str, _User]" = OrderedDict()


def load_items() -> list[Item]:
    global _items
    if _items is None:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        _items = [Item(x["id"], x["pool"], x["kind"], x.get("title", ""), list(x["parts"])) for x in raw["items"]]
    return _items


def reading_seconds(text: str) -> float:
    """Сколько секунд нужно на чтение: около 15 знаков в секунду, но не меньше 6 и не больше 28"""
    return max(6.0, min(28.0, len(text) / 15 + 2))


def _user(key: str) -> _User:
    u = _users.get(key)
    if u is None:
        u = _users[key] = _User(step=random.randrange(len(_ORDER)))
        while len(_users) > _MAX_USERS:
            _users.popitem(last=False)
    else:
        _users.move_to_end(key)
    return u


def _format(item: Item, part: int) -> str:
    if item.kind == "story":
        n = len(item.parts)
        return f"📖 {item.title} ({part + 1}/{n})\n\n{item.parts[part]}"
    return _EMOJI.get(item.kind, "") + item.parts[0]


def next_item(user_key: str, pools: Iterable[str] = ("general",), rng: random.Random | None = None) -> Shown:
    """Следующее, что увидит этот человек: недочитанная история, иначе по кругу лайфхак / история / факт без повторов"""
    r = rng or random
    allowed = set(pools)
    items = [i for i in load_items() if i.pool in allowed]
    u = _user(str(user_key))
    by_id = {i.id: i for i in items}

    # история, начатая раньше: следующая часть
    last_was_story = u.last_story
    for sid, nxt in list(u.cursor.items()):
        item = by_id.get(sid)
        if item and nxt < len(item.parts) and not last_was_story:
            u.cursor[sid] = nxt + 1
            if nxt + 1 >= len(item.parts):
                del u.cursor[sid]
                u.seen.add(sid)
            u.last_story = True
            text = _format(item, nxt)
            return Shown(text, reading_seconds(text))
        if not item:
            del u.cursor[sid]

    for _ in range(len(_ORDER)):
        kind = _ORDER[u.step % len(_ORDER)]
        u.step += 1
        pool = [i for i in items if i.kind == kind]
        if not pool:
            continue
        fresh = [i for i in pool if i.id not in u.seen]
        if not fresh:
            # всё этого вида показано: начинаем круг заново
            u.seen -= {i.id for i in pool}
            fresh = pool
        item = r.choice(fresh)
        if item.kind == "story":
            u.cursor[item.id] = 1
            if len(item.parts) == 1:
                del u.cursor[item.id]
                u.seen.add(item.id)
            u.last_story = True
            text = _format(item, 0)
        else:
            u.seen.add(item.id)
            u.last_story = False
            text = _format(item, 0)
        return Shown(text, reading_seconds(text))
    return Shown("⌛ Ещё чуть-чуть…", 6.0)


def next_line(user_key: str, lines: list[str], rng: random.Random | None = None) -> str:
    """Короткая служебная фраза («Листаю учебники…») без повторов, пока не показаны все"""
    r = rng or random
    u = _user(str(user_key))
    ids = {f"line:{i}": t for i, t in enumerate(lines)}
    fresh = [k for k in ids if k not in u.seen]
    if not fresh:
        u.seen -= set(ids)
        fresh = list(ids)
    k = r.choice(fresh)
    u.seen.add(k)
    u.last_story = False
    return ids[k]


def reset_for_tests() -> None:
    _users.clear()
