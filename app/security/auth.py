"""Аутентификация и лимиты (§28, §33 ТЗ).

Два независимых контура:

- `/v1` — service-to-service: образовательный сайт MedAP подтверждает себя общим
  токеном. `userId` из тела запроса сам по себе доверия не даёт (§28): это лишь
  ссылка на пользователя того сервиса, который уже прошёл аутентификацию.
- админка — вход по паролю, сессия в подписанной httponly-куке.
"""

import hmac
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import settings

ADMIN_COOKIE = "medap_admin"
# Сессия админки живёт сутки — рабочий день без повторного логина.
ADMIN_SESSION_MAX_AGE = 24 * 60 * 60


def require_service_token(authorization: str = Header(default="")) -> None:
    """Проверяет `Authorization: Bearer <SERVICE_TOKEN>` для эндпоинтов /v1."""
    if not settings.SERVICE_TOKEN:
        # Пустой токен не означает «пускать всех»: сервис просто не сконфигурирован.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SERVICE_TOKEN не задан — API /v1 отключён",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, settings.SERVICE_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отсутствующий service-token",
        )


class RateLimiter:
    """Простой скользящий лимит на пользователя в минуту (§33: rate limiting configurable).

    Состояние в памяти процесса: сервис работает одним инстансом, а при
    горизонтальном масштабировании лимит переедет в общее хранилище.
    """

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много запросов, попробуйте через минуту",
            )
        window.append(now)


rate_limiter = RateLimiter(settings.RATE_LIMIT_PER_MINUTE)


def _serializer() -> URLSafeTimedSerializer:
    secret = settings.ADMIN_SESSION_SECRET or settings.ADMIN_WEB_PASSWORD
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Админка не сконфигурирована: задайте ADMIN_WEB_PASSWORD",
        )
    return URLSafeTimedSerializer(secret, salt="medap-admin-session")


def issue_admin_session() -> str:
    return _serializer().dumps({"role": "admin"})


def check_admin_password(password: str) -> bool:
    if not settings.ADMIN_WEB_PASSWORD:
        return False
    return hmac.compare_digest(password, settings.ADMIN_WEB_PASSWORD)


def is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        return False
    try:
        _serializer().loads(token, max_age=ADMIN_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired, HTTPException):
        return False
    return True


def require_admin(request: Request) -> None:
    if not is_admin(request):
        # Роуты админки ловят это и показывают страницу входа.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Нужен вход в админку")
