"""LLMProvider — единая точка вызова моделей (§3.1, §56, §60 ТЗ).

Провайдер выбирается по атомарной задаче через TaskModelMap, а не «умным» роутером
на каждый запрос (§66). DeepSeek и OpenAI обслуживаются одним адаптером: у DeepSeek
OpenAI-совместимый API, отличаются base_url, ключ и id модели.

Здесь же выполняются два обязательных требования ТЗ:
- §29: structured output валидируется по схеме, при невалидном ответе — один
  контролируемый повтор, затем ошибка; мусор дальше по пайплайну не уходит.
- §59: на каждый вызов сохраняется запись о расходе.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import openai

from app.llm.registry import ModelProfile, profile
from app.llm.task_map import DEEPSEEK, OPENAI, Task, provider_for
from app.llm.usage import record_usage
from config import settings

logger = logging.getLogger(__name__)

# Понятное пользователю сообщение: детали провайдера наружу не выносим.
PROVIDER_ERROR_MESSAGE = "Произошла ошибка, попробуй позже."


class LLMError(RuntimeError):
    """Вызов модели не удался после повторов."""


class StructuredOutputError(LLMError):
    """Модель не вернула валидный по схеме JSON (§29)."""


@dataclass
class LLMResult:
    text: str
    data: dict[str, Any] | None
    provider: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    latency_ms: int
    retry_count: int
    fallback_from: str | None


@lru_cache(maxsize=4)
def _client(provider_key: str) -> openai.AsyncOpenAI:
    if provider_key == DEEPSEEK:
        return openai.AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
        )
    if provider_key == OPENAI:
        # base_url пустой = облако OpenAI; непустой — шлюз или совместимый провайдер.
        return openai.AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL or None,
        )
    raise ValueError(f"Неизвестный провайдер: {provider_key}")


def _usage_tokens(response: Any) -> tuple[int, int, int]:
    """(входные, кэшированные входные, выходные) токены из ответа провайдера.

    Поле кэш-хитов называется по-разному: у OpenAI это
    `prompt_tokens_details.cached_tokens`, у DeepSeek — `prompt_cache_hit_tokens`.
    Если провайдер не отдал usage, считаем нулями — лучше недооценить расход в
    одной строке, чем уронить ответ.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0

    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0

    cached = getattr(usage, "prompt_cache_hit_tokens", None)
    if cached is None:
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
    return input_tokens, cached or 0, output_tokens


def _max_tokens_for(task: Task, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    # Режим «на паре» намеренно короткий (§53.1, §57).
    if task is Task.CLASS_QUICK:
        return settings.CLASS_QUICK_MAX_OUTPUT_TOKENS
    return settings.MAX_OUTPUT_TOKENS


async def complete(
    task: Task,
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_output_tokens: int | None = None,
    json_schema: dict | None = None,
    image_units: int = 0,
) -> LLMResult:
    """Выполняет атомарную AI-задачу на закреплённом за ней провайдере.

    `json_schema` включает structured output: ответ парсится и проверяется на
    обязательные поля верхнего уровня; при провале делается один повтор.
    """
    provider_key, fallback_from = provider_for(task)
    prof: ModelProfile = profile(provider_key)
    client = _client(provider_key)

    request: dict[str, Any] = {
        "model": prof.model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": _max_tokens_for(task, max_output_tokens),
    }
    if json_schema is not None:
        request["response_format"] = {"type": "json_object"}

    attempts = 2 if json_schema is not None else 1
    started = time.monotonic()
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            response = await client.chat.completions.create(**request)
        except openai.APIError as exc:
            last_error = exc
            logger.warning("Провайдер %s вернул ошибку (попытка %d): %s", provider_key, attempt + 1, exc)
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5)
                continue
            break

        text = response.choices[0].message.content or ""
        input_tokens, cached_tokens, output_tokens = _usage_tokens(response)
        latency_ms = int((time.monotonic() - started) * 1000)

        data: dict[str, Any] | None = None
        if json_schema is not None:
            data = _parse_structured(text, json_schema)
            if data is None:
                last_error = StructuredOutputError("Модель вернула невалидный по схеме JSON")
                logger.warning("Невалидный structured output от %s (попытка %d)", provider_key, attempt + 1)
                await record_usage(
                    task=task.value,
                    profile=prof,
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    retry_count=attempt,
                    fallback_from=fallback_from,
                    image_units=image_units,
                    error="invalid_structured_output",
                )
                if attempt + 1 < attempts:
                    continue
                raise StructuredOutputError(
                    "Не удалось получить валидный структурированный ответ модели"
                ) from last_error

        await record_usage(
            task=task.value,
            profile=prof,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            retry_count=attempt,
            fallback_from=fallback_from,
            image_units=image_units,
        )

        return LLMResult(
            text=text,
            data=data,
            provider=provider_key,
            model=prof.model_id,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            retry_count=attempt,
            fallback_from=fallback_from,
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    await record_usage(
        task=task.value,
        profile=prof,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        latency_ms=latency_ms,
        retry_count=attempts - 1,
        fallback_from=fallback_from,
        error=str(last_error) if last_error else "unknown_provider_error",
    )
    raise LLMError(PROVIDER_ERROR_MESSAGE) from last_error


def _parse_structured(text: str, json_schema: dict) -> dict[str, Any] | None:
    """Парсит JSON и проверяет обязательные поля верхнего уровня.

    Полноценная JSON-Schema-валидация появится вместе с типизированными оценочными
    контрактами (этап 4); здесь достаточно отсечь мусор и недостающие поля, чтобы
    ничего непроверенного не ушло дальше по пайплайну (§29).
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for field in json_schema.get("required", []):
        if field not in data:
            return None
    return data
