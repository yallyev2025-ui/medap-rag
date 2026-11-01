"""Мониторинг клинреков (MedAP, этап 5): python -m scripts.test_clinrek_monitor (без сети и базы)."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")
heavy = types.ModuleType("scripts.load_books")
heavy.load_book = lambda *a, **k: None  # type: ignore[attr-defined]
sys.modules["scripts.load_books"] = heavy

from app.integrations.clinrek_sync import ClinrecUpdate, RemoteClinrec, updates_payload  # noqa: E402

ups = [
    ClinrecUpdate(remote=RemoteClinrec(external_ref="1_2", code="1", version="2", title="Гипертоническая болезнь", category=None), is_new=False, previous_book_id=7),
    ClinrecUpdate(remote=RemoteClinrec(external_ref="9_1", code="9", version="1", title="Новая", category=None), is_new=True),
]
p = updates_payload(ups)
assert p[0] == {"code": "1", "version": "2", "title": "Гипертоническая болезнь", "externalRef": "1_2", "isNew": False, "previousBookId": 7}, p[0]
assert p[1]["isNew"] is True and p[1]["previousBookId"] is None
assert updates_payload([]) == []
print("OK: 3 проверки")
