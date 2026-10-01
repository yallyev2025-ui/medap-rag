"""Диалоговый слой Telegram/админки (батч 20): продолжение по «да», сжатая строка
источников, условный пересказ запроса по истории.

Всё детерминированно — ни одного нового вызова LLM. Вопрос-приглашение в конце ответа
(«➡️ Хочешь, расскажу дальше про X и Y?») пишет модель по инструкции из
rag/generator.py; здесь он распознаётся и используется для продолжения по «да».
"""

import re

from app.evidence.citations import _PAGE

DIALOG_CHANNELS = frozenset({"telegram", "admin"})

OFFER_MARK = "➡️"
CONTINUATION_LINE = "➡️ Продолжу с этого места — напиши «да»."
ASK_TOPIC_TEXT = "Скажи, какую тему разобрать — раскрою её по материалам."

AFFIRM = re.compile(
    r"^\s*(?:да(?:[,!\s]+(?:пожалуйста|давай|расскажи|дальше|продолжай))?|давай(?:\s+дальше)?|"
    r"ага|угу|ок(?:ей)?|конечно|продолжай|продолжи|дальше|ещё|еще|расскажи|хочу|👍|\+)[\s!.,)]*$",
    re.IGNORECASE,
)

_ANAPHORA = re.compile(
    r"\b(это|этот|эта|эти|этого|этом|этой|он|она|оно|они|его|её|их|ним|ней|тот|такой|"
    r"а\s+как|а\s+какие|а\s+какой|а\s+что|ещё|еще)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^\s*\*\*(.+?)\*\*\s*$")
_SOURCE_BRACKET = re.compile(r"\[[^\[\]]*стр\.?[^\[\]]*\]", re.IGNORECASE)


def needs_rewrite(question: str) -> bool:
    """Переписывать запрос по истории имеет смысл только для короткого вопроса или
    вопроса с отсылкой («а дозы?», «как он действует»). Самостоятельный длинный
    вопрос идёт в поиск как есть — без лишнего вызова модели."""
    return len(question.split()) <= 5 or bool(_ANAPHORA.search(question))


def _offer_line(answer: str) -> str | None:
    for line in reversed(answer.splitlines()):
        if OFFER_MARK in line:
            return line
    return None


def extract_offer_topics(answer: str) -> str | None:
    line = _offer_line(answer)
    if line is None or "Продолжу с этого места" in line:
        return None
    match = re.search(r"про\s+(.+?)\s*\?", line)
    if match:
        return match.group(1).strip()
    text = line.replace(OFFER_MARK, "").strip()
    return text or None


def extract_headings(answer: str) -> list[str]:
    return [m.group(1).strip() for line in answer.splitlines() if (m := _HEADING.match(line))]


def was_truncated(answer: str) -> bool:
    return "Продолжу с этого места" in answer


def build_continuation(root_question: str, last_answer: str, covered: list[str]) -> tuple[str, str]:
    """(вопрос для генерации, поисковый запрос) для ответа «да». `root_question` — исходный
    вопрос темы (не само «да»), `covered` — уже раскрытые разделы за всю цепочку."""
    if was_truncated(last_answer):
        body = last_answer.split(OFFER_MARK)[0].rstrip()
        tail = body[-300:]
        return (
            f"Продолжи ответ на вопрос «{root_question}» ровно с того места, где он оборвался, "
            f"не повторяя уже сказанное. Конец предыдущей части: «…{tail}»",
            root_question,
        )

    topics = extract_offer_topics(last_answer)
    what = f"раскрой {topics}" if topics else "раскрой следующие крупные разделы темы"
    done = f" Уже раскрыто (не повторяй): {'; '.join(covered)}." if covered else ""
    question = f"Продолжи разбор темы «{root_question}»: {what}.{done}"
    search = f"{root_question} {topics}".strip() if topics else root_question
    return question, search


def continuation_from_turns(turns: list[tuple[str, str]]) -> tuple[str, str]:
    """Продолжение по цепочке «вопрос → да → да»: корень — последний вопрос, который сам
    не был «да»; уже раскрытые разделы собираются со всех ответов цепочки."""
    root_index = len(turns) - 1
    for i in range(len(turns) - 1, -1, -1):
        if not AFFIRM.match(turns[i][0]):
            root_index = i
            break
    covered: list[str] = []
    for _question, answer in turns[root_index:]:
        covered.extend(extract_headings(answer))
    return build_continuation(turns[root_index][0], turns[-1][1], covered)


def _source_name(citation: dict) -> str:
    title = citation.get("sourceTitle") or ""
    author = (citation.get("author") or "").strip()
    if author and author.lower() not in title.lower():
        return f"{author}, {title}"
    return title


def _merge_pages(pages: list[tuple[int, int]]) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(pages):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in merged)


def compact_sources(citations: list[dict]) -> str:
    """«📚 Источники: Аляутдин, Фармакология — стр. 227–228, 248–255; Харкевич — стр. 140–141».
    Собирается из реальных цитат (то, что модель указала и что совпало с найденными
    фрагментами), а не выдумывается моделью."""
    grouped: dict[str, list[tuple[int, int]]] = {}
    for citation in citations:
        name = _source_name(citation)
        pages = grouped.setdefault(name, [])
        page = citation.get("page")
        if page is not None:
            pages.append((page, citation.get("pageTo") or page))

    parts = []
    for name, pages in grouped.items():
        parts.append(f"{name} — стр. {_merge_pages(pages)}" if pages else name)
    return "📚 Источники: " + "; ".join(parts) if parts else ""


def strip_source_brackets(text: str) -> str:
    """Убирает «[Автор, Название, стр. N]» из текста: строки, состоящие только из таких
    скобок (и слова «Источники»), удаляются, прочие вхождения вырезаются."""
    out: list[str] = []
    for line in text.splitlines():
        if _SOURCE_BRACKET.search(line):
            rest = _SOURCE_BRACKET.sub("", line)
            if re.sub(r"[\W_]+", "", rest).lower() in ("", "источники", "источник"):
                continue
            line = re.sub(r"[ \t]+([,.;:])", r"\1", re.sub(r" {2,}", " ", rest)).rstrip()
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def finalize_dialog_answer(text: str, citations: list[dict], truncated: bool) -> str:
    """Хвост ответа в диалоге: текст → «📚 источники» → «➡️ вопрос-приглашение».
    Обрезанный по лимиту ответ получает «Продолжу с этого места» вместо видимого обрыва."""
    offer = _offer_line(text)
    body_lines = [line for line in text.splitlines() if line != offer]
    body = strip_source_brackets("\n".join(body_lines))

    if truncated and offer is None:
        offer = CONTINUATION_LINE

    parts = [body]
    sources = compact_sources(citations)
    if sources:
        parts.append(sources)
    if offer:
        parts.append(offer.strip())
    return "\n\n".join(p for p in parts if p)
