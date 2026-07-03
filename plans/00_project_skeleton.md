# Фаза 00 — Скелет проекта и конфигурация

## Цель
Создать базовую структуру проекта, конфигурацию через переменные окружения и зависимости, чтобы все последующие фазы могли добавлять код без переделки структуры.

## Что создать

### Структура каталогов
```
medap-bot/                  (всё внутри корня репозитория, в medap-bot/)
├── bot/
│   ├── __init__.py
│   ├── main.py
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── start.py        (заглушка, фаза 03)
│   │   ├── query.py         (заглушка, фаза 03)
│   │   └── admin.py         (заглушка, фаза 05)
│   └── middlewares/
│       ├── __init__.py
│       └── limits.py        (заглушка, фаза 04)
├── rag/
│   ├── __init__.py
│   ├── processor.py         (заглушка, фаза 01)
│   ├── embedder.py           (заглушка, фаза 01)
│   ├── retriever.py          (заглушка, фаза 01)
│   └── generator.py          (заглушка, фаза 02)
├── db/
│   ├── __init__.py
│   ├── models.py             (заглушка, фаза 01/04)
│   └── crud.py               (заглушка, фаза 04)
├── scripts/
│   └── load_books.py         (заглушка, фаза 01)
├── config.py
├── requirements.txt
├── .env.example
└── railway.toml
```

Заглушки — это файлы с docstring "реализуется в фазе X", чтобы импорты не падали и структура была видна сразу.

### `config.py`
Через `pydantic-settings` или простой `os.environ` + `python-dotenv`:
- `TELEGRAM_BOT_TOKEN: str`
- `GEMINI_API_KEY: str`
- `GEMINI_MODEL: str = "gemini-2.0-flash"`
- `DATABASE_URL: str`
- `ADMIN_IDS: list[int]` (парсится из строки `"123,456"`)
- `EMBEDDING_MODEL_NAME: str = "intfloat/multilingual-e5-large"`
- `EMBEDDING_DIM: int = 1024`
- `FREE_DAILY_LIMIT: int = 10`
- `CHUNK_SIZE_TOKENS: int = 512`
- `CHUNK_OVERLAP_TOKENS: int = 50`
- `RETRIEVAL_TOP_K: int = 5`

Загружается через `python-dotenv` (`load_dotenv()`) при импорте.

### `requirements.txt`
```
aiogram>=3.4
sentence-transformers
torch --index-url https://download.pytorch.org/whl/cpu
sqlalchemy[asyncio]>=2.0
asyncpg
pgvector
pdfplumber
google-generativeai
python-dotenv
pydantic-settings
```
(точные версии зафиксировать после `pip install` — pip freeze в requirements.txt)

### `.env.example`
```
TELEGRAM_BOT_TOKEN=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
ADMIN_IDS=123456789
EMBEDDING_MODEL_NAME=intfloat/multilingual-e5-large
EMBEDDING_DIM=1024
FREE_DAILY_LIMIT=10
```

### `railway.toml` (заготовка, дорабатывается в фазе 06)
Базовый build/start через nixpacks, без healthcheck пока (добавится в фазе 06).

### Проверка готовности фазы
1. `pip install -r requirements.txt` — без ошибок (допускается долгая установка torch).
2. `python -c "from medap_bot import config; print(config.settings)"` — выводит конфиг без ошибок при наличии `.env`.
3. Структура каталогов и заглушки закоммичены.

## Чек-лист пользователя (см. также основной план)
- Создать Telegram-бота через @BotFather → токен.
- Узнать свой Telegram ID через @userinfobot.
- Получить Gemini API key на aistudio.google.com.
- Создать Postgres на Railway, выполнить `CREATE EXTENSION IF NOT EXISTS vector;`, скопировать DATABASE_URL.
- Создать локально `.env` (не коммитить) на основе `.env.example`, заполнить реальными значениями.
