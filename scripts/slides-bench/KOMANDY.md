# Команды этапа 1: стенд замеров стоимости

Файл лежит в репозитории, чтобы команды не терялись. Копия — в репозитории бота: `scripts/slides-bench/KOMANDY.md`.

**Что делает стенд.** Делает настоящие запросы к моделям (черновик на 5/10/15/20 слайдов, «сократить слайд», «перепиши», текст доклада), берёт из ответов токены, которые сам поставщик выставляет в счёт, и умножает на цену. На выходе — таблица «модель × размер → рубли» с отметкой, уложились ли в потолок (15 ₽ для 5–10 слайдов, 25 ₽ для 15–20).

**Где какие ключи.** Anthropic (Claude) — на сервере бота. OpenAI — на сервере сайта (DeepSeek по решению владельца не сравниваем). Поэтому два запуска; в каждом команды идут в таком порядке: проверка → оценка → прогон → таблица.

**Чтобы консоль не потерялась.** В каждой команде есть `--log файл.log`: весь вывод дублируется в файл. Файлы лежат в `/tmp` на сервере; результаты прогона (`out-*.json`) — тоже там.

---

## 1. Сервер САЙТА (OpenAI)

Скрипт лежит в образе сайта: `/app/scripts/slides-bench/slides-bench.mjs` (после выкладки этого этапа). Node там есть.

Модели OpenAI для сравнения: **GPT-6 Luna** (самая дешёвая) и **GPT-6.1 Sol** (посильнее). Точные id и цены я не знаю (по сторонним сайтам Luna $0,10/$0,50, Sol $2/$10 за миллион токенов — **проверьте на platform.openai.com/pricing**). Ниже `LUNA` и `SOL` — это id из списка, `ВХОД/ВЫХОД` — цены в долларах за миллион токенов.

```bash
cd /app

# 1) Какие модели видны по ключу OpenAI. В конце стенд напишет, какие «похожи на нужные» (luna, sol, nano, mini)
node scripts/slides-bench/slides-bench.mjs --list-models --providers openai

# 2) Проверка ключей и связи (копейки)
node scripts/slides-bench/slides-bench.mjs --check --providers openai --openai-models LUNA,SOL --price LUNA=ВХОД/ВЫХОД,SOL=ВХОД/ВЫХОД --usd-rub 95 --log /tmp/stend-site-check.log

# 3) Оценка расхода БЕЗ запросов к моделям
node scripts/slides-bench/slides-bench.mjs --dry-run --providers openai --openai-models LUNA,SOL --price LUNA=ВХОД/ВЫХОД,SOL=ВХОД/ВЫХОД --usd-rub 95 --log /tmp/stend-site-dry.log

# 4) Боевой прогон (минуты)
node scripts/slides-bench/slides-bench.mjs --providers openai --openai-models LUNA,SOL --price LUNA=ВХОД/ВЫХОД,SOL=ВХОД/ВЫХОД --usd-rub 95 --out /tmp/out-site.json --log /tmp/stend-site-run.log

# 5) Таблица и файл-пакет для передачи
node scripts/slides-bench/slides-bench.mjs merge /tmp/out-site.json --usd-rub 95 --out /tmp/stend-site.md --log /tmp/stend-site-merge.log
cat /tmp/stend-site.md
```

Если `--openai-models` не указать, стенд сам возьмёт по списку ключа модели с «luna» и «sol» в названии (а если их нет — «nano» и «mini»). Цену всё равно нужно указать, иначе модель будет пропущена.
Если на сервере задан `MEDAP_USD_RUB_RATE`, `--usd-rub 95` можно не писать (подставьте свой курс вместо 95).

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
node scripts/slides-bench/slides-bench.mjs --check --providers anthropic --usd-rub 95 --log /tmp/stend-bot-check.log

# 2) Оценка без запросов
node scripts/slides-bench/slides-bench.mjs --dry-run --providers anthropic --usd-rub 95 --log /tmp/stend-bot-dry.log

# 3) Боевой прогон. Потолок по Claude по умолчанию $0.9 (ваш баланс $1): дойдёт и остановится сам
node scripts/slides-bench/slides-bench.mjs --providers anthropic --usd-rub 95 --out /tmp/out-bot.json --log /tmp/stend-bot-run.log

# 4) Таблица и файл-пакет
node scripts/slides-bench/slides-bench.mjs merge /tmp/out-bot.json --usd-rub 95 --out /tmp/stend-bot.md --log /tmp/stend-bot-merge.log
cat /tmp/stend-bot.md
```

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
