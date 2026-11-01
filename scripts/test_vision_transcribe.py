"""Чтение фото плана/оглавления (MedAP, 01.11.2026): python -m scripts.test_vision_transcribe (без сети и базы).

Модель подменена: проверяется разбор ответа — пустые строки и лишние пробелы убираются, число строк ограничено,
уверенность берётся из ответа, кривой ответ не роняет функцию.
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("OPENAI_API_KEY", "x")

for heavy in ["rag.generator", "rag.retriever", "app.workflows.multi"]:
    m = types.ModuleType(heavy)
    m.__getattr__ = lambda name: (lambda *a, **k: None)  # type: ignore[assignment]
    sys.modules[heavy] = m

from app.workflows import vision  # noqa: E402

checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        raise SystemExit(f"НЕ ПРОШЛО: {label}")


class _Result:
    def __init__(self, data):
        self.data = data


async def main() -> None:
    async def fake(data):
        async def complete(*a, **k):
            return _Result(data)

        return complete

    original = vision.llm.complete
    try:
        vision.llm.complete = lambda *a, **k: asyncio.sleep(0, _Result({"lines": ["1.  Воспаление ", "", "  2. Тромбоз", 5], "confidence": 0.9, "qualityIssue": None}))
        lines, conf, issue = await vision.transcribe_image(b"x")
        check(lines == ["1. Воспаление", "2. Тромбоз", "5"], f"строки очищены: {lines}")
        check(abs(conf - 0.9) < 1e-9 and issue is None, "уверенность из ответа")

        vision.llm.complete = lambda *a, **k: asyncio.sleep(0, _Result({"lines": [f"т{i}" for i in range(2000)]}))
        lines, _, _ = await vision.transcribe_image(b"x")
        check(len(lines) == vision.MAX_TRANSCRIBE_LINES, "число строк ограничено")

        vision.llm.complete = lambda *a, **k: asyncio.sleep(0, _Result({"lines": "не список"}))
        lines, conf, _ = await vision.transcribe_image(b"x")
        check(lines == [], "кривой ответ — пустой список, а не падение")
    finally:
        vision.llm.complete = original


asyncio.run(main())
print(f"OK: {checks} проверок")
