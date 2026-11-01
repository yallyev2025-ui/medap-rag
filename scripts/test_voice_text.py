"""Подготовка текста к озвучке: python -m scripts.test_voice_text (без сети и базы)."""

from app.voice.text import apply_stress, expand_spoken, prepare_for_speech, split_for_speech, strip_markup

checks = 0


def ok(cond: bool, what: str) -> None:
    global checks
    assert cond, what
    checks += 1


md = "## Эмболия\n**Эмболия** — закупорка сосуда [1], см. [учебник](https://x.ru/a) и https://pubmed.ncbi.nlm.nih.gov/1.\n- первый пункт\n1. второй\nИсточник: Литвицкий, стр. 38"
clean = strip_markup(md)
ok("##" not in clean and "**" not in clean and "[1]" not in clean and "http" not in clean, "разметка и ссылки убраны")
ok("учебник" in clean and "первый пункт" in clean and "Литвицкий" not in clean, "текст остался, строка источника убрана")
ok(expand_spoken("АД 120/80 мм рт. ст., ЧСС 90 уд./мин.") == "артериальное давление 120/80 миллиметров ртутного столба, частота сердечных сокращений 90 ударов в минуту", "сокращения раскрыты")
ok(expand_spoken("снижение на 5%") == "снижение на 5 процентов", "проценты словами")
ok(apply_stress("Эмболия и тромбоз, некрозом") == "Эмбол+ия и тромб+оз, некр+озом", "ударения, регистр и окончания")
ok(apply_stress("слово", {}) == "слово", "пустой словарь ничего не меняет")
ok("+" not in prepare_for_speech("Эмболия"), "для OpenAI ударения не ставятся")
ok("+" in prepare_for_speech("Эмболия", stress=True), "для Piper ставятся")
long = ("Первое предложение. " * 120).strip()
parts = split_for_speech("Короткий абзац.\n" + long, limit=300)
ok(all(len(p) <= 300 for p in parts) and parts[0] == "Короткий абзац." and len(parts) > 5, "длинный текст режется по предложениям")
ok(split_for_speech("   \n  ") == [], "пустой текст — нет частей")
ok(all(len(p) <= 50 for p in split_for_speech("а" * 400 + " " + "б" * 400, limit=50)), "слово длиннее лимита режется")
print(f"voice_text: {checks} проверок прошло")
