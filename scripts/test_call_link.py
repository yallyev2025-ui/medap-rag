"""Ссылка «Звонок» (MedAP, этап 5): python -m scripts.test_call_link (без сети и базы)."""

import base64
import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.voice.call_link import call_url, sign_call_token  # noqa: E402

checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"НЕ ПРОШЛО: {label}")


tok = sign_call_token("секрет", 12345, "Патологическая физиология", now=1000.0)
body, mac = tok.split(".")
check(hmac.compare_digest(mac, hmac.new("секрет".encode(), body.encode(), hashlib.sha256).hexdigest()), "подпись — HMAC-SHA256 от тела общим секретом")
data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
check(data == {"tg": "12345", "subject": "Патологическая физиология", "exp": 1000 + 7200}, f"содержимое: {data}")
check(sign_call_token("секрет", 1, None, now=0.0).split(".")[0] != body, "разные пользователи — разные ссылки")
check(call_url("https://site.ru/", tok) == f"https://site.ru/call?t={tok}", "адрес без двойного слэша")
print(f"OK: {checks} проверок")
