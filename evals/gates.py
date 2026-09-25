"""Gates (§49 ТЗ, этап 4B) — пороги качества, при провале которых релиз блокируется.

Пороги в конфиге (`GATE_*`), выключены по умолчанию (`GATES_ENABLED=False`):
честные числа ставятся только после baseline на настоящих учебниках. Пока
выключены — результаты показываются информативно, ничего не блокируют.

Критичные категории (утечка приватных данных, prompt injection) — блокер
независимо от числовых порогов (§49: регрессия по безопасности/изоляции).
"""

from dataclasses import asdict, dataclass
from typing import Any

from config import settings
from constants import CRITICAL_REGRESSION_CATEGORIES


@dataclass
class GateResult:
    gate: str
    threshold: str
    actual: str
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def check_gates(report: dict[str, Any]) -> list[GateResult]:
    """Проверка одного отчёта evals/run.py (или одной ветки benchmark). Метрика,
    которую нельзя посчитать (нет кейсов с ожидаемым источником, только поиск без
    генерации), не проваливает gate — она просто не проверяется."""
    summary = report.get("summary", {})
    results: list[GateResult] = []

    recall = summary.get("retrieval", {}).get("recallAtK")
    if recall is not None:
        results.append(
            GateResult("Recall@K", f"≥ {settings.GATE_MIN_RECALL_AT_K:.2f}", _fmt(recall), recall >= settings.GATE_MIN_RECALL_AT_K)
        )

    generation = summary.get("generation")
    if generation:
        total = generation.get("total") or 0
        if total:
            pass_rate = generation["passed"] / total
            results.append(
                GateResult("Доля пройденных ответов", f"≥ {settings.GATE_MIN_PASS_RATE:.2f}", _fmt(pass_rate), pass_rate >= settings.GATE_MIN_PASS_RATE)
            )
        hallucinations = generation.get("hallucinations", 0)
        results.append(
            GateResult(
                "Галлюцинации (критично)",
                f"≤ {settings.GATE_MAX_HALLUCINATIONS}",
                str(hallucinations),
                hallucinations <= settings.GATE_MAX_HALLUCINATIONS,
            )
        )
        verified_rate = generation.get("verifiedRate")
        if verified_rate is not None:
            results.append(
                GateResult("Verified rate", f"≥ {settings.GATE_MIN_VERIFIED_RATE:.2f}", _fmt(verified_rate), verified_rate >= settings.GATE_MIN_VERIFIED_RATE)
            )

    for case in report.get("cases", []):
        if case.get("category") in CRITICAL_REGRESSION_CATEGORIES and case.get("generation"):
            if not case["generation"].get("passed"):
                results.append(
                    GateResult(f"Критичная категория: {case['category']}", "все кейсы проходят", "провал", False)
                )

    return results


def gates_passed(results: list[GateResult]) -> bool:
    return all(r.passed for r in results)


def gates_block(results: list[GateResult]) -> bool:
    """Блокирует ли результат релиз/смену модели: только если Gates включены."""
    return settings.GATES_ENABLED and not gates_passed(results)
