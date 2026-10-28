"""Рисование картинок для презентаций сайта (этап 7, 21.10.2026; модели — 27.10.2026).

Сайт сам решает, что и где рисовать, и пишет описание; боту достаётся только вызов рисовальной модели: ключ поставщика есть только на этом сервере.
Как и у текстовых моделей, здесь жёсткие рамки: размер картинки — из закрытого списка, описание ограничено, число заказов на человека в час ограничено,
расход пишется в учёт при любом исходе. Поставщик подключаемый (`IMAGE_PROVIDER`): сейчас реализован fal.ai.

Модели (`MODELS`): `flux-schnell` (по умолчанию, дёшево), `flux-2-dev` (следующее поколение Flux), `gpt-2.5-flare-low` и `gpt-2.5-flare-medium`
(GPT Image 2.5, вариант Flare). Какую взять, решает сайт: обычным людям — всегда модель по умолчанию, владельцу — выбор перед рисованием. Неизвестное имя
модели — отказ, а не молчаливая замена. Новая модель — запись в `MODELS`, остальной код не меняется.
"""

import base64
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import httpx

from app.llm.registry import ModelProfile
from app.llm.usage import record_usage
from config import settings

logger = logging.getLogger(__name__)

# Размеры только из этого списка (ширина и высота кратны 16, около одного мегапикселя — цена предсказуема)
ASPECTS: dict[str, tuple[int, int]] = {"wide": (1344, 768), "square": (1024, 1024), "tall": (768, 1344)}
ASPECT_INDEX = {"wide": 0, "square": 1, "tall": 2}
MAX_PROMPT_CHARS = 2000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 90.0
# Каждое описание заканчивается этим: Flux не поддерживает «негативное описание», поэтому запреты — прямо в тексте.
# Откуда fal отдаёт готовые файлы: другой адрес в ответе поставщика не скачивается (защита от подмены адреса)
FAL_CDN_HOSTS = ("fal.media", "fal.run", "storage.googleapis.com")
GUARD = " No text, no letters, no numbers, no logos, no watermark, no captions. No human faces and no hands in close-up."


@dataclass(frozen=True)
class Model:
    key: str
    endpoint: str
    family: str  # flux | flux2 | gpt
    label: str
    quality: str = ""
    # Размеры (ширина, высота) для wide / square / tall: у каждой модели свои допустимые
    sizes: tuple[tuple[int, int], tuple[int, int], tuple[int, int]] = ((1344, 768), (1024, 1024), (768, 1344))
    # Цена одной картинки, $: (квадрат, остальные размеры). fal.ai, 27.10.2026; для GPT Image 2.5 токенный расчёт — «остальные» оценены по площади
    price: tuple[float, float] = (0.004, 0.004)


MODELS: dict[str, Model] = {
    "flux-schnell": Model("flux-schnell", "fal-ai/flux/schnell", "flux", "Flux Schnell"),
    "flux-2-dev": Model("flux-2-dev", "fal-ai/flux-2", "flux2", "FLUX.2 dev", sizes=((1280, 768), (992, 992), (768, 1280)), price=(0.012, 0.012)),
    "gpt-2.5-flare-low": Model("gpt-2.5-flare-low", "openai/gpt-image-2.5/flare/text-to-image", "gpt", "GPT Image 2.5 Flare · low", "low", sizes=((1536, 864), (1024, 1024), (864, 1536)), price=(0.00588, 0.0075)),
    "gpt-2.5-flare-medium": Model("gpt-2.5-flare-medium", "openai/gpt-image-2.5/flare/text-to-image", "gpt", "GPT Image 2.5 Flare · medium", "medium", sizes=((1536, 864), (1024, 1024), (864, 1536)), price=(0.01317, 0.0167)),
}
DEFAULT_MODEL = "flux-schnell"


def resolve_model(key: str | None = None) -> Model:
    """Модель по имени из запроса, иначе из настройки `IMAGE_MODEL`, иначе Flux Schnell. Неизвестное имя в запросе — ошибка"""
    if key:
        if key not in MODELS:
            raise ImageError("Неизвестная модель рисования")
        return MODELS[key]
    configured = (settings.IMAGE_MODEL or DEFAULT_MODEL).strip()
    if configured in MODELS:
        return MODELS[configured]
    # прежнее значение настройки — адрес модели Flux Schnell
    return MODELS[DEFAULT_MODEL]


def price_usd(model: Model, width: int, height: int) -> float:
    """Сколько стоит одна картинка этой моделью (для Flux Schnell — цена из настройки `IMAGE_PRICE_USD`)"""
    if model.key == "flux-schnell":
        return settings.IMAGE_PRICE_USD
    return model.price[0] if width == height else model.price[1]


class ImageError(RuntimeError):
    """Ошибка рисования: текст для владельца и человека, ключей и адресов в нём нет."""


@dataclass
class ImageResult:
    data: bytes
    mime: str
    width: int
    height: int
    model: str
    cost_usd: float


_recent: dict[str, deque[float]] = defaultdict(deque)


def _check_quota(user_id: str) -> None:
    now = time.monotonic()
    q = _recent[user_id]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= settings.IMAGE_PER_HOUR:
        raise ImageError(f"Слишком много картинок за час (не больше {settings.IMAGE_PER_HOUR}). Попробуйте позже.")
    q.append(now)


def check_request(prompt: str, aspect: str) -> None:
    if aspect not in ASPECTS:
        raise ImageError("Неизвестный размер картинки")
    if len(prompt.strip()) < 10:
        raise ImageError("Описание картинки слишком короткое")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ImageError(f"Описание длиннее {MAX_PROMPT_CHARS} знаков")


def _profile(model: Model) -> ModelProfile:
    return ModelProfile(
        provider=settings.IMAGE_PROVIDER, model_id=model.endpoint, base_url=None, supports_vision=False, supports_structured_output=False,
        supports_tools=False, context_window=0, pricing_version="2026-10", input_price_usd=0.0, cached_input_price_usd=0.0, output_price_usd=0.0,
        enabled=bool(settings.IMAGE_API_KEY),
    )


async def _fal(prompt: str, width: int, height: int, transport: httpx.AsyncBaseTransport | None = None, model: Model | None = None) -> tuple[bytes, str, int, int]:
    """fal.ai: синхронный вызов `fal.run/<модель>` отдаёт ссылку на готовую картинку; скачиваем её сами и отдаём сайту байтами."""
    model = model or resolve_model()
    base = settings.IMAGE_BASE_URL.rstrip("/")
    headers = {"content-type": "application/json", "authorization": f"Key {settings.IMAGE_API_KEY}"}
    size = {"width": width, "height": height}
    if model.family == "gpt":
        # только подтверждённые поставщиком поля: описание, размер (кратен 16, не уже 1:3) и качество
        body = {"prompt": prompt, "image_size": size, "quality": model.quality}
    elif model.family == "flux2":
        body = {"prompt": prompt, "image_size": size, "num_images": 1, "enable_safety_checker": True, "output_format": "jpeg"}
    else:
        body = {"prompt": prompt, "image_size": size, "num_inference_steps": 4, "num_images": 1, "enable_safety_checker": True, "output_format": "jpeg"}
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
        for attempt in (1, 2):
            try:
                resp = await client.post(f"{base}/{model.endpoint}", headers=headers, json=body)
            except httpx.HTTPError as exc:
                if attempt == 1:
                    continue
                raise ImageError(f"Поставщик картинок не ответил: {type(exc).__name__}") from exc
            if resp.status_code in (429, 500, 502, 503) and attempt == 1:
                continue
            if resp.status_code != 200:
                # Тело ответа может содержать подробности поставщика; наружу — только код
                raise ImageError(f"Поставщик картинок ответил ошибкой {resp.status_code}")
            break
        data = resp.json()
        images = data.get("images") or []
        if not images or not isinstance(images[0], dict) or not images[0].get("url"):
            raise ImageError("Поставщик картинок не вернул картинку")
        if (data.get("has_nsfw_concepts") or [False])[0]:
            raise ImageError("Картинка не прошла проверку безопасности поставщика — перепишите описание")
        url = str(images[0]["url"])
        host = (httpx.URL(url).host or "").lower() if url.startswith("https://") else ""
        if not host or not any(host == h or host.endswith("." + h) for h in FAL_CDN_HOSTS):
            raise ImageError("Поставщик картинок вернул небезопасный адрес")
        pic = await client.get(url)
        if pic.status_code != 200 or not pic.content or len(pic.content) > MAX_IMAGE_BYTES:
            raise ImageError("Готовую картинку не удалось получить")
        mime = str(pic.headers.get("content-type", "image/jpeg")).split(";")[0].strip()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise ImageError("Поставщик вернул не картинку")
        return pic.content, mime, int(images[0].get("width") or width), int(images[0].get("height") or height)


PROVIDERS = {"fal": _fal}


async def generate(user_id: str, prompt: str, aspect: str, model_key: str | None = None) -> ImageResult:
    """Одна картинка по описанию сайта; расход записывается в любом исходе."""
    check_request(prompt, aspect)
    if not settings.IMAGE_API_KEY:
        raise ImageError("Рисование картинок не подключено: в переменных бота нет IMAGE_API_KEY")
    model = resolve_model(model_key)
    adapter = PROVIDERS.get(settings.IMAGE_PROVIDER)
    if adapter is None:
        raise ImageError("Поставщик картинок не поддерживается (IMAGE_PROVIDER)")
    _check_quota(user_id)
    width, height = model.sizes[ASPECT_INDEX[aspect]]
    started = time.monotonic()
    prof = _profile(model)
    try:
        data, mime, w, h = await adapter(prompt.strip() + GUARD, width, height, model=model)
    except ImageError as exc:
        await record_usage(task="PRESENTATION_IMAGE", profile=prof, input_tokens=0, cached_input_tokens=0, output_tokens=0, latency_ms=int((time.monotonic() - started) * 1000), error=str(exc)[:300])
        raise
    price = price_usd(model, width, height)
    await record_usage(task="PRESENTATION_IMAGE", profile=prof, input_tokens=0, cached_input_tokens=0, output_tokens=0, latency_ms=int((time.monotonic() - started) * 1000), image_units=1, fixed_cost_usd=price)
    return ImageResult(data=data, mime=mime, width=w, height=h, model=model.key, cost_usd=price)


def to_b64(result: ImageResult) -> str:
    return base64.b64encode(result.data).decode()
