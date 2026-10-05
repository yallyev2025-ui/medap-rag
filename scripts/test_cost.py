"""Проверка расчёта стоимости вызова модели (формула §65): без сети и без ключей.

Запуск: python scripts/test_cost.py
Цифры — с живого экрана владельца 05.10.2026: «Схема и краткий конспект», три вызова,
18 750 → 11 062 токенов, ≈ 4,4 ₽. Сверка с этим числом невозможна без разбивки на
кешированные токены, поэтому проверяется сама формула на ручных примерах.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "x")

from app.llm.registry import ModelProfile, cost_usd, to_rub  # noqa: E402
from config import settings  # noqa: E402


def profile() -> ModelProfile:
    from app.llm.registry import profile as get_profile

    return get_profile("openai")


def approx(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def main() -> None:
    p = profile()

    # Тариф: вход / кеш / выход за миллион токенов
    assert approx(p.input_price_usd, 0.75), p.input_price_usd
    assert approx(p.cached_input_price_usd, 0.075), p.cached_input_price_usd
    assert approx(p.output_price_usd, 4.50), p.output_price_usd

    # Без кеша: 18 750 входных и 11 062 выходных
    expected = (18_750 * 0.75 + 11_062 * 4.50) / 1_000_000
    assert approx(cost_usd(p, 18_750, 0, 11_062), expected)

    # Кеш вычитается из полного входа и берётся по своей ставке
    with_cache = cost_usd(p, 18_750, 12_000, 11_062)
    expected_cached = ((18_750 - 12_000) * 0.75 + 12_000 * 0.075 + 11_062 * 4.50) / 1_000_000
    assert approx(with_cache, expected_cached)
    assert with_cache < cost_usd(p, 18_750, 0, 11_062)

    # Кеша больше входа — не уходим в минус
    assert approx(cost_usd(p, 100, 500, 0), 500 * 0.075 / 1_000_000)

    # Рубли по курсу из настроек
    assert approx(to_rub(1.0), settings.USD_RUB_RATE)

    print("ok: стоимость считается верно")


if __name__ == "__main__":
    main()
