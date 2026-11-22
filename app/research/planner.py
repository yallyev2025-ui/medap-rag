"""Планировщик исследования (этап 1 Mentor 2.0).

Вопрос студента сначала превращается в план: что обязательно раскрыть и какими
словами искать. План нужен по трём причинам:

1. **Полнота.** Одна длинная фраза даёт модели размазанный ответ: на вопрос
   «Таламус: все ядра, группы, функции каждого ядра» одна выдача приносит три
   ядра из двенадцати. Части вопроса — отдельные поисковые фразы.
2. **Проверка.** Без списка обязательных пунктов не с чем сравнивать готовый
   ответ, и «полно» превращается в «модель написала много букв».
3. **Экономия.** Простой вопрос не нуждается в плане и втором круге поиска.
   Планировщик на дешёвой модели вызывается только там, где план окупается.

Модуль держит две половины: **чистые** решения (`looks_complex`,
`fallback_plan` — проверяются тестом без сети и без модели) и **вызов модели**
(`plan_research`), который при любой ошибке откатывается на чистую половину.
Отказ планировщика не должен ронять ответ студенту.
"""

import re
from dataclasses import dataclass

from app.research.text import meaningful_words
from app.workflows.expand import query_variants

# Виды задач. От вида зависит и формат ответа, и нужен ли многошаговый поиск.
KIND_SIMPLE = "simple"      # короткий факт: «что такое ацидоз»
KIND_EXPLAIN = "explain"    # объяснение механизма
KIND_CONSPECT = "conspect"  # полный конспект по теме
KIND_COMPARE = "compare"    # сравнение препаратов/методов
KIND_LIST = "list"          # перечисление: классификация, все виды, функции каждого
KIND_CLINICAL = "clinical"  # клинический вопрос: диагностика, лечение, дозы
KIND_RESEARCH = "research"  # разбор исследования или рекомендаций

KINDS = (KIND_SIMPLE, KIND_EXPLAIN, KIND_CONSPECT, KIND_COMPARE, KIND_LIST, KIND_CLINICAL, KIND_RESEARCH)

# Признаки составного запроса. Не медицинский шаблон, а признаки самой
# формулировки: просят перечислить, сравнить, сделать конспект или раскрыть
# несколько сторон. Такой вопрос без плана почти всегда выходит неполным.
_COMPLEX = re.compile(
    r"\b(все|всё|всех|кажд\w+|перечисл\w*|классификац\w*|виды|типы|формы|"
    r"сравн\w*|отличи\w*|разниц\w*|конспект\w*|подробн\w*|полн\w+|"
    r"механизм\w*|патогенез\w*|этиолог\w*|диагностик\w*|лечени\w*|"
    r"доз\w*|схем\w*|показан\w*|противопоказан\w*|осложнен\w*|"
    r"рекомендац\w*|исследован\w*|доказательств\w*)\b",
    re.IGNORECASE,
)

_SPLIT = re.compile(r"[,;:()\n]|\s[—–-]\s")
_SPACES = re.compile(r"\s+")
_TAIL = re.compile(r"[\s.!?…:;,-]+$")

# Порог «вопрос из нескольких частей»: три и больше смысловых слов в части и
# хотя бы две части. Односложный вопрос («что такое ацидоз») сюда не попадает.
_MIN_WORDS_IN_PART = 2
_MAX_QUERIES = 6


@dataclass(frozen=True)
class Aspect:
    """Обязательный пункт ответа.

    `weight` 2 — без него ответ неполный (определение, классификация, лечение);
    1 — желательно, но пропуск не делает ответ неверным.
    `from_requirement` — пункт прямо назван студентом («и лечение тоже»), такие
    не выбрасываются при сокращении плана.
    """

    title: str
    weight: int = 1
    from_requirement: bool = False


@dataclass(frozen=True)
class ResearchPlan:
    """План одного вопроса. Пустой план — законное состояние: искать как раньше."""

    kind: str
    goal: str
    aspects: tuple[Aspect, ...]
    queries: tuple[str, ...]
    # Сложный вопрос: нужен второй круг поиска при пробелах.
    deep: bool = False
    # Откуда план: `model` | `code` — видно в диагностике, не влияет на ответ.
    origin: str = "code"

    @property
    def must(self) -> tuple[Aspect, ...]:
        return tuple(a for a in self.aspects if a.weight >= 2)

    @property
    def empty(self) -> bool:
        return not self.queries


def _parts(question: str) -> list[str]:
    base = " ".join((question or "").split())
    return [p.strip(" .?!") for p in _SPLIT.split(base) if p.strip(" .?!")]


def looks_complex(question: str) -> bool:
    """Нужен ли план и второй круг. Решает формулировка, а не длина ответа:
    «сравни А и Б», «все ядра и функции каждого», «сделай полный конспект» —
    сложные даже в одну строку."""
    if not question or len(question.strip()) < 12:
        return False
    if _COMPLEX.search(question):
        return True
    parts = _parts(question)
    long_parts = [p for p in parts if len(meaningful_words(p)) >= _MIN_WORDS_IN_PART]
    if len(long_parts) >= 2:
        return True
    # Длинный вопрос без маркеров: две и больше фразы — тоже несколько тем.
    return len(re.findall(r"[.?!]", question)) >= 2 and len(meaningful_words(question)) >= 8


def guess_kind(question: str) -> str:
    """Вид задачи по формулировке — запасной вариант, когда модель недоступна."""
    q = (question or "").lower()
    # Границы слов обязательны: «ацидоз» содержит «доз», но это не вопрос о дозах.
    if re.search(r"\b(конспект|план ответа|шпаргалк)", q):
        return KIND_CONSPECT
    if re.search(r"\b(сравн|отличи|разниц|лучше|хуже)", q):
        return KIND_COMPARE
    if re.search(r"\b(перечисл|классификац|все виды|все формы|кажд\w+)", q):
        return KIND_LIST
    if re.search(r"\b(доз|схем\w+ лечен|назнач|противопоказан|показан)", q):
        return KIND_CLINICAL
    if re.search(r"\b(клиническ\w+ рекомендац|исследован|доказательств)", q):
        return KIND_RESEARCH
    if re.search(r"\b(механизм|патогенез|почему|как работает|объясни)", q):
        return KIND_EXPLAIN
    return KIND_SIMPLE


def fallback_plan(question: str) -> ResearchPlan:
    """План без модели: части вопроса как пункты, `query_variants` как запросы.

    Ошибка планировщика или его отсутствие не отменяют полноту: даже без модели
    составной вопрос ищется по частям, а не одной длинной фразой.
    """
    kind = guess_kind(question)
    parts = [p for p in _parts(question) if len(meaningful_words(p)) >= _MIN_WORDS_IN_PART]
    aspects = tuple(Aspect(title=p, weight=2 if i == 0 else 1, from_requirement=True) for i, p in enumerate(parts[:5]))
    queries = tuple(query_variants(question)[:_MAX_QUERIES])
    deep = looks_complex(question)
    goal = question.strip()
    return ResearchPlan(
        kind=kind,
        goal=goal,
        aspects=aspects,
        queries=queries or (question.strip(),),
        deep=deep,
        origin="code",
    )


PLAN_SYSTEM = """Ты — медицинский методист, который готовит план ответа для студента-медика.
Тебе дают вопрос студента и, если есть, прошлые реплики разговора.

Задача: понять, какую ЦЕЛЬ студент ставит, и что обязано быть в ответе, чтобы он
не пришёл переспрашивать «а где классификация», «добавь лечение».

Правила:
- План строится под ЭТОТ вопрос, а не по универсальному шаблону болезни. На
  вопрос про сравнение препаратов нужны критерии сравнения, а не «этиология».
- Обязательные пункты — только те, без которых ответ на этот вопрос будет
  неверным или явно неполным. Не раздувай список: 2–6 пунктов.
- Формулируй пункты словами темы («классификация по функции», «пороги дозы»),
  а не общими («определение»).
- Поисковые фразы — самостоятельные запросы, которые прозвучали бы в учебнике.
  Обязательно включи медицинские синонимы и латинские названия, если студент
  написал по-русски. 3–6 фраз, без повторов.
- Ничего не придумывай о медицине: ты не отвечаешь, ты только планируешь поиск.

Верни JSON: {"kind": "...", "goal": "...", "must": ["..."], "nice": ["..."], "queries": ["..."]}
kind — одно из: simple, explain, conspect, compare, list, clinical, research."""


def _clean_strings(raw, limit: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if not text or len(text) > 300 or text.lower() in {o.lower() for o in out}:
            continue
        out.append(text)
    return out[:limit]


def parse_plan(data: dict, question: str) -> ResearchPlan | None:
    """Сборка плана из ответа модели. Кривой ответ — `None`, вызывающий возьмёт
    запасной план; мусор в поля не пропускаем."""
    if not isinstance(data, dict):
        return None
    queries = _clean_strings(data.get("queries"), _MAX_QUERIES)
    if not queries:
        return None
    kind = str(data.get("kind") or "").strip()
    if kind not in KINDS:
        kind = guess_kind(question)

    aspects: list[Aspect] = []
    for weight, field_name in ((2, "must"), (1, "nice")):
        for title in _clean_strings(data.get(field_name), 8):
            if any(a.title.lower() == title.lower() for a in aspects):
                continue
            aspects.append(Aspect(title=title, weight=weight, from_requirement=False))
    if not aspects:
        # Модель дала только запросы — пунктами станут они, чтобы проверять было с чем.
        aspects = [Aspect(title=q, weight=2 if i == 0 else 1) for i, q in enumerate(queries[:4])]

    goal = " ".join(str(data.get("goal") or "").split())[:300] or question.strip()
    return ResearchPlan(
        kind=kind,
        goal=goal,
        aspects=tuple(aspects[:8]),
        queries=tuple(queries),
        deep=True,
        origin="model",
    )


async def plan_research(question: str, turns: list[tuple[str, str]] | None = None) -> ResearchPlan:
    """План для сложного вопроса. Простой вопрос не тратит вызов модели.

    `turns` — прошлые обмены: «а добавь лечение» без истории не понять.
    """
    if not looks_complex(question):
        spoken = " ".join((question or "").split())
        return ResearchPlan(
            kind=guess_kind(question),
            goal=spoken,
            aspects=(),
            queries=(spoken,),
            deep=False,
            origin="code",
        )

    # Импорт внутри функции: чистая половина модуля (планы без модели, разбор
    # ответа) должна работать без провайдера и без ключей — ею пользуются тесты.
    from app.llm import provider as llm
    from app.llm.task_map import Task

    context = ""
    if turns:
        context = "Прошлые реплики:\n" + "\n".join(
            f"Студент: {q}\nОтвет: {a[:400]}" for q, a in turns[-2:]
        ) + "\n\n"

    try:
        result = await llm.complete(
            Task.PLAN,
            [
                {"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content": f"{context}Вопрос студента: {question}"},
            ],
            temperature=0.0,
            max_output_tokens=600,
            json_schema={"required": ["kind", "goal", "queries"]},
        )
    except llm.LLMError:
        return fallback_plan(question)

    parsed = parse_plan(result.data or {}, question)
    return parsed or fallback_plan(question)

def normalize_question(question: str) -> str:
    """Ключ кеша планов: регистр, лишние пробелы и знаки на конце не создают новый план."""
    text = _SPACES.sub(" ", (question or "").strip().lower())
    return _TAIL.sub("", text)[:500]


def plan_to_dict(plan: ResearchPlan) -> dict:
    """План в JSON — для кеша и журнала исследования."""
    return {
        "kind": plan.kind,
        "goal": plan.goal,
        "aspects": [
            {"title": a.title, "weight": a.weight, "from_requirement": a.from_requirement}
            for a in plan.aspects
        ],
        "queries": list(plan.queries),
        "deep": plan.deep,
        "origin": plan.origin,
    }


def plan_from_dict(data: dict) -> ResearchPlan | None:
    """План из кеша. Битая или чужая запись — `None`: план соберётся заново, а не сломает ответ."""
    if not isinstance(data, dict):
        return None
    queries = tuple(q for q in data.get("queries") or [] if isinstance(q, str) and q.strip())
    if not queries:
        return None
    aspects = tuple(
        Aspect(
            title=str(item.get("title") or "").strip(),
            weight=int(item.get("weight") or 1),
            from_requirement=bool(item.get("from_requirement")),
        )
        for item in data.get("aspects") or []
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    )
    return ResearchPlan(
        kind=str(data.get("kind") or "simple"),
        goal=str(data.get("goal") or "").strip() or queries[0],
        aspects=aspects[:8],
        queries=queries[:8],
        deep=bool(data.get("deep", True)),
        origin="cache",
    )
