from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    TELEGRAM_BOT_TOKEN: str = ""
    OPENAI_API_KEY: str = ""
    # Модель для учебников и служебных задач (роутер интентов). Можно дешёвую (mini).
    OPENAI_MODEL: str = "gpt-4.1"
    # УСТАРЕЛО: выбор модели теперь делает TaskModelMap (app/llm/task_map.py).
    # Поле оставлено, чтобы не падал старый .env; значение больше не используется.
    CLINREK_OPENAI_MODEL: str = "gpt-4.1"
    # Базовый URL OpenAI-совместимого API. Пусто = облако OpenAI. Задаётся, если
    # трафик идёт через шлюз или совместимого провайдера.
    OPENAI_BASE_URL: str = ""

    # --- Провайдеры по ТЗ §56/§60 -------------------------------------------------
    # DeepSeek — grounded-генерация и работа с текстом; API OpenAI-совместимый,
    # поэтому обслуживается тем же адаптером, отличаются base_url, ключ и модель.
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"
    # GPT-5.4 Mini — восприятие (Vision) и оценка ответов студента. Пусто = берём
    # OPENAI_MODEL, чтобы переезд не менял модель на проде молча. При смене модели
    # обязательно обновить цены ниже — они версионируются вместе с PRICING_VERSION.
    OPENAI_EVAL_MODEL: str = ""
    # Переопределение TaskModelMap без выката кода: JSON вида
    # {"GROUNDED_QA": "openai", "VISION_EXTRACT": "deepseek"}.
    TASK_MODEL_MAP_OVERRIDES: str = ""

    # --- Цены провайдеров (§59, §64). Доллары за 1M токенов, с версией и датой -----
    PRICING_VERSION: str = "2026-09-19"
    DEEPSEEK_INPUT_PRICE_USD: float = 0.30
    DEEPSEEK_CACHED_INPUT_PRICE_USD: float = 0.03
    DEEPSEEK_OUTPUT_PRICE_USD: float = 1.20
    OPENAI_EVAL_INPUT_PRICE_USD: float = 0.75
    OPENAI_EVAL_CACHED_INPUT_PRICE_USD: float = 0.075
    OPENAI_EVAL_OUTPUT_PRICE_USD: float = 4.50
    # Курс для перевода стоимости в рубли на дашборде (курс ЦБ на дату baseline).
    USD_RUB_RATE: float = 84.1975

    DATABASE_URL: str = ""

    ADMIN_IDS_RAW: str = ""

    # Ключ для внешнего HTTP /search (api/main.py) — сервис-сервис вызов от
    # сценариста рилсов (репозиторий medap), не от людей. Пусто = /search отключён
    # (503), пока ключ не задан явно в переменных окружения.
    RAG_API_KEY: str = ""

    # --- Безопасность /v1 и админки (§33) ----------------------------------------
    # Service-to-service токен для /v1: образовательный сайт MedAP подтверждает им
    # себя. userId из тела запроса сам по себе доверия не даёт (§28).
    SERVICE_TOKEN: str = ""
    # Пароль входа в закрытую админку и секрет подписи её сессионной куки.
    ADMIN_WEB_PASSWORD: str = ""
    ADMIN_SESSION_SECRET: str = ""
    # Простой лимит запросов к /v1 на пользователя в минуту.
    RATE_LIMIT_PER_MINUTE: int = 60
    # Максимальный размер загружаемого через админку файла источника, МБ.
    MAX_UPLOAD_MB: int = 300

    # --- Версии для воспроизводимости ответа (§35) --------------------------------
    PROMPT_VERSION: str = "v1"
    RETRIEVAL_VERSION: str = "v1-dense-rerank"

    # --- Бюджеты вывода по workflow (§57) ----------------------------------------
    MAX_OUTPUT_TOKENS: int = 1500
    # «Помощник на паре»: ответ должен быть коротким (§53.1).
    CLASS_QUICK_MAX_OUTPUT_TOKENS: int = 350

    EMBEDDING_MODEL_NAME: str = "intfloat/multilingual-e5-large"
    EMBEDDING_DIM: int = 1024

    # Реранкер (cross-encoder). Топовый по качеству — с RAM ≥16 ГБ влезает спокойно.
    # При нехватке RAM можно вернуть лёгкий:
    # RERANKER_MODEL_NAME=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
    RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-v2-m3"
    # Размер батча реранка — больше при достаточном RAM/CPU, ниже снижает пик памяти.
    RERANK_BATCH_SIZE: int = 16

    FREE_DAILY_LIMIT: int = 5
    # Дневной лимит для премиум-подписки (защита экономики от злоупотреблений;
    # обычный юзер столько не задаёт). Админы — без лимита.
    PREMIUM_DAILY_LIMIT: int = 50

    CHUNK_SIZE_TOKENS: int = 512
    CHUNK_OVERLAP_TOKENS: int = 50
    # Сколько кандидатов достаём вектором перед реранком и сколько оставляем после.
    # TOP_K крупнее → больше материала в контексте → подробнее ответ (важно для экзамена).
    # Лишнее всё равно отсекается порогом реранка RERANK_SCORE_THRESHOLD.
    RETRIEVAL_CANDIDATES: int = 60
    RERANK_TOP_K: int = 12
    # Клинреки: сколько фрагментов берём из ОДНОЙ доминирующей рекомендации при
    # фокусе на документе. Больше RERANK_TOP_K — ответ врачу должен быть глубоким
    # и подробным, но строго из одной рекомендации (без мешанины из разных).
    CLINREK_TOP_K: int = 20
    # Дифдиагноз/сочетание болезней: широкий поиск по МНОГИМ рекомендациям —
    # сколько фрагментов оставляем для разбора (нужно охватить разные версии).
    DIFFERENTIAL_TOP_K: int = 18
    # Порог релевантности реранкера (0..1). Ниже — фрагмент считается нерелевантным.
    # TODO: откалибровать через scripts/eval_rag.py на реальных вопросах.
    RERANK_SCORE_THRESHOLD: float = 0.3
    # Запасной косинусный порог, если реранкер недоступен.
    MAX_DISTANCE_THRESHOLD: float = 0.5

    # Проверочный проход: второй вызов LLM сверяет ответ с контекстом и при выдумке
    # запускает корректирующую перегенерацию. Можно выключить ради экономии/скорости.
    VERIFY_GROUNDING: bool = True

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        # Managed-БД (Timeweb, как раньше Railway) отдаёт DATABASE_URL со схемой
        # postgres(ql)://, а нам нужен asyncpg-драйвер для SQLAlchemy.
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    @property
    def ADMIN_IDS(self) -> list[int]:
        return [int(x) for x in self.ADMIN_IDS_RAW.split(",") if x.strip()]


settings = Settings()
