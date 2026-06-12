# Фаза 04 — PostgreSQL (users/usage/queries), лимиты запросов

## Цель
Добавить полноценную модель данных для пользователей, учёта лимитов и истории запросов; подключить middleware, ограничивающий бесплатных пользователей 10 запросами в день.

## Предпосылки
Фазы 01–03 выполнены. `db/models.py` уже содержит `Book` и `BookChunk` (фаза 01).

## Что создать

### `db/models.py` (расширение)
Новые таблицы:
- `User`:
  - `id: BigInteger` (PK, = Telegram user id)
  - `username: str | None`
  - `full_name: str | None`
  - `created_at: datetime` (default now)
  - `is_premium: bool` (default False)
  - `is_banned: bool` (default False)
- `Usage`:
  - `id` PK
  - `user_id: BigInteger` (FK → users.id)
  - `date: Date`
  - `count: int` (default 0)
  - unique constraint `(user_id, date)`
- `Query`:
  - `id` PK
  - `user_id: BigInteger` (FK → users.id)
  - `question: text`
  - `answer: text`
  - `subject: str | None` (определяется автоматически — см. ниже)
  - `response_time_ms: int | None` (для статистики в фазе 05)
  - `created_at: datetime` (default now)

Запустить `db/init_db.py` повторно (или alembic-миграцию) для создания новых таблиц.

### `db/crud.py`
Асинхронные функции (SQLAlchemy async session):
- `get_or_create_user(session, telegram_user) -> User` — по `message.from_user`.
- `get_today_usage(session, user_id) -> int` — count за текущую дату (UTC).
- `increment_usage(session, user_id)` — upsert строки в `usage` (date=today, count+=1).
- `is_limit_exceeded(session, user) -> bool` — `False` если `user.is_premium` или `user.id in settings.ADMIN_IDS`, иначе `get_today_usage >= settings.FREE_DAILY_LIMIT`.
- `log_query(session, user_id, question, answer, subject, response_time_ms)`.
- `set_ban(session, user_id, banned: bool)`.
- `set_premium(session, user_id, premium: bool)`.

### Определение `subject` для записи в `Query`
В `rag/retriever.py` (фаза 01) у каждого `ChunkResult` есть `subject`. В `rag/generator.py` или в хендлере: `subject = Counter(c.subject for c in chunks).most_common(1)[0][0]` если чанки прошли порог релевантности, иначе `None`.

### `bot/middlewares/limits.py`
`class LimitsMiddleware(BaseMiddleware)`:
- На входящем сообщении (не команда): открыть сессию БД, `get_or_create_user`, проверить `is_banned` → если забанен, тихо игнорировать (не отвечать).
- Проверить `is_limit_exceeded`:
  - если превышен → ответить:
    ```
    Ты использовал 10 бесплатных запросов сегодня.
    Лимит обновится в 00:00.

    Хочешь безлимит? → MedAP Premium [ссылка]
    ```
    и не передавать управление дальше (`return`, не вызывать `handler`).
  - если не превышен → вызвать `handler(event, data)`, после успешного ответа — `increment_usage`.

Регистрация middleware в `bot/main.py`: `dp.message.middleware(LimitsMiddleware())`.

### `bot/handlers/query.py` (доработка из фазы 03)
- Замерить время генерации ответа (`time.monotonic()` до/после `generate_answer`) → `response_time_ms`.
- После отправки ответа — `log_query(session, user.id, question, answer, subject, response_time_ms)`.

### `/limit` (полная реализация в `bot/handlers/start.py`)
```
Сегодня использовано: {count}/{FREE_DAILY_LIMIT}
(или "У тебя безлимитный доступ ✅" для premium/admin)
```

### Тест фазы
1. `db/init_db.py` создаёт новые таблицы без ошибок.
2. Локально отправить от тестового аккаунта 11 сообщений подряд — на 11-м должен прийти текст про лимит, до этого — обычные ответы.
3. `/limit` показывает корректный счётчик после нескольких запросов.
4. Проверить, что для `user.id in ADMIN_IDS` лимит не применяется.
5. Проверить, что в таблице `queries` появляются записи с корректным `subject` и `response_time_ms`.

## Зависимость для следующих фаз
- Фаза 05 использует `crud.py` (set_ban, set_premium) и таблицы `users`/`queries`/`books` для `/stats`.
