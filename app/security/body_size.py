"""Лимит размера тела запроса ДО того, как FastAPI распарсит его в модель (батч 14,
закрывает находку батча 7).

`Depends()` здесь не годится: FastAPI резолвит параметр `payload: SomeModel`
(а значит читает и парсит всё тело) раньше, чем внутри обработчика успевает
сработать любая проверка над уже распарсенными полями — ровно это и было
найдено батчем 7 (`/v1/documents`/`/v1/evaluate/oral` проверяли размер файла
уже ПОСЛЕ полного приёма+decode, `/v1/vision/analyze` — не проверяли вовсе).
`require_service_token` (app/security/auth.py) устроен иначе не просто так: он
читает только заголовок `Authorization`, тело не трогает — тот же приём здесь
не подходит, т.к. лимит должен относиться именно к телу.

`Content-Length` — быстрый путь (не читаем тело вовсе, если заголовок уже
превышает лимит), но полагаться только на него нельзя: заголовок можно не
прислать или занизить, chunked-передача его может не нести совсем. Поэтому
тело в любом случае читается ограниченным потоком: как только прочитанных байт
больше лимита — обрыв с 413, до того как это тело успеет попасть в память
целиком. Прочитанные (в пределах лимита) байты кладутся в `request._body` —
не в `_receive`: `BaseHTTPMiddleware` в Starlette оборачивает запрос в
`_CachedRequest`, чей `wrapped_receive()` смотрит именно на `_body` (см.
`starlette/middleware/base.py`), а не на `_receive` — доклад в `_receive`
из dispatch() до обычного FastAPI body-parsing попросту не доходит.

Лимиты — с запасом ~40% сверх декодированного MB-предела конкретной задачи
(USER_DOCUMENT_MAX_MB/ORAL_MAX_AUDIO_MB/VISION_MAX_IMAGE_MB): тело — это base64
(+33% к размеру исходного файла) плюс сама JSON-обёртка, и легитимный запрос
ровно на предельный размер файла не должен отбиваться на этом внешнем слое
раньше внутренней, точной проверки в обработчике/workflow.

Осознанная граница V1 (тот же принцип, что в app/security/ssrf.py — не молчать
о пределах): защищает от переполнения памяти одним большим телом, не полноценный
anti-DoS периметр — от очень МНОГИХ параллельных запросов среднего размера
защищает rate_limiter, не это.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Путь не в этом словаре, но начинающийся с "/v1" — получает этот потолок.
# Обычные текстовые поля (chat/pubmed/web-search/content/*) в МБ не нуждаются;
# это просто разумный потолок на весь /v1 разом, тем же самым механизмом.
DEFAULT_V1_MAX_BYTES = 2 * 1024 * 1024


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limits: dict[str, int], default_prefix: str = "/v1", default_max_bytes: int = DEFAULT_V1_MAX_BYTES):
        super().__init__(app)
        self._limits = limits
        self._default_prefix = default_prefix
        self._default_max_bytes = default_max_bytes

    def _limit_for(self, path: str) -> int | None:
        if path in self._limits:
            return self._limits[path]
        if path.startswith(self._default_prefix):
            return self._default_max_bytes
        return None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        max_bytes = self._limit_for(request.url.path)
        if max_bytes is None:
            return await call_next(request)

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    return _too_large(max_bytes)
            except ValueError:
                pass  # заголовок сломан — не доверяем, читаем тело ограниченным потоком ниже

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                return _too_large(max_bytes)

        # request.body() делает то же самое (полный stream() + join), но без
        # ограничения по пути — здесь просто отдаём уже прочитанное туда же,
        # куда его положил бы сам body(), не читая поток дважды.
        request._body = bytes(body)  # noqa: SLF001
        return await call_next(request)


def _too_large(max_bytes: int) -> JSONResponse:
    return JSONResponse(
        {"detail": f"Тело запроса больше {max_bytes // (1024 * 1024)} МБ"},
        status_code=413,
    )
