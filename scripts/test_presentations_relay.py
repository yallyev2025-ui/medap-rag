"""Проверка посредника презентаций /v1/presentations/complete: без сети, БД и ключей (поставщики подменены).

Запуск: python scripts/test_presentations_relay.py
Проверяется: токен сервиса, закрытый список моделей, потолки токенов и размера, незнакомый шаг, формат запроса и разбор ответа
Anthropic, учёт расхода (в т.ч. при сбое), ошибка поставщика приходит в поле error, а не кодом 5xx.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

# Тяжёлые и не нужные этой проверке библиотеки (torch, aiogram и др.) на машине разработчика могут быть не установлены:
# недостающие подменяются заглушками, пока приложение не импортируется целиком.
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

from app.workflows import presentations as pr  # noqa: E402
from config import settings  # noqa: E402

TOKEN = "t" * 32
settings.SERVICE_TOKEN = TOKEN
HEAD = {"Authorization": f"Bearer {TOKEN}"}
BASE = {
    "model": "sonnet", "step": "write", "system": "Система", "user": "Напиши слайд", "maxTokens": 2000, "json": True,
    "context": {"userId": "u1"},
}

recorded: list[dict] = []
calls: list[tuple] = []


async def fake_record(**kwargs) -> None:
    recorded.append(kwargs)


async def fake_sonnet(system, user, max_tokens, json_mode):
    calls.append(("sonnet", max_tokens, json_mode))
    return pr.RelayResult(text='{"slide":1}', input_tokens=1200, cached_input_tokens=0, output_tokens=300, model="claude-sonnet-5-5")


async def fake_luna(system, user, max_tokens, json_mode):
    calls.append(("luna", max_tokens, json_mode))
    return pr.RelayResult(text="{}", input_tokens=500, cached_input_tokens=100, output_tokens=50, model="gpt-6-luna")


REAL_SONNET = pr._sonnet  # настоящий вызов Sonnet для проверки на подменённом транспорте
pr.record_usage = fake_record
pr._sonnet = fake_sonnet
pr._luna = fake_luna

app = FastAPI()
app.include_router(v1.router)
client = TestClient(app)


def post(body: dict, headers: dict | None = HEAD):
    return client.post("/v1/presentations/complete", json=body, headers=headers or {})


def check_sonnet_http() -> None:
    """Настоящий `_sonnet` на подменённом транспорте: заголовки, повтор при 529, ошибки словами, без ключа — отказ."""
    import httpx

    from app.workflows import presentations as live

    seen: list[httpx.Request] = []
    script: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return script.pop(0)

    real = httpx.AsyncClient
    live.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)  # type: ignore[assignment]
    real_sleep = live.asyncio.sleep

    async def no_sleep(_s):
        return None

    live.asyncio.sleep = no_sleep  # type: ignore[assignment]
    try:
        settings.ANTHROPIC_API_KEY = ""
        try:
            asyncio.run(REAL_SONNET("s", "u", 100, False))
            raise AssertionError("без ключа вызов не должен проходить")
        except live.RelayError as exc:
            assert "ANTHROPIC_API_KEY" in str(exc) and "sk-" not in str(exc), str(exc)

        settings.ANTHROPIC_API_KEY = "sk-secret-key"
        ok = {"content": [{"type": "text", "text": "ответ"}], "usage": {"input_tokens": 10, "output_tokens": 5}, "model": "claude-sonnet-5-5"}
        script[:] = [httpx.Response(529, json={"error": {"type": "overloaded_error"}}), httpx.Response(200, json=ok)]
        res = asyncio.run(REAL_SONNET("s", "u", 100, True))
        assert res.text == "ответ" and len(seen) == 2, "один повтор при 529"
        assert seen[0].headers["x-api-key"] == "sk-secret-key" and seen[0].headers["anthropic-version"] == "2023-06-01"
        assert str(seen[0].url) == "https://api.anthropic.com/v1/messages"

        script[:] = [httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "секрет sk-secret-key в тексте"}})]
        try:
            asyncio.run(REAL_SONNET("s", "u", 100, False))
            raise AssertionError("ошибка 400 должна подниматься")
        except live.RelayError as exc:
            assert "400" in str(exc) and "invalid_request_error" in str(exc) and "sk-secret-key" not in str(exc), str(exc)

        script[:] = [httpx.Response(503), httpx.Response(503)]
        try:
            asyncio.run(REAL_SONNET("s", "u", 100, False))
            raise AssertionError("после одного повтора ошибка поднимается")
        except live.RelayError as exc:
            assert "503" in str(exc)
    finally:
        live.httpx.AsyncClient = real  # type: ignore[assignment]
        live.asyncio.sleep = real_sleep  # type: ignore[assignment]
        settings.ANTHROPIC_API_KEY = ""


def main() -> None:
    # токен сервиса
    assert post(BASE, headers={}).status_code == 401
    assert post(BASE, headers={"Authorization": "Bearer wrong"}).status_code == 401

    # Sonnet и Luna: текст, расход и модель возвращаются; учёт записан
    r = post(BASE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == '{"slide":1}' and body["inputTokens"] == 1200 and body["outputTokens"] == 300, body
    assert body["model"] == "claude-sonnet-5-5" and body["error"] is None, body
    assert calls[-1] == ("sonnet", 2000, True), calls
    assert recorded[-1]["task"] == "PRESENTATION_COMPLETE" and recorded[-1]["profile"].model_id == "claude-sonnet-5-5"
    assert recorded[-1]["profile"].input_price_usd == 2.0 and recorded[-1]["profile"].output_price_usd == 10.0

    r = post({**BASE, "model": "luna", "step": "outline"})
    assert r.status_code == 200 and r.json()["model"] == "gpt-6-luna", r.text
    assert recorded[-1]["cached_input_tokens"] == 100

    # закрытый список: чужая модель, неизвестный шаг, потолки
    assert post({**BASE, "model": "gpt-9"}).status_code == 422
    assert post({**BASE, "maxTokens": pr.MAX_OUTPUT_TOKENS + 1}).status_code == 422
    assert post({**BASE, "maxTokens": 0}).status_code == 422
    assert post({**BASE, "user": ""}).status_code == 422
    n = len(calls)
    r = post({**BASE, "step": "что-то-новое"})
    assert r.status_code == 200 and "Неизвестный шаг" in r.json()["error"], r.text
    r = post({**BASE, "system": "я" * 100_000, "user": "я" * 60_000})
    assert r.status_code == 200 and "длиннее" in r.json()["error"], r.text
    assert len(calls) == n, "платный вызов до проверки потолков"

    # ошибка поставщика — в поле error, ключей и адресов в тексте нет, расход записан с ошибкой
    async def boom(*_a):
        raise pr.RelayError("Anthropic ответил ошибкой 529 (overloaded_error)")

    pr._sonnet = boom
    r = post(BASE)
    assert r.status_code == 200 and r.json()["text"] == "" and "529" in r.json()["error"], r.text
    assert recorded[-1]["error"] and recorded[-1]["output_tokens"] == 0

    # формат запроса и разбор ответа Anthropic
    b = pr.anthropic_body("Система", "Запрос", 1500, True)
    assert b["model"] == "claude-sonnet-5-5" and b["max_tokens"] == 1500 and b["output_config"] == {"effort": "low"}
    assert "JSON" in b["system"] and b["messages"][0]["content"][0]["text"] == "Запрос"
    assert "JSON" not in pr.anthropic_body("Система", "Запрос", 1500, False)["system"]
    parsed = pr.parse_anthropic({
        "content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "А"}, {"type": "text", "text": "Б"}],
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10, "output_tokens": 70},
        "stop_reason": "max_tokens", "model": "claude-sonnet-5-5",
    })
    assert parsed.text == "АБ" and parsed.input_tokens == 150 and parsed.cached_input_tokens == 40 and parsed.output_tokens == 70
    assert parsed.truncated is True

    check_sonnet_http()
    print("ok: посредник презентаций — токен, закрытый список, потолки, учёт расхода, ошибки поставщика")


if __name__ == "__main__":
    main()
