# Фаза 05 — Админ-команды и статистика

## Цель
Реализовать `/stats`, `/addbook`, `/broadcast`, `/ban`, `/premium` — доступны только пользователям из `settings.ADMIN_IDS`.

## Предпосылки
Фазы 01–04 выполнены. Есть таблицы `users`, `usage`, `queries`, `books`, `book_chunks`, рабочий `scripts/load_books.py`.

## Что создать

### `bot/handlers/admin.py`
Router с фильтром `F.from_user.id.in_(settings.ADMIN_IDS)` на все хендлеры этого роутера (либо явная проверка в начале каждого хендлера + `return` с "Команда недоступна" для прочих — выбрать filter на уровне router, это чище).

#### `/stats`
Собирает и форматирует:
```
Пользователи:
- Всего: {total_users}
- Новые сегодня: {new_today}
- Активные сегодня: {active_today}
- Премиум: {premium_count}

Запросы:
- Всего: {total_queries}
- Сегодня: {today_queries}
- Среднее в день: {avg_per_day}

Топ-10 тем:
{top_10_questions}

Разбивка по предметам:
{subject_percentages}

Среднее время ответа: {avg_response_time} мс
```
Реализация через SQL-агрегаты (SQLAlchemy `func.count`, `func.avg`, `group by`):
- `active_today` — `count(distinct user_id)` в `queries` за сегодня.
- `top_10_questions` — `group by question, order by count desc limit 10` (можно группировать по нормализованному/lowercase тексту вопроса; для топ-тем достаточно простого group by на сырой текст — если вопросы редко повторяются буквально, в комментарии к коду указать как возможное улучшение — но не реализовывать сверх ТЗ).
- `subject_percentages` — `group by subject`, посчитать процент от `total_queries` (исключая `NULL` subject из знаменателя или показывать отдельной строкой "без ответа в материалах: X%").
- `avg_per_day` — `total_queries / count(distinct date(created_at))`.

#### `/addbook`
Многошаговый сценарий (FSM aiogram, `StatesGroup`):
1. Админ отправляет `/addbook` → бот просит прислать PDF-файл (документ).
2. Бот получает `message.document`, проверяет `mime_type == "application/pdf"` и размер ≤ 20MB (лимит Telegram Bot API на скачивание файла ботом), скачивает через `bot.download(file)` во временную папку.
3. Бот спрашивает subject (inline-кнопки со списком: pathanatomy, pathphys, physiology, anatomy, biochemistry, pharmacology, + "другой" с вводом текста).
4. Бот спрашивает author и title (текстовые сообщения, по очереди).
5. Вызывает функцию из `scripts/load_books.py` (рефакторить CLI в переиспользуемую функцию `load_book(pdf_path, subject, author, title) -> int` (возвращает chunks_count), CLI обёртка остаётся для ручного использования).
6. После успешной загрузки — удалить временный PDF (`os.remove`), сообщить "Учебник добавлен: {title}, {chunks_count} чанков".
7. Обработка ошибок (повреждённый PDF, нет текста) — сообщить админу, не оставлять "битую" запись в `books`/`book_chunks` (откатить транзакцию).

#### `/broadcast`
1. `/broadcast <текст>` или FSM: админ отправляет `/broadcast`, бот просит следующее сообщение как текст рассылки.
2. Получить всех `user.id` из `users` где `is_banned = False`.
3. В цикле `await bot.send_message(user_id, text)`, с `try/except` на `TelegramForbiddenError` (пользователь заблокировал бота — пропустить) и `asyncio.sleep(0.05)` между сообщениями (чтобы не упереться в Telegram rate limit ~30 msg/sec).
4. После завершения — отчёт админу: "Отправлено: {success}, не доставлено: {failed}".

#### `/ban <user_id>` и `/unban <user_id>`
- Парсинг аргумента команды (`message.text.split()`), `crud.set_ban(session, user_id, True/False)`.
- Ответ "Пользователь {user_id} заблокирован/разблокирован".

#### `/premium <user_id>` и `/unpremium <user_id>`
- Аналогично, `crud.set_premium`.

### Регистрация
`bot/main.py` — добавить `dp.include_router(admin_router)` (порядок: admin router до общего query-роутера, чтобы команды админа не попадали в `query.py` как обычный текст — впрочем команды `/...` и так не матчатся фильтром `F.text` без слэша в фазе 03, но FSM-сообщения внутри `/addbook`/`/broadcast` сценария — обычный текст, поэтому FSM-состояния должны иметь приоритет; aiogram обрабатывает это через `StatesGroup` фильтры автоматически при правильном порядке роутеров).

### Тест фазы
1. От обычного пользователя `/stats` → нет ответа или "команда недоступна" (по решению — тихо игнорировать вписать в код).
2. От админа `/stats` → корректно оформленный отчёт с реальными цифрами из БД, накопленными в фазах 03-04 тестов.
3. `/addbook` с тестовым PDF → книга появляется в `books`, чанки в `book_chunks`, retriever (фаза 01) находит чанки из новой книги.
4. `/broadcast тест` → тестовый пользователь получает сообщение.
5. `/ban <id>` → забаненный пользователь не получает ответов на вопросы (middleware фазы 04 должен это учитывать — проверить интеграцию).
6. `/premium <id>` → у пользователя `/limit` показывает безлимит, лимит не применяется.
