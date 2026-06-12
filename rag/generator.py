"""Генерация ответов через OpenAI GPT-4.1 + главный промпт."""

import re
from functools import lru_cache
from typing import Literal

import openai

from config import settings
from rag.retriever import ChunkResult

SYSTEM_PROMPT = """Ты медицинский ассистент MedAP.

АБСОЛЮТНОЕ ПРАВИЛО: отвечай ТОЛЬКО на основе
контекста из учебников ниже. Никаких других
источников. Никогда.

Если ответа нет в контексте — говори:
"Этой информации нет в материалах MedAP.
Попробуй переформулировать вопрос."

ЗАПРЕЩЕНО:
- Использовать знания не из контекста
- Додумывать и дополнять от себя
- Отвечать по памяти
- Говорить "обычно", "как правило", "известно что"

ОБЯЗАТЕЛЬНО:
- Указывай источник: [Автор, учебник]
- Отвечай на русском языке
- Просто и чётко, как студент студенту
- Без воды

{mode_instruction}

Контекст из учебников:
{context}

Вопрос студента: {question}"""

NO_CONTEXT_ANSWER = (
    "Этой информации нет в материалах MedAP.\n"
    "Попробуй переформулировать вопрос."
)

# TODO: подобрать на реальных данных после загрузки учебников (фаза 06).
MAX_DISTANCE_THRESHOLD = 0.5

MODE_INSTRUCTIONS = {
    "question": "Дай прямой и точный ответ на вопрос.",
    "conspect": "Сделай структурированный конспект по теме: заголовки, пункты, ключевые определения.",
    "explanation": "Объясни простыми словами, используй аналогии и примеры ТОЛЬКО из контекста ниже.",
}

CONSPECT_PATTERN = re.compile(
    r"конспект|кратко разбери|структурируй|выпиши основное", re.IGNORECASE
)
EXPLANATION_PATTERN = re.compile(
    r"объясни|простыми словами|по-простому|на пальцах|аналоги", re.IGNORECASE
)


def detect_mode(question: str) -> Literal["question", "conspect", "explanation"]:
    if CONSPECT_PATTERN.search(question):
        return "conspect"
    if EXPLANATION_PATTERN.search(question):
        return "explanation"
    return "question"


def build_context(chunks: list[ChunkResult]) -> str:
    return "\n---\n".join(f"[{c.author}, {c.title}]\n{c.content}" for c in chunks)


@lru_cache(maxsize=1)
def _get_client() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def generate_answer(question: str, chunks: list[ChunkResult]) -> str:
    if not chunks or all(c.distance > MAX_DISTANCE_THRESHOLD for c in chunks):
        return NO_CONTEXT_ANSWER

    mode = detect_mode(question)
    context = build_context(chunks)
    prompt = SYSTEM_PROMPT.format(
        mode_instruction=MODE_INSTRUCTIONS[mode],
        context=context,
        question=question,
    )

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    except openai.APIError as e:
        raise RuntimeError("Произошла ошибка, попробуй позже.") from e

    return response.choices[0].message.content
