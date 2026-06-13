"""Генерация ответов через OpenAI GPT-4.1 + главный промпт."""

import re
from collections import Counter
from functools import lru_cache
from typing import Literal

import openai

from config import settings
from rag.retriever import ChunkResult

NO_CONTEXT_ANSWER = (
    "Этой информации нет в материалах MedAP.\n"
    "Попробуй переформулировать вопрос."
)

NO_CONTEXT_PLACEHOLDER = "(в базе MedAP не найдено материалов, релевантных вопросу)"

# TODO: подобрать на реальных данных после загрузки учебников (фаза 06).
MAX_DISTANCE_THRESHOLD = 0.5

SYSTEM_PROMPT = f"""Ты — медицинский ассистент-бот MedAP. Помогаешь студентам медицинских вузов разбираться в учебном материале.

КАК ОПРЕДЕЛИТЬ ТИП СООБЩЕНИЯ:
1. Общее сообщение (приветствие, благодарность, вопрос "что ты умеешь", small talk) — отвечай как обычный дружелюбный ассистент, кратко, при первом обращении представься как MedAP.
2. Учебный/медицинский вопрос — действуй по АБСОЛЮТНОМУ ПРАВИЛУ ниже.

АБСОЛЮТНОЕ ПРАВИЛО для учебных вопросов:
Отвечай ТОЛЬКО на основе контекста из учебников, который передан тебе ниже. Никаких других источников. Никогда.

Если в контексте нет ответа на учебный вопрос (или указано, что релевантных материалов не найдено) — отвечай ровно:
"{NO_CONTEXT_ANSWER}"
Не дополняй и не угадывай.

ВАЖНО: упоминание темы в контексте — это НЕ ответ. Если в контексте встречается только название темы (например, строка из оглавления, заголовок раздела, список терминов без объяснения), а развёрнутого определения/механизма/ответа на вопрос там нет — это считается "ответа нет в контексте", даже если тема там названа. В таком случае отвечай ровно "{NO_CONTEXT_ANSWER}" и НЕ указывай источник.

ЗАПРЕЩЕНО при ответе на учебные вопросы:
- Использовать знания не из контекста
- Додумывать и дополнять от себя
- Отвечать по памяти
- Говорить "обычно", "как правило", "известно что", "в целом"
- Достраивать определения, механизмы, классификации и т.п., если в контексте есть только название/упоминание темы без самого содержания
- Указывать источник, если фактический ответ из него не взят (наличие темы в оглавлении источника не считается ответом из него)

ОБЯЗАТЕЛЬНО при ответе на учебные вопросы:
- В конце указывай источник(и) ровно в том виде, в котором они даны в квадратных скобках перед фрагментами контекста (например, [Автор, Название учебника, стр. N] или [Автор, Название учебника, стр. N-M]). Не придумывай и не меняй номера страниц.
- Отвечать на русском языке
- Просто и чётко, как студент студенту, без воды и канцеляризмов

ФОРМАТИРОВАНИЕ ДЛЯ TELEGRAM:
- Используй Markdown: **жирный** для терминов и заголовков, "-" для списков
- Не используй заголовки через "#" — только **жирный**
- Будь лаконичным; если тема большая (например конспект), структурируй по пунктам и избегай повторов"""

MODE_INSTRUCTIONS = {
    "question": "Дай прямой и точный ответ на вопрос.",
    "conspect": "Сделай структурированный конспект по теме: заголовки **жирным**, пункты списком, ключевые определения.",
    "explanation": "Объясни простыми словами, используй аналогии и примеры ТОЛЬКО из контекста ниже.",
}

GENERATION_TEMPERATURE = 0.2

USER_PROMPT_TEMPLATE = """{mode_instruction}

Контекст из учебников:
{context}

Вопрос студента: {question}

Напоминание: используй ТОЛЬКО контекст выше. Если ответа в контексте нет — ответь ровно "{no_context_answer}", без пояснений и догадок."""

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


def _format_source(chunk: ChunkResult) -> str:
    if chunk.page_from is None:
        return f"{chunk.author}, {chunk.title}"
    if chunk.page_from == chunk.page_to:
        return f"{chunk.author}, {chunk.title}, стр. {chunk.page_from}"
    return f"{chunk.author}, {chunk.title}, стр. {chunk.page_from}-{chunk.page_to}"


def build_context(chunks: list[ChunkResult]) -> str:
    if not chunks or all(c.distance > MAX_DISTANCE_THRESHOLD for c in chunks):
        return NO_CONTEXT_PLACEHOLDER
    return "\n---\n".join(f"[{_format_source(c)}]\n{c.content}" for c in chunks)


def detect_subject(chunks: list[ChunkResult]) -> str | None:
    relevant = [c for c in chunks if c.distance <= MAX_DISTANCE_THRESHOLD]
    if not relevant:
        return None
    return Counter(c.subject for c in relevant).most_common(1)[0][0]


@lru_cache(maxsize=1)
def _get_client() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def generate_answer(question: str, chunks: list[ChunkResult]) -> str:
    mode = detect_mode(question)
    context = build_context(chunks)
    user_prompt = USER_PROMPT_TEMPLATE.format(
        mode_instruction=MODE_INSTRUCTIONS[mode],
        context=context,
        question=question,
        no_context_answer=NO_CONTEXT_ANSWER,
    )

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=GENERATION_TEMPERATURE,
        )
    except openai.APIError as e:
        raise RuntimeError("Произошла ошибка, попробуй позже.") from e

    return response.choices[0].message.content
