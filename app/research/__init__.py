"""Research Runtime (Mentor 2.0, этап 1): план исследования, проверка полноты, добор.

Модуль не зависит от каналов: им пользуется единый конвейер `app/workflows/ask.py`,
поэтому сайт и Telegram получают одно и то же поведение.

Здесь экспортируется только чистая (безмодельная) половина — планы и проверка
покрытия. Ход исследования (`research_material`) живёт в `runner.py` и требует
настроек и поиска, поэтому импортируется напрямую: `from app.research.runner import
research_material`. Так тесты чистой логики не тянут настройки и провайдера.
"""

from app.research.coverage import Coverage, Gap, assess_coverage, confirm_gaps, gap_queries
from app.research.planner import Aspect, ResearchPlan, fallback_plan, looks_complex, plan_research

__all__ = [
    "Aspect",
    "Coverage",
    "Gap",
    "ResearchPlan",
    "assess_coverage",
    "confirm_gaps",
    "fallback_plan",
    "gap_queries",
    "looks_complex",
    "plan_research",
]