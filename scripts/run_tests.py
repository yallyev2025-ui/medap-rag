"""Запуск всех проверок бота одной командой (MedAP, этап 1): python -m scripts.run_tests

Проверки двух видов: одни запускаются через pytest (набор функций test_*), другие написаны как
скрипты и сами печатают итог. Им не нужны ни torch, ни база, ни сеть: тяжёлые зависимости
подменяются в самих файлах, поэтому запуск работает и в обычном окружении CI.
Код возврата не ноль, если упала любая проверка.
"""

import os
import subprocess
import sys

PYTEST_FILES = [
    "scripts/test_layout.py",
    "scripts/test_sources.py",
    "scripts/test_versions.py",
    "scripts/test_book_admin.py",
    "scripts/test_verification.py",
    "scripts/test_reranker_switch.py",
]

SCRIPT_MODULES = [
    "scripts.test_cost",
    "scripts.test_usage_summary",
    "scripts.test_documents_search",
    "scripts.test_presentations_relay",
    "scripts.test_reranker_fallback",
    "scripts.test_slides_bench",
    "scripts.test_slides_bot",
    "scripts.test_slides_bot_dispatch",
    "scripts.test_waiting_content",
]

# Файлы, которые не входят сюда сознательно: test_generator.py и test_retrieve.py требуют модели
# поиска (torch) и базу; test_web_page.py, test_pubmed_sources.py, test_presentation_images.py —
# async-проверки, которые запускаются как скрипты на машине разработчика (pytest их не выполняет).


def main() -> int:
    env = {"DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db", "TELEGRAM_BOT_TOKEN": "x", **os.environ}
    failed: list[str] = []

    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *PYTEST_FILES], env=env)
    if result.returncode != 0:
        failed.append("pytest")

    for module in SCRIPT_MODULES:
        result = subprocess.run([sys.executable, "-m", module], env=env, capture_output=True, text=True)
        status = "ok " if result.returncode == 0 else "FAIL"
        print(f"{status} {module}: {(result.stdout.strip().splitlines() or [''])[-1][:100]}")
        if result.returncode != 0:
            print(result.stdout[-1500:], result.stderr[-1500:])
            failed.append(module)

    print("ВСЕ ПРОВЕРКИ ПРОШЛИ" if not failed else f"УПАЛИ: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
