"""Клиент бота презентаций к сайту Medap: одна дверь `POST {SLIDES_SITE_URL}/api/slides-bot`.

Запрос подписан общим секретом (HMAC-SHA256 от «время.тело», заголовки `x-slides-timestamp` и `x-slides-signature`) — тот же
алгоритм, что в `apps/web/src/lib/slides/bot-sign.ts` на сайте. Секрет и адрес берутся из настроек; без них клиент честно
отвечает «связь не настроена», а не падает.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Создание презентации и выдача файла на сайте могут идти дольше обычного запроса
TIMEOUT_SECONDS = 100.0


def sign_body(secret: str, timestamp: int, body: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{timestamp}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()


class SiteClient:
    def __init__(self, base_url: str | None = None, secret: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._base = (base_url if base_url is not None else settings.SLIDES_SITE_URL).rstrip("/")
        self._secret = secret if secret is not None else settings.SLIDES_SITE_SECRET
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base) and len(self._secret) >= 16

    async def call(self, action: str, telegram_id: int | None = None, **payload: Any) -> dict[str, Any]:
        """Вызвать действие сайта. Всегда возвращает словарь с `ok`; сетевые сбои превращаются в `ok: False` с понятным текстом."""
        if not self.configured:
            return {"ok": False, "error": "Связь с сайтом не настроена (SLIDES_SITE_URL, SLIDES_SITE_SECRET)"}
        body = json.dumps({"action": action, **({"telegramId": telegram_id} if telegram_id is not None else {}), **payload}, ensure_ascii=False)
        ts = int(time.time())
        headers = {"content-type": "application/json", "x-slides-timestamp": str(ts), "x-slides-signature": sign_body(self._secret, ts, body)}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self._transport) as http:
                resp = await http.post(f"{self._base}/api/slides-bot", content=body.encode("utf-8"), headers=headers)
        except httpx.HTTPError:
            logger.warning("Сайт не ответил на действие %s", action, exc_info=True)
            return {"ok": False, "error": "Сайт сейчас не отвечает. Попробуйте через минуту.", "network": True}
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and "ok" in data:
            return data
        logger.warning("Сайт ответил %s на действие %s", resp.status_code, action)
        return {"ok": False, "error": "Сайт ответил неожиданно. Попробуйте ещё раз позже.", "network": True}


site = SiteClient()
