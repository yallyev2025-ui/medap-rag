"""SSRF-защита при ingestion URL (§19, §34 ТЗ, этап 4A.6): запрет фетча по URL,
указывающему на приватную/служебную сеть (RFC1918, loopback, link-local — включая
облачный metadata-эндпоинт 169.254.169.254 — multicast, reserved, unspecified).

Проверяется РЕЗОЛВНУТЫЙ IP-адрес, а не только текст хоста в URL: "localhost",
"127.0.0.1" и т.п. попадают под запрет одинаково через сам IP после DNS-résolve.

Осознанное и явно задокументированное ограничение V1: защита от прямого SSRF
(приватный/служебный IP в самом URL) и от редиректов на приватный адрес (в
app/workflows/web_research.py редиректы отключены целиком), но НЕ от DNS rebinding
между моментом этой проверки и моментом фактического HTTP-запроса (TOCTOU) — тот
же уровень строгости, что у остальной security в этом репозитории (простой
bearer-токен, простой rate limiter), не защита периметра банковского уровня.
"""

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}


class BlockedURLError(Exception):
    """URL заблокирован: недопустимая схема или резолвится в приватную/служебную сеть."""


def _is_blocked_ip(ip_str: str) -> bool:
    addr = ipaddress.ip_address(ip_str)
    # IPv6-mapped IPv4 (::ffff:127.0.0.1) разворачиваем перед проверкой — иначе
    # is_loopback/is_private может не сработать на замаскированный адрес.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_public_url(url: str) -> str:
    """Возвращает hostname после проверки. Бросает BlockedURLError, если схема
    не http/https, хост не резолвится, либо хоть один резолвнутый адрес — приватный/служебный.

    Синхронная (socket.getaddrinfo блокирующий) — вызывающий код оборачивает в
    asyncio.to_thread, как и другую блокирующую работу в этом репозитории
    (rag/processor.py::extract_document, scripts/load_books.py::load_book)."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise BlockedURLError(f"Недопустимая схема URL: {parsed.scheme or '(нет)'}")
    if not parsed.hostname:
        raise BlockedURLError("Не удалось определить хост в URL")

    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise BlockedURLError(f"Не удалось разрешить хост: {parsed.hostname}") from exc

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = sockaddr[0]
        if _is_blocked_ip(ip):
            raise BlockedURLError(
                f"URL указывает на приватный/служебный адрес ({ip}) — заблокировано"
            )

    return parsed.hostname
