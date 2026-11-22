"""Готовность зависимостей: что уже загружено, а что ещё нет.

Нужно, чтобы проверка «жив ли сервер» (`/health`) и проверка «можно ли отвечать
по учебникам» (`/ready`) были разными вещами. Первая обязана отвечать сразу
после старта, вторая честно говорит «модели ещё грузятся» — и не путает
платформу деплоя, которая иначе считает живой сервер мёртвым.
"""

from enum import Enum


class ModelsState(str, Enum):
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


_state = ModelsState.LOADING


def models_state() -> ModelsState:
    return _state


def mark_models(state: ModelsState) -> None:
    global _state
    _state = state
