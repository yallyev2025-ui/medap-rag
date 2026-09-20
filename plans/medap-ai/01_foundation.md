# Этап 1 — Фундамент: Timeweb, ядро сервиса, провайдеры, деньги, безопасность

**Цель:** сервис работает на Timeweb в Docker; AI-логика отделена от Telegram и доступна через
versioned API; модель под задачу выбирается детерминированно; стоимость каждого запроса известна;
есть базовая защита и каркас админки.

## 1.1. Docker вместо Nixpacks

- `Dockerfile` на `python:3.12-slim`:
  - apt: `tesseract-ocr`, `tesseract-ocr-rus`, `tesseract-ocr-eng`, `poppler-utils` — переносятся из
    `nixpacks.toml`. Без них перестанет работать OCR сканов (`rag/processor.py:73`, `_ocr_pdf_page`).
  - `pip install --extra-index-url https://download.pytorch.org/whl/cpu` — дефолтный `torch==2.12.0`
    тянет CUDA-колёса (~2.5 ГБ лишних в образе), GPU на Timeweb нет.
  - `ENV HF_HOME=/opt/models` и прогрев весов на этапе сборки (`SentenceTransformer` +
    `CrossEncoder`): веса ложатся в слой образа, при старте эфемерного контейнера не качаются.
  - `CMD ["python", "-m", "bot.main"]` — точка входа не меняется.
- `.dockerignore`: `.git`, `notebooks/`, `plans/`, `eval/`, `*.md`, `клинреки/`, `__pycache__`, `.env`.
- `docker-compose.yml`: `app` + `db` (`pgvector/pgvector:pg17`, именованный том) — локальная разработка.
- После успешного деплоя удалить `railway.toml` и `nixpacks.toml` отдельным коммитом.

Фолбэк, если сборщик не достучится до huggingface.co: `ENV HF_ENDPOINT=https://hf-mirror.com` либо
сборка образа локально и пуш в реестр.

## 1.2. Структура модулей (§46)

```
app/
  api/            # роутеры /v1 и админки
  orchestration/  # intent router, context builder
  retrieval/      # обёртки над rag/retriever (этап 2 — гибридный поиск)
  evidence/       # Evidence Pack (этап 3)
  verification/   # Verification Layer (этап 3)
  workflows/      # ask, explain, ... — бизнес-логика workflow
  llm/            # LLMProvider, Model Registry, TaskModelMap, usage/cost
  memory/         # диалоговая память (существующая лёгкая история)
  integrations/
    medap_student/  # контракты для образовательного сайта
    telegram/       # адаптер Telegram
  observability/  # request_id, логи по стадиям
  security/       # service-to-service auth, admin-сессия, rate limit
  admin/          # шаблоны и роуты админки
evals/            # прогон и отчёты
```

Модульный монолит: дробить на микросервисы без измеримой необходимости запрещено (§46).
FastAPI-приложение остаётся одно (`api/main.py`), новые роутеры подключаются к нему — v0-эндпоинты
`/search`, `/answer`, `/subjects` продолжают работать.

## 1.3. API `/v1` и контракт контекста

- `POST /v1/chat` — вопрос студента, ответ + источники.
- `POST /v1/explain` — объяснение темы.
- Вход — `StudentAIContext` (§28): `userId`, `subjectId?`, `topicId?`, `contentId?`, `examId?`,
  `examQuestionId?`, `knowledgeUnitIds?`, `selectedText?`, `sourceMode?`, `locale?`.
- **`userId` от фронтенда не доверяем**: личность подтверждается service-to-service токеном
  (`Authorization: Bearer <SERVICE_TOKEN>`), `userId` из тела принимается только как ссылка на
  пользователя того сервиса, который уже прошёл аутентификацию.
- Ответ: `answer`, `citations[]`, `sources[]`, `diagnostics` (workflow, модель, retrieval,
  verification), `usage` (токены, стоимость), `requestId`, `versions` (prompt/retrieval/model).
  Полноценные `citations` с `exactSupportingText` появятся на этапе 3; на этапе 1 отдаются
  источник, автор, название и страницы — то, что уже умеет `rag/retriever.ChunkResult`.

## 1.4. Orchestrator (§5, §2)

Определяет: тип входа; медицинский или немедицинский запрос; нужен ли retrieval; какие коллекции
разрешены (`sourceMode`); формат ответа. Немедицинский безопасный запрос («привет», «спасибо»)
отвечается коротко и **не запускает** дорогой Evidence pipeline (вариант C, §2).
Реализация — детерминированные правила плюс существующий `detect_intent`; лишней LLM не добавляем.

## 1.5. Провайдеры, TaskModelMap, Model Registry (§56, §60)

- `LLMProvider` — протокол: `complete(messages, schema?, budgets) -> LLMResult` c usage.
  Один адаптер `OpenAICompatProvider` обслуживает и OpenAI, и DeepSeek (у DeepSeek
  OpenAI-совместимый API), отличаются `base_url`, ключом и id модели.
- `TaskModelMap` (§56.5) — перечисление задач и провайдер под каждую:

  | Задача | Модель |
  |---|---|
  | `GROUNDED_QA`, `EXPLAIN`, `CLASS_QUICK`, `TEST_SOLVE_TEXT`, `DOCUMENT_QA`, `TARGETED_REPAIR` | DeepSeek V4.1 Flash |
  | `VISION_EXTRACT`, `RECALL_EVALUATE`, `FREE_RECALL_EVALUATE`, `ORAL_EVALUATE`, `ERROR_DIAGNOSIS`, `CLAIM_EVIDENCE_CHECK` | GPT-5.4 Mini |

  Маппинг — конфигурация, не код. Никакого LLM-роутера на каждый запрос (§66).
- `ModelProfile` (§60): provider, modelId, capabilities, supportsVision, supportsStructuredOutput,
  supportsTools, contextWindow, pricingVersion, inputPrice, cachedInputPrice, outputPrice,
  enabledWorkflows, tier, status. Цены — в конфиге с датой действия, не размазаны по коду.
- Бюджеты на workflow (§57): максимум кандидатов, максимум evidence, лимит входных и выходных
  токенов. У `CLASS_QUICK` особенно короткий выходной бюджет.
- Structured outputs со схемой (§29): критичные результаты не парсятся регулярками из прозы;
  при невалидной схеме — контролируемый повтор, затем ошибка.

## 1.6. Телеметрия стоимости (§59)

Таблица `ai_usage_events`: `userId`, `requestId`, `workflow`, `provider`, `model`, `inputTokens`,
`cachedInputTokens`, `outputTokens`, `imageUnits?`, `audioUnits?`, `sttCost?`, `providerCost`,
`currency`, `latencyMs`, `retryCount`, `fallbackFrom?`, `timestamp`, `promptVersion`,
`retrievalVersion`. Пишется на каждый вызов провайдера.

Стоимость считается формулой из §65 по фактическому usage и версионированным ценам; фиксированные
числа в бизнес-логике не хранятся.

## 1.7. Безопасность-база (§33)

Service-to-service auth для `/v1`; авторизация по `user_id`; rate limiting из конфига; MIME и
лимиты размеров при загрузке; секреты только в переменных окружения; audit log критичных операций
(загрузка и удаление источника, смена маппинга моделей, вход в админку); путь удаления данных.

## 1.8. Админка: каркас

- Вход по `ADMIN_WEB_PASSWORD`, подписанная httponly-кука; меню из `TZ_ADDENDUM.md` §2 (неактивные
  разделы помечены как «этап N»).
- **Dashboard**: запросов сегодня, активные пользователи, ошибки, расход DeepSeek и OpenAI, общий
  расход, стоимость на пользователя, P50/P90/P99, самые дорогие workflow, прогноз месячного расхода.
- **Models**: текущий `TaskModelMap`, профили моделей и цены.
- **Загрузка источника**: форма (файл, тип, предмет, автор, название), фоновая обработка через
  существующий `scripts/load_books.load_book()` (`scripts/load_books.py:20`), статус в таблице
  `ingest_jobs`. Ограничение Telegram в 20 МБ здесь не действует.
- Зависимости: `python-multipart`, `jinja2`, `itsdangerous`.

## 1.9. Telegram как клиент (§30)

`bot/handlers/query.py` перестаёт собирать собственный конвейер и вызывает тот же workflow, что и
`/v1/chat`. Промпты, политика источников и верификация существуют в одном экземпляре.

## 1.10. Аудит и baseline (§45, §54)

- `STUDENT_AI_INTEGRATION_PLAN.md` в корне: стек; где сейчас RAG, loaders, эмбеддинги, векторное
  хранилище, промпты, провайдеры, Telegram-хендлеры, storage, деплой; что переиспользуется; где
  дублирование; чего из ТЗ нет; migration path; структура модулей; список миграций и изменений конфига.
- `evals/`: прогон по `eval/dataset.jsonl`, метрики retrieval отдельно от генерации, отчёт с
  задержкой и стоимостью. Baseline сохраняется в репозитории с датой и версиями.

## Что осталось сделать на вашей стороне

Код этапа готов и залит в ветку. Эти шаги требуют доступа к панели Timeweb и
реальных данных, из среды разработки их выполнить нельзя:

1. Создать приложение (тип сборки Dockerfile, Нидерланды, порт 8000, тариф ≥ 8 ГБ RAM)
   и БД с включённым расширением `vector`; прописать переменные окружения из `.env.example`.
2. Один раз выполнить `python -m db.init_db` в консоли приложения.
3. Загрузить учебники через админку `https://<домен>/admin` (Knowledge Base).
4. Снять baseline качества: `python -m evals.run --out evals/reports/baseline.json`
   (до загрузки источников в наборе лежат шаблоны — цифры будут неинформативны).
5. Убедиться, что бот и `/v1/chat` отвечают одинаково, и выключить сервис на Railway.
6. Удалить `railway.toml` и `nixpacks.toml` отдельным коммитом.

Сборку образа проверить в среде разработки не удалось: демон Docker там недоступен.
Первая сборка — на стороне Timeweb или локально командой `docker compose build`.

## Чек-лист приёмки этапа 1

- [ ] `docker compose build` собирается; веса моделей внутри образа (`du -sh /opt/models`).
- [ ] На Timeweb: `GET /health` зелёный, нет OOM и докачки моделей, рестарт < 2 минут.
- [ ] `python -m db.init_db` на managed-БД создаёт расширение `vector` и таблицы.
- [ ] `POST /v1/chat` даёт тот же ответ и источники, что бот на тот же вопрос.
- [ ] Запрос без service-token → 401; подделанный `userId` не влияет на выдачу; rate limit срабатывает.
- [ ] Telegram и API используют один код workflow (нет второй копии промптов).
- [ ] После каждого ответа в БД есть `AIUsageEvent`; Dashboard показывает расход и перцентили.
- [ ] Смена модели для задачи = правка одной записи маппинга, код не трогается.
- [ ] Учебник заливается через админку целиком (> 20 МБ), задача доходит до `done`.
- [ ] По `request_id` в логах восстанавливается путь запроса с версиями промптов и моделей.
- [ ] Ни одного секрета в репозитории (скан истории).
- [ ] `STUDENT_AI_INTEGRATION_PLAN.md` готов, eval-baseline зафиксирован.
- [ ] Railway выключен, `railway.toml`/`nixpacks.toml` удалены.
