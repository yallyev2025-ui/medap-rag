"""Проверка посредника картинок /v1/images/generate: без сети, БД и ключей (поставщик подменён).

Запуск: python scripts/test_presentation_images.py
Проверяется: токен сервиса, закрытый список размеров, потолки описания, запреты в тексте для Flux, потолок заказов на человека в час,
разбор ответа fal.ai (ссылка → байты), чужой адрес картинки не скачивается, проверка безопасности поставщика, повтор при 5xx,
ошибки поставщика приходят в поле error (не кодом 5xx), расход пишется по цене за картинку — и при сбое тоже.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

from unittest.mock import MagicMock  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

for _ in range(30):
    try:
        from app.api import v1  # noqa: E402
        break
    except ModuleNotFoundError as _exc:
        _name = _exc.name or ""
        if not _name or _name.split(".")[0] in ("app", "db", "rag", "config", "constants"):
            raise
        sys.modules[_name] = MagicMock()
        for _k in [k for k in sys.modules if k.startswith("app.api")]:
            del sys.modules[_k]

import httpx  # noqa: E402

from app.workflows import images as im  # noqa: E402
from config import settings  # noqa: E402

TOKEN = "t" * 32
settings.SERVICE_TOKEN = TOKEN
HEAD = {"Authorization": f"Bearer {TOKEN}"}
settings.IMAGE_API_KEY = "k" * 20
settings.IMAGE_PRICE_USD = 0.004
settings.IMAGE_PER_HOUR = 3
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 200

recorded: list[dict] = []
prompts: list[str] = []


async def fake_record(**kwargs) -> None:
    recorded.append(kwargs)


im.record_usage = fake_record


def fal_transport(nsfw: bool = False, url: str = "https://v3.fal.media/files/a/b.jpg", fail_first: bool = False) -> httpx.MockTransport:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            state["n"] += 1
            if fail_first and state["n"] == 1:
                return httpx.Response(503, text="busy")
            assert request.headers["authorization"] == "Key " + "k" * 20
            body = __import__("json").loads(request.content)
            prompts.append(body["prompt"])
            assert body["image_size"] == {"width": 1344, "height": 768} and body["num_images"] == 1 and body["enable_safety_checker"] is True, body
            return httpx.Response(200, json={"images": [{"url": url, "width": 1344, "height": 768, "content_type": "image/jpeg"}], "has_nsfw_concepts": [nsfw]})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    return httpx.MockTransport(handler)


async def test_adapter() -> None:
    data, mime, w, h = await im._fal("A calm abstract background in teal and amber", 1344, 768, fal_transport())
    assert data == JPEG and mime == "image/jpeg" and (w, h) == (1344, 768)
    data, *_ = await im._fal("A calm abstract background in teal and amber", 1344, 768, fal_transport(fail_first=True))
    assert data == JPEG, "один повтор при 503"
    for bad in (fal_transport(nsfw=True), fal_transport(url="https://evil.test/x.jpg"), fal_transport(url="http://v3.fal.media/x.jpg"), fal_transport(url="https://169.254.169.254/latest")):
        try:
            await im._fal("A calm abstract background in teal and amber", 1344, 768, bad)
        except im.ImageError:
            continue
        raise AssertionError("небезопасный или заблокированный ответ принят")

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret provider details")

    try:
        await im._fal("A calm abstract background in teal and amber", 1344, 768, httpx.MockTransport(broken))
    except im.ImageError as exc:
        assert "401" in str(exc) and "secret" not in str(exc), "наружу только код, без подробностей поставщика"
    else:
        raise AssertionError("ошибка поставщика не поднята")


def test_endpoint() -> None:
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    body = {"prompt": "Soft abstract gradient background, teal and warm amber, lots of empty space", "aspect": "wide", "context": {"userId": "u1"}}

    async def fake_fal(prompt, w, h):
        prompts.append(prompt)
        return JPEG, "image/jpeg", w, h

    im.PROVIDERS["fal"] = fake_fal
    prompts.clear()
    r = client.post("/v1/images/generate", json=body, headers=HEAD)
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    j = r.json()
    assert __import__("base64").b64decode(j["imageBase64"]) == JPEG and j["mime"] == "image/jpeg" and (j["width"], j["height"]) == (1344, 768) and j["costUsd"] == 0.004, j
    assert prompts[-1].endswith(im.GUARD) and "No text" in prompts[-1] and "faces" in prompts[-1], "запреты на текст и лица дописаны к описанию"
    ok_rec = recorded[-1]
    assert ok_rec["task"] == "PRESENTATION_IMAGE" and ok_rec["image_units"] == 1 and ok_rec["fixed_cost_usd"] == 0.004 and ok_rec.get("error") is None, "расход: одна картинка по цене за штуку"

    assert client.post("/v1/images/generate", json=body).status_code == 401
    assert client.post("/v1/images/generate", json={**body, "aspect": "huge"}, headers=HEAD).status_code == 422
    assert client.post("/v1/images/generate", json={**body, "prompt": "коротко"}, headers=HEAD).status_code == 422
    assert client.post("/v1/images/generate", json={**body, "prompt": "x" * 901}, headers=HEAD).status_code == 422

    # потолок заказов на человека в час (3): третья проходит, четвёртая отклонена словами, а не 5xx
    im._recent.clear()
    for _ in range(3):
        assert client.post("/v1/images/generate", json=body, headers=HEAD).json()["error"] is None
    r = client.post("/v1/images/generate", json=body, headers=HEAD)
    assert r.status_code == 200 and "Слишком много картинок" in r.json()["error"], r.text
    assert client.post("/v1/images/generate", json={**body, "context": {"userId": "u2"}}, headers=HEAD).json()["error"] is None, "потолок у каждого свой"

    # сбой поставщика: error, а не 5xx; расход со сбоем записан без стоимости
    async def boom(prompt, w, h):
        raise im.ImageError("Поставщик картинок ответил ошибкой 500")

    im.PROVIDERS["fal"] = boom
    im._recent.clear()
    n = len(recorded)
    r = client.post("/v1/images/generate", json=body, headers=HEAD)
    assert r.status_code == 200 and "500" in r.json()["error"] and r.json()["imageBase64"] == "", r.text
    assert len(recorded) == n + 1 and recorded[-1]["error"] and "fixed_cost_usd" not in recorded[-1], "сбой записан без стоимости"

    # ключ не задан — честный отказ
    settings.IMAGE_API_KEY = ""
    r = client.post("/v1/images/generate", json=body, headers=HEAD)
    assert "IMAGE_API_KEY" in r.json()["error"], r.text
    settings.IMAGE_API_KEY = "k" * 20


def main() -> None:
    asyncio.run(test_adapter())
    test_endpoint()
    print("ok: посредник картинок — токен, размеры, потолки, запреты в описании, ответ fal.ai, чужой адрес, расход и ошибки")


if __name__ == "__main__":
    main()
