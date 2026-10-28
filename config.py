import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    TELEGRAM_BOT_TOKEN: str = ""
    OPENAI_API_KEY: str = ""
    # Claude Sonnet для презентаций сайта (посредник /v1/presentations/complete). Пустой ключ = Sonnet отключён,
    # Luna работает как раньше. Адрес пустой = облако Anthropic.
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""
    # Личные документы студента (батч 27): сколько документов можно держать одновременно.
    USER_DOCUMENTS_MAX: int = 10
    # Документ из не более чем стольких фрагментов (≈20 тыс. токенов) целиком отдаётся модели
    # без поиска — самое надёжное против «ответ в документе был, а бот не нашёл».
    DOC_FULL_CONTEXT_CHUNKS: int = 40
    # Порог релевантности для документов мягче, чем для учебников (конспект пишут кратко).
    DOC_RERANK_SCORE_THRESHOLD: float = 0.1
    # Несколько вопросов в одном скрине/сообщении: порция на одну генерацию и фрагментов на вопрос.
    MULTI_QUESTION_BATCH: int = 12
    # Потолок вопросов, извлекаемых с одного изображения (страховка расхода).
    VISION_MAX_QUESTIONS: int = 60
    MULTI_QUESTION_CHUNKS_PER_QUESTION: int = 3
    # Конспект для сайта (батч 30): пунктов темы на одну генерацию (подробный/краткий), фрагментов
    # на пункт, потолок фрагментов на порцию, параллельных генераций и лимит вывода одной порции.
    # Режимы ответа студента (батч 31): сколько фрагментов берётся и какой провайдер отвечает.
    # Быстрый — 12–15 фрагментов (по умолчанию 14) и дешёвая модель; глубокий — 20 фрагментов
    # и сильная модель. Провайдеры — ключи реестра: 'deepseek' | 'openai'.
    TIER_FAST_TOP_K: int = 10
    # Сколько кандидатов реранкер проверяет перед выбором лучших (12.10.2026). Реранкер — самая
    # медленная часть ответа (~25 с на 60 кусков), а «быстрый» режим раньше быстрее не искал: брал
    # те же 60. Теперь быстрый проверяет меньше (быстрее, но чуть выше шанс упустить нужный кусок
    # из дальнего хвоста грубого отбора), глубокий — как раньше. Без режима (сайт, генерация) —
    # RETRIEVAL_CANDIDATES. Меняется переменной окружения без правки кода
    TIER_FAST_CANDIDATES: int = 35
    TIER_DEEP_CANDIDATES: int = 45
    # Глубокий: 15 фрагментов (было 30, затем 20). Замер 12.10.2026: при 30 ответ выходил короче и с меньшим числом
    # источников, чем у быстрого на 10 — лишние фрагменты разбавляли материал; по литературе (RAGGED и др.) качество
    # растёт до плато и дальше падает от шума, обычная практика 5–15 после реранка. Владелец 13.10.2026 выбрал 15
    # (можно вернуть переменной TIER_DEEP_TOP_K). Проверить 10/15/20: scripts/compare_tiers
    TIER_DEEP_TOP_K: int = 15
    TIER_FAST_PROVIDER: str = "deepseek"
    TIER_DEEP_PROVIDER: str = "openai"
    # Не больше стольких фрагментов с одного «места» учебника (книга + блок из трёх страниц):
    # так 14 фрагментов покрывают разные стороны вопроса, а не одну страницу.
    DIVERSITY_PER_SPOT: int = 2
    # Кеш ответов по смыслу (батч 31). Вопрос должен быть близок (косинус не ниже
    # CACHE_MIN_SIMILARITY) И поиск обязан найти те же фрагменты (доля общих не ниже
    # CACHE_MIN_OVERLAP) — иначе ответ пишется заново. CACHE_ENABLED=false выключает кеш.
    CACHE_ENABLED: bool = True
    CACHE_MIN_SIMILARITY: float = 0.92
    CACHE_MIN_OVERLAP: float = 0.7
    CACHE_MAX_AGE_DAYS: int = 30
    CONSPECT_POINTS_PER_BATCH_DETAILED: int = 5
    CONSPECT_POINTS_PER_BATCH_BRIEF: int = 8
    CONSPECT_FRAGMENTS_PER_POINT: int = 6
    CONSPECT_MAX_FRAGMENTS: int = 36
    CONSPECT_CONCURRENCY: int = 2
    CONSPECT_MAX_OUTPUT_TOKENS: int = 8192
    # Модель OpenAI: ответы студентам (в т.ч. глубокий режим), конспекты, Vision (фото
    # тестов), оценка ответов. С 13.10.2026 — GPT-6 Luna (дешевле и лучше GPT-5.4 Mini).
    # Если OpenAI ответит «модели нет», сервис сам попробует запасные из
    # OPENAI_MODEL_FALLBACKS и запомнит рабочую.
    OPENAI_MODEL: str = "gpt-6-luna"
    # Запасные модели через запятую, пробуются по порядку при 404 / model_not_found.
    OPENAI_MODEL_FALLBACKS: str = "gpt-5.4-mini,gpt-4.1-mini"
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
    # GPT-6 Luna — восприятие (Vision) и оценка ответов студента. Пусто = берём
    # OPENAI_MODEL, чтобы переезд не менял модель на проде молча. При смене модели
    # обязательно обновить цены ниже — они версионируются вместе с PRICING_VERSION.
    OPENAI_EVAL_MODEL: str = ""
    # Переопределение TaskModelMap без выката кода: JSON вида
    # {"GROUNDED_QA": "openai", "VISION_EXTRACT": "deepseek"}.
    TASK_MODEL_MAP_OVERRIDES: str = ""

    # --- Speech-to-Text (§20 ТЗ, этап 4A.7) — только OpenAI Whisper, у DeepSeek
    # нет STT API, поэтому TaskModelMap здесь не применяется, провайдер жёстко OpenAI.
    WHISPER_MODEL: str = "whisper-1"
    # МБ — устный ответ короткий, а тарификация у Whisper по минутам аудио, а не
    # по МБ файла, поэтому лимит на размер защищает не от лишних денег, а от
    # раздутого запроса.
    ORAL_MAX_AUDIO_MB: int = 15
    # Фото/скрин теста (§17 ТЗ, этап 4A.4) — раньше лимита не было вообще (батч 7:
    # найдено аудитом, не закрыто; закрыто батчем 14). Того же порядка, что и
    # устный ответ — реальное фото/скрин крупнее не бывает без потери смысла.
    VISION_MAX_IMAGE_MB: int = 15

    # --- Настоящий веб-поиск (§19 ТЗ) — Yandex Search API, сам фетчит страницы,
    # поэтому SSRF-защита (app/security/ssrf.py) здесь не нужна: мы зовём один
    # доверенный API, а не произвольный URL студента. Российский биллинг (не
    # Tavily — тому нужна карта с западным биллингом, недоступная пользователю).
    # Оба поля нужны вместе: folderId — отдельная сущность Yandex Cloud, не сам
    # ключ. Пусто у любого = честная "не настроено", без похода в сеть.
    YANDEX_SEARCH_API_KEY: str = ""
    YANDEX_FOLDER_ID: str = ""
    WEB_SEARCH_MAX_RESULTS: int = 5
    WEB_SEARCH_SNIPPET_MAX_CHARS: int = 2000

    # --- PubMed (сверх исходного ТЗ, добавлено по запросу) — NCBI E-utilities,
    # бесплатный публичный API, ключ не обязателен (лимит 3 запроса/сек без ключа).
    PUBMED_MAX_RESULTS: int = 5
    # Сколько кандидатов достаём из PubMed, прежде чем отобрать PUBMED_MAX_RESULTS лучших
    # по уровню доказательности (мета-анализы/РКИ/обзоры выше) и свежести.
    PUBMED_CANDIDATES: int = 15
    # Рекомендуется NCBI (не обязательно) — добавляется в запросы, если задано.
    PUBMED_CONTACT_EMAIL: str = ""
    # Необязательный бесплатный ключ NCBI (аккаунт на ncbi.nlm.nih.gov → Settings → API Key):
    # лимит 3 → 10 запросов/сек и меньше шансов на отказ 429/403 для облачных IP.
    PUBMED_API_KEY: str = ""

    # Запасной DNS (DNS-over-HTTPS к 1.1.1.1 / 8.8.8.8), если системный DNS хостинга
    # не ответил (`Temporary failure in name resolution`). Включается только при сбое.
    DNS_FALLBACK_ENABLED: bool = True

    # --- Цены провайдеров (§59, §64). Доллары за 1M токенов, с версией и датой -----
    PRICING_VERSION: str = "2026-10-13"
    DEEPSEEK_INPUT_PRICE_USD: float = 0.30
    DEEPSEEK_CACHED_INPUT_PRICE_USD: float = 0.03
    DEEPSEEK_OUTPUT_PRICE_USD: float = 1.20
    OPENAI_EVAL_INPUT_PRICE_USD: float = 0.10
    OPENAI_EVAL_CACHED_INPUT_PRICE_USD: float = 0.01
    OPENAI_EVAL_OUTPUT_PRICE_USD: float = 0.50
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
    OCR_WORKERS: int = 2
    # Щадящая загрузка источников (владелец 28.10.2026: «грузишь учебники — презентации и ответы перестают работать»): во время загрузки
    # эмбеддер использует не больше INGEST_THREADS ядер (остальные остаются ответам), а между пачками делается пауза INGEST_PAUSE_S секунд,
    # чтобы запросы людей успевали проходить. Больше потоков и меньше пауза — загрузка быстрее, но ответы в это время медленнее.
    INGEST_THREADS: int = 4
    INGEST_PAUSE_S: float = 0.4

    # Батч 16: без этого PyTorch/BLAS (расчёт эмбеддингов, rag/embedder.py)
    # по умолчанию претендует на ВСЕ видимые ядра процессора — во время
    # загрузки источника это отбирает процессор у event loop (бот/админка/API)
    # настолько, что всё остальное подвисает. Авто-подбор: одно ядро оставляем
    # свободным; переопределяется переменной окружения CPU_THREAD_LIMIT, если
    # нужно вручную (например, если контейнер видит больше ядер, чем реально
    # выделено тарифом Timeweb).
    CPU_THREAD_LIMIT: int = Field(default_factory=lambda: max(1, (os.cpu_count() or 2) - 1))

    ADMIN_IDS_RAW: str = ""

    # --- Бот презентаций Medap Slide (отдельный Telegram-бот в том же процессе) ----------------
    # Токен от @BotFather. Пусто = бот презентаций не запускается (основной бот работает как раньше).
    SLIDES_BOT_TOKEN: str = ""
    # Имя бота без @ (для ссылок «открыть бота»).
    SLIDES_BOT_USERNAME: str = ""
    # Адрес сайта (https://…) и общий секрет, которым бот подписывает запросы к сайту. Тот же секрет
    # записан на сайте в SLIDES_SITE_SECRET: одна и та же длинная случайная строка на обеих сторонах.
    SLIDES_SITE_URL: str = ""
    SLIDES_SITE_SECRET: str = ""

    # Ключ для внешнего HTTP /search (api/main.py) — сервис-сервис вызов от
    # сценариста рилсов (репозиторий medap), не от людей. Пусто = /search отключён
    # (503), пока ключ не задан явно в переменных окружения.
    RAG_API_KEY: str = ""

    # --- Безопасность /v1 и админки (§33) ----------------------------------------
    # --- Картинки для презентаций (генерация, 21.10.2026) ---
    # Поставщик и ключ вписывает владелец; без ключа генерация честно отвечает «не подключена», остальное работает.
    IMAGE_PROVIDER: str = "fal"
    IMAGE_API_KEY: str = ""
    # Модель рисования по умолчанию (для всех, кому сайт не выбрал другую): flux-schnell | flux-2-dev | gpt-2.5-flare-low | gpt-2.5-flare-medium
    IMAGE_MODEL: str = "flux-schnell"
    IMAGE_BASE_URL: str = "https://fal.run"
    # Цена одной картинки Flux Schnell, $ (≈ $0,003 за мегапиксель; 1344×768 ≈ 1 МП) — идёт в учёт расхода; у GPT Image цена по таблице в images.py
    IMAGE_PRICE_USD: float = 0.004
    # Сколько картинок один человек может заказать за час (защита счёта от ошибки или перебора)
    IMAGE_PER_HOUR: int = 20

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
    # Текст страницы или PDF по ссылке из ТЗ презентации (24.10.2026): размер файла и объём текста, который отдаётся сайту
    WEB_PAGE_MAX_KB: int = 8192
    WEB_PAGE_MAX_CHARS: int = 60000

    # --- Gates (§49 ТЗ, этап 4B) ---------------------------------------------------
    # Выключены по умолчанию: пороги честно ставить только после baseline на
    # настоящих учебниках. Значения ниже — заглушки, вписать реальные после
    # первого прогона evals/benchmark на загруженном корпусе.
    GATES_ENABLED: bool = False
    GATE_MIN_RECALL_AT_K: float = 0.7
    GATE_MIN_PASS_RATE: float = 0.7
    GATE_MAX_HALLUCINATIONS: int = 0
    GATE_MIN_VERIFIED_RATE: float = 0.8

    # --- Версии для воспроизводимости ответа (§35) --------------------------------
    PROMPT_VERSION: str = "v1"
    RETRIEVAL_VERSION: str = "v2-hybrid-bm25-rrf"

    # --- Бюджеты вывода по workflow (§57) ----------------------------------------
    # 8192 — жёсткий потолок DeepSeek: фактически «без лимита» (на Railway лимита не было,
    # 1500 из этапа 1 обрывал ответы). Стоимость держит месячный ₽-бюджет, не обрезка.
    MAX_OUTPUT_TOKENS: int = 8192
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

    # УСТАРЕЛО (батч 10): раньше был gate по количеству запросов в день. Заменён
    # месячным ₽-бюджетом ниже (точнее отражает реальную стоимость — голос/фото
    # съедают бюджет быстрее текстового вопроса). Поля оставлены для истории/
    # обратной совместимости, для gate больше не используются.
    FREE_DAILY_LIMIT: int = 5
    PREMIUM_DAILY_LIMIT: int = 50
    # Месячный ₽-бюджет на AI-расход одного пользователя Telegram (батч 10) —
    # заменяет дневной счётчик количества запросов выше. 150₽/мес — при цене
    # подписки Premium 400₽/мес. Считается по сумме AIUsageEvent.provider_cost_rub
    # за текущий календарный месяц (db.crud.month_spend_rub). Только Telegram —
    # у сайта/API пока нет флага premium в StudentAIContext.
    FREE_MONTHLY_BUDGET_RUB: float = 20.0
    PREMIUM_MONTHLY_BUDGET_RUB: float = 150.0

    CHUNK_SIZE_TOKENS: int = 512
    CHUNK_OVERLAP_TOKENS: int = 50
    # Сколько кандидатов достаём вектором перед реранком и сколько оставляем после.
    # TOP_K крупнее → больше материала в контексте → подробнее ответ (важно для экзамена).
    # Лишнее всё равно отсекается порогом реранка RERANK_SCORE_THRESHOLD.
    RETRIEVAL_CANDIDATES: int = 60
    RERANK_TOP_K: int = 30
    # Сколько вопросов одновременно может занимать реранкер (он грузит все ядра).
    # Остальные ждут в очереди, а не тормозят всех разом. При ~25% CPU на вопрос
    # 3 одновременных оставляют запас боту и админке.
    RERANK_CONCURRENCY: int = 3
    # Клинреки: сколько фрагментов берём из ОДНОЙ доминирующей рекомендации при
    # фокусе на документе. Больше RERANK_TOP_K — ответ врачу должен быть глубоким
    # и подробным, но строго из одной рекомендации (без мешанины из разных).
    CLINREK_TOP_K: int = 20
    # Дифдиагноз/сочетание болезней: широкий поиск по МНОГИМ рекомендациям —
    # сколько фрагментов оставляем для разбора (нужно охватить разные версии).
    DIFFERENTIAL_TOP_K: int = 18
    # Порог релевантности реранкера (0..1). Ниже — фрагмент считается нерелевантным.
    # 13.10.2026: прежние 0.3 не откалиброваны, а оценки torch были сжаты в 0.50–0.73 (двойная сигмоида, см.
    # rag/reranker.py:_torch_scores), так что порог не отсекал ничего. Теперь оценки настоящие (посторонние вопросы
    # вроде «борщ» дают 0.00–0.04, подходящие — до 0.99). 0.05 — осторожный порог: отсекает явный мусор, а решает,
    # хватает ли материала для ответа, модель. Точнее подбирать: python -m scripts.compare_rerankers (помечайте строки
    # вопросов, которых в учебниках быть не должно, знаком «!» в начале)
    RERANK_SCORE_THRESHOLD: float = 0.05
    # Реранкер: "torch" — как было (sentence-transformers, 32-битные веса); "onnx" — та же модель,
    # сжатая в int8 и запускаемая через onnxruntime (быстрее на процессоре, меньше памяти).
    # Откат — вернуть "torch" и перезапустить: код и веса старого пути остаются в образе (12.10.2026).
    # Если ONNX не загрузился (нет файлов, сломан), бот сам продолжает на "torch" и пишет это в лог
    # Резерв на большую модель (torch, ≈2,3 ГБ памяти): True — если сжатая ONNX-модель не работает, бот загрузит большую и продолжит
    # (как раньше); False — большая модель в память НЕ загружается вообще, при сбое ONNX поиск отвечает ошибкой (владелец 28.10.2026:
    # «большая модель не должна грузиться, памяти мало»). Включайте False вместе с RERANKER_BACKEND=onnx, когда сжатая модель проверена.
    RERANKER_TORCH_FALLBACK: bool = True
    RERANKER_BACKEND: str = "torch"
    # Папка с model_quantized.onnx и токенизатором (делает scripts/export_reranker_onnx.py при сборке)
    RERANKER_ONNX_DIR: str = "/opt/models/reranker-onnx-int8"
    RERANKER_ONNX_FILE: str = "model_quantized.onnx"
    # У int8 шкала оценок слегка сдвигается, поэтому свой порог релевантности; пусто — общий
    RERANK_SCORE_THRESHOLD_ONNX: float | None = None
    # Запасной косинусный порог, если реранкер недоступен.
    MAX_DISTANCE_THRESHOLD: float = 0.5

    # Проверочный проход: второй вызов LLM сверяет ответ с контекстом и при выдумке
    # запускает корректирующую перегенерацию. Можно выключить ради экономии/скорости.
    VERIFY_GROUNDING: bool = True
    # Учебный ответ, не прошедший проверку даже после исправления, выдаётся с пометкой «часть не подтверждена»,
    # а не заменяется отказом (владелец 12.10.2026: ответ на 10 пунктов выбрасывали из-за одного). Клинреки — по-прежнему отказ
    VERIFY_SOFT_FAIL: bool = True

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


def rerank_threshold() -> float:
    """Порог релевантности реранкера для выбранного способа (у int8 может быть свой)."""
    if settings.RERANKER_BACKEND == "onnx" and settings.RERANK_SCORE_THRESHOLD_ONNX is not None:
        return settings.RERANK_SCORE_THRESHOLD_ONNX
    return settings.RERANK_SCORE_THRESHOLD


# Батч 16: должно случиться ДО первого импорта torch где-либо в процессе —
# config.py импортируется практически везде первым, поэтому это надёжная
# точка. setdefault(), а не прямое присваивание: если оператор уже явно
# выставил переменную окружения сам, не переписываем её.
os.environ.setdefault("OMP_NUM_THREADS", str(settings.CPU_THREAD_LIMIT))
os.environ.setdefault("MKL_NUM_THREADS", str(settings.CPU_THREAD_LIMIT))
os.environ.setdefault("OPENBLAS_NUM_THREADS", str(settings.CPU_THREAD_LIMIT))

# Батч 24: запасной DNS (DoH 1.1.1.1 / 8.8.8.8) на случай, когда системный DNS
# Timeweb не отвечает. config.py импортируется первым — подключаем до любых сетевых вызовов.
if settings.DNS_FALLBACK_ENABLED:
    from app.net import dns_fallback

    dns_fallback.install()
