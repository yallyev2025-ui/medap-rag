"""Режим «Экзамен» в Telegram (MedAP, этап 5): чистая логика без сети, базы и моделей.

Список вопросов студента, очередь «Спроси меня» (сначала ещё не заданные по порядку, потом те, что просил повторить),
обрезка длинных вопросов, подсказки для кнопок.
"""

from __future__ import annotations

import json
import re

MAX_QUESTIONS = 300
MAX_QUESTION_CHARS = 400
MIN_QUESTION_CHARS = 5

_NUMBER = re.compile(r"^\s*(?:\(?\d{1,3}[\).:\-–—]|№\s*\d{1,3}[\).:\-–—]?)\s*")
_BULLET = re.compile(r"^\s*[-•*·▪●]\s+")
_SPACES = re.compile(r"\s+")


def clean_question(line: str) -> str:
    """Без номера в начале и маркеров, с одинарными пробелами."""
    text = _NUMBER.sub("", line)
    text = _BULLET.sub("", text)
    return _SPACES.sub(" ", text).strip()


def parse_questions(raw: str | list[str]) -> list[str]:
    """Список вопросов из текста (каждый с новой строки) или из готовых строк.

    Номера и маркеры убираются, пустые и слишком короткие строки пропускаются, повторы склеиваются без учёта регистра,
    длинная строка обрезается. Многострочный вопрос с отступом (строка продолжения без номера, когда вопросы пронумерованы)
    приклеивается к предыдущему.
    """
    lines = raw.splitlines() if isinstance(raw, str) else list(raw)
    numbered = sum(1 for ln in lines if _NUMBER.match(ln)) >= max(2, len(lines) // 3)
    out: list[str] = []
    for ln in lines:
        if not ln.strip():
            continue
        starts_new = bool(_NUMBER.match(ln)) or bool(_BULLET.match(ln)) or not numbered
        text = clean_question(ln)
        if not text:
            continue
        if numbered and not starts_new and out:
            out[-1] = f"{out[-1]} {text}"
        else:
            out.append(text)
    seen: set[str] = set()
    result: list[str] = []
    for q in out:
        q = q[:MAX_QUESTION_CHARS].strip()
        key = q.lower()
        if len(q) < MIN_QUESTION_CHARS or key in seen:
            continue
        seen.add(key)
        result.append(q)
        if len(result) >= MAX_QUESTIONS:
            break
    return result


def load_list(questions_json: str | None) -> list[str]:
    try:
        data = json.loads(questions_json or "[]")
    except ValueError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def load_weak(weak_json: str | None, total: int) -> list[int]:
    try:
        data = json.loads(weak_json or "[]")
    except ValueError:
        return []
    return sorted({int(x) for x in data if isinstance(x, int) and 0 <= x < total}) if isinstance(data, list) else []


def next_index(total: int, position: int, weak: list[int]) -> tuple[int | None, int]:
    """Какой вопрос задавать теперь: (индекс или None, если список пуст; новая позиция).

    Сначала идут ещё не заданные по порядку; когда они кончились, по кругу задаются те, что студент просил повторить.
    Если и их нет — список пройден целиком (None)."""
    if total <= 0:
        return None, 0
    if position < total:
        return position, position + 1
    pending = [i for i in weak if 0 <= i < total]
    if pending:
        return pending[0], position
    return None, position


def progress_line(index: int, total: int, weak_count: int) -> str:
    base = f"Вопрос {index + 1} из {total}"
    return f"{base} · на повтор: {weak_count}" if weak_count else base


def question_by_number(questions: list[str], text: str) -> tuple[int, str] | None:
    """«7» → (6, текст седьмого вопроса); «№7», «7.» тоже; не число — None."""
    m = re.fullmatch(r"\s*(?:№\s*)?(\d{1,3})\s*[.)]?\s*", text)
    if not m:
        return None
    n = int(m.group(1))
    if 1 <= n <= len(questions):
        return n - 1, questions[n - 1]
    return None
