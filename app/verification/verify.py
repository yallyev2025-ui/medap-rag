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

from dataclasses import dataclass

from app.llm.task_map import Task
from app.verification.numeric import find_unsupported_numbers

VERIFY_SYSTEM_PROMPT = """Ты — строгий фактчекер. Тебе дают КОНТЕКСТ (фрагменты учебников) и ОТВЕТ ассистента.
Проверь, что КАЖДОЕ фактическое утверждение в ОТВЕТЕ прямо подтверждается КОНТЕКСТОМ.
Строку источника в конце ([Автор, Название, стр. N]) и вежливые/служебные фразы проверять не нужно.

Если все фактические утверждения подтверждены контекстом — первой строкой выведи ровно:
GROUNDED
Если есть хотя бы одно утверждение, которого нет в контексте или которое ему противоречит — первой строкой выведи ровно:
NOT_GROUNDED
а ниже коротко перечисли проблемные утверждения."""

VERIFY_USER_TEMPLATE = """КОНТЕКСТ:
{context}

ОТВЕТ:
{answer}"""


@dataclass
class VerificationResult:
    grounded: bool | None
    issues: str = ""


async def verify_answer(context: str, answer: str) -> VerificationResult:
    # Отложенный импорт: rag.generator импортирует этот модуль на уровне модуля
    # (verify_answer используется в generate_answer/_reasoning_answer), поэтому
    # обратный импорт _complete здесь должен быть внутри функции, иначе цикл.
    from rag.generator import _complete

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
