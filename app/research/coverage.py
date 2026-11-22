"""Проверка покрытия: что из обязательных пунктов плана реально нашлось.

Смысл модуля — не дать большому тексту сойти за полный ответ. Модель может
написать десять абзацев вокруг двух найденных кусков; покрытие считается по
материалу, а не по готовому тексту, и потому не зависит от красноречия модели.

Сначала проверка кодом (бесплатно): для каждого пункта плана ищем в найденных
кусках слова его темы. Не сходится — пункт считается пробелом. Сомнительные
случаи (слово есть, но, возможно, в другом смысле) разбирает модель одним
коротким вызовом — и только там, где это что-то меняет.
"""

import math
from dataclasses import dataclass

from app.research.planner import Aspect, ResearchPlan
from app.research.text import has_partial_match, stems

# Доля слов темы пункта, которая должна найтись, чтобы считать пункт закрытым.
# Обязательный пункт строже: «классификация» не закрывается одним словом
# «функция», встреченным в абзаце.
_NEED_MUST = 0.6
_NEED_NICE = 0.5

# Сколько знаков куска отправляем модели при спорной проверке.
_SNIPPET = 400


@dataclass(frozen=True)
class Gap:
    """Пункт плана, который материал не закрывает."""

    aspect: Aspect
    share: float  # какая доля слов пункта нашлась (0..1)


@dataclass(frozen=True)
class Coverage:
    """Итог проверки: закрытые пункты, пробелы и сводная оценка по весам."""

    covered: tuple[str, ...]
    gaps: tuple[Gap, ...]
    score: float

    @property
    def full(self) -> bool:
        return not self.gaps


def _share(aspect: Aspect, chunk_stems: list[set[str]]) -> float:
    """Лучшая доля слов пункта, найденная в одном куске.

    Считаем по одному куску, а не по объединению всех: иначе «ядра» из одного
    абзаца и «функции» из другого закрывали бы пункт «функции ядер», хотя
    в материале нет ни одного места, где сказано и то и другое.
    """
    need = stems(aspect.title)
    if not need:
        return 1.0
    best = 0.0
    for found in chunk_stems:
        hit = sum(1 for stem in need if has_partial_match(stem, found))
        best = max(best, hit / len(need))
    return best


def assess_coverage(aspects: tuple[Aspect, ...] | list[Aspect], chunks: list) -> Coverage:
    """Бесплатная проверка по материалу. `chunks` — объекты с полем `content`."""
    if not aspects:
        return Coverage(covered=(), gaps=(), score=1.0)

    chunk_stems = [stems(getattr(chunk, "content", "") or "") for chunk in chunks]
    covered: list[str] = []
    gaps: list[Gap] = []

    for aspect in aspects:
        share = _share(aspect, chunk_stems)
        need = _NEED_MUST if aspect.weight >= 2 else _NEED_NICE
        if share + 1e-9 >= need:
            covered.append(aspect.title)
        else:
            gaps.append(Gap(aspect=aspect, share=share))

    total = sum(a.weight for a in aspects)
    open_weight = sum(g.aspect.weight for g in gaps)
    score = 1.0 - (open_weight / total if total else 0.0)
    # Пробелы важнее закрытых пунктов: первым — обязательный, затем по нехватке.
    gaps.sort(key=lambda g: (-g.aspect.weight, g.share))
    return Coverage(covered=tuple(covered), gaps=tuple(gaps), score=max(0.0, min(1.0, score)))


def gap_queries(plan: ResearchPlan, coverage: Coverage, *, limit: int = 4) -> list[str]:
    """Запросы для добора: тема вопроса + название незакрытого пункта.

    Без темы («только функции») добор уходит в случайные учебники, поэтому
    тема вопроса подставляется всегда, когда её видно.
    """
    if coverage.full:
        return []
    base = plan.goal.strip()
    topic = base.split(":")[0].split(",")[0].strip()[:120]
    out: list[str] = []
    for gap in coverage.gaps:
        title = gap.aspect.title.strip()
        query = title if not topic or topic.lower() in title.lower() else f"{topic} {title}"
        query = " ".join(query.split())[:300]
        if query and query.lower() not in {q.lower() for q in out}:
            out.append(query)
        if len(out) >= limit:
            break
    return out


COVERAGE_SYSTEM = """Ты проверяешь, закрывает ли найденный материал каждый пункт плана.
Тебе дают список пунктов и короткие фрагменты найденных кусков.

Для каждого пункта ответь: закрыт ли он — то есть есть ли в фрагментах сведения,
по которым можно раскрыть этот пункт, а не просто знакомое слово.

Не считай пункт закрытым, если фрагмент лишь упоминает слово без сведений.

Верни JSON: {"open": ["пункты, которые НЕ закрыты, дословно из списка"]}
Если закрыто всё — {"open": []}."""


async def confirm_gaps(plan: ResearchPlan, coverage: Coverage, chunks: list) -> Coverage:
    """Уточнить пробелы моделью — один дешёвый вызов, только если пробелы есть.

    Нужен там, где слова пункта в кусках есть, а сведений нет («осложнения» —
    раздел называется иначе). Цена ограничена: список пунктов и по 400 знаков
    с каждого куска, не больше шести кусков. Любая ошибка модели — считаем, что
    код был прав (пробел настоящий): так ответ не станет короче из-за сбоя.
    """
    if coverage.full or len(coverage.gaps) > 4:
        return coverage

    # Импорт внутри функции: проверка кодом работает без провайдера (ею пользуются
    # тесты), а модель нужна только для спорных случаев.
    from app.llm import provider as llm
    from app.llm.task_map import Task

    aspects = [g.aspect.title for g in coverage.gaps]
    snippets = []
    for chunk in chunks[:6]:
        text = " ".join((getattr(chunk, "content", "") or "").split())[:_SNIPPET]
        if text:
            snippets.append(text)
    if not snippets:
        return coverage

    try:
        result = await llm.complete(
            Task.COVERAGE,
            [
                {"role": "system", "content": COVERAGE_SYSTEM},
                {
                    "role": "user",
                    "content": "Пункты плана:\n" + "\n".join(f"- {a}" for a in aspects)
                    + "\n\nФрагменты материала:\n" + "\n---\n".join(snippets),
                },
            ],
            temperature=0.0,
            max_output_tokens=300,
            json_schema={"required": ["open"]},
        )
    except llm.LLMError:
        return coverage

    opened = (result.data or {}).get("open")
    if not isinstance(opened, list):
        return coverage
    open_titles = {str(t).strip().lower() for t in opened if isinstance(t, str)}

    still_open: list[Gap] = []
    newly_covered: list[str] = []
    for gap in coverage.gaps:
        if gap.aspect.title.strip().lower() in open_titles:
            still_open.append(gap)
        else:
            newly_covered.append(gap.aspect.title)

    covered = coverage.covered + tuple(newly_covered)
    total = sum(a.weight for a in plan.aspects) or 1
    open_weight = sum(g.aspect.weight for g in still_open)
    return Coverage(
        covered=covered,
        gaps=tuple(still_open),
        score=max(0.0, min(1.0, 1.0 - open_weight / total)),
    )


def rounded(score: float) -> float:
    return math.floor(score * 100) / 100