"""Дублирует вывод скрипта в файл: закрыл вкладку консоли — итог остаётся и открывается командой `cat файл`.

Файл живёт, пока работает контейнер (при новом деплое или перезапуске папка eval пустеет), поэтому важное лучше сразу
скопировать или заснять на экран.
"""

import sys
from pathlib import Path


def tee_output(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 — живёт до конца процесса
    original = sys.stdout

    class Tee:
        def write(self, text: str) -> int:
            original.write(text)
            handle.write(text)
            return len(text)

        def flush(self) -> None:
            original.flush()
            handle.flush()

        def __getattr__(self, name: str):
            return getattr(original, name)

    sys.stdout = Tee()  # type: ignore[assignment]
    print(f"(итог также записывается в {path}; открыть позже: cat {path})")
