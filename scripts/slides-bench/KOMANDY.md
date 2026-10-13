# Команды этапа 1: стенд замеров стоимости

Файл лежит в репозитории, чтобы команды не терялись. Копия — в репозитории бота: `scripts/slides-bench/KOMANDY.md`.

**Что делает стенд.** Делает настоящие запросы к моделям (черновик на 5/10/15/20 слайдов, «сократить слайд», «перепиши», текст доклада), берёт из ответов токены, которые сам поставщик выставляет в счёт, и умножает на цену. На выходе — таблица «модель × размер → рубли» с отметкой, уложились ли в потолок (15 ₽ для 5–10 слайдов, 25 ₽ для 15–20).

**Где какие ключи.** Anthropic (Claude) — на сервере бота. OpenAI — на сервере сайта (DeepSeek по решению владельца не сравниваем). Поэтому два запуска; в каждом команды идут в таком порядке: проверка → оценка → прогон → таблица.

**Чтобы консоль не потерялась.** В каждой команде есть `--log файл.log`: весь вывод дублируется в файл. Файлы лежат в `/tmp` на сервере; результаты прогона (`out-*.json`) — тоже там.

---

## 1. Сервер САЙТА (OpenAI)

Скрипт лежит в образе сайта: `/app/scripts/slides-bench/slides-bench.mjs` (после выкладки). Node там есть.

Модели и цены уже зашиты в скрипт (подтверждены владельцем 13.10.2026): **gpt-6-luna** — $0,10 вход / $0,50 выход, **gpt-6.1-sol** — $2 / $10 за миллион токенов. Id Luna сверен с сайтом разработчиков OpenAI; **id Sol взят из прессы и OpenRouter — шаг 1 покажет, есть ли он у вашего ключа.** Курс 84,2 ₽ за доллар — как в боте; если нужен другой, поменяйте число после `--usd-rub`.

Копируйте блок целиком:

```bash
cd /app

# 1) Какие модели видны по вашему ключу. В конце: ✓ есть / ✗ НЕТ В СПИСКЕ для gpt-6-luna и gpt-6.1-sol
node scripts/slides-bench/slides-bench.mjs --list-models --providers openai

# 2) Проверка ключа и связи (копейки)
node scripts/slides-bench/slides-bench.mjs --check --providers openai --usd-rub 84.2 --log /tmp/stend-site-check.log

# 3) Оценка расхода БЕЗ запросов к моделям
node scripts/slides-bench/slides-bench.mjs --dry-run --providers openai --usd-rub 84.2 --log /tmp/stend-site-dry.log

# 4) Боевой прогон (минуты)
node scripts/slides-bench/slides-bench.mjs --providers openai --usd-rub 84.2 --out /tmp/out-site.json --log /tmp/stend-site-run.log

# 5) Таблица и файл-пакет для передачи
node scripts/slides-bench/slides-bench.mjs merge /tmp/out-site.json --usd-rub 84.2 --out /tmp/stend-site.md --log /tmp/stend-site-merge.log
cat /tmp/stend-site.md
```

Если в шаге 1 у Sol стоит «✗ НЕТ В СПИСКЕ» — скопируйте из списка точный id (строки с `gpt-6`) и в шагах 2–5 добавьте `--openai-models gpt-6-luna,ТОЧНЫЙ_ID_SOL --price ТОЧНЫЙ_ID_SOL=2/10`. Если Sol нет совсем — запускайте только Luna: `--openai-models gpt-6-luna`.

## 2. Сервер БОТА (Claude)

Скрипт лежит в репозитории бота: `scripts/slides-bench/slides-bench.mjs` (после выкладки бота). Бот работает на `python:3.12-slim`, **Node там, скорее всего, нет** — поставьте один раз в консоли контейнера:

```bash
apt-get update && apt-get install -y nodejs
node -v     # годится 18 и новее
```
Если `apt` недоступен, запасной путь (мной не проверялся): скачать готовый Node в папку и запускать его оттуда — `curl -fsSL https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-x64.tar.gz | tar -xz -C /tmp`, дальше `/tmp/node-v22.11.0-linux-x64/bin/node` вместо `node`.

```bash
cd /app      # или туда, где лежит код бота

# 1) Проверка ключа Anthropic (копейки)
node scripts/slides-bench/slides-bench.mjs --check --providers anthropic --usd-rub 84.2 --log /tmp/stend-bot-check.log

# 2) Оценка без запросов
node scripts/slides-bench/slides-bench.mjs --dry-run --providers anthropic --usd-rub 84.2 --log /tmp/stend-bot-dry.log

# 3) Боевой прогон. Потолок по Claude по умолчанию $0.9 (ваш баланс $1): дойдёт и остановится сам
node scripts/slides-bench/slides-bench.mjs --providers anthropic --usd-rub 84.2 --out /tmp/out-bot.json --log /tmp/stend-bot-run.log

# 4) Таблица и файл-пакет
node scripts/slides-bench/slides-bench.mjs merge /tmp/out-bot.json --usd-rub 84.2 --out /tmp/stend-bot.md --log /tmp/stend-bot-merge.log
cat /tmp/stend-bot.md
```

## 2а. Проверка замены GPT-5.4 → GPT-6 Luna (бот)

С 13.10.2026 бот целиком работает на **gpt-6-luna**: ответы студентам, глубокий режим, конспекты, фото тестов, оценка ответов (цены $0,10 / $0,01 кеш / $0,50). Проверка после выкладки бота — в консоли контейнера бота (Python и ключи там уже есть):

```bash
cd /app      # или туда, где лежит код бота

# Что видит бот, кто на какой модели, один живой запрос (копейки). В конце должно быть: ✓ всё на «gpt-6-luna», цены верные
python scripts/check_openai_model.py

# Не осталось ли на сервере старых значений, которые перебивают новые (должно быть пусто или gpt-6-luna / 0.1 / 0.01 / 0.5)
env | grep -E '^(OPENAI_MODEL|OPENAI_EVAL|OPENAI_BASE_URL|PRICING_VERSION|TASK_MODEL_MAP_OVERRIDES)' | sed 's/KEY=.*/KEY=(скрыто)/'
```

Если скрипт пишет «✗ в работе модель gpt-5.4-mini» или про цены — в панели Timeweb (переменные приложения бота) удалите `OPENAI_MODEL`, `OPENAI_EVAL_MODEL`, `OPENAI_EVAL_INPUT_PRICE_USD`, `OPENAI_EVAL_CACHED_INPUT_PRICE_USD`, `OPENAI_EVAL_OUTPUT_PRICE_USD` (тогда возьмутся новые из кода) и перезапустите бота.

После этого вручную, на сайте/в боте:
1. Задайте 3–4 вопроса в обычном и в глубоком режиме — ответы должны прийти, без «Произошла ошибка».
2. Сгенерируйте один конспект — на экране генерации должно быть «Писал: OpenAI · модель gpt-6-luna …», а стоимость — копейки.
3. Отправьте боту фото теста — должен прочитать.
4. Поищите ошибки в логах бота: `grep -iE "400|404|reasoning_effort|temperature|model" <лог>` — не должно быть повторяющихся отказов OpenAI. (Одна строка «Параметр … не принят — повторяю без него» допустима: бот сам справляется.)

Если что-то сломалось и срочно нужно вернуть старое без выкладки кода: переменные `OPENAI_MODEL=gpt-5.4-mini`, `OPENAI_EVAL_INPUT_PRICE_USD=0.75`, `OPENAI_EVAL_CACHED_INPUT_PRICE_USD=0.075`, `OPENAI_EVAL_OUTPUT_PRICE_USD=4.50` и перезапуск.

## 3. Что прислать мне
С каждого сервера:
1. Таблицу (`cat /tmp/stend-*.md` — скриншот или текст);
2. Файл `/tmp/stend-*-pack.txt`: откройте (`cat`), скопируйте **весь** текст (это одна длинная строка) и пришлите;
3. Если что-то упало — файл `/tmp/stend-*-run.log` целиком.

Из пакетов я соберу безымянные PDF и PPTX (названия моделей скрыты: A, B, C…), вы выберете «слепо» лучшие.

## 4. Как убедиться, что суммы правдивые
До прогона запишите баланс в кабинетах (Claude, DeepSeek, OpenAI), после — сравните. Падение должно совпасть с суммой, которую напечатал стенд (в долларах), с небольшой погрешностью. Если расходится заметно — скажите, я разберусь.

## 5. Если что-то пошло не так
- `✗ …: ключ не найден в окружении` — на этом сервере нет нужного ключа; запускайте тот провайдер там, где он лежит.
- `✗ …: не задана цена` — добавьте `--price МОДЕЛЬ=ВХОД/ВЫХОД`.
- `⏹ …: потолок расхода достигнут` — сработал защитный предел ($0,9 по Claude); это не ошибка.
- Повторный запуск безопасен, но тратит деньги заново.
