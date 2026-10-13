"""Проверка, что бот ходит в GPT-6 Luna, а не в старую модель (без секретов в выводе).

Запуск (из корня кода бота):  python scripts/check_openai_model.py
Что делает:
1. Печатает настройки, которые реально видит бот (модель, запасные, цены, адрес, переопределения).
   Если на сервере в переменных окружения остались старые значения — они будут видны здесь.
2. Показывает, на каком провайдере сидят ответы, конспекты, фото и оценка.
3. Один крошечный живой запрос к OpenAI: какая модель ответила, сколько токенов, сколько стоит.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm.provider import _build_request, _call_with_compat, _client, _usage_tokens  # noqa: E402
from app.llm.registry import cost_usd, model_candidates, profile, to_rub  # noqa: E402
from app.llm.task_map import DEFAULT_TASK_MODEL_MAP, OPENAI, Task, _overrides  # noqa: E402
from config import settings  # noqa: E402

WANTED = "gpt-6-luna"


def main() -> int:
    prof = profile(OPENAI)
    print("== Настройки, которые видит бот ==")
    print(f"ключ OpenAI:            {'есть' if settings.OPENAI_API_KEY else 'НЕТ'}")
    print(f"OPENAI_MODEL:           {settings.OPENAI_MODEL}")
    print(f"OPENAI_EVAL_MODEL:      {settings.OPENAI_EVAL_MODEL or '(пусто — берётся OPENAI_MODEL)'}")
    print(f"запасные модели:        {settings.OPENAI_MODEL_FALLBACKS}")
    print(f"модель в работе:        {prof.model_id}   (цепочка: {' → '.join(model_candidates(OPENAI))})")
    print(f"цены $/млн вход/кеш/выход: {prof.input_price_usd} / {prof.cached_input_price_usd} / {prof.output_price_usd}   (версия цен {settings.PRICING_VERSION})")
    print(f"адрес OpenAI:           {settings.OPENAI_BASE_URL or '(облако OpenAI)'}")
    print(f"глубокий режим:         провайдер «{settings.TIER_DEEP_PROVIDER}»")
    print(f"переопределения задач:  {settings.TASK_MODEL_MAP_OVERRIDES or '(нет)'}")

    print("\n== Кто что делает ==")
    overrides = _overrides()
    for task in (Task.GROUNDED_QA, Task.EXPLAIN, Task.DOCUMENT_QA, Task.CONSPECT_WRITE, Task.VISION_EXTRACT, Task.RECALL_EVALUATE):
        provider = overrides.get(task, DEFAULT_TASK_MODEL_MAP[task])
        who = prof.model_id if provider == OPENAI else f"{provider} (НЕ OpenAI)"
        print(f"{task.value:22s} → {who}")

    problems = []
    if prof.model_id != WANTED:
        problems.append(f"в работе модель «{prof.model_id}», а нужна «{WANTED}» — проверьте переменные OPENAI_MODEL / OPENAI_EVAL_MODEL на сервере")
    if abs(prof.output_price_usd - 0.5) > 1e-9 or abs(prof.input_price_usd - 0.1) > 1e-9:
        problems.append("цены не 0.10 / 0.50 — на сервере остались старые OPENAI_EVAL_*_PRICE_USD, суммы на дашборде будут неверными")
    if not settings.OPENAI_API_KEY:
        problems.append("нет ключа OpenAI")

    if settings.OPENAI_API_KEY:
        print("\n== Живой запрос (копейки) ==")
        request = _build_request(OPENAI, prof.model_id, [{"role": "user", "content": "Ответь одним словом: готово"}], 0.2, 64, False)
        print("параметры запроса:", {k: v for k, v in request.items() if k != "messages"})
        started = time.monotonic()
        try:
            response = asyncio.run(_call_with_compat(OPENAI, _client(OPENAI), request))
        except Exception as exc:  # noqa: BLE001 — здесь нужен любой текст ошибки для владельца
            print(f"✗ запрос не удался: {type(exc).__name__}: {str(getattr(exc, 'message', exc))[:300]}")
            problems.append("живой запрос не прошёл")
        else:
            inp, cached, out = _usage_tokens(response)
            usd = cost_usd(prof, inp, cached, out)
            print(f"✓ ответила модель:   {getattr(response, 'model', request['model'])}  (просили {request['model']})")
            print(f"  ответ:             {(response.choices[0].message.content or '').strip()[:80]!r}")
            print(f"  токены вход/кеш/выход: {inp} / {cached} / {out}")
            print(f"  стоимость:         ${usd:.6f} ≈ {to_rub(usd):.4f} ₽  за {time.monotonic() - started:.1f} с")
            if request["model"] != prof.model_id:
                print(f"  ⚠ основная модель не ответила, сработала запасная: {request['model']}")
                problems.append("сработала запасная модель — основной id не принят OpenAI")

    print("\n== Итог ==")
    if problems:
        for p in problems:
            print("✗", p)
        return 1
    print(f"✓ всё на «{WANTED}», цены верные")
    return 0


if __name__ == "__main__":
    sys.exit(main())
