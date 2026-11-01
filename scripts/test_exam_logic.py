"""Режим «Экзамен» (MedAP, этап 5): python -m scripts.test_exam_logic (без сети и базы)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.exam.logic import MAX_QUESTIONS, load_list, load_weak, next_index, parse_questions, progress_line, question_by_number  # noqa: E402

checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"НЕ ПРОШЛО: {label}")


# нумерация и маркеры убираются, повторы склеиваются, короткое пропускается
raw = "1. Определение воспаления\n2) Стадии воспаления\n3. Определение воспаления\n- Тромбоз и его исходы\n6. да\n\n№5 Эмболия: виды"
q = parse_questions(raw)
check(q == ["Определение воспаления", "Стадии воспаления", "Тромбоз и его исходы", "Эмболия: виды"], f"разбор: {q}")

# перенос строки внутри пронумерованного вопроса приклеивается к предыдущему
wrapped = "1. Этиология и патогенез\nострого воспаления\n2. Хроническое воспаление\n3. Исходы воспаления"
check(parse_questions(wrapped) == ["Этиология и патогенез острого воспаления", "Хроническое воспаление", "Исходы воспаления"], "перенос строки")

# без номеров — каждая строка вопрос
check(parse_questions("Воспаление\nТромбоз\nЭмболия") == ["Воспаление", "Тромбоз", "Эмболия"], "без номеров")

# потолок количества и длины
many = parse_questions("\n".join(f"{i}. Вопрос номер {i} по теме" for i in range(1, 400)))
check(len(many) == MAX_QUESTIONS, "потолок количества")
check(len(parse_questions("а" * 1000)[0]) == 400, "потолок длины")

# очередь: сначала по порядку, потом слабые, потом конец
pos = 0
weak: list[int] = []
order = []
for _ in range(3):
    idx, pos = next_index(3, pos, weak)
    order.append(idx)
check(order == [0, 1, 2] and pos == 3, f"по порядку: {order}")
weak = [1]
idx, pos = next_index(3, pos, weak)
check(idx == 1, "после списка — слабые")
idx, pos = next_index(3, pos, [])
check(idx is None, "нет слабых — список пройден")
check(next_index(0, 0, [])[0] is None, "пустой список")

check(load_list("не json") == [] and load_list('["а","б"]') == ["а", "б"], "чтение списка")
check(load_weak("[0, 2, 9, \"x\"]", 3) == [0, 2], "слабые только в пределах списка")
check(question_by_number(["а", "бб", "вв"], "№2") == (1, "бб") and question_by_number(["а"], "5") is None and question_by_number(["а"], "про воспаление") is None, "номер вопроса")
check(progress_line(2, 10, 0) == "Вопрос 3 из 10" and "на повтор: 2" in progress_line(2, 10, 2), "строка прогресса")

print(f"OK: {checks} проверок")
