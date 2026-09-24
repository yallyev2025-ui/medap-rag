from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator, model_validator
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

    # --- Speech-to-Text (§20 ТЗ, этап 4A.7) — только OpenAI Whisper, у DeepSeek
    # нет STT API, поэтому TaskModelMap здесь не применяется, провайдер жёстко OpenAI.
    WHISPER_MODEL: str = "whisper-1"
    # МБ — устный ответ короткий, в отличие от Vision-изображений это реальная
    # тарификация по минутам аудио, поэтому лимит на размер файла обязателен.
    ORAL_MAX_AUDIO_MB: int = 15

    # --- Настоящий веб-поиск (§19 ТЗ) — Tavily, сам фетчит страницы, поэтому SSRF-
    # защита (app/security/ssrf.py) здесь не нужна: мы зовём один доверенный API,
    # а не произвольный URL студента. Пусто = функция вернёт честную "не настроено".
    TAVILY_API_KEY: str = ""
    TAVILY_MAX_RESULTS: int = 5
    WEB_SEARCH_SNIPPET_MAX_CHARS: int = 2000

    # --- PubMed (сверх исходного ТЗ, добавлено по запросу) — NCBI E-utilities,
    # бесплатный публичный API, ключ не обязателен (лимит 3 запроса/сек без ключа).
    PUBMED_MAX_RESULTS: int = 5
    # Рекомендуется NCBI (не обязательно) — добавляется в запросы, если задано.
    PUBMED_CONTACT_EMAIL: str = ""

    # --- Цены провайдеров (§59, §64). Доллары за 1M токенов, с версией и датой -----
    PRICING_VERSION: str = "2026-09-19"
    DEEPSEEK_INPUT_PRICE_USD: float = 0.30
    DEEPSEEK_CACHED_INPUT_PRICE_USD: float = 0.03
    DEEPSEEK_OUTPUT_PRICE_USD: float = 1.20
    OPENAI_EVAL_INPUT_PRICE_USD: float = 0.75
    OPENAI_EVAL_CACHED_INPUT_PRICE_USD: float = 0.075
    OPENAI_EVAL_OUTPUT_PRICE_USD: float = 4.50
    # Whisper API тарифицируется по минутам аудио, не по токенам ($ за минуту).
    OPENAI_WHISPER_PRICE_USD_PER_MINUTE: float = 0.006
    # Курс для перевода стоимости в рубли на дашборде (курс ЦБ на дату baseline).
    USD_RUB_RATE: float = 84.1975

    DATABASE_URL: str = ""
    # SSL для подключения к БД: "require" — шифровать соединение, но не проверять
    # сертификат сервера (безопасно по умолчанию для managed-БД без своего CA-бандла
    # в образе); "verify-full" — проверять сертификат по системному хранилищу
    # доверенных центров (включайте, если провайдер использует публично доверенный
    # сертификат); "disable" — без SSL. Драйвер asyncpg не понимает параметр
    # "sslmode" в самом DATABASE_URL (это особенность psycopg2/libpq, не asyncpg),
    # поэтому SSL настраивается этой отдельной переменной, а не в URL.
    DATABASE_SSL_MODE: str = "require"

    # --- S3-хранилище оригиналов источников (§8.2.4, §33 ТЗ) ----------------------
    # Пусто = оригиналы не сохраняются — мягкая деградация: чанки и эмбеддинги всё
    # равно создаются, просто без сохранённого оригинала (без него нельзя будет
    # переиндексировать без повторной загрузки и нельзя подсветить фрагмент в
    # Source Viewer на этапе 3, но сам поиск и генерация ответа не зависят от S3).
    S3_ENDPOINT_URL: str = ""
    S3_BUCKET: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    # S3-совместимые провайдеры (в т.ч. Timeweb) обычно не проверяют регион
    # строго, но boto3 требует какое-то значение для подписи запросов.
    S3_REGION: str = "ru-1"

    # Параллелизм OCR сканов (rag/processor.py): рендер страниц батча и
    # распознавание каждой картинки идут одновременно в OCR_WORKERS потоков —
    # подберите под число vCPU тарифа Timeweb. Больше — быстрее, но выше
    # пиковая память при одновременном рендере нескольких страниц (в процессе
    # уже живут эмбеддер и реранкер).
    OCR_WORKERS: int = 4

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
    # Личный документ студента (§18, этап 4A.5) — не полный учебник, лимит меньше.
    USER_DOCUMENT_MAX_MB: int = 20
    # Отдельный лимит именно на загрузку документа — не делит квоту с обычными вопросами.
    USER_DOCUMENT_UPLOAD_PER_MINUTE: int = 3

    # --- Web Research (§19, §34, этап 4A.6) — фетч ОДНОЙ страницы по URL ----------
    WEB_RESEARCH_TIMEOUT_SECONDS: float = 10.0
    # Лимит размера страницы (КБ) — до извлечения текста.
    WEB_RESEARCH_MAX_KB: int = 2048
    # Сколько текста страницы реально уходит в промпт (символы) — дальше отсекается.
    WEB_RESEARCH_MAX_CHARS: int = 12000

    # --- Версии для воспроизводимости ответа (§35) --------------------------------
    PROMPT_VERSION: str = "v1"
    RETRIEVAL_VERSION: str = "v2-hybrid-bm25-rrf"

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
                v = "postgresql+asyncpg://" + v[len(prefix):]
                break

        # Панели управления (в т.ч. Timeweb) часто дают строку подключения с
        # "?sslmode=require"/"verify-full" — это параметр libpq/psycopg2, а
        # asyncpg передаёт query-параметры URL как есть в свой connect(), у
        # которого нет аргумента sslmode → TypeError и падение при старте.
        # SSL для asyncpg настраивается отдельно (DATABASE_SSL_MODE, см. выше и
        # db/session.py), поэтому здесь просто убираем несовместимые параметры,
        # если они пришли в URL, — а не заставляем вручную чистить строку в панели.
        parts = urlsplit(v)
        if parts.query:
            filtered = [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if key not in ("sslmode", "channel_binding")
            ]
            v = urlunsplit(parts._replace(query=urlencode(filtered)))
        return v

    @model_validator(mode="after")
    def _strip_all_string_settings(self) -> "Settings":
        # Пароли, токены и ключи задаются через панели хостинга (Timeweb и
        # похожие), где при копипасте с телефона/планшета легко зацепить лишний
        # пробел или перевод строки по краям значения — глазами его не видно, а
        # сравнение "введённый пароль == ADMIN_WEB_PASSWORD" из-за него не
        # совпадёт ни при каком реально верном пароле (та же ловушка, что была
        # с DATABASE_URL). Подчищаем ВСЕ строковые настройки одним местом, чтобы
        # больше не гадать, в каком конкретно поле затесался пробел.
        for name in self.__class__.model_fields:
            value = getattr(self, name)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped != value:
                    setattr(self, name, stripped)
        return self

    @property
    def ADMIN_IDS(self) -> list[int]:
        return [int(x) for x in self.ADMIN_IDS_RAW.split(",") if x.strip()]


settings = Settings()
