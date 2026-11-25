"""Чистая часть материала КР: id фрагментов, выбор рекомендации и отбор кусков.

Без базы, сети и тяжёлых зависимостей — чтобы тесты разбора не поднимали torch.
Сборка в `ChunkResult` и загрузка страницы живут в `clinreky_answer.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# Один документ = 100 000 синтетических id. Разделов и рекомендаций в КР
# заведомо меньше (у J44 — 113), так что коллизий между документами нет.
DOCUMENT_STRIDE = 100_000
# Раздел рекомендации больше не режем на 4 тыс. знаков: владелец 26.11.2026
# попросил отдавать нужный раздел целиком, а не первую треть.
MAX_CONTENT = 14_000

# «По всем пунктам»: определение, эпидемиология, классификация, диагностика,
# лечение, наблюдение, реабилитация, профилактика, приложения. Это не вопрос
# об одном разделе — это разбор болезни целиком.
_WHOLE = (
    "по всем пунктам",
    "по всем разделам",
    "полный разбор",
    "разбери всё",
    "разбери все",
    "всё о болезни",
    "все о болезни",
    "полностью по рекомендации",
    "весь документ",
    "все разделы",
)


def asks_whole_guideline(question: str) -> bool:
    """Просит ли человек разбор болезни по всем пунктам рекомендации."""
    text = " ".join(question.lower().split())
    return any(marker in text for marker in _WHOLE)


@dataclass(frozen=True)
class GuidelineCandidate:
    book_id: int
    title: str
    external_ref: str
    subject: str
    author: str
    """Адрес страницы на clinreky.ru. Заполнен для каталога без загруженной книги."""
    url: str | None = None


def evidence_id(document_id: int, index: int) -> int:
    """Отрицательный id фрагмента разобранного документа (раздел или рекомендация)."""
    return -((int(document_id) * DOCUMENT_STRIDE) + int(index) + 1)


def decode_evidence_id(value: int) -> tuple[int, int] | None:
    """Разобрать отрицательный id обратно в (document_id, index)."""
    if value >= 0:
        return None
    number = -int(value) - 1
    document_id, index = divmod(number, DOCUMENT_STRIDE)
    if document_id <= 0:
        return None

    return document_id, index


_WORD = re.compile(r"[а-яёa-z0-9]{4,}", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    """Значимые слова для простого сравнения без морфологии.

    Берём первые пять букв: «лечение» и «лечении» совпадут, «ХОБЛ» останется
    собой. Этого достаточно, чтобы выбрать рекомендацию по названию, а не
    угадывать её моделью.
    """
    return {match.group(0).lower()[:5] for match in _WORD.finditer(text or "")}


def _overlap(question: str, text: str) -> int:
    return len(_tokens(question) & _tokens(text))


def _acronym(title: str) -> str:
    """Аббревиатура названия: «Хроническая обструктивная болезнь лёгких» → ХОБЛ."""
    skip = {"для", "при", "или", "под", "над", "без", "про", "как"}
    words = [word for word in re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", title) if word.lower() not in skip]

    return "".join(word[0] for word in words).upper()


def _acronym_bonus(question: str, title: str) -> int:
    """Вопрос назвал аббревиатуру (ХОБЛ, АГ, ОКС) — это сильнее общего слова."""
    acronym = _acronym(title)
    if len(acronym) < 2:
        return 0
    for token in re.findall(r"[А-ЯЁA-Z]{2,6}", question):
        if acronym.startswith(token) or token.startswith(acronym):
            return 2

    return 0


def pick_candidate(question: str, candidates: Sequence[GuidelineCandidate]) -> GuidelineCandidate | None:
    """Самая близкая по названию рекомендация; без пересечения слов — None."""
    best: tuple[int, int, GuidelineCandidate] | None = None
    for candidate in candidates:
        score = _overlap(question, candidate.title) + _acronym_bonus(question, candidate.title)
        # При равенстве выигрывает более конкретное (длинное) название.
        key = (score, len(candidate.title))
        if score > 0 and (best is None or key > (best[0], best[1])):
            best = (score, len(candidate.title), candidate)

    return best[2] if best is not None else None


def page_fragments(
    page: Any,
    *,
    document_id: int,
    book_id: int,
    subject: str,
    author: str,
) -> list[dict[str, Any]]:
    """Разделы и рекомендации страницы как простые фрагменты с отрицательными id."""
    result: list[dict[str, Any]] = []
    index = 0
    for section in page.sections or []:
        text = str(section.get("text") or "").strip()
        title = str(section.get("title") or "").strip()
        if not text or not title:
            continue
        result.append(
            {
                "id": evidence_id(document_id, index),
                "content": text[:MAX_CONTENT],
                "subject": subject,
                "author": author,
                "title": page.title,
                "section": title,
                "book_id": book_id,
                "authority_level": "clinical_guideline",
                "verification_status": "VERIFIED",
            }
        )
        index += 1
    for number, recommendation in enumerate(page.recommendations or [], start=1):
        text = str(getattr(recommendation, "text", "") or "").strip()
        if len(text) < 20:
            continue
        level = getattr(recommendation, "level", None)
        topic = str(getattr(recommendation, "section", "") or "").strip()
        section = topic or f"Рекомендация {number}"
        if level:
            section = f"{section} ({level})"
        result.append(
            {
                "id": evidence_id(document_id, index),
                "content": text[:MAX_CONTENT],
                "subject": subject,
                "author": author,
                "title": page.title,
                "section": section,
                "book_id": book_id,
                "authority_level": "clinical_guideline",
                "verification_status": "VERIFIED",
            }
        )
        index += 1

    return result


def select_whole_section(
    fragments: list[dict[str, Any]],
    question: str,
    *,
    max_chars: int = 64_000,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Нужный раздел целиком, а не четыре самых похожих куска.

    Сначала находим раздел, чьё название лучше всего отвечает вопросу
    («лечение», «диагностика»), и берём ВСЕ его фрагменты: текст раздела и
    рекомендации с этой пометкой. Если места осталось — добавляем соседние
    разделы по близости. Пусто — обычный отбор.
    """
    # Базовые разделы — не отдельные рекомендации: рекомендации с пометкой
    # «Лечение (A)» должны попасть в раздел «Лечение», а не жить сами по себе.
    bases: list[tuple[int, str]] = []
    seen_titles: set[str] = set()
    for fragment in fragments:
        title = str(fragment.get("section") or "")
        if not title or title.startswith("Рекомендация") or title.lower().startswith(("приложение", "шкала")):
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)
        bases.append((_overlap(question, title) + 1, title))
    bases.sort(key=lambda pair: pair[0], reverse=True)

    picked: list[dict[str, Any]] = []
    total = 0
    taken: set[int] = set()

    for _score, title in bases[:1]:
        group = [
            (i, f)
            for i, f in enumerate(fragments)
            if i not in taken and (str(f.get("section") or "") == title or str(f.get("section") or "").startswith(title))
        ]
        group_size = sum(len(str(f.get("content") or "")) for _, f in group)
        if group and (not picked or total + group_size <= max_chars):
            for i, fragment in group:
                taken.add(i)
                picked.append(fragment)
                total += len(str(fragment.get("content") or ""))

    if not picked:
        return select_fragments(fragments, question, limit, max_chars)

    # Добираем оставшееся близкими фрагментами, если раздел оказался короче лимита
    if total < max_chars // 2:
        rest = [f for i, f in enumerate(fragments) if i not in taken]
        picked.extend(select_fragments(rest, question, limit - len(picked), max_chars - total))
    return picked[:limit]


def select_all_fragments(
    fragments: list[dict[str, Any]],
    *,
    max_chars: int = 80_000,
    limit: int = 120,
) -> list[dict[str, Any]]:
    """Все разделы и рекомендации по порядку — для разбора болезни «по всем пунктам»."""
    picked: list[dict[str, Any]] = []
    total = 0
    for fragment in fragments:
        size = len(str(fragment.get("content") or ""))
        if picked and total + size > max_chars:
            break
        picked.append(fragment)
        total += size
        if len(picked) >= limit:
            break
    return picked


def select_fragments(
    fragments: Iterable[dict[str, Any]],
    question: str,
    limit: int,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Самые близкие к вопросу фрагменты; разделы получают небольшой приоритет.

    Приложения со шкалами и алгоритмами не должны вытеснять основной раздел
    «Диагностика» или «Лечение» только потому, что в них много знакомых слов:
    за совпадение с названием раздела даём отдельный бонус, а приложение
    слегка понижаем.
    """
    scored = []
    for fragment in fragments:
        section = str(fragment.get("section") or "")
        if section.startswith("Рекомендация"):
            section_bonus = 0
        else:
            section_bonus = 1
            if section and _overlap(question, section) > 0:
                section_bonus += 2
            if section.lower().startswith(("приложение", "шкала")):
                section_bonus -= 1
        scored.append((_overlap(question, f"{section} {fragment.get('content') or ''}") + section_bonus, fragment))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for _score, fragment in scored:
        key = str(fragment.get("content") or "")[:200].lower()
        if key in seen:
            continue
        seen.add(key)
        size = len(str(fragment.get("content") or ""))
        if max_chars is not None and picked and total + size > max_chars:
            break
        picked.append(fragment)
        total += size
        if len(picked) >= limit:
            break

    return picked
