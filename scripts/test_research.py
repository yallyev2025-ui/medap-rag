"""План исследования и проверка покрытия без модели и без сети.

python -m scripts.test_research
"""

import types

# Планировщик тянет за собой провайдера моделей и реестр, а тесты здесь —
# про чистую логику (когда нужен план, что считается пробелом). torch нужен
# только эмбеддингам поиска, которого в этих тестах нет.
try:
    import torch  # noqa: F401
except ImportError:
    sys_modules = __import__("sys").modules
    sys_modules.setdefault("rag.embedder", types.SimpleNamespace(embed_query=None, embed_passages=None, limited_threads=None))

from app.research.coverage import Gap, assess_coverage, gap_queries
from app.workflows.dialog import strip_missing_summary
from app.research.planner import (
    KIND_COMPARE,
    KIND_CONSPECT,
    KIND_LIST,
    KIND_SIMPLE,
    Aspect,
    ResearchPlan,
    fallback_plan,
    guess_kind,
    looks_complex,
    normalize_question,
    parse_plan,
    plan_from_dict,
    plan_to_dict,
)
from app.research.text import has_partial_match, stems


class Chunk:
    """Кусок учебника в том виде, в каком его видит проверка покрытия."""

    def __init__(self, content: str, chunk_id: int = 1, book_id: int = 1):
        self.content = content
        self.id = chunk_id
        self.book_id = book_id


def plan_with(aspects, goal: str = "Таламус: все ядра") -> ResearchPlan:
    return ResearchPlan(
        kind=KIND_LIST,
        goal=goal,
        aspects=tuple(aspects),
        queries=(goal,),
        deep=True,
    )


# --- Основы слов ---------------------------------------------------------------


def test_stems_merge_word_forms_and_drop_service_words() -> None:
    # Скелет слова: «ядра», «ядер», «ядро» — одно и то же слово в разных формах.
    assert stems("ядра ядер ядро") == {"ядр"}
    assert "что" not in stems("что такое ядро")
    assert has_partial_match("ядр", stems("ядра таламуса"))


# --- Сложность вопроса ---------------------------------------------------------


def test_simple_question_is_not_complex() -> None:
    assert not looks_complex("что такое ацидоз")
    assert not looks_complex("дай определение")
    assert not looks_complex("")


def test_enumeration_markers_make_question_complex() -> None:
    assert looks_complex("Перечисли все ядра таламуса и функции каждого")
    assert looks_complex("Классификация ХОБЛ")
    assert looks_complex("Сравни два препарата")
    assert looks_complex("Сделай полный конспект по ХОБЛ")


def test_multi_part_question_is_complex_even_without_markers() -> None:
    assert looks_complex("Таламус: группы ядер, связи с корой")


def test_kind_by_formulation() -> None:
    assert guess_kind("Сделай конспект по ХОБЛ") == KIND_CONSPECT
    assert guess_kind("Сравни амоксициллин и азитромицин") == KIND_COMPARE
    assert guess_kind("Классификация пневмоний") == KIND_LIST
    assert guess_kind("Что такое ацидоз") == KIND_SIMPLE


# --- Запасной план -------------------------------------------------------------


def test_fallback_plan_splits_composite_question_into_queries() -> None:
    plan = fallback_plan("Таламус: все ядра, группы ядер, функции каждого ядра")
    assert plan.origin == "code"
    assert plan.deep is True
    assert len(plan.queries) >= 2, plan.queries
    assert any("ядер" in q or "ядр" in q for q in plan.queries)


def test_fallback_plan_for_simple_question_keeps_single_query() -> None:
    plan = fallback_plan("что такое ацидоз")
    assert plan.deep is False
    assert plan.queries == ("что такое ацидоз",)
    assert plan.aspects == ()


# --- Разбор ответа модели -------------------------------------------------------


def test_parse_plan_reads_must_and_nice_with_weights() -> None:
    plan = parse_plan(
        {
            "kind": KIND_LIST,
            "goal": "Полный разбор ядер таламуса",
            "must": ["классификация ядер по функции", "функции каждой группы"],
            "nice": ["кровоснабжение"],
            "queries": ["ядра таламуса классификация", "thalamic nuclei functions"],
        },
        "Таламус: все ядра",
    )
    assert plan is not None
    assert plan.origin == "model"
    assert [a.weight for a in plan.aspects] == [2, 2, 1]
    assert plan.queries[1] == "thalamic nuclei functions"


def test_parse_plan_rejects_empty_or_broken_answer() -> None:
    assert parse_plan({}, "вопрос") is None
    assert parse_plan({"queries": []}, "вопрос") is None
    assert parse_plan({"queries": ["единственный запрос"]}, "вопрос") is not None


def test_parse_plan_unknown_kind_falls_back_to_guess() -> None:
    plan = parse_plan({"kind": "невидано", "queries": ["классификация ХОБЛ"]}, "Классификация ХОБЛ")
    assert plan is not None and plan.kind == KIND_LIST


# --- Покрытие ------------------------------------------------------------------


def test_aspect_covered_when_words_meet_in_one_chunk() -> None:
    plan = plan_with([Aspect("функции ядер", weight=2)])
    chunks = [Chunk("Функции неспецифических ядер — активация коры.")]
    coverage = assess_coverage(plan.aspects, chunks)
    assert coverage.full, coverage.gaps
    assert coverage.score == 1.0


def test_aspect_is_gap_when_words_scattered_across_chunks() -> None:
    plan = plan_with([Aspect("функции ядер", weight=2)])
    chunks = [
        Chunk("Ядра таламуса делятся на группы.", chunk_id=1),
        Chunk("Функции коры больших полушарий.", chunk_id=2),
    ]
    coverage = assess_coverage(plan.aspects, chunks)
    assert not coverage.full
    assert coverage.gaps[0].aspect.title == "функции ядер"


def test_must_gap_sorts_before_nice_gap() -> None:
    plan = plan_with(
        [
            Aspect("кровоснабжение", weight=1),
            Aspect("классификация ядер", weight=2),
        ]
    )
    coverage = assess_coverage(plan.aspects, [Chunk("Таламус — часть промежуточного мозга.")])
    assert [g.aspect.weight for g in coverage.gaps] == [2, 1]


def test_score_falls_by_weight_not_by_count() -> None:
    plan = plan_with([Aspect("классификация ядер", weight=2), Aspect("кровоснабжение", weight=1)])
    coverage = assess_coverage(plan.aspects, [Chunk("Классификация ядер таламуса: специфические и неспецифические.")])
    assert coverage.score == 1.0 - 1 / 3


def test_empty_aspects_is_full_coverage() -> None:
    coverage = assess_coverage((), [Chunk("что угодно")])
    assert coverage.full and coverage.score == 1.0


# --- Запросы добора ------------------------------------------------------------


def test_gap_queries_add_topic_and_skip_closed_aspects() -> None:
    plan = plan_with([Aspect("функции ядер", weight=2)], goal="Таламус: все ядра")
    coverage = assess_coverage(plan.aspects, [Chunk("Таламус — часть промежуточного мозга.")])
    queries = gap_queries(plan, coverage)
    assert queries == ["Таламус функции ядер"]


def test_gap_queries_empty_when_nothing_missing() -> None:
    plan = plan_with([Aspect("функции ядер", weight=2)])
    coverage = assess_coverage(plan.aspects, [Chunk("Функции ядер таламуса")])
    assert gap_queries(plan, coverage) == []


def test_gap_queries_respect_limit() -> None:
    plan = plan_with([Aspect(f"пункт {i}", weight=2) for i in range(6)], goal="Тема")
    coverage = assess_coverage(plan.aspects, [Chunk("ничего по теме")])
    assert len(gap_queries(plan, coverage, limit=2)) == 2


# --- Кеш планов: ключ и сериализация -------------------------------------------

def test_normalize_question_ignores_case_spaces_and_tail() -> None:
    assert normalize_question("  Таламус:  все ЯДРА! ") == "таламус: все ядра"
    assert normalize_question("Классификация ХОБЛ?") == normalize_question("классификация хобл")


def test_plan_survives_round_trip() -> None:
    plan = plan_with([Aspect("функции ядер", weight=2), Aspect("кровоснабжение", weight=1)])
    restored = plan_from_dict(plan_to_dict(plan))
    assert restored is not None
    assert restored.origin == "cache"
    assert [a.title for a in restored.aspects] == ["функции ядер", "кровоснабжение"]
    assert [a.weight for a in restored.aspects] == [2, 1]
    assert restored.queries == plan.queries


def test_broken_cache_entry_is_not_used() -> None:
    assert plan_from_dict({}) is None
    assert plan_from_dict({"queries": []}) is None
    assert plan_from_dict({"queries": ["ядра таламуса"]}) is not None


def test_old_missing_tail_is_removed() -> None:
    text = "Ответ по теме.\n\nВ материалах не нашлось: причины: неврологические; патогенез: механизм."
    assert strip_missing_summary(text) == "Ответ по теме."


def test_answer_without_tail_is_untouched() -> None:
    text = "Ответ по теме.\n\nВторой абзац с нормальным текстом."
    assert strip_missing_summary(text) == text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)