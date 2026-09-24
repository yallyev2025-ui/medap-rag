"""Quick Outline — «Схема для тетради» (см. `plans/medap-ai/QUICK_OUTLINE_SPEC.md`, полный ТЗ).

Это НЕ студенческий workflow: обычный вопрос студента боту/сайту генерирует
обычный ответ, как и раньше, — это никак не меняется. Quick Outline вызывается
С СОБСТВЕННОГО САЙТА владельца продукта (отдельный проект) через
`POST /v1/quick-outline/generate` — он даёт тему, получает готовую
структурированную схему по загруженным учебникам и сам решает, куда её
выложить. Хранения на нашей стороне нет (stateless, как evaluate/repair/vision).

Правила схемы взяты из присланного ТЗ: строго один из 10 типов, компактность
без потери медицинского смысла, пригодность для переноса в тетрадь от руки,
никаких выдуманных фактов вне загруженных материалов, requiredPoints — поле,
ОТДЕЛЬНОЕ от самой схемы (не то же самое, что содержимое schema).
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.evidence.pack import build_citations
from app.llm import provider as llm
from app.llm.task_map import Task
from app.observability.context import current_request_id
from constants import SOURCE_TEXTBOOK
from rag.generator import build_context, relevant_chunks
from rag.retriever import retrieve

logger = logging.getLogger(__name__)

OUTLINE_TYPES = (
    "mechanism",
    "classification",
    "sequence",
    "comparison",
    "definition",
    "cause_effect",
    "process",
    "pharmacology",
    "physiology",
    "pathophysiology",
    "anatomy",
)

QUICK_OUTLINE_SYSTEM_PROMPT = """Ты готовишь Quick Outline — «схему для тетради», первый и самый короткий уровень учебного материала по теме (Level 1 из трёх: Quick Outline → Core Explanation → Deep Material).

ЦЕЛЬ: студент читает схему 1–3 минуты, закрывает её и должен суметь восстановить структуру темы по памяти и рассказать своими словами. Это НЕ конспект-пересказ и НЕ сокращённая статья.

СТРОГО ПО КОНТЕКСТУ: используй только факты из переданных материалов. Не придумывай медицинские факты, не заменяй отсутствующие данные общими знаниями. Если в контексте недостаточно материала для темы — включи в schema только то, что реально есть, не заполняй пробелы выдумкой.

ВЫБЕРИ РОВНО ОДИН ТИП схемы, подходящий теме:
- mechanism: триггер → рецептор → сигнальный путь → эффектор → результат
- classification: дерево (основной объект → типы → признаки)
- sequence: этап → этап → этап (строгий порядок событий)
- comparison: таблица/два столбца по одинаковым признакам (A vs B)
- definition: термин → короткое точное определение
- cause_effect: причина → следствие (цепочка)
- process: этап 1 → этап 2 → ... → результат
- pharmacology: препарat → мишень → механизм действия → эффект → показания → важные НЯ
- physiology: стимул → рецептор → центр/регулятор → эффектор → ответ → обратная связь
- pathophysiology: причина → первичное нарушение → патогенез → вторичные изменения → проявления
- anatomy: структура → расположение/корешки/кровоснабжение → функция

ПРАВИЛА КОМПАКТНОСТИ: короткие фразы, стрелки (→, ↑, ↓), минимум сплошного текста, максимум структуры. Каждый item — короткая фраза или связка "X → Y", не предложение с придаточными.

НЕ УПРОЩАТЬ ДО ПОТЕРИ СМЫСЛА: если для понимания механизма нужны причинно-следственная связь, рецептор, медиатор, фермент, орган, клеточная структура, направление изменения — они должны остаться.

ЗАПРЕЩЕНО: вступления вида «в этой теме мы рассмотрим…»; мотивационный текст; один большой абзац; декоративные элементы, которые не помогают учиться; перегрузка второстепенными деталями; бессвязные bullet points без структуры типа выше.

Верни ТОЛЬКО JSON:
{
  "type": "один из перечисленных выше типов",
  "blocks": [{"title": "название блока (например 'Причина', 'Механизм', 'Результат')", "items": ["короткая фраза", "..."]}],
  "requiredPoints": ["конкретный пункт, который должен быть покрыт при проверке ответа", "..."]
}

requiredPoints — это НЕ то же самое, что blocks: schema показывает структуру для восстановления по памяти, requiredPoints — что конкретно должно быть названо, если ответ проверяют."""

QUICK_OUTLINE_USER_TEMPLATE = """Тема: {topic}

Материалы:
{context}"""

_QUICK_OUTLINE_SCHEMA = {"required": ["type", "blocks", "requiredPoints"]}

_NO_MATERIAL_MESSAGE = "По этой теме в загруженных материалах MedAP ничего не найдено — схему построить не из чего."


@dataclass
class OutlineBlock:
    title: str
    items: list[str]


@dataclass
class QuickOutlineResult:
    topic: str
    outline_type: str | None
    blocks: list[OutlineBlock]
    required_points: list[str]
    evidence_references: list[dict[str, Any]] = field(default_factory=list)
    # Заполняется, только если по теме вообще нет материалов или провайдер недоступен —
    # в обоих случаях schema пустая, а не выдуманная (см. модуль-докстринг).
    error: str | None = None
    request_id: str | None = None


async def generate_quick_outline(
    topic: str,
    source_type: str = SOURCE_TEXTBOOK,
    subject: str | None = None,
) -> QuickOutlineResult:
    chunks = await retrieve(topic, source_type=source_type, subject=subject)
    relevant = relevant_chunks(chunks)
    if not relevant:
        return QuickOutlineResult(
            topic=topic, outline_type=None, blocks=[], required_points=[],
            error=_NO_MATERIAL_MESSAGE, request_id=current_request_id(),
        )

    context = build_context(chunks)
    messages = [
        {"role": "system", "content": QUICK_OUTLINE_SYSTEM_PROMPT},
        {"role": "user", "content": QUICK_OUTLINE_USER_TEMPLATE.format(topic=topic, context=context)},
    ]
    try:
        result = await llm.complete(Task.QUICK_OUTLINE, messages, temperature=0.2, json_schema=_QUICK_OUTLINE_SCHEMA)
        data = result.data or {}
    except llm.LLMError:
        logger.exception("Quick Outline недоступен (сбой провайдера)")
        return QuickOutlineResult(
            topic=topic, outline_type=None, blocks=[], required_points=[],
            error="Генерация схемы сейчас недоступна, попробуй позже.",
            request_id=current_request_id(),
        )

    outline_type = data.get("type")
    if outline_type not in OUTLINE_TYPES:
        logger.warning("Quick Outline: модель вернула неизвестный type=%r", outline_type)

    blocks = [
        OutlineBlock(title=b.get("title", ""), items=[i for i in b.get("items", []) if isinstance(i, str)])
        for b in data.get("blocks", [])
        if isinstance(b, dict)
    ]
    citations = build_citations(relevant[:8])

    return QuickOutlineResult(
        topic=topic,
        outline_type=outline_type,
        blocks=blocks,
        required_points=[p for p in data.get("requiredPoints", []) if isinstance(p, str)],
        evidence_references=[c.to_dict() for c in citations],
        request_id=current_request_id(),
    )
