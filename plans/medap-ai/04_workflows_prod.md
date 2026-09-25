# Этап 4 — Workflows, мультимодальность и выход в production

Самый объёмный этап, внутри два блока. **4B начинается после зелёного чек-листа 4A.**

Предусловие: закрыт чек-лист этапа 3 (есть Evidence Pack, citations, Verification Layer).

**Статус:** пользователь хочет все 8 частей 4A и (частично, после реальных данных) 4B. Слишком
большой объём для одного захода — делаем батчами.

- **Батч 1 (готов):** 4A.1 Tutor/EXPLAIN + CLASS_QUICK. Попутно найден и исправлен реальный баг:
  `Workflow.EXPLAIN`/`CLASS_QUICK` из роутера с этапа 1 только помечали запрос ярлыком в диагностике —
  `generate_answer()` всегда шёл по своей независимой `detect_mode()`-эвристике и всегда вызывал модель
  с `Task.GROUNDED_QA`, поэтому `CLASS_QUICK_MAX_OUTPUT_TOKENS` никогда не применялся, а EXPLAIN не
  получал отдельного стиля ответа. Плюс отдельный найденный баг: `intent == "DIFFERENTIAL"/"MULTI"`
  запускал клинреко-врачебные промпты (`generate_differential`/`generate_multi`) даже для
  `source_type='учебник'`, если `detect_intent()` ошибочно распознавал студенческий вопрос как разбор
  симптомов — теперь эта ветка работает только при `source_type='клинрек'`.
- **Батч 2 (готов):** 4A.2 оценка ответов Recall/Free-recall (текстовые — Oral требует голоса,
  оставлен в батче 6) + 4A.3 диагностика ошибок и repair. `app/workflows/evaluate.py` —
  сравнение не пословно с эталоном, а с материалами по теме (тот же retrieval, что и обычные
  вопросы); формальных Knowledge Units в репозитории нет (`BookChunk.knowledge_unit_ids`
  зарезервировано, не заполняется), поэтому covered/missing — пункты темы из найденного контекста.
  Найденные ошибки (incorrect/causalErrors/contradictions) автоматически запускают
  `generate_repair()` (rag/generator.py) — короткую адресную коррекцию, не повторную лекцию.
  `evidenceReferences` — реальные citations из retrieval, не выдумываются моделью. AI не возвращает
  mastery score (§21/§25) — только сырую структурированную оценку. API: `POST /v1/evaluate/recall`,
  `POST /v1/evaluate/free-answer`, `POST /v1/repair` (отдельно, если оценка уже была). Проверить можно
  в админке: `/admin/playground` → секция «Оценка ответа студента». `/v1/errors/diagnose` отдельным
  эндпоинтом не сделан — то же самое уже возвращают `incorrect`/`causalErrors`/`contradictions` в
  ответе evaluate.
- **Батч 3 (готов):** 4A.4 Vision/Test Solver. `app/workflows/vision.py`:
  `extract_from_image()` — VISION_EXTRACT (GPT-5.4 Mini, multi-part image_url content в сообщении,
  `image_units=1` в телеметрию расхода) распознаёт вопрос/варианты/схему с фото, возвращает confidence;
  при `confidence < 0.5` или пустом вопросе — `needs_retake=True`, честная просьба переснять, а не
  угадывание нечитаемого текста. Распознанный вопрос решается через ОБЫЧНЫЙ Evidence-конвейер
  (`retrieve()` + `generate_answer(..., task=Task.TEST_SOLVE_TEXT)` — новая
  `TEST_SOLVE_MODE_INSTRUCTION` в `rag/generator.py`, просит объяснить и почему дистракторы неверны),
  тот же Verification Layer, citations только из реально процитированного. Vision не источник
  истины — только извлекает вход. API: `POST /v1/vision/analyze` (base64 в JSON, не multipart).
  Telegram: `bot/handlers/vision.py` — `F.photo` хендлер; `bot/middlewares/limits.py` расширен
  (раньше пропускал фото мимо лимитов/db_user, т.к. проверял только `event.text`).
- **Quick Outline API (готов, вне нумерации 4A — отдельная фича по ТЗ владельца, см.
  `plans/medap-ai/QUICK_OUTLINE_SPEC.md`):** НЕ студенческий workflow — обычный Q&A не меняется.
  `app/workflows/quick_outline.py::generate_quick_outline()` — retrieve() по теме → структурированная
  генерация (`Task.QUICK_OUTLINE` → DeepSeek) строго одного из 10 типов схемы
  (mechanism/classification/sequence/comparison/definition/cause_effect/process/pharmacology/
  physiology/pathophysiology/anatomy) с `blocks`+`requiredPoints` по присланному ТЗ; если по теме нет
  материалов — честная пустая схема с `error`, ничего не выдумывается. API:
  `POST /v1/quick-outline/generate` (тот же контракт `StudentAIContext`+service-token, что у
  остальных `/v1/*`), stateless — хранения и публикации на нашей стороне нет, вызывается с отдельного
  сайта владельца. Админский Playground получил секцию-предпросмотр (`/admin/playground/quick-outline`)
  для проверки до готовности сайта-потребителя.
- **Батч 4 (готов):** 4A.5 документы пользователя. `app/workflows/user_documents.py`:
  `ingest_user_document()`/`ask_user_document()`/`delete_user_document()`/`list_user_documents()`.
  Изоляция `user_id + document_id (=Book.id) + опциональный exam_id` — активированы уже
  зарезервированные с этапа 2 поля `BookChunk.user_id`/`exam_id`; `retrieve()` (rag/retriever.py)
  получил параметры `book_id`/`user_id`, оба фильтруются в самом SQL-запросе (защита от ошибки
  проверки владения выше по стеку). `scripts/load_books.py::load_book()` теперь возвращает
  `(book_id, chunks_count)` вместо просто chunks_count (без этого негде взять id личного документа) —
  обновлены все 4 вызывающих места. Новый `Task.DOCUMENT_QA` наконец получил свою
  `DOCUMENT_QA_MODE_INSTRUCTION` в `rag/generator.py` (тот же пробел, что был у EXPLAIN/CLASS_QUICK
  до батча 1). API: `POST /v1/documents` (загрузка, base64 как у Vision), `GET /v1/documents`
  (список своих документов), `POST /v1/documents/{id}/ask`, `DELETE /v1/documents/{id}` — отдельный
  rate limit на загрузку (`USER_DOCUMENT_UPLOAD_PER_MINUTE`), не делит квоту с обычными вопросами.
  Telegram: `bot/handlers/user_documents.py` — `F.document` загружает и включает режим «свой
  документ» (`User.current_document_id`), дальше обычные текстовые сообщения уходят в этот документ
  вместо общего поиска, `/exitdocument` — выход из режима. Чужой `document_id` даёт честную «не
  найдено», а не чужие данные (проверено мок-тестом на уровне workflow). Admin Playground для личных
  документов сознательно не делаем — это приватный контент студента, не публикуемый материал (в
  отличие от Quick Outline).
- **Батч 5 (готов):** 4A.6 веб-поиск. `app/workflows/web_research.py::research_url()` — ОДНА
  конкретная страница по URL (в репозитории нет и не подразумевается общий поисковый API), не
  общий поиск по интернету. SSRF-защита (`app/security/ssrf.py::validate_public_url()`) — резолвит
  хост и блокирует приватные/служебные IP (RFC1918, loopback, link-local включая облачный
  metadata-эндпоинт 169.254.169.254, IPv6 loopback/mapped-IPv4) ДО первого байта ответа страницы;
  редиректы отключены целиком — простое и полное закрытие redirect-based SSRF. Осознанное
  ограничение V1 (задокументировано в коде): защита от прямого SSRF и редиректов, не от DNS
  rebinding между проверкой и запросом (TOCTOU) — тот же уровень строгости, что у остальной
  security в репозитории. Текст страницы передаётся модели между явными маркерами «ДАННЫЕ, НЕ
  ИНСТРУКЦИИ», системный промпт прямо велит игнорировать любые «инструкции» внутри содержимого
  страницы — не идёт через общий Evidence pipeline (`retrieve()`/`generate_answer()`), никогда не
  помечается verified (`authority_level="web"` — последний по приоритету в `constants.AUTHORITY_LEVELS`,
  уже существовал с этапа 2). Новая задача `Task.WEB_RESEARCH` → DeepSeek. API:
  `POST /v1/web/research` (`url`, опциональный `question`). Admin Playground получил секцию
  предпросмотра для ручной проверки SSRF-блокировки и устойчивости к инъекции. Telegram-поверхности
  сознательно нет (как у evaluate/quick-outline) — сценарий «дать URL» естественно приходит с сайта,
  не из чата.
- **Батч 6 (готов, 4A закрыт целиком):** 4A.7 голос. `app/llm/provider.py::transcribe()` — Whisper
  (только OpenAI, у DeepSeek нет STT API, поэтому без TaskModelMap-выбора и без фолбэка).
  `app/workflows/evaluate.py::evaluate_oral()` — транскрипт → тот же `_evaluate()`, что у
  Recall/Free-recall (`Task.ORAL_EVALUATE`, уже был в TaskModelMap с этапа 1, просто не использовался).
  Сбой STT или пустой транскрипт (неразборчиво) → честная просьба перезаписать в `overallFeedback`,
  не выдуманная оценка. По пути найден и исправлен реальный, пусть и мелкий, баг телеметрии:
  `record_usage()` принимал `audio_seconds`, но не передавал его в `cost_usd()` — стоимость аудио
  всегда считалась нулевой (та же природа проблемы, что уже отмечена как известный пробел для
  `image_units`, но эту конкретную решили сразу, так как сами вводили первого потребителя поля).
  `ModelProfile` получил `audio_price_usd_per_minute`, `cost_usd()` — параметр `audio_seconds`.
  API: `POST /v1/evaluate/oral` (audioBase64, как у Vision). `EvaluationResponse` получил поле
  `transcript` (заполнено только для устного ответа). Admin Playground — форма с загрузкой
  аудиофайла, результат рендерится тем же блоком, что Recall/Free-recall (общий формат оценки).
  Telegram-поверхности не было до батча 8 — с него доступно через `/selfcheck`.

## 4A — итог

Все 8 пунктов 4A закрыты: Tutor/EXPLAIN+CLASS_QUICK, Recall/Free-recall+error-diagnosis+repair,
Vision/Test Solver, документы пользователя, web-поиск, голос — плюс отдельно стоящий Quick Outline
API (не часть нумерации 4A, но реализован в этом же цикле работы). Каждый батч задеплоен на Timeweb
и подтверждён пользователем перед переходом к следующему. 4B (Content Studio, Benchmark, Regression,
Gates) остаётся отложенным до реального трафика и загруженного корпуса учебников — по договорённости
с пользователем, см. раздел «Статус» выше. System Health (часть 4B, не требует реальных данных) можно
делать в любой момент по отдельному запросу.
- **4B:** System Health **готов** (`app/admin/health.py::system_health()`, `/admin/health`) — живой
  статус БД/провайдеров/S3/очереди загрузки. Content Studio/Benchmark/Regression-категории/Gates —
  осознанно отложены до реального трафика и загруженного корпуса учебников (без этого физически
  нечего бенчмаркать и не из чего считать пороги; сам механизм регрессии — прогон датасета,
  DB-кейсы, отчёт до/после — уже полностью работает с батча 3B, `/admin/evals`, не хватает только
  категоризации типа ошибки, которая не строилась в этом заходе).

## Батч 8 (готов) — §19 закрыт полностью (настоящий веб-поиск) + PubMed + Telegram-вывод оценки

Батч 5 реализовал только «дай URL, разберём одну страницу» — этот батч добавляет то, что явно
требовал §19: реальный поиск по интернету по запросу.

- **Настоящий веб-поиск** — `app/workflows/web_search.py::search_and_answer()`, Yandex Search API
  (сам фетчит страницы результатов — SSRF-защита не нужна, это один доверенный API, не произвольный
  URL студента; изначально реализовано на Tavily, заменено на Yandex — у пользователя нет карты с
  западным биллингом). Пусто `YANDEX_SEARCH_API_KEY` или `YANDEX_FOLDER_ID` → честная «не настроено»,
  без похода в сеть — активируется само, как только владелец продукта заведёт Yandex Cloud аккаунт и
  впишет оба значения, без дополнительных правок кода. Новая `Task.WEB_SEARCH`
  → DeepSeek (отдельно от `WEB_RESEARCH` в телеметрии). API: `POST /v1/web/search`. Ответ содержит
  `sources: [{title, url}]` — НЕ `Citation`/`evidenceId`, честно: это не `BookChunk` из БД.
- **PubMed** (сверх исходного ТЗ, добавлено по запросу пользователя) — `app/workflows/pubmed.py`,
  NCBI E-utilities (esearch → efetch), бесплатный публичный API, ключ не обязателен. Парсинг реального
  XML-формата NCBI (`xml.etree.ElementTree`, стандартная библиотека): заголовок, склеенный абстракт
  (с лейблами BACKGROUND/METHODS/... если есть), журнал, год (с fallback на `MedlineDate` для старых
  записей без `Year`); статьи без абстракта пропускаются — нечем заземлить ответ, не выдумываем
  содержание. `url` строится по PMID — реальная, проверяемая ссылка на pubmed.ncbi.nlm.nih.gov. Новая
  `Task.PUBMED_SEARCH` → DeepSeek. API: `POST /v1/pubmed/search`. **Живой сетевой тест к NCBI из
  песочницы разработки был недоступен** (egress-политика окружения блокирует хост, не наш код) —
  проверено мок-тестом на реальном формате XML-ответа NCBI (не придуманном), включая оба варианта
  года (Year/MedlineDate) и пропуск статьи без абстракта.
- **Telegram**: когда бот не находит ответ в материалах, `CONSENT_KEYBOARD` (`bot/handlers/query.py`)
  теперь предлагает не только «общие знания ИИ», но и «🌐 Интернет» / «🔬 PubMed» — три равнозначные
  кнопки в один ряд («работать как обычный ИИ», формулировка пользователя). **Плюс прямой вход**,
  не дожидаясь, пока бот сам скажет «не знаю»: команды **`/websearch`**/**`/pubmed`** открывают тот
  же режим сразу (FSM `SearchStates`, тот же общий хелпер `_run_web_search`/`_run_pubmed_search`, что
  и у кнопок — код не дублируется под двумя точками входа). Плюс новая команда **`/selfcheck`**
  (`bot/handlers/recall.py`, FSM по образцу `/addbook`) — студент пишет вопрос/тему, выбирает
  «конкретный вопрос» или «пересказ темы», отвечает текстом ИЛИ голосом; голос уходит в
  `evaluate_oral()` (Whisper-транскрипт показывается студенту), текст — в `evaluate_recall()`/
  `evaluate_free_recall()` по выбранному режиму. То, что раньше было доступно только через API для
  сайта (батч 2 и батч 6), теперь работает и в Telegram. Попутный фикс той же природы, что уже
  дважды случался в этой сессии (фото, документы): `bot/middlewares/limits.py` не пропускал голосовые
  сообщения — `db_user`/`usage_ctx` не инжектились бы в хендлер устного ответа.
- Admin Playground получил секции-предпросмотра для обоих новых workflow.

---

# 4A. Учебные workflows и мультимодальность

Покрывает §16–§24, §53, §18, §19, §20 ТЗ. Все workflow доступны и образовательному сайту, и Telegram —
это один и тот же код за `/v1`.

## 4A.1. Tutor и быстрый режим (§24, §53.1)

- `EXPLAIN/LEARN`: причинно-следственно, с учётом уровня студента, начиная со структуры, без
  «AI-воды», примеры только когда помогают, переход к retrieval при необходимости.
- `CLASS_QUICK`: режим «на паре» — минимальное время, короткий выходной бюджет. Скорость не отменяет
  Evidence/Verification для медицински значимых утверждений.

## 4A.2. Оценка ответов студента (§21, §22)

- `RECALL_EVALUATION`, `FREE_RECALL_EVALUATION`, `ORAL_EVALUATION`.
- Сравнение не пословно с эталоном, а с обязательными Knowledge Units, связями и evidence.
- Типы ошибок: omission, factual error, causal/mechanism error, terminology error, contradiction,
  partially correct, irrelevant content.
- Контракт `OralEvaluation` (§21): `coveredKnowledgeUnits`, `missingKnowledgeUnits`,
  `incorrectKnowledgeUnits`, `partiallyCorrectKnowledgeUnits`, `causalErrors`, `terminologyErrors`,
  `unsupportedStatements`, `repairTargets`, `evidenceReferences`.
- **AI не возвращает authoritative mastery score** — Knowledge State обновляет продуктовый backend
  по детерминированным правилам (§21, §25).

## 4A.3. Диагностика ошибок и repair (§23)

```
Student Answer → Evaluation → Error Extraction → Error Classification →
Root KnowledgeUnit → Targeted Repair → Retry Task
```

Repair короткий и адресный: при локальной ошибке не отвечать повторной длинной лекцией.
Повторяющиеся ошибки передаются продуктовому backend'у.

## 4A.4. Vision и Test Solver (§16, §17, §53.2)

```
Photo/Screenshot → VISION_EXTRACT (GPT-5.4 Mini) → question + options + diagram →
confidence check → trusted retrieval → solving → evidence verification → answer
```

- Vision интерпретирует вход, но **не является источником медицинской истины**.
- Для single/multiple-choice определить формат и полный набор вариантов; при низкой уверенности
  распознавания не угадывать текст, а просить переснять.
- Результат: выбранный ответ, короткое объяснение, почему дистракторы неверны (если evidence
  позволяет), citations, предупреждение при недостаточном evidence или плохом качестве изображения.
- V1 не позиционируется как диагностическая система чтения рентгена/КТ/МРТ.

## 4A.5. Документы пользователя (§18)

```
upload → malware/type/size validation → parse → structure extraction → chunk →
embed → private collection/namespace → retrieval
```

Изоляция: `user_id + document_id + optional exam_id`. Документ одного пользователя никогда не
попадает в retrieval другого и не становится MedAP Verified автоматически. Удаление документа
удаляет или деактивирует связанные чанки и эмбеддинги.

## 4A.6. Web Research (§19, §34)

Отдельный workflow: включить поиск, дать конкретный URL, изучить страницу. Web-контент всегда
получает отдельный provenance и не становится verified автоматически; предпочтение authoritative
источникам. SSRF-защита при ingestion URL. Текст страницы — данные, а не инструкции: «ignore
previous instructions» внутри контента не меняет system policy.

## 4A.7. Voice / Oral V1 (§20)

```
Recorded Voice → STT → transcript → reference Knowledge Units / Evidence →
Oral Evaluator → Verification → structured evaluation
```

Realtime-диалог в V1 не требуется. Transcript и оценка хранятся по политике приватности и retention.

## 4A.8. API

`/v1/evaluate/recall`, `/v1/evaluate/free-answer`, `/v1/evaluate/oral`, `/v1/errors/diagnose`,
`/v1/repair`, `/v1/vision/analyze`, `/v1/documents`, `/v1/web/research` — типизированные и
версионированные контракты (§27, §29).

## Чек-лист 4A

- [ ] Ответ студента → structured evaluation с покрытыми/пропущенными/неверными пунктами.
- [ ] Невалидный structured output не может обновить состояние на стороне продукта.
- [ ] `CLASS_QUICK` заметно быстрее и короче, проверки те же.
- [ ] Фото теста → правильный вариант + объяснение + citations; размытое фото → просьба переснять.
- [ ] Пользователь A не получает ни одного чанка документа пользователя B (автотест).
- [ ] Удаление документа немедленно убирает его из выдачи.
- [ ] Веб-ответ отделён от ответа по учебникам; запрос к приватной подсети блокируется.
- [ ] Страница с «ignore previous instructions» не меняет поведение системы.
- [ ] Голосовое → транскрипт → оценка с ссылками на evidence; сбой STT → просьба перезаписать.
- [ ] Vision, Voice и recall видны в телеметрии стоимости отдельными строками.

---

# 4B. Content Studio, benchmark и production gates

Покрывает §31, §32, §38, §40, §49, §50, §61, §62 ТЗ + разделы 11, 12, 14 дополнения.

## 4B.1. Content Studio (§31, §32)

```
Verified Note / Source → Knowledge Extraction → Draft Cards / Tests / Cases →
Evidence Verification → Admin Review/Edit → Approve → Publish
```

- Публикация требует подтверждения человеком; AI только помогает генерировать.
- Прогрессивная сложность кейсов (§32): recognize → explain → connect findings → reason about
  additional data → integrate multiple topics. Каждый кейс трассируется к Knowledge Units и evidence.
- Динамическая персональная генерация для адресного recall/repair разрешена, но глобальным
  verified-контентом не становится.

## 4B.2. Benchmark и смена моделей (§38, §60, §62)

- Прогон DeepSeek V4.1 Flash и GPT-5.4 Mini по **каждой атомарной задаче** на одинаковом датасете,
  одинаковом Evidence Pack, одинаковой source policy, с versioned prompts.
- Метрики: factual correctness, evidence support rate, citation correctness, unsupported claim rate,
  согласие оценок с человеческой рубрикой, зависимость от retrieval, latency, throughput, токены,
  стоимость на workflow, прогноз стоимости на активного пользователя в месяц.
- Фиксация production-маппинга: побеждает самая дешёвая конфигурация, проходящая quality/safety
  gates на конкретном workflow. Смена — через конфигурацию, canary и rollback; код сайта не меняется.
- Три рычага экономии перед запуском (§64.4): можно ли перенести `VISION_EXTRACT` на DeepSeek;
  можно ли перенести часть Recall/Error evaluation на DeepSeek без потери согласия с рубрикой;
  сокращение Evidence Pack и выходных бюджетов плюс кэширование.

## 4B.3. Regression suite (§40)

Каждая найденная серьёзная ошибка превращается в regression-тест. Категории: hallucination, wrong
citation, retrieval miss, wrong source priority, conflict suppression, numeric error, oral grading
error, private data leak, vision extraction error, prompt injection failure.
Нельзя выпускать новую версию модели/промпта/retrieval, если она ломает critical gates.

## 4B.4. Production gates (§49) и Definition of Done (§50)

Пороги устанавливаются после baseline, а не выдумываются заранее. Обязательные gates: критичные
медицинские фактические ошибки; доля неподтверждённых утверждений; корректность цитат; успешность
retrieval; обработка конфликтов; числовая безопасность; изоляция приватных данных; устойчивость к
prompt injection; согласие оценщика с рубрикой; стабильность задержки и пропускной способности.
Критичная регрессия по безопасности или изоляции данных — блокер релиза.

Definition of Done v1 (§50): один API обслуживает сайт и Telegram; medical QA идёт через Evidence
pipeline; citations трассируются до реальных источников; неподтверждённые утверждения
repair/abstain; Recall/Free/Oral возвращают typed outputs; Vision решает учебные фото через
Evidence; приватные документы изолированы; Web Research отделён provenance; Content Studio требует
human approval; benchmark выполнен; regression/eval suite работает автоматически; мониторинг
позволяет найти причину ошибки; Knowledge State остаётся вне AI-сервиса; замена модели не ломает
Learning/Exam Mode.

## 4B.5. System Health и аналитика (раздел 14 дополнения)

**Статус: готово.** Состояние БД (проба + задержка), провайдеров (настроен/нет ключа), S3, очереди
загрузки источников по статусам — `/admin/health` (`app/admin/health.py::system_health()`). Доля
fallback, ошибки за сутки, cost/user, P50/P90/P99, самые дорогие workflows, прогноз месячного
расхода — уже были на Dashboard (`app/admin/stats.py::dashboard_stats()`, этап 1), System Health их
переиспользует, а не считает заново. Vision/Voice/Recall usage — видны в тех же `ai_usage_events`
отдельными workflow-строками (этап 4A). Budget alerts (обрыв ответа на середине при превышении
бюджета) не реализован — сознательно: подтверждённый медицинский ответ обрываться не должен (§59),
а порог для алерта без реального трафика ставить не из чего (см. Gates, 4B.4).

## Чек-лист 4B

- [x] Опубликовать материал Content Studio без подтверждения человеком невозможно — API отдаёт только
  `status: "AI_DRAFT"`, хранения/публикации на нашей стороне нет; публикация — на сайте владельца.
- [x] Каждый сгенерированный элемент трассируется к evidence (evidenceIds конкретных фрагментов;
  элемент без опоры отбрасывается). Knowledge Units в репозитории не существуют — трассировка к чанкам.
- [x] `evals/` запускается одной командой (`python -m evals.run`, `python -m evals.benchmark`), отчёт по
  категориям с ценой и задержкой; прогоны сохраняются в БД.
- [ ] Production-маппинг моделей зафиксирован по результатам прогона — **механизм готов** (benchmark +
  смена/откат на странице Models), сам прогон — после загрузки настоящих учебников.
- [x] Категории регрессий (§40), кнопка «Добавить в Evals» из Answer Inspector с категорией по причине.
- [ ] Critical gates определены числами — **механизм готов, выключен** (`GATES_ENABLED=false`), числа
  вписать после baseline на настоящих учебниках. Критичные категории (утечка данных, prompt injection)
  проваливают gate независимо от чисел.
- [x] System Health показывает реальное состояние подсистем.
- [ ] Все пункты Definition of Done v1 (§50) отмечены и проверены вручную — за пользователем на Timeweb.

## Батч 11 — 4B целиком (Content Studio API, регрессии, Benchmark, Gates)

- **Content Studio** (`app/workflows/content_studio.py`): `POST /v1/content/recall-items`,
  `/test-questions`, `/clinical-case` — черновики для сайта владельца (как Quick Outline, без хранения у
  нас). Фрагменты передаются модели с метками `[F1]…`, каждый элемент обязан сослаться на метки;
  backend детерминированно мапит их на реальные чанки, элемент без валидной опоры или с битой структурой
  (например `correctIndex` вне вариантов) отбрасывается и считается в `droppedUnsupported`. Задачи
  `CONTENT_RECALL/TEST/CASE` → DeepSeek. Предпросмотр — секция в Playground (ссылка «Content Studio» в меню).
- **Регрессии**: `constants.REGRESSION_CATEGORIES` (§40 + старые категории датасета), выбор категории
  при добавлении кейса, разбивка «по категориям» у каждого прогона.
- **Прогоны в БД** (`EvalRun`): раньше отчёты жили в памяти и терялись при деплое. Кнопка «Сделать
  baseline», дельта каждой метрики относительно baseline.
- **Benchmark** (`evals/benchmark.py`, кнопка на странице Evals): тот же датасет на каждом провайдере с
  ключом; провайдер принудительно меняется через `ContextVar` только для задач генерации и только внутри
  прогона — production не затронут. Задачи оценки ответов студента не покрыты (нет датасета «ответ →
  ожидаемые ошибки»).
- **Смена production-провайдера без пересборки** (`TaskModelOverride`, страница Models): «Применить» и
  «Откатить», история, аудит. Приоритет: админка → `TASK_MODEL_MAP_OVERRIDES` → код. При включённых
  Gates смена на провайдера, провалившего gate в последнем benchmark, блокируется.
- **Gates** (`evals/gates.py`, `GATE_*` в конфиге): выключены по умолчанию; результат показывается у
  каждого прогона; `python -m evals.run --gates` возвращает код 1 при провале, если включены.

Проверка: компиляция + pyflakes; рендер всех изменённых шаблонов на реалистичных данных; смоук 14/14;
регрессия (batch10 45, web_search/pubmed 28, clinrek 14, direct-search 8); новый мок-тест 32/32
(отбраковка элементов без опоры, маппинг меток на evidenceId, приоритет принуждение → админка → код,
деградация при недоступной БД, gates вкл/выкл, benchmark по разу на провайдера); 401 без токена на всех
`/v1/content/*`, редирект на вход на всех новых маршрутах админки.

## Батч 10 — grounding-строгость, запрет fallback для клинреков, контракт ответа, ₽-бюджет

Пользователь прислал развёрнутый спек ("MedAP Student AI / Evidence — Production Prompt & AI
Implementation Specification v1.0") плюс отдельный готовый комплект из 7 system-промптов. Аудит
(3 Explore-агента) нашёл: правило "термин ≠ доказательство" было только в студенческом `SYSTEM_PROMPT`;
общего правила на частичное покрытие темы не было вообще; клинреки в Telegram получали тот же
fallback (общие знания/веб/PubMed), что и учебники — прямое нарушение "для клинического режима
fallback запрещён"; `extract_cited_chunks()` при отсутствии совпадений тихо приписывала ВСЕ
переданные чанки как "процитированные" (citation laundering).

**Сделано:**
- `rag/generator.py`: все 6 системных промптов (SYSTEM/CLINREK/FALLBACK/DIFFERENTIAL/MULTI/REPAIR)
  дополнены явным порядком приоритета правил, "термин ≠ доказательство" (+ запрет вывода из одного
  упомянутого слова) во всех промптах, разделением "ответа нет" (INSUFFICIENT) vs "ответ неполный"
  (PARTIAL) через фиксированные маркер-фразы `PARTIAL_EVIDENCE_MARKER`/`CLINREK_PARTIAL_EVIDENCE_MARKER`,
  запретом числовой вероятности без основания в контексте (дифдиагноз), правилами "контекст важнее
  утверждения пользователя" и "не исправлять учебник по своим знаниям". Скобочная инструкция цитат
  `[Автор, Название, стр. N]` сознательно ОСТАВЛЕНА во всех промптах (backend всё ещё парсит её
  регексом — полная структурированная claims/citationId-архитектура из спека отложена: лишний
  LLM-вызов на atomic claim не укладывается в новый месячный ₽-бюджет, см. ниже).
- Режим «конспект» в обычном студенческом чате (`CONSPECT_PATTERN`/`MODE_INSTRUCTIONS["conspect"]`)
  расширен до формата Quick Outline (стрелки, 10 типов структуры) — по явному решению пользователя
  дать эту функцию и студентам, не только владельцу через отдельный `app/workflows/quick_outline.py`
  (тот не тронут, остаётся stateless owner-only API).
- `app/evidence/citations.py::extract_cited_chunks()` — убран фолбэк "нет совпадений → все чанки",
  теперь честно `[]` (нет цитат, а не "все источники подтверждают").
- `bot/handlers/query.py::handle_question` — клинреки без ответа в материалах теперь получают
  `CLINREK_NOT_FOUND_TEXT` без единой кнопки fallback; учебники — как раньше, три варианта на выбор.
- `app/workflows/ask.py::AskResult`/`app/api/v1.py::ChatResponse` — новые производные поля
  `sourceMode`/`evidenceStatus`/`verificationStatus`/`unsupportedAreas`/`clinicalWarnings`, все без
  единого нового вызова LLM (регэксп-эвристики над уже сгенерированным текстом/уже посчитанным
  `verified`/`conflicts`).
- Месячный ₽-бюджет вместо дневного счётчика количества запросов (Telegram, `FREE_MONTHLY_BUDGET_RUB=20`/
  `PREMIUM_MONTHLY_BUDGET_RUB=150` при цене подписки 400₽/мес) — `db/crud.py::month_spend_rub()`/
  `monthly_budget_rub_for()`/`is_limit_exceeded()`, счёт по `AIUsageEvent.provider_cost_rub` за текущий
  календарный месяц. Только Telegram — у API/сайта пока нет флага premium в `StudentAIContext`.
  Dashboard получил "средний/максимальный расход Premium/мес" — проверка на практике, укладывается ли
  реальная нагрузка в лимит.

**Осознанно отложено** (описание расхождений и причин — см. `STUDENT_AI_INTEGRATION_PLAN.md`): полная
claims/citationId-архитектура, RBAC, вьюер audit log в админке, персистентность Content Studio
(draft→review→publish), явные cross-user/cross-subject проверки в валидаторе (уже обеспечены
структурно фильтрами retrieval), денежный лимит для сайта/API.

Проверка: компиляция всех изменённых файлов; смоук-тест 14/14; новый мок-тест (`test_batch10.py`,
42 проверки) — наличие новых правил-строк в промптах, фикс citation laundering, gate fallback для
клинреков (плюс регрессия для учебников), вычисление всех пяти производных полей на синтетических
комбинациях, `is_limit_exceeded()`/`monthly_budget_rub_for()` на моках. Один пуш.
