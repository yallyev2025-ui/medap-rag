"""Model Registry (§60 ТЗ): профили моделей с ценами, отделённые от доменного кода.

Цены хранятся в конфиге с версией (`PRICING_VERSION`) и не размазываются по
бизнес-логике: в коде считается формула из §65, а не фиксированное «0.14 ₽».

Секреты в профиль не попадают — ключи резолвит `app/llm/provider.py`. Профиль
безопасно показывать в админке.
"""

from dataclasses import dataclass

from config import settings

# Ключи провайдеров, которыми оперирует TaskModelMap.
DEEPSEEK = "deepseek"
OPENAI = "openai"


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    model_id: str
    base_url: str | None
    supports_vision: bool
    supports_structured_output: bool
    supports_tools: bool
    context_window: int
    pricing_version: str
    # Цены в долларах за 1M токенов.
    input_price_usd: float
    cached_input_price_usd: float
    output_price_usd: float
    # Ключ задан в окружении — модель можно использовать.
    enabled: bool

    @property
    def status(self) -> str:
        return "enabled" if self.enabled else "no_api_key"


def _deepseek() -> ModelProfile:
    return ModelProfile(
        provider=DEEPSEEK,
        model_id=settings.DEEPSEEK_MODEL,
        base_url=settings.DEEPSEEK_BASE_URL,
        supports_vision=True,
        supports_structured_output=True,
        supports_tools=True,
        context_window=1_000_000,
        pricing_version=settings.PRICING_VERSION,
        input_price_usd=settings.DEEPSEEK_INPUT_PRICE_USD,
        cached_input_price_usd=settings.DEEPSEEK_CACHED_INPUT_PRICE_USD,
        output_price_usd=settings.DEEPSEEK_OUTPUT_PRICE_USD,
        enabled=bool(settings.DEEPSEEK_API_KEY),
    )


def _openai() -> ModelProfile:
    return ModelProfile(
        provider=OPENAI,
        model_id=settings.OPENAI_EVAL_MODEL or settings.OPENAI_MODEL,
        base_url=settings.OPENAI_BASE_URL or None,
        supports_vision=True,
        supports_structured_output=True,
        supports_tools=True,
        context_window=400_000,
        pricing_version=settings.PRICING_VERSION,
        input_price_usd=settings.OPENAI_EVAL_INPUT_PRICE_USD,
        cached_input_price_usd=settings.OPENAI_EVAL_CACHED_INPUT_PRICE_USD,
        output_price_usd=settings.OPENAI_EVAL_OUTPUT_PRICE_USD,
        enabled=bool(settings.OPENAI_API_KEY),
    )


def model_registry() -> dict[str, ModelProfile]:
    """Профили читаются из конфига при каждом вызове: смена цены или модели в
    переменных окружения подхватывается перезапуском, без правок кода."""
    return {DEEPSEEK: _deepseek(), OPENAI: _openai()}


def profile(provider_key: str) -> ModelProfile:
    registry = model_registry()
    if provider_key not in registry:
        raise ValueError(f"Неизвестный провайдер: {provider_key}")
    return registry[provider_key]


def cost_usd(prof: ModelProfile, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    """Стоимость вызова по формуле §65: считается по фактическому usage провайдера.

    Кэшированные входные токены тарифицируются по отдельной, более низкой ставке,
    поэтому из общего числа входных они вычитаются.
    """
    fresh_input = max(input_tokens - cached_input_tokens, 0)
    return (
        fresh_input * prof.input_price_usd
        + cached_input_tokens * prof.cached_input_price_usd
        + output_tokens * prof.output_price_usd
    ) / 1_000_000


def to_rub(usd: float) -> float:
    return usd * settings.USD_RUB_RATE
