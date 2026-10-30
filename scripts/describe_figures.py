"""Описать рисунки книги моделью (один раз на картинку, с кэшем по хэшу).

    python -m scripts.describe_figures --book-id 12 [--version 2] [--limit 50]
"""

import argparse
import asyncio

from app.workflows.figure_describe import describe_book_figures


def main() -> None:
    parser = argparse.ArgumentParser(description="Описание рисунков учебника")
    parser.add_argument("--book-id", type=int, required=True)
    parser.add_argument("--version", type=int)
    parser.add_argument("--limit", type=int, help="Не больше стольких вызовов модели за запуск")
    args = parser.parse_args()
    result = asyncio.run(describe_book_figures(args.book_id, args.version, args.limit))
    print(
        f"Описано моделью: {result['described']}, взято из кэша: {result['from_cache']}, "
        f"пропущено: {result['skipped']}, сбоев: {result['failed']}."
    )


if __name__ == "__main__":
    main()
