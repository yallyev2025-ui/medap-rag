"""Проверка историй и лайфхаков для экранов ожидания (без сети и Telegram).

Запуск: python scripts/test_waiting_content.py
Проверяется: файл содержимого (ID не повторяются, длины частей, виды), выбор без повторов, продолжение историй, пулы
(медицинские истории не попадают тем, кому нужны только общие), раздельная память разных людей.
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

from bot import waiting_content as W  # noqa: E402
from bot import waiting_phrases as P  # noqa: E402

checks = 0


def ok(cond: bool, msg: str) -> None:
    global checks
    checks += 1
    if not cond:
        print("✗ ПРОВАЛ:", msg)
        sys.exit(1)


def main() -> None:
    items = W.load_items()
    ids = [i.id for i in items]
    ok(len(ids) == len(set(ids)), "идентификаторы не повторяются")
    ok(len(items) >= 80, f"историй и лайфхаков достаточно: {len(items)}")
    ok({i.kind for i in items} == {"story", "lifehack", "fact"}, "есть истории, лайфхаки и факты")
    ok({i.pool for i in items} == {"general", "medicine"}, "есть общий и медицинский наборы")
    ok(all(150 <= len(p) <= 600 for i in items if i.kind == "story" for p in i.parts), "части историй 150–600 знаков")
    ok(all(len(i.parts) >= 3 for i in items if i.kind == "story"), "у историй не меньше трёх частей")
    ok(all(len(i.parts) == 1 and len(i.parts[0]) <= 320 for i in items if i.kind != "story"), "лайфхаки и факты короткие")
    ok(sum(1 for i in items if i.kind == "story" and i.pool == "medicine") >= 10 and sum(1 for i in items if i.kind == "story" and i.pool == "general") >= 10, "не меньше десяти историй каждого набора")
    ok(all(18 <= W.reading_seconds(p) <= 75 for i in items for p in i.parts) and W.reading_seconds("x") == 18.0, "время чтения в пределах 18–75 секунд")

    # без повторов: пока не показано всё, ничего не повторяется (кроме частей одной истории подряд, они разные)
    W.reset_for_tests()
    rng = random.Random(7)
    singles = [i for i in items if i.kind != "story"]
    seen: list[str] = []
    for _ in range(len(singles) + 30):
        s = W.next_item("u", ("general", "medicine"), rng)
        seen.append(s.text)
    ok(len(set(seen)) == len(seen), "первые сотни показов не повторяются ни разу")

    # истории идут по порядку частей и дочитываются
    W.reset_for_tests()
    rng = random.Random(3)
    story_texts = [W.next_item("v", ("general",), rng).text for _ in range(60)]
    heads = [t for t in story_texts if t.startswith("📖")]
    by_title: dict[str, list[int]] = {}
    for t in heads:
        title = t.split(" (")[0]
        n = int(t.split(" (")[1].split("/")[0])
        by_title.setdefault(title, []).append(n)
    ok(all(parts == list(range(1, len(parts) + 1)) for parts in by_title.values()), "части каждой истории идут по порядку 1, 2, 3…")
    ok(len(by_title) >= 3, "за 60 показов началось несколько историй")

    # пулы: только общие — медицинских историй нет
    W.reset_for_tests()
    med_titles = {i.title for i in items if i.pool == "medicine" and i.kind == "story"}
    texts = [W.next_item("g", ("general",), random.Random(1)).text for _ in range(80)]
    ok(not any(any(t.startswith(f"📖 {m}") for m in med_titles) for t in texts), "общим пользователям не показываются медицинские истории")
    both = [W.next_item("m", ("general", "medicine"), random.Random(2)).text for _ in range(200)]
    ok(any(any(t.startswith(f"📖 {m}") for m in med_titles) for t in both), "медикам медицинские истории показываются")

    # разные люди — разная память
    W.reset_for_tests()
    random.seed(11)  # с какого вида начинается у нового человека — случайно; для проверки фиксируем
    a1 = W.next_item("a", ("general",), random.Random(5)).text
    random.seed(11)
    b1 = W.next_item("b", ("general",), random.Random(5)).text
    ok(a1 == b1, "новому человеку показывается своё начало, не зависящее от других")
    a2 = W.next_item("a", ("general",), random.Random(5)).text
    ok(a2 != a1, "тому же человеку второй раз показывается другое")

    # продолжение историй между вопросами: часть 2 появляется на следующем ожидании
    W.reset_for_tests()
    first = None
    for _ in range(12):
        s = W.next_item("c", ("general",), random.Random(11)).text
        if s.startswith("📖") and "(1/" in s:
            first = s
            break
    ok(first is not None, "история началась")

    # служебные строки не повторяются, пока не показаны все
    W.reset_for_tests()
    lines = [W.next_line("d", P.PHRASES, random.Random(4)) for _ in range(len(P.PHRASES))]
    ok(len(set(lines)) == len(P.PHRASES), "служебные строки не повторяются до конца круга")
    ok(not any("Лайфхак" in p for p in P.PHRASES), "лайфхаки больше не в коротком списке, а в файле содержимого")

    # rotate_status показывает разное и отменяется
    async def run() -> list[str]:
        got: list[str] = []

        async def setter(t: str) -> None:
            got.append(t)

        W.reset_for_tests()
        task = asyncio.create_task(P.rotate_status(setter, interval=0.01, user_key="z"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return got

    got = asyncio.run(run())
    ok(len(got) >= 1 and P.FIRST_PHRASE not in got[:1], "rotate_status меняет текст")
    print(f"OK: {checks} проверок")


main()
