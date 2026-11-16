"""Регрессия аудита: изоляция, ранняя защита загрузок, реальные лимиты тела."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.evidence.viewer import fetch_evidence
from app.security.admin_boundary import AdminBoundaryMiddleware
from app.security.auth import RateLimiter
from app.security.body_size import MaxBodySizeMiddleware, v1_body_limits


def test_evidence_denies_other_user_before_presigning():
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            return SimpleNamespace(user_id="owner")

    async def check():
        with patch("app.evidence.viewer.async_session", Session), patch("app.evidence.viewer.s3.presigned_url") as presign:
            assert await fetch_evidence(7) is None
            assert await fetch_evidence(7, owner_id="other") is None
            presign.assert_not_called()
    asyncio.run(check())


def test_limiter_expires_abandoned_keys_and_bounds_memory():
    limiter = RateLimiter(2, max_keys=3)
    with patch("app.security.auth.time.monotonic", return_value=0):
        for user in ("a", "b", "c"):
            limiter.check(user)
    with patch("app.security.auth.time.monotonic", return_value=61):
        limiter.check("d")
    assert list(limiter._hits) == ["d"]


def test_upload_auth_runs_before_reading_body_and_login_is_limited():
    async def endpoint(request):
        await request.body()
        return JSONResponse({"ok": True})

    async def check():
        read = False

        async def body():
            nonlocal read
            read = True
            yield b"untrusted multipart"

        app = Starlette(routes=[Route("/admin/sources/upload", endpoint, methods=["POST"]), Route("/admin/login", endpoint, methods=["POST"])])
        app.add_middleware(AdminBoundaryMiddleware)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("app.security.admin_boundary.is_admin", return_value=False):
                response = await client.post("/admin/sources/upload", content=body())
            assert response.status_code == 401 and not read
            for _ in range(10):
                assert (await client.post("/admin/login", content=b"x")).status_code == 200
            assert (await client.post("/admin/login", content=b"x")).status_code == 429
    asyncio.run(check())


def test_admin_chunked_body_is_bounded():
    async def endpoint(request):
        await request.body()
        return JSONResponse({"ok": True})

    async def check():
        app = Starlette(routes=[Route("/admin/models/apply", endpoint, methods=["POST"])])
        app.add_middleware(AdminBoundaryMiddleware)

        async def body():
            yield bytes(40 * 1024)
            yield bytes(40 * 1024)
        with patch("app.security.admin_boundary.is_admin", return_value=True):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.post("/admin/models/apply", content=body())).status_code == 413
    asyncio.run(check())


def test_voice_and_vision_use_upload_limits():
    async def endpoint(request):
        await request.body()
        return JSONResponse({"ok": True})

    async def check():
        paths = ["/v1/voice/transcribe", "/v1/evaluate/oral", "/v1/vision/transcribe"]
        app = Starlette(routes=[Route(path, endpoint, methods=["POST"]) for path in paths])
        app.add_middleware(MaxBodySizeMiddleware, limits=v1_body_limits())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in paths:
                assert (await client.post(path, content=bytes(3 * 1024 * 1024))).status_code == 200
            assert (await client.post(paths[0], headers={"content-length": str(v1_body_limits()[paths[0]] + 1)})).status_code == 413
    asyncio.run(check())


def test_unverified_answer_never_enters_cache():
    from app.workflows.answer_cache import store

    async def check():
        with patch("app.workflows.answer_cache._embed", side_effect=AssertionError("must not embed")):
            await store(question="test question", tier="fast", source_type="учебник", subject=None,
                        mode="question", kind="api", fmt="plain", chunk_ids=[1], answer="test",
                        citations=[], verified=None)
    asyncio.run(check())


def test_cache_cannot_reuse_removed_citation():
    import json
    from app.workflows.answer_cache import pick_match

    entry = SimpleNamespace(chunk_ids="[1,2]", citations=json.dumps([{"evidenceId": "2"}]))
    assert pick_match([(entry, 0.99)], [1], 0.9, 0.7) is None
    assert pick_match([(entry, 0.99)], [1, 2], 0.9, 0.7) is entry
