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
- `sentence-transformers`: `intfloat/multilingual-e5-large` (эмбеддинги) +
  cross-encoder реранкер (`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`)
- OpenAI GPT — генерация ответов с проверкой заземления

## Как устроен RAG (защита от выдумок)

Бот построен так, чтобы отвечать строго по материалам и не галлюцинировать:

1. **Чистка при загрузке** — страницы оглавления/индекса отсеиваются, текст режется
   на чанки по предложениям (определения не рвутся), у каждого чанка хранится
   номер страницы.
2. **Двухэтапный поиск** — pgvector достаёт кандидатов, cross-encoder реранкер
   переупорядочивает их и даёт честный скор релевантности `0..1`.
3. **Логика отказа** — если ни один фрагмент не проходит порог реранкера
   (`RERANK_SCORE_THRESHOLD`), бот отвечает «этой информации нет в материалах»
   и НЕ выдумывает.
4. **Заземление ответа** — генератор отвечает только по контексту и указывает
   источник `[Автор, Название, стр. N]`.
5. **Проверочный проход** — второй вызов LLM сверяет каждое утверждение с
   контекстом; при выдумке запускается корректирующая перегенерация
   (`VERIFY_GROUNDING`).

Замер качества и калибровка порога — `scripts/eval_rag.py` на наборе
`eval/dataset.jsonl` (см. комментарии в файлах).

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
4. Загрузить хотя бы один учебник (PDF/`.docx`/`.txt`; сканы распознаются OCR,
   если установлены `tesseract-ocr`/`tesseract-ocr-rus`/`poppler-utils`):
   ```bash
   python -m scripts.load_books --file path/to/book.pdf --subject pathanatomy \
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
| `FREE_DAILY_LIMIT` | например, `5` |

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

**Поддерживаемые форматы:**
- **PDF с текстовым слоем** — текст извлекается напрямую (`pdfplumber`).
- **Сканированный PDF** (страницы-картинки) — бот сам распознаёт текст через OCR
  (Tesseract, русский+английский). Сохраняет номера страниц. Загрузка дольше.
- **Word `.docx`** и **текстовый `.txt`** — извлекается текст. Номера страниц в
  цитатах будут только если это `.txt`-sidecar от `ocrmypdf` (страницы разделены `\f`).

OCR требует системных пакетов `tesseract-ocr`, `tesseract-ocr-rus`, `poppler-utils` —
они ставятся автоматически через `nixpacks.toml`. Локально без них текстовые
PDF/docx/txt работают, а сканы просто не распознаются (мягкая деградация).

**Ограничение Telegram Bot API**: бот может скачать присланный файл размером
**не более 20 МБ**. Если учебник больше — разбить его на части и прислать их
**все сразу** в одном `/addbook`: предмет/автор/название указываются один раз,
бот сам сохранит части как «Название — Часть 1», «Часть 2» и т.д. (один файл —
без суффикса). Файлы альбома приходят отдельными сообщениями, поэтому сбор идёт
под блокировкой на пользователя, и кнопка «Готово» завершает приём.

### Оценка ресурсов

В памяти живут две модели: эмбеддер `intfloat/multilingual-e5-large` (≈2.2 ГБ весов)
и реранкер `BAAI/bge-reranker-v2-m3` (≈2.3 ГБ весов, топовый по качеству) —
суммарное потребление процесса ориентировочно **6–7 ГБ RAM**, с запасом влезает
в лимит 24 ГБ.

Если в логах появляется OOM (например, при понижении плана):
- уменьшить `RERANK_BATCH_SIZE`;
- вернуть лёгкий реранкер: `RERANKER_MODEL_NAME=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`
  (≈0.5 ГБ, общее потребление падает до **4–5 ГБ**, качество реранка чуть ниже);
- эмбеддер полегче: `intfloat/multilingual-e5-small` (`EMBEDDING_DIM=384`) — но это
  требует пересчитать эмбеддинги всех загруженных книг (повторно `/addbook`).

## Команды бота

**Все пользователи**: `/start`, `/help`, `/limit`

**Администраторы** (`ADMIN_IDS_RAW`): `/stats`, `/addbook`, `/delbook`, `/broadcast`,
`/ban`, `/unban`, `/premium`, `/unpremium`
