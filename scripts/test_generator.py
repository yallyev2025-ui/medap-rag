"""Ручная проверка generator (фаза 02): python -m scripts.test_generator"""

import asyncio

from rag.generator import build_context, detect_mode, generate_answer
from rag.retriever import ChunkResult

MOCK_CHUNKS = [
    ChunkResult(
        content=(
            "Инфаркт миокарда — это острое заболевание, обусловленное возникновением "
            "одного или нескольких очагов ишемического некроза в сердечной мышце "
            "вследствие абсолютной или относительной недостаточности кровоснабжения "
            "миокарда."
        ),
        subject="pathanatomy",
        author="Струков",
        title="Патологическая анатомия",
        distance=0.1,
        page_from=12,
        page_to=12,
    ),
    ChunkResult(
        content=(
            "Наиболее частая причина инфаркта миокарда — тромбоз коронарной артерии, "
            "развивающийся на фоне атеросклеротической бляшки."
        ),
        subject="pathanatomy",
        author="Струков",
        title="Патологическая анатомия",
        distance=0.15,
        page_from=12,
        page_to=13,
    ),
    ChunkResult(
        content=(
            "По локализации выделяют инфаркт передней, задней, боковой стенки левого "
            "желудочка, а также инфаркт межжелудочковой перегородки."
        ),
        subject="pathanatomy",
        author="Струков",
        title="Патологическая анатомия",
        distance=0.2,
        page_from=13,
        page_to=13,
    ),
]

IRRELEVANT_CHUNKS = [
    ChunkResult(
        content="Текст не имеет отношения к вопросу.",
        subject="biochemistry",
        author="Иванов",
        title="Биохимия",
        distance=0.9,
    ),
]


def test_pure_functions() -> None:
    print("=== detect_mode ===")
    print("question:", detect_mode("Что такое инфаркт миокарда?"))
    print("conspect:", detect_mode("Сделай конспект по теме инфаркт миокарда"))
    print("explanation:", detect_mode("Объясни простыми словами, что такое инфаркт"))

    print("\n=== build_context ===")
    print(build_context(MOCK_CHUNKS[:1]))


async def test_generate_answer() -> None:
    print("\n=== generate_answer: прямой вопрос ===")
    answer = await generate_answer("Что такое инфаркт миокарда?", MOCK_CHUNKS)
    print(answer.text, "| verified=", answer.verified)

    print("\n=== generate_answer: конспект ===")
    answer = await generate_answer("Сделай конспект по теме инфаркт миокарда", MOCK_CHUNKS)
    print(answer.text, "| verified=", answer.verified)

    print("\n=== generate_answer: нерелевантные чанки (порог distance) ===")
    answer = await generate_answer("Что такое инфаркт миокарда?", IRRELEVANT_CHUNKS)
    print(answer.text, "| verified=", answer.verified)

    print("\n=== generate_answer: пустой список чанков ===")
    answer = await generate_answer("Что такое инфаркт миокарда?", [])
    print(answer.text, "| verified=", answer.verified)


async def main() -> None:
    test_pure_functions()

    if not settings_has_openai_key():
        print("\nOPENAI_API_KEY не задан — пропускаю вызовы GPT-4.1.")
        return

    await test_generate_answer()


def settings_has_openai_key() -> bool:
    from config import settings

    return bool(settings.OPENAI_API_KEY) and settings.OPENAI_API_KEY != "test"


if __name__ == "__main__":
    asyncio.run(main())
