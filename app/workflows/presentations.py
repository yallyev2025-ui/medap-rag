"""Посредник моделей для презентаций сайта (этап 3 презентаций, 16.10.2026).

Сайт сам собирает промпты, источники и разбирает ответ; боту достаётся только вызов модели: модели Sonnet и Luna
доступны лишь с этого сервера (за границей). Это не «умный роутер»: сайт называет модель явно, список закрыт,
всё остальное отклоняется. Потолок вывода и размера запроса защищает от случайных и вредных трат: токен сервиса
есть только у сайта, но ошибка в сайте не должна разорить счёт.

Расход пишется как у остальных задач (AIUsageEvent, §59 ТЗ) и возвращается сайту, который ведёт свой журнал шагов.
"""

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from app.llm import provider as llm
from app.llm.registry import ModelProfile, profile
from app.llm.task_map import OPENAI
from app.llm.usage import record_usage
from config import settings

logger = logging.getLogger(__name__)

ModelKey = Literal["sonnet", "luna"]

SONNET_MODEL = "claude-sonnet-5-5"
LUNA_MODEL = "gpt-6-luna"

# Шаги конвейера сайта: незнакомый шаг отклоняется (опечатка в сайте не должна молча уходить в модель).
STEPS = frozenset(
    {
        "outline", "write", "rewrite", "parse", "command", "notes", "verify", "look", "place", "photoplan", "photojudge",
        # MedAP, этап 1: роли конвейера знаний (docs/medap/plan, таблица ролей) идут через тот же посредник:
        # сайт стоит в России и до модели не дотягивается, бот — да. Потолки и учёт расхода общие.
        "role_structure", "role_passport", "role_extractor", "role_writer", "role_checker", "role_fixer", "role_completeness",
    }
)

# Снимки человека (шаг «look») и найденные/нарисованные картинки на строгом отборе (шаг «photojudge»): картинки уходят модели только на этом шаге; потолки защищают счёт и память.
IMAGE_STEPS = frozenset({"look", "photojudge"})
MAX_IMAGES = 12
MAX_IMAGE_B64_CHARS = 600_000  # ~450 КБ файла
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})

# Потолки: вывод одного вызова и размер запроса (знаков). Пачка из 5 слайдов «подробно» укладывается с запасом.
MAX_OUTPUT_TOKENS = 16_000
MAX_REQUEST_CHARS = 150_000

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_TIMEOUT_S = 170.0
ANTHROPIC_RETRY_STATUSES = frozenset({429, 500, 502, 503, 529})

# Цены Sonnet 5.5, $ за миллион токенов (кэш-чтение — десятая часть входа).
SONNET_PROFILE = ModelProfile(
    provider="anthropic",
    model_id=SONNET_MODEL,
    base_url=None,
    supports_vision=True,
    supports_structured_output=False,
    supports_tools=True,
    context_window=200_000,
    pricing_version="2026-10",
    input_price_usd=2.0,
    cached_input_price_usd=0.2,
    output_price_usd=10.0,
    enabled=True,
)


class RelayError(RuntimeError):
    """Ошибка посредника: текст предназначен владельцу, ключей и адресов в нём нет."""


@dataclass
class RelayResult:
    text: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    model: str
    truncated: bool = False


def check_request(system: str, user: str, max_tokens: int, step: str, images: list[dict] | None = None) -> None:
    """Проверка потолков до любого платного вызова."""
    if images:
        if step not in IMAGE_STEPS:
            raise RelayError(f"Шаг «{step}» не принимает картинки")
        if len(images) > MAX_IMAGES:
            raise RelayError(f"Картинок не больше {MAX_IMAGES}")
        for img in images:
            if img.get("mime") not in IMAGE_MIMES:
                raise RelayError("Картинка должна быть JPEG, PNG или WebP")
            if not img.get("data") or len(img["data"]) > MAX_IMAGE_B64_CHARS:
                raise RelayError("Картинка пустая или слишком большая")
    if step not in STEPS:
        raise RelayError(f"Неизвестный шаг «{step}»")
    if max_tokens < 1 or max_tokens > MAX_OUTPUT_TOKENS:
        raise RelayError(f"Лимит вывода должен быть от 1 до {MAX_OUTPUT_TOKENS} токенов")
    if len(system) + len(user) > MAX_REQUEST_CHARS:
        raise RelayError(f"Запрос длиннее {MAX_REQUEST_CHARS} знаков")
    if not user.strip():
        raise RelayError("Пустой запрос")


def anthropic_body(system: str, user: str, max_tokens: int, json_mode: bool, images: list[dict] | None = None) -> dict:
    """Тело запроса Sonnet. «Мысли» модели входят в выходные токены, поэтому усилие низкое: иначе дороже реального пути."""
    sys_text = f"{system}\n\nОтвет — ровно один JSON-объект, без пояснений и без markdown." if json_mode else system
    return {
        "model": SONNET_MODEL,
        "max_tokens": max_tokens,
        "system": sys_text,
        "messages": [{"role": "user", "content": [*[{"type": "image", "source": {"type": "base64", "media_type": i["mime"], "data": i["data"]}} for i in images or []], {"type": "text", "text": user}]}],
        "output_config": {"effort": "low"},
    }


def parse_anthropic(data: dict) -> RelayResult:
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage") or {}
    cached = int(usage.get("cache_read_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    return RelayResult(
        text=text,
        input_tokens=int(usage.get("input_tokens") or 0) + cached + created,
        cached_input_tokens=cached,
        output_tokens=int(usage.get("output_tokens") or 0),
        model=str(data.get("model") or SONNET_MODEL),
        truncated=data.get("stop_reason") == "max_tokens",
    )


async def _sonnet(system: str, user: str, max_tokens: int, json_mode: bool, images: list[dict] | None = None) -> RelayResult:
    if not settings.ANTHROPIC_API_KEY:
        raise RelayError("Sonnet не подключён: в переменных бота нет ANTHROPIC_API_KEY")
    base = (settings.ANTHROPIC_BASE_URL or "https://api.anthropic.com").rstrip("/")
    headers = {"content-type": "application/json", "x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": ANTHROPIC_VERSION}
    body = anthropic_body(system, user, max_tokens, json_mode, images)
    async with httpx.AsyncClient(timeout=ANTHROPIC_TIMEOUT_S) as client:
        for attempt in (1, 2):
            try:
                response = await client.post(f"{base}/v1/messages", headers=headers, json=body)
            except httpx.HTTPError as exc:
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                raise RelayError(f"Anthropic не ответил: {type(exc).__name__}") from exc
            if response.status_code in ANTHROPIC_RETRY_STATUSES and attempt == 1:
                await asyncio.sleep(1.0)
                continue
            if response.status_code != 200:
                # Тело ответа может содержать подробности поставщика; наружу — только код и тип ошибки.
                kind = ""
                try:
                    kind = str((response.json().get("error") or {}).get("type") or "")
                except ValueError:
                    pass
                raise RelayError(f"Anthropic ответил ошибкой {response.status_code}{f' ({kind})' if kind else ''}")
            return parse_anthropic(response.json())
    raise RelayError("Anthropic не ответил")  # недостижимо, для типизации


async def _luna(system: str, user: str, max_tokens: int, json_mode: bool, images: list[dict] | None = None) -> RelayResult:
    if not settings.OPENAI_API_KEY:
        raise RelayError("Luna не подключена: в переменных бота нет OPENAI_API_KEY")
    content: object = user
    if images:
        content = [{"type": "text", "text": user}, *[{"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}} for i in images]]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    request = llm._build_request(OPENAI, LUNA_MODEL, messages, 0.2, max_tokens, json_mode)  # noqa: SLF001
    try:
        response = await llm._call_with_compat(OPENAI, llm._client(OPENAI), request)  # noqa: SLF001
    except llm.openai.APIError as exc:
        raise RelayError(f"OpenAI: {llm.classify_error(exc, OPENAI, request['model'])}") from exc
    choice = response.choices[0]
    input_tokens, cached, output_tokens = llm._usage_tokens(response)  # noqa: SLF001
    return RelayResult(
        text=choice.message.content or "",
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        model=request["model"],
        truncated=getattr(choice, "finish_reason", None) == "length",
    )


async def complete(
    model: ModelKey, step: str, system: str, user: str, max_tokens: int, json_mode: bool, images: list[dict] | None = None
) -> RelayResult:
    """Один вызов модели по просьбе сайта; расход записывается в любом исходе."""
    check_request(system, user, max_tokens, step, images)
    started = time.monotonic()
    prof = SONNET_PROFILE if model == "sonnet" else dataclasses.replace(profile(OPENAI), model_id=LUNA_MODEL)
    try:
        result = await (_sonnet if model == "sonnet" else _luna)(system, user, max_tokens, json_mode, images)
    except RelayError as exc:
        await record_usage(
            task="PRESENTATION_COMPLETE", profile=prof, input_tokens=0, cached_input_tokens=0, output_tokens=0,
            latency_ms=int((time.monotonic() - started) * 1000), error=str(exc)[:300],
        )
        raise
    prof = dataclasses.replace(prof, model_id=result.model)
    await record_usage(
        task="PRESENTATION_COMPLETE", profile=prof, input_tokens=result.input_tokens,
        cached_input_tokens=result.cached_input_tokens, output_tokens=result.output_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    if result.truncated:
        logger.warning("Презентации: шаг %s (%s) упёрся в лимит вывода %s", step, model, max_tokens)
    return result
