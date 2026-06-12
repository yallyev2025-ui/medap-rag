# medap-rag

Telegram-бот MedAP — RAG-ассистент для студентов медицинских вузов. Отвечает на вопросы
строго по материалам загруженных учебников (с указанием источника), умеет делать
конспекты и объяснять простыми словами.

Архитектура и пошаговый план разработки — в [`plans/`](plans):

- [`00_project_skeleton.md`](plans/00_project_skeleton.md) — структура проекта, конфиг, БД
- [`01_rag_core.md`](plans/01_rag_core.md) — эмбеддинги, чанкинг, pgvector retriever, загрузка книг
- [`02_generator.md`](plans/02_generator.md) — генерация ответов (OpenAI GPT)
- [`03_telegram_bot.md`](plans/03_telegram_bot.md) — Telegram-бот: `/start`, `/help`, вопросы
- [`04_db_limits.md`](plans/04_db_limits.md) — пользователи, дневные лимиты, история запросов
- [`05_admin_stats.md`](plans/05_admin_stats.md) — админ-команды и статистика
- [`06_deploy_railway.md`](plans/06_deploy_railway.md) — деплой на Railway

## Стек

- Python, [aiogram 3](https://docs.aiogram.dev/) (Telegram-бот, long polling)
- SQLAlchemy (async) + asyncpg + PostgreSQL с расширением `pgvector`
- `sentence-transformers` (`intfloat/multilingual-e5-large`) — эмбеддинги
- OpenAI GPT — генерация ответов

## Локальный запуск

1. Установить зависимости:
   ```bash
   pip install -r requirements.txt
   ```
2. Скопировать `.env.example` в `.env` и заполнить значения:
   ```bash
   cp .env.example .env
   ```
   - `TELEGRAM_BOT_TOKEN` — токен бота от [@BotFather](https://t.me/BotFather)
   - `OPENAI_API_KEY`, `OPENAI_MODEL` — доступ к OpenAI API
   - `DATABASE_URL` — строка подключения к PostgreSQL с `pgvector`
   - `ADMIN_IDS_RAW` — Telegram ID администраторов через запятую
3. Создать расширение `vector` и таблицы:
   ```bash
   python -m db.init_db
   ```
4. Загрузить хотя бы один учебник (PDF с текстовым слоем):
   ```bash
   python -m scripts.load_books --pdf path/to/book.pdf --subject pathanatomy \
       --author "Автор" --title "Название учебника"
   ```
5. Запустить бота:
   ```bash
   python -m bot.main
   ```

## Деплой на Railway

### Сервисы

1. **PostgreSQL** — создать через Railway (плагин/шаблон Postgres), убедиться, что
   расширение `pgvector` доступно (Railway-образ Postgres его поддерживает).
2. **Бот** — отдельный сервис из этого репозитория, билдер `NIXPACKS` (см. `railway.toml`):
   ```toml
   [build]
   builder = "NIXPACKS"

   [deploy]
   startCommand = "python -m bot.main"
   restartPolicyType = "ON_FAILURE"
   restartPolicyMaxRetries = 5
   ```
   Это polling-бот, HTTP-порт не нужен.

### Переменные окружения сервиса бота

| Переменная | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота |
| `OPENAI_API_KEY` | ключ OpenAI |
| `OPENAI_MODEL` | например, `gpt-4.1` |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference variable из плагина Postgres) |
| `ADMIN_IDS_RAW` | Telegram ID админов через запятую |
| `EMBEDDING_MODEL_NAME` | `intfloat/multilingual-e5-large` |
| `EMBEDDING_DIM` | `1024` |
| `FREE_DAILY_LIMIT` | например, `10` |

`DATABASE_URL` от Railway приходит со схемой `postgres://`/`postgresql://` —
`config.py` автоматически приводит её к `postgresql+asyncpg://`, дополнительно
менять не нужно.

### Инициализация БД на проде

Перед первым запуском бота выполнить один раз (Railway Shell / одноразовый Run
в сервисе бота):
```bash
python -m db.init_db
```
Это создаст расширение `vector` и все таблицы.

### Загрузка учебников на проде

Через `/addbook` в боте (доступно администраторам из `ADMIN_IDS_RAW`).

**Ограничение Telegram Bot API**: бот может скачать присланный файл размером
**не более 20 МБ**. Если PDF учебника больше — сжать его или разбить на части
(каждую часть загрузить как отдельную "книгу" с тем же subject/author, но
разным title, например "Том 1", "Том 2").

### Оценка ресурсов

`intfloat/multilingual-e5-large` — ≈2.2 ГБ весов, в момент инференса процесс бота
потребляет ориентировочно **3–4 ГБ RAM**. Перед запуском проверить лимит RAM сервиса
в Railway (Settings → Resources) и при необходимости увеличить план.

Если в логах появляется OOM:
- увеличить лимит RAM сервиса (Railway Pro), либо
- перейти на `intfloat/multilingual-e5-small` (`EMBEDDING_DIM=384`) — потребует
  пересчитать эмбеддинги для всех уже загруженных книг (повторно выполнить
  `scripts/load_books.py` / `/addbook` после смены модели).

## Команды бота

**Все пользователи**: `/start`, `/help`, `/limit`

**Администраторы** (`ADMIN_IDS_RAW`): `/stats`, `/addbook`, `/broadcast`, `/ban`,
`/unban`, `/premium`, `/unpremium`
