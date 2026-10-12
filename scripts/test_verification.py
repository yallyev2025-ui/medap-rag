"""Проверка слоя проверки ответов без моделей: python -m scripts.test_verification"""

import asyncio
import sys
import types

# Моделей эмбеддингов (torch) на машине разработчика может не быть, проверкам они не нужны
try:
    import torch  # noqa: F401
except ImportError:
    sys.modules.setdefault("rag.embedder", types.SimpleNamespace(embed_query=None))

import rag.generator as generator
from app.verification.numeric import find_unsupported_numbers
from config import settings
from rag.generator import NO_CONTEXT_ANSWER, SOFT_FAIL_NOTE, _verify_and_repair
from app.verification.verify import VerificationResult, strip_illustrations


def test_same_numbers_written_differently_are_not_errors() -> None:
    # Так выглядел отказ 12.10.2026: учебник «от 6 до 12 мес», ответ «6–12 мес»
    assert find_unsupported_numbers("занимает 6–12 мес.", "занимает от 6 до 12 мес") == []
    assert find_unsupported_numbers("6 месяцев", "6 мес") == []
    assert find_unsupported_numbers("48 часов", "через 48 ч") == []
    assert find_unsupported_numbers("30 мин", "30 минут") == []
    assert find_unsupported_numbers("2–3 суток", "2-3 сут") == []


def test_wrong_numbers_are_still_caught() -> None:
    assert find_unsupported_numbers("доза 5 мг", "доза 50 мг") == ["5 мг"]
    assert find_unsupported_numbers("длится 7 мес", "длится 6 мес") == ["7 мес"]
    assert find_unsupported_numbers("48 часов", "через 24 ч") == ["48 ч"]


def run(coro):
    return asyncio.run(coro)


def fake_verify(results):
    queue = list(results)

    async def verify(context, answer):
        return queue.pop(0)

    return verify


async def correct(issues: str) -> str:
    return "исправленный ответ"


def test_failed_check_gives_answer_with_note() -> None:
    original = generator.verify_answer
    bad = VerificationResult(False, "что-то не так")
    try:
        generator.verify_answer = fake_verify([bad, bad])
        settings.VERIFY_SOFT_FAIL = True
        result = run(_verify_and_repair("ctx", "ответ", correct, soft_fail=True))
        assert result.text == "исправленный ответ" + SOFT_FAIL_NOTE
        assert result.verified is None and result.flagged is True

        # клинрекомендации (soft_fail не включён) — как раньше, отказ
        generator.verify_answer = fake_verify([bad, bad])
        result = run(_verify_and_repair("ctx", "ответ", correct, soft_fail=False))
        assert result.text == NO_CONTEXT_ANSWER and result.verified is False

        # выключатель в конфиге возвращает прежнее поведение
        generator.verify_answer = fake_verify([bad, bad])
        settings.VERIFY_SOFT_FAIL = False
        result = run(_verify_and_repair("ctx", "ответ", correct, soft_fail=True))
        assert result.text == NO_CONTEXT_ANSWER and result.verified is False
    finally:
        generator.verify_answer = original
        settings.VERIFY_SOFT_FAIL = True


def test_passing_and_corrected_answers_are_untouched() -> None:
    original = generator.verify_answer
    try:
        generator.verify_answer = fake_verify([VerificationResult(True)])
        result = run(_verify_and_repair("ctx", "ответ", correct, soft_fail=True))
        assert result.text == "ответ" and result.verified is True and not result.flagged

        generator.verify_answer = fake_verify([VerificationResult(False, "x"), VerificationResult(True)])
        result = run(_verify_and_repair("ctx", "ответ", correct, soft_fail=True))
        assert result.text == "исправленный ответ" and result.verified is True and not result.flagged
    finally:
        generator.verify_answer = original


def test_model_refusal_after_correction_stays_a_refusal() -> None:
    original = generator.verify_answer
    bad = VerificationResult(False, "x")

    async def refuse(issues: str) -> str:
        return NO_CONTEXT_ANSWER

    try:
        generator.verify_answer = fake_verify([bad, bad])
        result = run(_verify_and_repair("ctx", "ответ", refuse, soft_fail=True))
        assert result.text == NO_CONTEXT_ANSWER and result.verified is False
    finally:
        generator.verify_answer = original


def test_illustration_blocks_are_not_checked() -> None:
    answer = (
        "**Синапс**\nПередатчик ацетилхолин выделяется в щель.\n\n"
        "**💡 Пример** *(придуман, не из учебника)* Как почтальон бросает письмо в 3 ящика за 5 минут.\n\n"
        "**Итог**\nСигнал передаётся химически."
    )
    cleaned = strip_illustrations(answer)
    assert "почтальон" not in cleaned
    assert "ацетилхолин" in cleaned and "Итог" in cleaned
    # числа из придуманного примера не должны превращаться в «неподтверждённые»
    assert find_unsupported_numbers(cleaned, "ацетилхолин выделяется") == []


def test_illustration_with_header_on_its_own_line_removes_next_paragraph() -> None:
    answer = "Суть.\n\n**💡 Образ для запоминания** *(придуман, не из учебника)*\n\nКак ключ и замок, 10 минут.\n\nФакт."
    cleaned = strip_illustrations(answer)
    assert "ключ" not in cleaned and "Суть." in cleaned and "Факт." in cleaned


def test_answer_without_illustrations_is_untouched() -> None:
    answer = "Первый абзац.\n\nВторой абзац про 6–12 мес."
    assert strip_illustrations(answer) == answer


def test_only_illustration_means_nothing_to_check() -> None:
    from app.verification.verify import verify_answer

    result = run(verify_answer("контекст", "**💡 Пример** *(придуман, не из учебника)* Как почтальон."))
    assert result.grounded is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
