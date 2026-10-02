"""Запасной DNS для всего процесса (батч 24).

На Timeweb App Platform системный DNS периодически не отвечает
(`[Errno -3] Temporary failure in name resolution`): бот не может узнать адрес
PubMed, Europe PMC, Яндекса и т.д., хотя сеть сама по себе работает.

`install()` оборачивает `socket.getaddrinfo` — через него узнают адреса все
библиотеки бота (httpx, aiohttp/aiogram, asyncpg, boto3, OpenAI SDK). Сначала
спрашиваем системный DNS как обычно; если он не ответил — запрашиваем A-запись
по DNS-over-HTTPS у Cloudflare (1.1.1.1), затем у Google (8.8.8.8). К ним
подключаемся прямо по IP (их сертификаты выданы на IP, TLS проверяется
полностью), поэтому сломанный системный DNS для этого не нужен. Подменяется
только адрес: TLS к целевому сайту проверяется по его настоящему имени как обычно.

Найденные так адреса кешируются по TTL (1–10 минут); пока запись в кеше свежая,
системный DNS для этого имени не дёргается (иначе каждое соединение ждало бы
его таймаута).
"""

import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
from urllib.parse import quote

logger = logging.getLogger(__name__)

_DOH_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("1.1.1.1", "/dns-query?name={name}&type=A"),
    ("8.8.8.8", "/resolve?name={name}&type=A"),
)
_DOH_TIMEOUT_SECONDS = 3.0
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 600

# Ошибки «DNS не ответил / не нашёл», на которых имеет смысл спросить запасной DNS.
_FALLBACK_ERRNOS = {
    code
    for code in (
        getattr(socket, "EAI_AGAIN", None),
        getattr(socket, "EAI_NONAME", None),
        getattr(socket, "EAI_FAIL", None),
        getattr(socket, "EAI_NODATA", None),
    )
    if code is not None
}

_original_getaddrinfo = None
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()


def _is_domain_name(host: str) -> bool:
    """Публичное доменное имя (не IP, не localhost, не однословное внутреннее имя)."""
    name = host.strip().rstrip(".").lower()
    if not name or name == "localhost" or "." not in name:
        return False
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return True
    return False


def _doh_lookup(name: str) -> tuple[list[str], int] | None:
    """A-записи имени по DNS-over-HTTPS: (адреса, TTL) или None, если не ответил никто."""
    context = ssl.create_default_context()
    for server_ip, path in _DOH_ENDPOINTS:
        try:
            conn = http.client.HTTPSConnection(server_ip, 443, timeout=_DOH_TIMEOUT_SECONDS, context=context)
            try:
                conn.request("GET", path.format(name=quote(name)), headers={"accept": "application/dns-json"})
                response = conn.getresponse()
                body = response.read()
            finally:
                conn.close()
            if response.status != 200:
                continue
            data = json.loads(body)
            if data.get("Status") != 0:
                continue
            answers = [a for a in data.get("Answer") or [] if a.get("type") == 1]
            ips = []
            for answer in answers:
                try:
                    ips.append(str(ipaddress.IPv4Address(str(answer.get("data", "")).strip())))
                except ValueError:
                    continue
            if ips:
                ttl = min(int(a.get("TTL", _MIN_TTL_SECONDS)) for a in answers)
                return ips, ttl
        except (OSError, ValueError, http.client.HTTPException):
            continue
    return None


def _cached(name: str) -> list[str] | None:
    with _lock:
        item = _cache.get(name)
        if item is None:
            return None
        expires, ips = item
        if expires < time.monotonic():
            _cache.pop(name, None)
            return None
        return ips


def _remember(name: str, ips: list[str], ttl: int) -> None:
    ttl = max(_MIN_TTL_SECONDS, min(_MAX_TTL_SECONDS, ttl))
    with _lock:
        _cache[name] = (time.monotonic() + ttl, ips)


def _port_number(port) -> int:
    if port is None:
        return 0
    if isinstance(port, int):
        return port
    text = port.decode() if isinstance(port, bytes) else str(port)
    try:
        return int(text)
    except ValueError:
        return socket.getservbyname(text)


def _as_addrinfo(ips: list[str], port, type_: int, proto: int) -> list[tuple]:
    port_number = _port_number(port)
    if type_:
        socktypes = [(type_, proto)]
    else:
        socktypes = [(socket.SOCK_STREAM, socket.IPPROTO_TCP), (socket.SOCK_DGRAM, socket.IPPROTO_UDP)]
    return [
        (socket.AF_INET, socktype, sock_proto or proto, "", (ip, port_number))
        for ip in ips
        for socktype, sock_proto in socktypes
    ]


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    name = host.decode() if isinstance(host, bytes) else host
    fallback_allowed = (
        isinstance(name, str) and family in (0, socket.AF_INET) and _is_domain_name(name)
    )
    if fallback_allowed:
        ips = _cached(name.rstrip(".").lower())
        if ips:
            return _as_addrinfo(ips, port, type, proto)

    try:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror as exc:
        if not fallback_allowed or exc.errno not in _FALLBACK_ERRNOS:
            raise
        key = name.rstrip(".").lower()
        found = _doh_lookup(key)
        if found is None:
            logger.warning("DNS: ни системный, ни запасной DNS не нашли адрес %s (%s)", key, exc)
            raise
        ips, ttl = found
        _remember(key, ips, ttl)
        logger.warning("DNS: системный DNS не ответил для %s (%s) — адрес взят у запасного DNS: %s", key, exc, ips)
        return _as_addrinfo(ips, port, type, proto)


def install() -> None:
    """Подключает запасной DNS. Повторный вызов ничего не делает."""
    global _original_getaddrinfo
    if _original_getaddrinfo is not None:
        return
    _original_getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = _getaddrinfo
