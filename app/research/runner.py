"""Ход исследования: план → поиск по частям → проверка полноты → добор → материал для ответа.

Это не отдельный сервис и не «агент»: один вход `research_material`, который
заменяет одиночный поиск там, где вопрос составной. Всё остальное (генерация,
проверка заземления, цитаты, кеш ответов) остаётся прежним — модуль отдаёт
найденный материал и рассказ о том, что делал.

Пределы зашиты в код, а не в промпт:
- первый круг всегда один реранк (самая дорогая часть поиска);
- второй круг — только при пробелах и только один раз;
- планировщик и уточняющая проверка вызываются лишь у сложных вопросов.
"""

from dataclasses import dataclass

from app.research.coverage import Coverage, assess_coverage, confirm_gaps, gap_queries, rounded
from app.research.store import plan_with_cache
from app.research.planner import ResearchPlan
from config import settings
from rag.generator import relevant_chunks
from rag.retriever import ChunkResult, retrieve, retrieve_multi


@dataclass(frozen=True)
class ResearchOutcome:
    """Что было сделано и что из этого вышло. Идёт в диагностику и в рассказ студенту."""

    plan: ResearchPlan
    coverage: Coverage
    rounds: int
    queries: tuple[str, ...]
    steps: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "kind": self.plan.kind,
            "origin": self.plan.origin,
            "goal": self.plan.goal,
            "deep": self.plan.deep,
            "queries": list(self.queries),
            "aspects": [{"title": a.title, "weight": a.weight} for a in self.plan.aspects],
            "covered": list(self.coverage.covered),
            "gaps": [
                {"title": g.aspect.title, "weight": g.aspect.weight, "share": round(g.share, 2)}
                for g in self.coverage.gaps
            ],
            "score": rounded(self.coverage.score),
            "rounds": self.rounds,
            "steps": list(self.steps),
        }


def _uniq(chunks: list[ChunkResult]) -> list[ChunkResult]:
    seen: set[int] = set()
    out: list[ChunkResult] = []
    for chunk in chunks:
        if chunk.id in seen:
            continue
        seen.add(chunk.id)
        out.append(chunk)
    return out


def _sources(chunks: list[ChunkResult]) -> int:
    return len({chunk.book_id for chunk in chunks})


async def research_material(
    question: str,
    *,
    source_type: str,
    subject: str | None,
    top_k: int,
    turns: list[tuple[str, str]] | None = None,
    candidates: int | None = None,
) -> tuple[list[ChunkResult], list[ChunkResult], ResearchOutcome]:
    """Ищет материал для ответа с проверкой полноты.

    Возвращает `(все найденные куски, релевантные куски, ход исследования)`.
    Никогда не бросает исключений из-за плана: сбой планировщика или проверки
    означает «ищи как раньше», а не отказ студенту.
    """
    plan = await plan_with_cache(question, turns)
    steps: list[str] = []

    if plan.deep and plan.aspects:
        must = len(plan.must)
        steps.append(f"Разобрал вопрос: пунктов {len(plan.aspects)}, обязательных {must}")
    elif plan.deep:
        steps.append("Вопрос составной — ищу по частям")
    else:
        steps.append("Вопрос простой — ищу напрямую")

    queries = list(plan.queries) or [question]
    extra_args = {"candidates": candidates} if candidates else {}
    if len(queries) > 1:
        chunks = await retrieve_multi(
            question,
            queries,
            source_type=source_type,
            subject=subject,
            top_k=top_k,
            **extra_args,
        )
        steps.append(f"Искал по {len(queries)} формулировкам")
    else:
        chunks = await retrieve(
            question,
            source_type=source_type,
            subject=subject,
            top_k=top_k,
            spread=False,
            **extra_args,
        )
        steps.append("Искал по учебникам")

    relevant = relevant_chunks(chunks)
    rounds = 1
    steps.append(f"Нашёл фрагментов: {len(chunks)}, подходящих: {len(relevant)}")
    coverage = assess_coverage(plan.aspects, relevant)

    # Второй круг: только обязательные пробелы, только один раз, только у сложных.
    if plan.deep and not coverage.full and relevant:
        if plan.aspects:
            steps.append(f"Проверил полноту: покрыто {len(coverage.covered)} из {len(plan.aspects)}")
            coverage = await confirm_gaps(plan, coverage, relevant)
        open_must = [g for g in coverage.gaps if g.aspect.weight >= 2]
        extra_queries = gap_queries(plan, coverage, limit=max(0, settings.RESEARCH_EXTRA_QUERIES))
        if extra_queries and (open_must or not plan.aspects):
            more = await retrieve_multi(
                question,
                extra_queries,
                source_type=source_type,
                subject=subject,
                top_k=top_k,
                **extra_args,
            )
            before = {c.id for c in relevant}
            merged = _uniq([*chunks, *more])
            new_relevant = relevant_chunks(merged)
            if any(c.id not in before for c in new_relevant):
                chunks, relevant = merged, new_relevant
                rounds = 2
                steps.append("Добрал по незакрытым пунктам: " + "; ".join(extra_queries[:3]))
                coverage = assess_coverage(plan.aspects, relevant)
            if plan.aspects:
                steps.append(f"После добора покрыто {len(coverage.covered)} из {len(plan.aspects)}")

    if plan.aspects:
        steps.append(f"Итог проверки: покрыто {len(coverage.covered)} из {len(plan.aspects)}")
    steps.append(f"Источников: {_sources(relevant) or _sources(chunks)}")

    outcome = ResearchOutcome(
        plan=plan,
        coverage=coverage,
        rounds=rounds,
        queries=tuple(queries),
        steps=tuple(steps),
    )
    return chunks, relevant, outcome


def missing_summary(outcome: ResearchOutcome) -> str:
    """Чего не хватило в материале — одной строкой для пометки под ответом.

    Пустая строка, если пробелов нет: лишние оговорки не нужны.
    """
    gaps = tuple(g for g in outcome.coverage.gaps if g.aspect.weight >= 2)
    if not gaps:
        return ""
    titles = [g.aspect.title for g in gaps]
    return "В материалах не нашлось: " + "; ".join(titles[:4])


def aspect_titles(outcome: ResearchOutcome) -> tuple[str, ...]:
    """Обязательные пункты — их передаём в генерацию как чек-лист."""
    return tuple(a.title for a in outcome.plan.aspects)