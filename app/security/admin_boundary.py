"""Отказ до multipart parser; авторизованные файлы читаются потоком с потолком."""

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.security.auth import RateLimiter, is_admin
from config import settings


class BodyLimitExceeded(Exception):
    pass


class AdminBoundaryMiddleware:
    def __init__(self, app):
        self.app = app
        self.login_limiter = RateLimiter(10)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/admin" or path.startswith("/admin/")):
            return await self.app(scope, receive, send)
        request = Request(scope)
        login = path.rstrip("/") == "/admin/login"
        if login and request.method == "POST":
            try:
                # Адрес сокета, а не произвольный X-Forwarded-For посетителя.
                self.login_limiter.check(request.client.host if request.client else "unknown")
            except HTTPException:
                return await JSONResponse({"detail": "Слишком много попыток входа"}, 429)(scope, receive, send)
        if not login and not is_admin(request):
            if request.method in ("GET", "HEAD"):
                return await RedirectResponse("/admin/login", status_code=303)(scope, receive, send)
            return await JSONResponse({"detail": "Нужен вход в админку"}, 401)(scope, receive, send)

        # multipart overhead включён; обычным формам не нужен лимит учебника.
        limit = 64 * 1024
        if path == "/admin/sources/upload":
            limit = (settings.MAX_UPLOAD_MB + 1) * 1024 * 1024
        elif path == "/admin/playground/oral":
            limit = (settings.ORAL_MAX_AUDIO_MB + 1) * 1024 * 1024
        length = request.headers.get("content-length")
        try:
            if length is not None and int(length) > limit:
                return await JSONResponse({"detail": "Запрос слишком большой"}, 413)(scope, receive, send)
        except ValueError:
            pass
        consumed = 0
        started = False

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise BodyLimitExceeded()
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except BodyLimitExceeded:
            if started:
                raise
            await JSONResponse({"detail": "Запрос слишком большой"}, 413)(scope, receive, send)
