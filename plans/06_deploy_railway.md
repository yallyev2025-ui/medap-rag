# Фаза 06 — Деплой на Railway, финальная проверка

## Цель
Подготовить проект к продакшен-запуску на Railway 24/7, проверить работу с реальными учебниками и реальными студенческими вопросами.

## Предпосылки
Фазы 00–05 выполнены и протестированы локально. У пользователя есть Railway-аккаунт, Postgres с pgvector (создан в фазе 00).

## Что сделать

### `railway.toml` (финал)
```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "python -m bot.main"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 5
```
- aiogram polling-бот не требует HTTP healthcheck (это не веб-сервис) — `restartPolicyType = ON_FAILURE` обеспечивает автоперезапуск из ТЗ.
- Если Railway требует слушать порт — добавить минимальный `aiohttp` healthcheck-сервер на `$PORT`, отвечающий 200 OK (опционально, по необходимости).

### Переменные окружения на Railway
В настройках сервиса добавить (Variables tab):
- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `GEMINI_MODEL=gemini-2.0-flash`
- `DATABASE_URL` — взять из связанного Postgres-плагина Railway (можно через reference variable `${{Postgres.DATABASE_URL}}`, адаптировать схему `postgresql://` → `postgresql+asyncpg://` в `config.py` если нужно)
- `ADMIN_IDS`
- `EMBEDDING_MODEL_NAME`, `EMBEDDING_DIM`
- `FREE_DAILY_LIMIT`

### Инициализация БД на проде
- Перед первым запуском бота выполнить `python -m db.init_db` через Railway Shell/одноразовый Run, чтобы создать `vector` extension и таблицы.

### Оценка ресурсов (ВАЖНО)
- `intfloat/multilingual-e5-large` ≈ 2.2GB веса + torch overhead → ориентировочно 3-4GB RAM в момент инференса.
- Railway Hobby план — проверить текущий лимит RAM в Settings → Resources. Если бот падает с OOM в логах:
  - вариант A: перейти на Railway Pro / увеличить RAM-лимит сервиса.
  - вариант B (fallback, без переписывания кода): сменить `EMBEDDING_MODEL_NAME` на `intfloat/multilingual-e5-small` (384-dim) — требует пересчитать все эмбеддинги в `book_chunks` (`EMBEDDING_DIM` меняется → перезагрузить книги через `load_books.py`).
- Зафиксировать в README выбранный план и причину.

### Загрузка реальных учебников
- Через `/addbook` (фаза 05) или напрямую `scripts/load_books.py` (если есть shell-доступ к Railway с примонтированным временным файлом — иначе только через бота).
- Загрузить минимум 1-2 учебника на разные предметы для финального теста.

### Финальный чек-лист
- [ ] Бот отвечает на `/start`, `/help`, `/limit`.
- [ ] Реальный вопрос по загруженному учебнику → ответ с `[Автор, учебник]`.
- [ ] Вопрос "сделай конспект по ..." → структурированный ответ.
- [ ] Вопрос "объясни простыми словами ..." → ответ с аналогиями из контекста.
- [ ] Вопрос не по теме материалов → "Этой информации нет в материалах MedAP...".
- [ ] 11-й запрос за день от обычного пользователя → сообщение о лимите.
- [ ] `/stats` от админа → корректные цифры.
- [ ] `/addbook` загружает новый учебник без перезапуска бота.
- [ ] `/broadcast`, `/ban`, `/premium` работают.
- [ ] Логи Railway не показывают OOM/краши за период наблюдения (например, 30 минут активного использования).

## После завершения
Обновить корневой `README.md`: краткое описание, ссылка на `plans/`, инструкция по локальному запуску (`.env`, `pip install`, `python -m db.init_db`, `python -m bot.main`).
