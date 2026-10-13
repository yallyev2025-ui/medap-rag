# Стенд замеров Medap Slide (копия из репозитория сайта)

Это тот же самостоятельный файл, что лежит в `scripts/slides-bench/` репозитория сайта. Он здесь,
потому что **ключ Anthropic лежит на сервере бота**, а стенду нужен только Node (версии 18 и новее) и ключ в окружении.

```bash
# проверить, что ключ и связь работают (копейки)
node scripts/slides-bench/slides-bench.mjs --check --providers anthropic
# оценка расхода без обращения к моделям
node scripts/slides-bench/slides-bench.mjs --dry-run --providers anthropic
# боевой прогон: потолок расхода по умолчанию $0.9
node scripts/slides-bench/slides-bench.mjs --providers anthropic --out out-bot.json
```

Если на сервере бота нет Node: `apt-get update && apt-get install -y nodejs` (годится и 18-я версия).
Подробности и справка: `node scripts/slides-bench/slides-bench.mjs --help`.
