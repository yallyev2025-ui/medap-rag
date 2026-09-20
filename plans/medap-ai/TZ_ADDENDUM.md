# Дополнение к ТЗ — Admin Panel, Sources и Citation UX

Дополнение к «MedAP Student AI / Evidence — Technical Specification v1.0». Сохранено как получено от
владельца продукта; в случае расхождений с основным ТЗ приоритет у этого документа.

## 1. Архитектурный принцип

Для MedAP Student AI не нужен отдельный пользовательский сайт.

Student AI состоит из:

```
MEDAP EDUCATIONAL WEBSITE
        ↓
MedAP Student AI API
        ↓
RAG / Evidence / Verification
        ↓
DeepSeek / OpenAI
```

Отдельно существует:

```
PRIVATE ADMIN PANEL
        ↓
MedAP Student AI Backend
```

Admin Panel доступна только владельцу/команде MedAP. Студенты не регистрируются в ней и не имеют к
ней доступа. У пользователя должна быть одна регистрация — на основной образовательной платформе
MedAP. Student AI подключается к ней через API.

## 2. Закрытая Admin Panel

Необходимо создать отдельную защищённую web-админку для управления MedAP Student AI. Это фактически
рабочее место владельца AI-системы. Через неё я должен иметь возможность управлять системой без
необходимости постоянно заходить в код.

Главный экран условно:

```
MEDAP AI ADMIN
Dashboard
Knowledge Base
Sources
Subjects
Retrieval Inspector
AI Playground
Answer Inspector
Prompts & Policies
Models
Evals
Content Studio
Usage & Costs
System Health
```

## 3. Knowledge Base / загрузка учебников

Через админку я должен самостоятельно загружать медицинские источники. Например:

```
+ Добавить источник
Название:    Патофизиология — Новицкий
Предмет:     Патофизиология
Тип:         Основной учебник
Издание:     ...
Год:         ...
Authority:   Primary textbook
Verification: Verified
```

После загрузки система автоматически запускает:

```
Upload → Parsing → Structure extraction → Chunking → Metadata → Embeddings → Indexing → Ready
```

В админке я должен видеть состояние:

```
Новицкий.pdf
✓ Parsed
✓ 2 431 chunks
✓ Embedded
✓ Indexed
✓ Production
```

Из интерфейса должно быть возможно: загрузить новый учебник; заменить файл; добавить методичку;
указать предмет; указать раздел/тему; назначить тип и authority источника; изменить metadata;
включить источник; отключить источник; архивировать; переиндексировать; посмотреть извлечённый
текст; посмотреть chunks; проверить конкретную страницу; посмотреть, используется ли источник AI.

Для обычного добавления нового учебника не должно требоваться изменение кода.

## 4. AI Playground внутри админки

Мне необходимо иметь возможность непосредственно из Admin Panel тестировать Student AI. Отдельный
пользовательский Student AI-сайт для этого не создаётся.

```
Предмет: Патофизиология
Source mode: MedAP Verified
[Почему при шоке развивается лактат-ацидоз?]   [Отправить]
```

После ответа я вижу одновременно пользовательское представление:

```
При тканевой гипоперфузии…
[1] Новицкий, стр. 314
```

и техническую информацию:

```
Workflow:     GROUNDED_QA
Model:        DeepSeek ...
Retrieved:    12 chunks
Evidence used: 4
Verification: PASS
Input / Output: ...
Cost:         0.13 ₽
Latency:      ...
```

То есть всю систему можно проверять из админки до того, как её увидит студент.

## 5. Retrieval Inspector

В AI Playground должна быть возможность включить Developer/Inspector Mode. Тогда для любого запроса
я вижу весь pipeline:

```
QUESTION → Normalized Query → Subject Routing → Metadata Filters → BM25 Results →
Dense Results → Fusion → Reranking → Evidence Pack → LLM → Verification → Final Answer
```

Я должен видеть конкретные chunks, scores, источники и Evidence Pack. Это необходимо для
диагностики неправильных ответов.

## 6. Кликабельные источники

Student AI должен возвращать образовательному сайту структурированные citations. Студент получает:

```
При снижении тканевой перфузии увеличивается анаэробный гликолиз… [1]
```

При нажатии `[1]` открывается источник. Если это учебник MedAP:

```
Новицкий
Патофизиология
стр. 314
─────────────────────
обычный текст...
[ВЫДЕЛЕН КОНКРЕТНЫЙ ФРАГМЕНТ, КОТОРЫЙ ПОДТВЕРЖДАЕТ ОТВЕТ]
обычный текст...
─────────────────────
```

То есть студент может сам проверить AI. Это тот же принцип Evidence UX, который используется в
концепции MedAP Doctor.

## 7. Source Viewer на образовательном сайте

Основной образовательный сайт должен иметь Source Viewer. Он получает от Student AI API:

```
sourceId, sourceTitle, page, section, citationId, evidenceId, exactSupportingText, URL (если существует)
```

И показывает нужное место источника. Если источник публичный — можно дополнительно открыть
оригинальную внешнюю ссылку. Если источник хранится внутри MedAP — открывается внутренний viewer.

Необходимо поддерживать: ответ → citation → источник → страница → выделенный evidence.

## 8. Answer Inspector

Все запросы студентов к AI должны быть доступны в Admin Panel в соответствии с принятой политикой
хранения данных и доступа. Для запроса необходимо видеть:

```
Question, Subject, Workflow, Retrieval, Evidence Pack, Model, Answer, Citations,
Verification, Latency, Tokens, Cost, Feedback
```

Если ответ неправильный, я должен иметь возможность отметить причину: Incorrect answer, Bad
retrieval, Bad citation, Insufficient source, Explanation problem, Source conflict, Evaluation
problem, Other.

## 9. Улучшение AI через админку

Под «дообучением MedAP под себя» в V1 понимается не постоянный fine-tuning базовой LLM, а
возможность управлять поведением всей системы через Admin Panel:

```
AI ошибся → открываю Answer Inspector → вижу Evidence Pack → нахожу причину →
исправляю source / metadata / retrieval / prompt / policy → добавляю ошибочный запрос в Eval →
запускаю Regression → проверяю исправление → Publish
```

Так MedAP AI постепенно становится лучше на наших реальных медицинских задачах.

## 10. Prompt / Policy Management

Через Admin Panel необходимо управлять prompts и policies, но обязательно через:

```
DRAFT → TEST → EVAL → PUBLISH → PRODUCTION
```

С возможностью Rollback. Нельзя изменять production prompt без version history.

## 11. Управление моделями

В Admin Panel отображается `TaskModelMap`:

```
GROUNDED_QA → DeepSeek
EXPLAIN → DeepSeek
TEST_SOLVE → DeepSeek
VISION → GPT
RECALL_EVALUATE → GPT
ORAL_EVALUATE → GPT
```

Можно менять mapping через configuration, но production-изменение проходит benchmark/eval. Не нужно
переписывать код сайта при замене модели.

## 12. Content Studio

Через админку должна быть возможность брать загруженный проверенный учебник и создавать из него
draft-контент для MedAP:

```
SOURCE → Knowledge Units → Outline → Active Recall → Tests → Clinical Cases → Explanations
```

После чего:

```
AI Draft → Human Review → Verified → Publish
```

Это особенно важно, когда начнём полностью загружать физиологию, патфиз, патан, фармакологию и
остальные предметы.

## 13. Evals

Из неправильного ответа прямо в админке должна быть кнопка «Добавить в Eval Dataset». После
исправления можно повторно прогнать этот и остальные контрольные вопросы. Так каждое найденное нами
слабое место не должно возвращаться после следующего обновления модели/RAG/prompts.

## 14. Analytics / Costs

Admin Panel должна показывать: AI requests today; Active users; Errors; Verification failures;
DeepSeek cost; OpenAI cost; Total AI cost; Cost / user; P50 / P90 / P99; Vision usage; Voice usage;
Recall usage; Most expensive workflows; Anomalous users; Projected monthly cost.

## 15. Конечная архитектура

```
                    ┌──────────────────────┐
                    │ PRIVATE ADMIN PANEL  │
                    │ Sources              │
                    │ AI Playground        │
                    │ Retrieval Inspector  │
                    │ Prompts              │
                    │ Evals                │
                    │ Models               │
                    │ Analytics            │
                    └──────────┬───────────┘
                               ↓
                    MEDAP STUDENT AI API
                 ┌─────────────┼─────────────┐
                 ↓             ↓             ↓
               RAG         Evidence      Verification
                 └─────────────┬─────────────┘
                               ↓
                         TaskModelMap
                               ↓
                    DeepSeek / OpenAI
                               ↓
                    Structured Answer
              answer + claims + citations + evidence + sources
                               ↓
                MEDAP EDUCATIONAL WEBSITE
                               ↓
                         STUDENT
                               ↓
              answer → source → page → highlighted text
```

Отдельной регистрации или отдельного Student AI-сайта для студентов нет. Студент знает только
основной MedAP. Админка — внутренний инструмент управления всей AI/Evidence-системой.
