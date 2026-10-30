# MedAP — план работ (указатель)

**Главный план лежит в репозитории сайта: `Medapsite/docs/medap/plan/`** (README с решениями владельца, аудит, ТЗ и файлы этапов). Работы идут в обоих репозиториях: сайт (`yallyev2025-ui/Medapsite`) — хранилище знаний, Studio, ментор; бот (`medap-rag`, этот репозиторий) — чтение учебников, поиск, модели, PubMed, Telegram. Если в сессии открыт только этот репозиторий — подключить сайт и читать план там.

| Этап | Файл (в Medapsite) | Что в боте |
|---|---|---|
| 1 | `docs/medap/plan/etap-1-fundament-i-uchebniki.md` | `rag/processor.py`: главы, печатные страницы, таблицы, рисунки с подписями (`BookFigure`, S3), флаг «плохо читается», дедуп, версии; `GET /v1/books`, `/v1/figures`; Luna главной в `app/llm/task_map.py`; pytest и CI |
| 2 | `docs/medap/plan/etap-2-pasporta-i-baza-znaniy.md` | поиск по книгам предмета для паспортов и извлечения знаний |
| 3 | `docs/medap/plan/etap-3-proverka-i-ispravlenie.md` | проверка по учебникам, PubMed для пробелов, сводка в Telegram |
| 4 | `docs/medap/plan/etap-4-proizvodstvo-i-dizayn.md` | писатель конспекта на пакете знаний (`/v1/content/conspect`), подбор рисунков |
| 5 | `docs/medap/plan/etap-5-mentor-i-kachestvo.md` | `/v1/chat` с пакетом знаний, фильтры поиска, PubMed сразу, метки происхождения, противоречия; Telegram наравне с сайтом, согласие в БД |
| 6 | `docs/medap/plan/etap-6-samoobuchenie.md` | версии промптов (`PromptVersion`) под проверкой контрольным набором |
| 7 | `docs/medap/plan/etap-7-golos.md` | синтез речи (Piper / OpenAI), голосовые в Telegram, faster-whisper вместо Whisper API, живой диалог, мониторинг клинреков |

Правила работы — в `CLAUDE.md` этого репозитория и в README плана на сайте.
