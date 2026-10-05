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
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import openai

from app.llm.registry import (
    ModelProfile,
    model_candidates,
    profile,
    remember_working_model,
)
from app.llm.task_map import DEEPSEEK, OPENAI, Task, resolve_provider
from app.llm.usage import record_usage
from config import settings

logger = logging.getLogger(__name__)

# Понятное пользователю сообщение: детали провайдера наружу не выносим.
PROVIDER_ERROR_MESSAGE = "Произошла ошибка, попробуй позже."

# У «рассуждающих» моделей OpenAI (GPT-5.x, o-серия) часть лимита вывода уходит на
# «мысли»; при малом лимите ответ получается пустым — держим нижний порог.
REASONING_MIN_OUTPUT_TOKENS = 2000
_REASONING_MODEL = re.compile(r"^(gpt-5|o\d)", re.IGNORECASE)


class LLMError(RuntimeError):
    """Вызов модели не удался после повторов. `reason` — настоящая причина по-русски
    (для админа и System Health); текст самой ошибки остаётся безопасным для студента."""

    def __init__(self, message: str = PROVIDER_ERROR_MESSAGE, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


class StructuredOutputError(LLMError):
    """Модель не вернула валидный по схеме JSON (§29)."""


# --- Последние ошибки/успехи провайдеров (для System Health) ------------------------
# В памяти процесса: это диагностика «что происходит сейчас», не история.
_last_error: dict[str, dict[str, Any]] = {}
_last_ok: dict[str, dict[str, Any]] = {}


def _note_error(provider_key: str, task: str, reason: str) -> None:
    _last_error[provider_key] = {"time": time.time(), "task": task, "reason": reason}


def _note_ok(provider_key: str, model: str) -> None:
    _last_ok[provider_key] = {"time": time.time(), "model": model}


def last_error(provider_key: str) -> dict[str, Any] | None:
    return _last_error.get(provider_key)


def last_ok(provider_key: str) -> dict[str, Any] | None:
    return _last_ok.get(provider_key)


def _is_reasoning_model(model: str) -> bool:
    return bool(_REASONING_MODEL.match(model)) and "chat" not in model.lower()


def _error_message(exc: Exception) -> str:
    return str(getattr(exc, "message", None) or exc)


def classify_error(exc: Exception, provider_key: str, model: str) -> str:
    """Понятная русская причина сбоя вызова провайдера."""
    name = "OpenAI" if provider_key == OPENAI else "DeepSeek"
    message = _error_message(exc)
    lowered = message.lower()
    code = str(getattr(exc, "code", "") or "")

    if isinstance(exc, openai.APITimeoutError):
        return f"{name}: таймаут ответа"
    if isinstance(exc, openai.APIConnectionError):
        cause = str(getattr(exc, "__cause__", "") or "")[:100]
        return f"нет соединения с {name}" + (f" ({cause})" if cause else "")
    if isinstance(exc, openai.AuthenticationError):
        return f"ключ {name} не принят (401) — проверь ключ в переменных"
    if isinstance(exc, openai.PermissionDeniedError):
        if any(w in lowered for w in ("country", "region", "territory")):
            return f"{name} блокирует регион/IP сервера (403)"
        return f"{name}: доступ запрещён (403): {message[:120]}"
    if isinstance(exc, openai.RateLimitError):
        if "insufficient_quota" in (code + lowered):
            return f"на счёте {name} нет средств или исчерпана квота (insufficient_quota)"
        return f"{name}: превышен лимит запросов (429)"
    if isinstance(exc, openai.NotFoundError):
        return f"модель {model} недоступна для этого ключа (404)"
    if isinstance(exc, openai.BadRequestError):
        return f"{name} отклонил запрос (400): {message[:150]}"
    status = getattr(exc, "status_code", None)
    if status:
        return f"{name}: HTTP {status}: {message[:120]}"
    return f"{name}: {message[:150]}"


def _is_model_missing(exc: Exception) -> bool:
    if isinstance(exc, openai.NotFoundError):
        return True
    return "model_not_found" in (str(getattr(exc, "code", "") or "") + _error_message(exc).lower())


def _incompatible_param(exc: Exception, request: dict[str, Any], dropped: set[str]) -> str | None:
    """Какой параметр запроса модель не принимает (400 с его упоминанием) — чтобы
    один раз повторить запрос без него / с его аналогом."""
    if not isinstance(exc, openai.BadRequestError):
        return None
    lowered = _error_message(exc).lower()
    for param in ("temperature", "reasoning_effort", "max_completion_tokens", "max_tokens", "response_format"):
        if param in lowered and param in request and param not in dropped:
            return param
    return None


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
    truncated: bool = False


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
    json_mode: bool = False,
    image_units: int = 0,
) -> LLMResult:
    """Выполняет атомарную AI-задачу на закреплённом за ней провайдере.

    `json_schema` включает structured output: ответ парсится и проверяется на
    обязательные поля верхнего уровня; при провале делается один повтор.
    `json_mode` — только режим провайдера «ответ обязан быть валидным JSON»: без
    разбора и без платного повтора (разбирает вызывающий). Нужен длинным ответам,
    где один сырой перенос строки в тексте ломает весь JSON.
    """
    try:
        provider_key, fallback_from = await resolve_provider(task, needs_vision=image_units > 0)
    except RuntimeError as exc:
        # Нет ни одного подходящего провайдера (нет ключа / нет зрения для фото).
        raise LLMError(PROVIDER_ERROR_MESSAGE, reason=str(exc)) from exc
    prof: ModelProfile = profile(provider_key)
    client = _client(provider_key)

    request = _build_request(
        provider_key, prof.model_id, messages, temperature,
        _max_tokens_for(task, max_output_tokens), json_schema is not None or json_mode,
    )

    attempts = 2 if json_schema is not None else 1
    started = time.monotonic()
    last_exc: Exception | None = None
    reason: str | None = None

    for attempt in range(attempts):
        try:
            response = await _call_with_compat(provider_key, client, request)
        except openai.APIError as exc:
            last_exc = exc
            reason = classify_error(exc, provider_key, request["model"])
            logger.warning(
                "Провайдер %s вернул ошибку (попытка %d): %s — %s", provider_key, attempt + 1, exc, reason
            )
            # Повтор имеет смысл только для временных сбоев, не для 400/401/404.
            if attempt + 1 < attempts and not isinstance(
                exc, (openai.BadRequestError, openai.AuthenticationError, openai.NotFoundError)
            ):
                await asyncio.sleep(0.5)
                continue
            break

        used_model = request["model"]
        if provider_key == OPENAI and used_model != prof.model_id:
            remember_working_model(OPENAI, used_model)
        _note_ok(provider_key, used_model)

        text = response.choices[0].message.content or ""
        truncated = getattr(response.choices[0], "finish_reason", None) == "length"
        if truncated:
            logger.warning(
                "Ответ %s упёрся в лимит токенов (%s) — текст обрезан",
                task.value,
                request.get("max_completion_tokens") or request.get("max_tokens"),
            )
        input_tokens, cached_tokens, output_tokens = _usage_tokens(response)
        latency_ms = int((time.monotonic() - started) * 1000)

        data: dict[str, Any] | None = None
        if json_schema is not None:
            data = _parse_structured(text, json_schema)
            if data is None:
                if not text.strip():
                    reason = (
                        "модель вернула пустой ответ (весь лимит ушёл на рассуждения)"
                        if truncated else "модель вернула пустой ответ"
                    )
                else:
                    reason = "ответ модели не в нужном формате (не JSON или нет обязательных полей)"
                last_exc = StructuredOutputError("Модель вернула невалидный по схеме JSON", reason=reason)
                logger.warning(
                    "Невалидный structured output от %s (попытка %d): %s | начало ответа: %r",
                    provider_key, attempt + 1, reason, text[:200],
                )
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
                _note_error(provider_key, task.value, reason)
                raise StructuredOutputError(
                    "Не удалось получить валидный структурированный ответ модели", reason=reason
                ) from last_exc

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
            model=used_model,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            retry_count=attempt,
            fallback_from=fallback_from,
            truncated=truncated,
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    reason = reason or "неизвестный сбой провайдера"
    _note_error(provider_key, task.value, reason)
    await record_usage(
        task=task.value,
        profile=prof,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        latency_ms=latency_ms,
        retry_count=attempts - 1,
        fallback_from=fallback_from,
        error=str(last_exc) if last_exc else "unknown_provider_error",
    )
    raise LLMError(PROVIDER_ERROR_MESSAGE, reason=reason) from last_exc


def _build_request(
    provider_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> dict[str, Any]:
    """Тело запроса под конкретного провайдера и модель.

    OpenAI: `max_completion_tokens` (его понимают и gpt-4.1, и GPT-5.x), а у
    рассуждающих моделей нет произвольной `temperature`, зато есть
    `reasoning_effort` — для разбора фото и оценки глубокие рассуждения не нужны.
    DeepSeek остаётся на классическом `max_tokens` + `temperature`.
    """
    request: dict[str, Any] = {"model": model, "messages": messages}
    if provider_key == OPENAI:
        if _is_reasoning_model(model):
            request["max_completion_tokens"] = max(max_tokens, REASONING_MIN_OUTPUT_TOKENS)
            request["reasoning_effort"] = "low"
        else:
            request["max_completion_tokens"] = max_tokens
            request["temperature"] = temperature
    else:
        request["max_tokens"] = max_tokens
        request["temperature"] = temperature
    if json_mode:
        request["response_format"] = {"type": "json_object"}
    return request


async def _call_with_compat(provider_key: str, client: openai.AsyncOpenAI, request: dict[str, Any]) -> Any:
    """Один вызов с автоподстройкой: параметр, который модель не принимает (400),
    снимается/заменяется один раз; «модели нет» (404) у OpenAI — переход к следующей
    модели из цепочки OPENAI_MODEL_FALLBACKS. `request` меняется на месте, чтобы
    следующая попытка и учёт расхода видели реально сработавшую модель."""
    dropped: set[str] = set()
    tried = {request["model"]}
    while True:
        try:
            return await client.chat.completions.create(**request)
        except openai.APIError as exc:
            param = _incompatible_param(exc, request, dropped)
            if param:
                dropped.add(param)
                logger.warning("Модель %s не принимает параметр %s — повтор без него", request["model"], param)
                value = request.pop(param)
                if param == "max_tokens":
                    request["max_completion_tokens"] = value
                elif param == "max_completion_tokens":
                    request["max_tokens"] = value
                continue
            if provider_key == OPENAI and _is_model_missing(exc):
                nxt = next((m for m in model_candidates(OPENAI) if m not in tried), None)
                if nxt:
                    logger.warning("Модель %s недоступна — пробую %s", request["model"], nxt)
                    tried.add(nxt)
                    fresh = _build_request(
                        provider_key, nxt, request["messages"],
                        request.get("temperature", 0.2),
                        request.get("max_completion_tokens") or request.get("max_tokens") or settings.MAX_OUTPUT_TOKENS,
                        "response_format" in request,
                    )
                    request.clear()
                    request.update(fresh)
                    dropped.clear()
                    continue
            raise


@dataclass
class ProbeResult:
    provider: str
    ok: bool
    model: str
    latency_ms: int = 0
    reason: str | None = None


_probe_cache: dict[str, tuple[float, ProbeResult]] = {}
_PROBE_TTL_SECONDS = 60


async def probe_provider(provider_key: str) -> ProbeResult:
    """Крошечный живой запрос к провайдеру для System Health (кэш 60 с, расход не
    пишется — это диагностика, не работа для студента)."""
    cached = _probe_cache.get(provider_key)
    if cached and time.monotonic() - cached[0] < _PROBE_TTL_SECONDS:
        return cached[1]

    prof = profile(provider_key)
    if not prof.enabled:
        result = ProbeResult(provider_key, False, prof.model_id, reason="ключ не задан")
    else:
        request = _build_request(
            provider_key, prof.model_id, [{"role": "user", "content": "Ответь одним словом: ок"}], 0.0, 64, False
        )
        started = time.monotonic()
        try:
            await _call_with_compat(provider_key, _client(provider_key), request)
            if provider_key == OPENAI and request["model"] != prof.model_id:
                remember_working_model(OPENAI, request["model"])
            _note_ok(provider_key, request["model"])
            result = ProbeResult(
                provider_key, True, request["model"], latency_ms=int((time.monotonic() - started) * 1000)
            )
        except openai.APIError as exc:
            reason = classify_error(exc, provider_key, request["model"])
            _note_error(provider_key, "PROBE", reason)
            result = ProbeResult(provider_key, False, request["model"], reason=reason)
    _probe_cache[provider_key] = (time.monotonic(), result)
    return result


async def transcribe(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """Speech-to-Text (§20 ТЗ, этап 4A.7) — Whisper, только OpenAI: у DeepSeek нет
    STT API, поэтому TaskModelMap здесь не применяется и провайдер жёстко OpenAI,
    без фолбэка (нет альтернативного провайдера, на который можно переключиться)."""
    prof = profile(OPENAI)
    if not prof.enabled:
        raise LLMError("Голосовой ввод сейчас недоступен (OPENAI_API_KEY не задан)")
    client = _client(OPENAI)

    started = time.monotonic()
    try:
        response = await client.audio.transcriptions.create(
            model=settings.WHISPER_MODEL,
            file=(filename, audio_bytes),
            response_format="verbose_json",
        )
    except openai.APIError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        reason = classify_error(exc, OPENAI, settings.WHISPER_MODEL)
        logger.warning("Whisper вернул ошибку: %s — %s", exc, reason)
        _note_error(OPENAI, "SPEECH_TO_TEXT", reason)
        await record_usage(
            task="SPEECH_TO_TEXT",
            profile=prof,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            error=str(exc),
        )
        raise LLMError(PROVIDER_ERROR_MESSAGE, reason=reason) from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    duration = float(getattr(response, "duration", 0.0) or 0.0)
    await record_usage(
        task="SPEECH_TO_TEXT",
        profile=prof,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        latency_ms=latency_ms,
        audio_seconds=duration,
    )
    return (response.text or "").strip()


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
        # Модель могла добавить слова до/после JSON — берём объект между крайними скобками.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    for field in json_schema.get("required", []):
        if field not in data:
            return None
    return data
