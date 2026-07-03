"""Ручная проверка retriever: python -m scripts.test_retrieve "вопрос"""

import asyncio
import sys

from rag.retriever import retrieve


async def main() -> None:
    question = " ".join(sys.argv[1:]) or "Как регулируется работа сердца?"
    results = await retrieve(question)
    for r in results:
        print(f"[{r.distance:.4f}] {r.title} ({r.author}, {r.subject})")
        print(r.content[:200].replace("\n", " "))
        print("---")


if __name__ == "__main__":
    asyncio.run(main())
