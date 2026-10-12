"""Verification Layer (§12, §14, §15, §36 ТЗ).

Заменяет прежний одиночный проход `_verify_grounded` в rag/generator.py: тот же
принцип (проверить КАЖДОЕ фактическое утверждение по контексту), плюс числовая
проверка (§14) поверх семантической.

`VerificationResult.grounded`:
- True  — заземление и числа подтверждены контекстом.
- False — есть проблема (в утверждениях и/или в числах); вызывающий код
  (rag/generator.py) делает один корректирующий проход и проверяет результат
  ещё раз; если проблема остаётся — честный отказ лучше недостоверного ответа (§15).
- None  — сам верификатор недоступен (сбой LLM, §36): ответ не помечается
  verified, но не отбрасывается — контролируемая, а не полная деградация.
"""

import re
from dataclasses import dataclass

from app.llm.task_map import Task
from app.verification.numeric import find_unsupported_numbers

VERIFY_SYSTEM_PROMPT = """Ты — фактчекер учебного ответа. Тебе дают КОНТЕКСТ (фрагменты учебников) и ОТВЕТ ассистента.
Найди в ОТВЕТЕ только утверждения, которых в КОНТЕКСТЕ НЕТ вообще или которые ему ПРЯМО ПРОТИВОРЕЧАТ (другое число, другой механизм, обратный смысл).

НЕ считай ошибкой:
- пересказ другими словами, сокращения, перестановку частей, объединение сведений из нескольких фрагментов или двух учебников;
- вывод, который прямо следует из подтверждённых фактов контекста;
- уточнение «к какому периоду/учебнику относится» — если сам факт в контексте есть;
- числа, совпадающие с контекстом по значению (6–12 мес = «от 6 до 12 мес», 48 ч = 48 часов);
- оформление, нумерацию, строку источника в конце ([Автор, Название, стр. N]) и служебные фразы.

Придуманные образы, аналогии, примеры из жизни и мнемоники (блоки с пометкой 💡 «придуман, не из учебника» и просто сравнения вроде «это как…») НЕ проверяй: они не должны быть в учебнике. Проверяй только медицинские утверждения.

Если неподтверждённых утверждений нет — первой строкой выведи ровно:
GROUNDED
Если есть хотя бы одно утверждение, которого нет в контексте или которое ему противоречит, — первой строкой выведи ровно:
NOT_GROUNDED
а ниже перечисли ТОЛЬКО такие утверждения (дословно из ответа), по одному на строке, с короткой причиной. Подтверждённые утверждения не перечисляй."""

VERIFY_USER_TEMPLATE = """КОНТЕКСТ:
{context}

ОТВЕТ:
{answer}"""


_ILLUSTRATION = re.compile(r"^\s*(?:\*\*|\*|_)*\s*💡")


def strip_illustrations(text: str) -> str:
    """Убирает из ответа блоки-иллюстрации (**💡 Пример**, **💡 Образ для запоминания** — модель обязана помечать
    придуманное): их нечем подтверждать по учебнику, а проверка принимала их за выдумку и браковала весь ответ
    (замер 12.10.2026). Блок — абзац с пометкой; если абзац состоит из одной строки-заголовка, уходит и следующий."""
    paragraphs = re.split(r"\n\s*\n", text)
    kept: list[str] = []
    skip_next = False
    for paragraph in paragraphs:
        if skip_next:
            skip_next = False
            continue
        if _ILLUSTRATION.match(paragraph):
            lines = [line for line in paragraph.strip().splitlines() if line.strip()]
            # Только заголовок без текста («**💡 Пример** *(придуман, не из учебника)*»): сам образ в следующем абзаце.
            # Строка, где после пометки идёт текст и точка, — целый блок, следующий абзац не трогаем
            skip_next = len(lines) == 1 and lines[0].rstrip().endswith(("*", "_"))
            continue
        kept.append(paragraph)
    return "\n\n".join(kept)


@dataclass
class VerificationResult:
    grounded: bool | None
    issues: str = ""


async def verify_answer(context: str, answer: str) -> VerificationResult:
    # Отложенный импорт: rag.generator импортирует этот модуль на уровне модуля
    # (verify_answer используется в generate_answer/_reasoning_answer), поэтому
    # обратный импорт _complete здесь должен быть внутри функции, иначе цикл.
    from rag.generator import _complete

    answer = strip_illustrations(answer)
    if not answer.strip():
        return VerificationResult(True)  # в ответе одни придуманные образы: проверять нечего

    numeric_issues = find_unsupported_numbers(answer, context)

    user_prompt = VERIFY_USER_TEMPLATE.format(context=context, answer=answer)
    try:
        verdict = await _complete(
            VERIFY_SYSTEM_PROMPT, user_prompt, temperature=0.0, task=Task.CLAIM_EVIDENCE_CHECK
        )
    except RuntimeError:
        if numeric_issues:
            # Числовая проверка детерминированная и не зависит от LLM — используем
            # её результат, даже если семантическая проверка недоступна.
            return VerificationResult(False, "Числа не подтверждены контекстом: " + "; ".join(numeric_issues))
        return VerificationResult(None)

    lines = verdict.strip().splitlines()
    first = lines[0].strip().upper() if lines else ""
    llm_grounded = first.startswith("GROUNDED")
    llm_issues = "" if llm_grounded else ("\n".join(lines[1:]).strip() or "(не указаны)")

    if llm_grounded and not numeric_issues:
        return VerificationResult(True)

    parts = [p for p in (
        llm_issues,
        "Числа не подтверждены контекстом: " + "; ".join(numeric_issues) if numeric_issues else "",
    ) if p]
    return VerificationResult(False, "\n".join(parts))
