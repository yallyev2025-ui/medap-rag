"""Ссылка на мини-приложение «Звонок» (MedAP, этап 5; решения 40–42): подпись общим секретом бота и сайта.

Бот выдаёт ссылку только подписчикам; сайт проверяет подпись и срок и больше ничего у бота не спрашивает.
Формат: `<base64url(JSON)>.<hex HMAC-SHA256>`; JSON: `tg` (id в Telegram), `subject` (предмет или null), `exp` (unix-секунды).
Чистая логика: без сети и базы.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

TTL_SECONDS = 2 * 60 * 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_call_token(secret: str, tg_id: int, subject: str | None, now: float | None = None, ttl: int = TTL_SECONDS) -> str:
    payload = json.dumps({"tg": str(tg_id), "subject": subject, "exp": int((now if now is not None else time.time()) + ttl)}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = _b64(payload)
    mac = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def call_url(base_url: str, token: str, mode: str | None = None) -> str:
    """Адрес мини-приложения; `mode="exam"` открывает устный экзамен по экзаменам студента на сайте"""
    return f"{base_url.rstrip('/')}/call?t={token}" + (f"&mode={mode}" if mode == "exam" else "")


def enter_url(base_url: str, token: str, to: str) -> str:
    """Вход в учёбу на сайте из мини-приложения: `to` — prepare («Подготовиться к паре»), exams («Экзамен») или learn"""
    target = to if to in ("prepare", "exams", "learn") else "exams"
    return f"{base_url.rstrip('/')}/api/tg/enter?t={token}&to={target}"
