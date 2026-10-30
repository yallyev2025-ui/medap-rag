"""Переобработка книги глубоким разбором как новой версии (MedAP, этап 1).

    python -m scripts.reprocess_book --book-id 12              # собрать версию N+1 рядом со старой
    python -m scripts.reprocess_book --book-id 12 --activate   # собрать и сразу переключить поиск
    python -m scripts.reprocess_book --book-id 12 --switch 2   # только переключить на версию 2 (или откатить на 1)

Сначала собирают без --activate, сверяют выдачу (python -m evals.run) и переключают по отчёту.
"""

import argparse
import asyncio

from scripts.load_books import activate_version, reprocess_book


def main() -> None:
    parser = argparse.ArgumentParser(description="Переобработка книги новой версией разбора")
    parser.add_argument("--book-id", type=int, required=True)
    parser.add_argument("--activate", action="store_true", help="Переключить поиск на новую версию сразу")
    parser.add_argument("--switch", type=int, help="Только переключить на указанную версию (откат или выкладка)")
    args = parser.parse_args()

    if args.switch is not None:
        count = asyncio.run(activate_version(args.book_id, args.switch))
        print(f"Книга {args.book_id}: поиск отвечает по версии {args.switch} ({count} фрагментов).")
        return

    result = asyncio.run(reprocess_book(args.book_id, activate=args.activate))
    print(
        f"Книга {result['book_id']}: версия {result['version']}, фрагментов {result['chunks']}, "
        f"рисунков {result['figures']}. " + ("Поиск переключён." if result["activated"] else "Поиск пока по прежней версии.")
    )


if __name__ == "__main__":
    main()
