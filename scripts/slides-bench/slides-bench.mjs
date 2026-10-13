#!/usr/bin/env node
/**
 * Стенд замеров Medap Slide: сколько стоит и за сколько делается презентация у разных моделей.
 *
 * ОДИН САМОСТОЯТЕЛЬНЫЙ ФАЙЛ, без зависимостей (чистый Node 22+, `fetch`). Причина: ключи лежат на разных
 * серверах — Anthropic на сервере бота (medap-rag), DeepSeek и OpenAI на сервере сайта, — а значит стенд
 * запускается в два приёма, и каждый раз ему нужен только Node.
 *
 *   # на сервере сайта
 *   node slides-bench.mjs --providers deepseek,openai --out out-site.json
 *   # на сервере бота
 *   node slides-bench.mjs --providers anthropic --out out-bot.json
 *   # где угодно: слияние в таблицу и «безымянные» черновики для слепого сравнения
 *   node slides-bench.mjs merge out-site.json out-bot.json --out stend-results.md
 *
 * Полезные режимы: --list-models, --check, --dry-run. Подробности: `node slides-bench.mjs --help`.
 *
 * ЧТО МЕРЯЕТСЯ (по каждому «модель × тема × число слайдов»):
 *   черновик   — один вызов: план и тексты всех слайдов (JSON) → стоимость, секунды, валидность
 *   дизайн     — сокращение самых длинных слайдов (по вызову на слайд) + проверка спорных слайдов
 *                по картинке (для моделей со зрением; картинка sample-slide.png лежит рядом)
 *   дополнительно — одно «перепиши слайд короче» и текст доклада для всех слайдов
 *
 * Деньги считаются по токенам, которые вернул сам поставщик (включая «мысли» моделей), и по ценам из
 * `--price`/таблицы ниже. Ключи не печатаются и не сохраняются: скрипт показывает только «ключ есть / нет».
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

/* ------------------------------------------------------------------ настройки */

/** Цены, доллары за миллион токенов (вход/выход). Источник — справочник Anthropic 06.10.2026 и прайс из packages/llm */
const PRICES = {
  "claude-haiku-5-5": { input: 0.1, output: 0.5 }, // до 100K входных токенов; у нас запросы короче
  "claude-sonnet-5-5": { input: 2, output: 10 },
  "deepseek-v4-pro": { input: 0.435, output: 0.87 },
  "deepseek-flash": { input: 0.14, output: 0.28 },
};

const TOPICS = [
  { id: "med", kind: "lecture", title: "Хроническая сердечная недостаточность", about: "учебная лекция для студентов-медиков" },
  { id: "law", kind: "talk", title: "Договор подряда", about: "доклад на семинаре по гражданскому праву" },
  { id: "biz", kind: "business", title: "Запуск кофейни", about: "деловая презентация для партнёра" },
];

const SIZES = [5, 10, 15, 20];

const BLOCK_TYPES = ["statement", "text", "compare", "stat", "quote", "steps", "timeline", "table", "photo"];

/** Что запускаем по поставщикам. `topics` ограничивает темы (Sonnet — только две, чтобы уложиться в баланс) */
const PLAN = {
  anthropic: [
    { model: "claude-haiku-5-5", vision: true },
    { model: "claude-sonnet-5-5", vision: true, topics: ["med", "biz"] },
  ],
  deepseek: [
    { model: "deepseek-flash", vision: false },
    { model: "deepseek-v4-pro", vision: false },
  ],
  openai: [{ model: "AUTO_MINI", vision: true }],
};

const ENDPOINTS = {
  anthropic: { key: "ANTHROPIC_API_KEY", base: "ANTHROPIC_BASE_URL", def: "https://api.anthropic.com" },
  deepseek: { key: "DEEPSEEK_API_KEY", base: "DEEPSEEK_BASE_URL", def: "https://api.deepseek.com" },
  openai: { key: "OPENAI_API_KEY", base: "OPENAI_BASE_URL", def: "https://api.openai.com" },
};

/* -------------------------------------------------------------------- разбор команды */

function parseArgs(argv) {
  const args = { _: [], flags: {} };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        args.flags[key] = next;
        i++;
      } else {
        args.flags[key] = true;
      }
    } else {
      args._.push(a);
    }
  }
  return args;
}

const { _: positional, flags } = parseArgs(process.argv.slice(2));

if (flags.help || flags.h) {
  console.log(`Стенд замеров Medap Slide.

Режимы:
  --list-models         показать модели, доступные по ключам (без вызовов, платных запросов нет)
  --check               по одному дешёвому вызову на модель: проверка ключа и адреса
  --dry-run             оценка расхода без вызовов моделей
  (без режима)          боевой прогон
  merge a.json b.json   слияние результатов в таблицу (--out файл.md)

Параметры боевого прогона:
  --providers p1,p2     anthropic, deepseek, openai (по умолчанию все, у кого есть ключ)
  --out файл.json       куда сохранить результат (по умолчанию out-<время>.json)
  --sizes 5,10          только эти размеры (по умолчанию 5,10,15,20)
  --topics med,law,biz  только эти темы
  --openai-model id     точный id модели OpenAI «mini» (иначе стенд выберет сам из списка по ключу)
  --price модель=вход/выход[,...]   цена в долларах за миллион токенов (обязательно для OpenAI)
  --cap anthropic=0.9   потолок расхода по поставщику, доллары (по умолчанию anthropic=0.9)
  --budget-rub 300      общий потолок в рублях (нужен MEDAP_USD_RUB_RATE или --usd-rub)
  --usd-rub 95          курс доллара
  --effort low          уровень «размышления» у Claude (low|medium|high), по умолчанию low
Адреса (если ходите через посредника): ANTHROPIC_BASE_URL, OPENAI_BASE_URL, DEEPSEEK_BASE_URL.
Ключи: ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY (читаются из окружения, не печатаются).`);
  process.exit(0);
}

/* ----------------------------------------------------------------------- вспомогательное */

const env = (name) => (process.env[name] ?? "").trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const csv = (v) => (typeof v === "string" ? v.split(",").map((s) => s.trim()).filter(Boolean) : null);

function parsePrices(raw, into) {
  for (const pair of csv(raw) ?? []) {
    const [model, prices] = pair.split("=");
    const [i, o] = (prices ?? "").split("/").map(Number);
    if (model && Number.isFinite(i) && Number.isFinite(o)) into[model] = { input: i, output: o };
  }
}

const prices = { ...PRICES };
parsePrices(env("MEDAP_LLM_PRICING"), prices);
parsePrices(typeof flags.price === "string" ? flags.price : "", prices);

const costOf = (model, usage) => {
  const p = prices[model];
  if (!p) return null;
  return (usage.input * p.input + usage.output * p.output) / 1_000_000;
};

function endpoint(provider) {
  const e = ENDPOINTS[provider];
  return { key: env(e.key), base: (env(e.base) || e.def).replace(/\/+$/, "") };
}

async function http(url, init, timeoutMs = 240_000) {
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...init, signal: ctl.signal });
      const text = await res.text();
      clearTimeout(timer);
      if (res.status === 429 || res.status >= 500) {
        lastError = new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
        await sleep(1500 * attempt);
        continue;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${text.slice(0, 400)}`);
      return JSON.parse(text);
    } catch (e) {
      clearTimeout(timer);
      if (e && /HTTP 4\d\d/.test(String(e.message)) && !/HTTP 429/.test(String(e.message))) throw e;
      lastError = e;
      await sleep(1500 * attempt);
    }
  }
  throw lastError ?? new Error("запрос не удался");
}

/* -------------------------------------------------------------------- вызовы поставщиков */

/**
 * Единый вызов: { system, user, image?, maxTokens, json } → { text, usage:{input,output}, ms }.
 * Anthropic: «мысли» модели включены по умолчанию и входят в output_tokens, поэтому уровень размышления
 * задаётся флагом --effort (low по умолчанию): иначе замер стоил бы дороже реального пути.
 */
async function callModel(provider, model, req) {
  const { key, base } = endpoint(provider);
  const t0 = performance.now();

  if (provider === "anthropic") {
    const content = [];
    if (req.image) content.push({ type: "image", source: { type: "base64", media_type: "image/png", data: req.image } });
    content.push({ type: "text", text: req.user });
    const body = {
      model,
      max_tokens: req.maxTokens,
      system: req.system,
      messages: [{ role: "user", content }],
      output_config: { effort: typeof flags.effort === "string" ? flags.effort : "low" },
    };
    const res = await http(`${base}/v1/messages`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01" },
      body: JSON.stringify(body),
    });
    const text = (res.content ?? []).filter((b) => b.type === "text").map((b) => b.text).join("");
    const u = res.usage ?? {};
    return {
      text,
      usage: { input: (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0), output: u.output_tokens ?? 0 },
      stop: res.stop_reason,
      ms: performance.now() - t0,
    };
  }

  // OpenAI-совместимые: OpenAI и DeepSeek
  const userContent = req.image
    ? [{ type: "image_url", image_url: { url: `data:image/png;base64,${req.image}` } }, { type: "text", text: req.user }]
    : req.user;
  const body = {
    model,
    messages: [
      { role: "system", content: req.system },
      { role: "user", content: userContent },
    ],
    ...(req.json ? { response_format: { type: "json_object" } } : {}),
    ...(provider === "openai" ? { max_completion_tokens: req.maxTokens } : { max_tokens: req.maxTokens }),
  };
  const path = provider === "deepseek" ? "/chat/completions" : "/v1/chat/completions";
  const res = await http(`${base}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    body: JSON.stringify(body),
  });
  const choice = res.choices?.[0];
  const u = res.usage ?? {};
  return {
    text: choice?.message?.content ?? "",
    usage: { input: u.prompt_tokens ?? 0, output: u.completion_tokens ?? 0 },
    stop: choice?.finish_reason,
    ms: performance.now() - t0,
  };
}

async function listModels(provider) {
  const { key, base } = endpoint(provider);
  if (provider === "anthropic") {
    const res = await http(`${base}/v1/models?limit=100`, { headers: { "x-api-key": key, "anthropic-version": "2023-06-01" } }, 60_000);
    return (res.data ?? []).map((m) => m.id);
  }
  const path = provider === "deepseek" ? "/models" : "/v1/models";
  const res = await http(`${base}${path}`, { headers: { authorization: `Bearer ${key}` } }, 60_000);
  return (res.data ?? []).map((m) => m.id);
}

/* ------------------------------------------------------------------------- промпты */

const SYSTEM = `Ты автор презентаций. Пишешь только по-русски. Твой ответ — один JSON-объект, без пояснений и без markdown.

Правила:
- Заголовок слайда — утверждение с выводом (не «Введение», не «Что такое …»).
- Никаких эмодзи. Не выдумывай статистику, названия исследований и ссылки; если точного числа нет — пиши словами («около», «в большинстве случаев»).
- Текст коротко: слайд — это опора для выступающего, а не стена текста.
- Типы блоков и обязательные поля:
  title      — титул: "title", "text" (подзаголовок)
  statement  — главная мысль: "title" (одно-два предложения), "text" (пояснение)
  text       — список: "title", "items" (3–5 коротких пунктов)
  compare    — сравнение: "title", "columns": [{"title","items":[2–3 пункта]}] (2–3 колонки)
  stat       — одна цифра: "title", "stat": {"value","label"}, "text"
  quote      — мысль: "title", "quote": {"text","author"}
  steps      — этапы: "title", "steps": [{"title","text"}] (3–5)
  timeline   — хронология: "title", "steps": [{"title","text"}] (3–6)
  table      — таблица: "title", "table": {"head":[...], "rows":[[...]]} (2–4 колонки, 3–5 строк, без чисел-выдумок)
  photo      — место под фото: "title", "text", "photo": {"caption"}
  end        — финал: "title", "text"
- Первый слайд — title, последний — end. Остальные чередуй, не повторяя один тип больше двух раз подряд; не больше двух слайдов подряд из списков.`;

function draftPrompt(topic, n) {
  return `Тема: «${topic.title}» (${topic.about}).
Сделай презентацию ровно из ${n} слайдов. Допустимые "blockType": title, ${BLOCK_TYPES.join(", ")}, end.
Формат ответа: {"slides":[{"blockType":"...","title":"...", ...поля по типу блока...}]}`;
}

function shortenPrompt(slide) {
  return `Сократи текст слайда примерно на 40%, сохранив смысл и формулировку заголовка. Верни тот же JSON слайда, ничего больше.
${JSON.stringify(slide)}`;
}

const VISION_SYSTEM = "Ты проверяешь внешний вид слайда презентации. Отвечай одним JSON-объектом без пояснений.";
const VISION_PROMPT =
  'Посмотри на слайд. Ответь JSON: {"ok": true|false, "issues": ["что не так: налезает текст, мелкий шрифт, пустота, плохой контраст"]}. Не более 3 замечаний.';

function rewritePrompt(slide) {
  return `Перепиши текст слайда короче и яснее, оставив тот же тип блока. Верни JSON слайда, ничего больше.
${JSON.stringify(slide)}`;
}

function notesPrompt(topic, slides) {
  return `Тема «${topic.title}». К каждому слайду напиши текст доклада на 2–3 предложения. Верни {"notes":["...", ...]} — ровно ${slides.length} строк по порядку слайдов.
${JSON.stringify(slides.map((s) => ({ title: s.title, blockType: s.blockType })))}`;
}

/* ------------------------------------------------------------------------ валидация */

function parseJson(text) {
  const clean = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  try {
    return JSON.parse(clean);
  } catch {
    const a = clean.indexOf("{");
    const b = clean.lastIndexOf("}");
    if (a >= 0 && b > a) {
      try {
        return JSON.parse(clean.slice(a, b + 1));
      } catch {
        /* не получилось */
      }
    }
  }
  return null;
}

/** Проверка черновика: {jsonOk, countOk, typesOk, fieldsOk, rhythmOk, problems[]} */
function validateDraft(parsed, n) {
  const r = { jsonOk: !!parsed, countOk: false, typesOk: false, fieldsOk: false, rhythmOk: false, problems: [] };
  if (!parsed || !Array.isArray(parsed.slides)) return r;
  const slides = parsed.slides;
  r.countOk = slides.length === n;
  if (!r.countOk) r.problems.push(`слайдов ${slides.length}, нужно ${n}`);
  const allowed = new Set(["title", ...BLOCK_TYPES, "end"]);
  r.typesOk = slides.every((s) => allowed.has(s?.blockType)) && slides[0]?.blockType === "title" && slides.at(-1)?.blockType === "end";
  if (!r.typesOk) r.problems.push("неверные типы блоков или нет титула/финала");

  let fieldsOk = true;
  const need = (cond, msg) => {
    if (!cond) {
      fieldsOk = false;
      r.problems.push(msg);
    }
  };
  slides.forEach((s, i) => {
    const t = s?.blockType;
    const where = `слайд ${i + 1} (${t})`;
    need(typeof s?.title === "string" && s.title.trim().length > 0, `${where}: нет заголовка`);
    if (t === "text") need(Array.isArray(s.items) && s.items.length >= 2, `${where}: нет items`);
    if (t === "compare") need(Array.isArray(s.columns) && s.columns.length >= 2 && s.columns.every((c) => c?.title && Array.isArray(c.items) && c.items.length > 0), `${where}: колонки`);
    if (t === "stat") need(!!s.stat?.value && !!s.stat?.label, `${where}: stat`);
    if (t === "quote") need(!!s.quote?.text, `${where}: quote`);
    if (t === "steps" || t === "timeline") need(Array.isArray(s.steps) && s.steps.length >= 3 && s.steps.every((x) => x?.title), `${where}: steps`);
    if (t === "table") need(Array.isArray(s.table?.head) && s.table.head.length >= 2 && Array.isArray(s.table.rows) && s.table.rows.length >= 2 && s.table.rows.every((row) => Array.isArray(row) && row.length === s.table.head.length), `${where}: таблица`);
    if (t === "photo" || t === "statement") need(typeof s.text === "string" || t === "statement", `${where}: text`);
  });
  r.fieldsOk = fieldsOk;

  // ритм: один тип не более двух раз подряд
  let run = 1;
  let rhythmOk = true;
  for (let i = 1; i < slides.length; i++) {
    run = slides[i]?.blockType === slides[i - 1]?.blockType ? run + 1 : 1;
    if (run > 2) rhythmOk = false;
  }
  r.rhythmOk = rhythmOk;
  if (!rhythmOk) r.problems.push("один тип блока больше двух раз подряд");
  return r;
}

const slideChars = (s) => JSON.stringify(s).length;

/* --------------------------------------------------------------------- боевой прогон */

async function benchOne(provider, spec, topic, n, sample) {
  const { model } = spec;
  const out = { provider, model, topic: topic.id, slides: n, phases: {}, valid: null, error: null, draft: null, calls: [] };
  const record = (phase, res) => {
    const usd = costOf(model, res.usage);
    const p = (out.phases[phase] ??= { calls: 0, ms: 0, input: 0, output: 0, usd: 0 });
    p.calls++;
    p.ms += res.ms;
    p.input += res.usage.input;
    p.output += res.usage.output;
    p.usd += usd ?? 0;
    out.calls.push({ phase, ms: Math.round(res.ms), input: res.usage.input, output: res.usage.output, usd, stop: res.stop });
    return usd ?? 0;
  };

  let spent = 0;

  // 1) черновик
  const draft = await callModel(provider, model, { system: SYSTEM, user: draftPrompt(topic, n), maxTokens: 900 + n * 500, json: true });
  spent += record("draft", draft);
  const parsed = parseJson(draft.text);
  out.valid = validateDraft(parsed, n);
  out.draft = parsed;
  if (!parsed?.slides) {
    out.error = "ответ не разобрался как JSON";
    return { out, spent };
  }
  const slides = parsed.slides;

  // 2) дизайн: сокращение самых длинных слайдов
  const k = Math.max(1, Math.ceil(n * 0.25));
  const longest = [...slides].map((s, i) => ({ s, i })).filter(({ s }) => !["title", "end"].includes(s?.blockType)).sort((a, b) => slideChars(b.s) - slideChars(a.s)).slice(0, k);
  for (const { s } of longest) {
    const res = await callModel(provider, model, { system: SYSTEM, user: shortenPrompt(s), maxTokens: 700, json: true });
    spent += record("design", res);
  }
  // проверка спорных слайдов по картинке (≈30% слайдов)
  if (spec.vision && sample) {
    const m = Math.max(1, Math.ceil(n * 0.3));
    for (let i = 0; i < m; i++) {
      const res = await callModel(provider, model, { system: VISION_SYSTEM, user: VISION_PROMPT, image: sample, maxTokens: 400, json: true });
      spent += record("vision", res);
    }
  }

  // 3) дополнительно: переписать слайд и текст доклада
  const target = slides.find((s) => !["title", "end"].includes(s?.blockType)) ?? slides[0];
  spent += record("rewrite", await callModel(provider, model, { system: SYSTEM, user: rewritePrompt(target), maxTokens: 700, json: true }));
  spent += record("notes", await callModel(provider, model, { system: "Ты пишешь текст доклада. Только по-русски, один JSON-объект.", user: notesPrompt(topic, slides), maxTokens: 300 + n * 220, json: true }));

  return { out, spent };
}

async function resolveOpenAiModel() {
  if (typeof flags["openai-model"] === "string") return flags["openai-model"];
  const ids = await listModels("openai");
  const minis = ids.filter((id) => /mini/i.test(id) && !/audio|realtime|transcribe|tts|search|image/i.test(id)).sort();
  return minis.at(-1) ?? null;
}

async function runBench() {
  const wanted = csv(flags.providers) ?? Object.keys(PLAN).filter((p) => endpoint(p).key);
  const sizes = (csv(flags.sizes) ?? SIZES.map(String)).map(Number);
  const topics = TOPICS.filter((t) => !csv(flags.topics) || csv(flags.topics).includes(t.id));
  const usdRub = Number(flags["usd-rub"] ?? env("MEDAP_USD_RUB_RATE")) || null;
  const budgetUsd = flags["budget-rub"] && usdRub ? Number(flags["budget-rub"]) / usdRub : Infinity;
  const caps = { anthropic: 0.9 };
  for (const pair of csv(typeof flags.cap === "string" ? flags.cap : "") ?? []) {
    const [p, v] = pair.split("=");
    if (p && Number(v) > 0) caps[p] = Number(v);
  }
  const sample = existsSync(join(HERE, "sample-slide.png")) ? readFileSync(join(HERE, "sample-slide.png")).toString("base64") : null;

  // очередь запусков: дешёвое раньше дорогого, малые размеры раньше больших
  const jobs = [];
  for (const provider of wanted) {
    if (!endpoint(provider).key) {
      console.log(`✗ ${provider}: ключ не найден в окружении (${ENDPOINTS[provider].key}) — пропускаю`);
      continue;
    }
    for (const spec0 of PLAN[provider] ?? []) {
      let model = spec0.model;
      if (model === "AUTO_MINI") {
        model = await resolveOpenAiModel();
        if (!model) {
          console.log("✗ openai: не нашёл модель «mini» в списке; укажите --openai-model");
          continue;
        }
        console.log(`• openai: беру модель «${model}»`);
      }
      if (!prices[model]) {
        console.log(`✗ ${provider}/${model}: не задана цена. Добавьте --price ${model}=вход/выход (доллары за миллион токенов) — без неё не работает потолок расхода`);
        continue;
      }
      for (const topic of topics) {
        if (spec0.topics && !spec0.topics.includes(topic.id)) continue;
        for (const n of sizes) jobs.push({ provider, spec: { ...spec0, model }, topic, n });
      }
    }
  }
  jobs.sort((a, b) => (prices[a.spec.model].output - prices[b.spec.model].output) || (a.n - b.n));

  if (flags["dry-run"]) return dryRun(jobs, usdRub);

  const result = { meta: { startedAt: new Date().toISOString(), usdRub, prices: Object.fromEntries([...new Set(jobs.map((j) => j.spec.model))].map((m) => [m, prices[m]])) }, runs: [] };
  const spentBy = {};
  let total = 0;
  const outFile = typeof flags.out === "string" ? flags.out : `out-${Date.now()}.json`;

  for (const job of jobs) {
    const cap = caps[job.provider] ?? Infinity;
    const label = `${job.spec.model} · ${job.topic.id} · ${job.n} сл.`;
    if ((spentBy[job.provider] ?? 0) >= cap || total >= budgetUsd) {
      console.log(`⏹ ${label}: потолок расхода достигнут — пропущено`);
      result.runs.push({ provider: job.provider, model: job.spec.model, topic: job.topic.id, slides: job.n, skipped: "потолок расхода" });
      continue;
    }
    try {
      const { out, spent } = await benchOne(job.provider, job.spec, job.topic, job.n, sample);
      spentBy[job.provider] = (spentBy[job.provider] ?? 0) + spent;
      total += spent;
      result.runs.push(out);
      const ph = out.phases;
      const rub = (u) => (usdRub ? ` (${(u * usdRub).toFixed(1)} ₽)` : "");
      console.log(`✓ ${label}: $${spent.toFixed(4)}${rub(spent)}, черновик ${(ph.draft.ms / 1000).toFixed(1)} с, валидно: ${out.valid.jsonOk && out.valid.countOk && out.valid.typesOk && out.valid.fieldsOk ? "да" : "НЕТ " + out.valid.problems.slice(0, 2).join("; ")}`);
    } catch (e) {
      console.log(`✗ ${label}: ${String(e.message).slice(0, 160)}`);
      result.runs.push({ provider: job.provider, model: job.spec.model, topic: job.topic.id, slides: job.n, error: String(e.message).slice(0, 300) });
    }
    writeFileSync(outFile, JSON.stringify(result, null, 2));
  }
  result.meta.spentUsd = spentBy;
  result.meta.totalUsd = total;
  writeFileSync(outFile, JSON.stringify(result, null, 2));
  console.log(`\nГотово. Потрачено $${total.toFixed(4)}${usdRub ? ` ≈ ${(total * usdRub).toFixed(1)} ₽` : ""}. Файл: ${outFile}`);
}

function dryRun(jobs, usdRub) {
  // оценка по прикидке токенов: вход ≈ 700 на вызов, выход ≈ 330 токенов на слайд черновика
  let total = 0;
  const lines = [];
  for (const j of jobs) {
    const p = prices[j.spec.model];
    const n = j.n;
    const k = Math.max(1, Math.ceil(n * 0.25));
    const m = j.spec.vision ? Math.max(1, Math.ceil(n * 0.3)) : 0;
    const input = 700 + n * 120 + k * (500 + 150) + m * 2200 + 500 + (400 + n * 40);
    const output = n * 330 + k * 150 + m * 60 + 200 + n * 120;
    const usd = (input * p.input + output * p.output) / 1_000_000;
    total += usd;
    lines.push(`  ${j.spec.model.padEnd(18)} ${j.topic.id} ${String(n).padStart(2)} сл.  ≈ $${usd.toFixed(4)}`);
  }
  console.log(lines.join("\n"));
  console.log(`\nИТОГО (грубая оценка): $${total.toFixed(3)}${usdRub ? ` ≈ ${(total * usdRub).toFixed(0)} ₽` : ""}. Модели, думающие дольше, выйдут дороже; реальный расход покажет прогон.`);
}

/* ---------------------------------------------------------------- режимы list/check */

async function runList() {
  for (const provider of csv(flags.providers) ?? Object.keys(ENDPOINTS)) {
    if (!endpoint(provider).key) {
      console.log(`${provider}: ключа нет`);
      continue;
    }
    try {
      const ids = await listModels(provider);
      console.log(`${provider}: ключ есть, моделей ${ids.length}\n  ${ids.slice(0, 80).join("\n  ")}`);
    } catch (e) {
      console.log(`${provider}: ключ есть, но список не получен — ${String(e.message).slice(0, 160)}`);
    }
  }
}

async function runCheck() {
  for (const provider of csv(flags.providers) ?? Object.keys(PLAN)) {
    if (!endpoint(provider).key) {
      console.log(`${provider}: ключа нет`);
      continue;
    }
    for (const spec of PLAN[provider]) {
      let model = spec.model;
      if (model === "AUTO_MINI") model = await resolveOpenAiModel();
      if (!model) {
        console.log(`${provider}: модель «mini» не найдена`);
        continue;
      }
      try {
        const res = await callModel(provider, model, { system: "Отвечай по-русски, одним словом.", user: "Ответь одним словом: готово", maxTokens: 300, json: false });
        const usd = costOf(model, res.usage);
        console.log(`✓ ${provider}/${model}: ответ получен за ${(res.ms / 1000).toFixed(1)} с, токены ${res.usage.input}/${res.usage.output}${usd === null ? ", цена не задана" : `, $${usd.toFixed(6)}`}`);
      } catch (e) {
        console.log(`✗ ${provider}/${model}: ${String(e.message).slice(0, 200)}`);
      }
    }
  }
}

/* ---------------------------------------------------------------------- слияние */

const CEILING_RUB = { 5: 15, 10: 15, 15: 25, 20: 25 };

function runMerge() {
  const files = positional.slice(1);
  if (files.length === 0) {
    console.log("Укажите файлы результатов: merge out-site.json out-bot.json --out stend-results.md");
    process.exit(1);
  }
  const all = files.map((f) => JSON.parse(readFileSync(f, "utf8")));
  const usdRub = Number(flags["usd-rub"] ?? env("MEDAP_USD_RUB_RATE")) || all.map((a) => a.meta?.usdRub).find(Boolean) || null;
  const runs = all.flatMap((a) => (a.runs ?? []).map((r) => ({ ...r, _prices: a.meta?.prices ?? {} })));

  const sum = (r) => Object.values(r.phases ?? {}).reduce((a, p) => a + p.usd, 0);
  const phaseUsd = (r, names) => names.reduce((a, n) => a + (r.phases?.[n]?.usd ?? 0), 0);
  const rub = (usd) => (usdRub ? usd * usdRub : null);
  const f1 = (v) => (v === null || v === undefined ? "—" : v.toFixed(1));

  const done = runs.filter((r) => r.phases && r.valid);
  const models = [...new Set(done.map((r) => r.model))];
  const lines = [];
  lines.push(`# Результаты стенда Medap Slide`, "", `Слито файлов: ${files.length}. Курс: ${usdRub ? `${usdRub} ₽/$` : "не задан (суммы в долларах)"}. Темы: ${[...new Set(done.map((r) => r.topic))].join(", ")}.`, "");
  lines.push("Потолки: ≤ 15 ₽ для 5 и 10 слайдов, ≤ 25 ₽ для 15 и 20. Правило прибыли: цена в 2–2,5 раза выше затрат. «Затраты» здесь — только модели; комиссия кассы, налог, рендер и хранение добавляются в финмодели.", "");
  lines.push("## Стоимость, ₽ (среднее по темам; потолок — в скобках)", "");
  lines.push("| Модель | Слайдов | Черновик | Дизайн (сокращение + зрение) | Доп. (переписать + доклад) | **Всего** | Потолок | Черновик, с | Валидных, % |");
  lines.push("|---|---|---|---|---|---|---|---|---|");
  for (const model of models) {
    for (const n of SIZES) {
      const rs = done.filter((r) => r.model === model && r.slides === n);
      if (rs.length === 0) continue;
      const avg = (fn) => rs.reduce((a, r) => a + fn(r), 0) / rs.length;
      const total = avg(sum);
      const ceil = CEILING_RUB[n];
      const totalRub = rub(total);
      const flag = totalRub === null ? "" : totalRub <= ceil ? " ✅" : " ❌";
      const validPct = (rs.filter((r) => r.valid.jsonOk && r.valid.countOk && r.valid.typesOk && r.valid.fieldsOk).length / rs.length) * 100;
      lines.push(
        `| ${model} | ${n} | ${f1(rub(avg((r) => phaseUsd(r, ["draft"]))))} | ${f1(rub(avg((r) => phaseUsd(r, ["design", "vision"]))))} | ${f1(rub(avg((r) => phaseUsd(r, ["rewrite", "notes"]))))} | **${f1(totalRub)}**${flag} | ${ceil} | ${f1(avg((r) => (r.phases.draft?.ms ?? 0) / 1000))} | ${validPct.toFixed(0)} |`,
      );
    }
  }
  lines.push("", "## Что ориентировочно брать за презентацию (2–2,5 × затраты на модели)", "");
  lines.push("| Модель | Слайдов | Затраты, ₽ | Цена при ×2 | Цена при ×2,5 |", "|---|---|---|---|---|");
  for (const model of models) {
    for (const n of SIZES) {
      const rs = done.filter((r) => r.model === model && r.slides === n);
      if (rs.length === 0) continue;
      const total = rub(rs.reduce((a, r) => a + sum(r), 0) / rs.length);
      lines.push(`| ${model} | ${n} | ${f1(total)} | ${total === null ? "—" : f1(total * 2)} | ${total === null ? "—" : f1(total * 2.5)} |`);
    }
  }
  lines.push("", "## Проблемы и пропуски", "");
  for (const r of runs.filter((x) => x.error || x.skipped || (x.valid && x.valid.problems?.length))) {
    lines.push(`- ${r.model} · ${r.topic} · ${r.slides} сл.: ${r.error ?? r.skipped ?? r.valid.problems.slice(0, 3).join("; ")}`);
  }

  const outFile = typeof flags.out === "string" ? flags.out : "stend-results.md";
  writeFileSync(outFile, lines.join("\n") + "\n");

  // безымянные черновики для слепого сравнения: A, B, C … + ключ
  const dir = outFile.replace(/\.md$/, "") + "-blind";
  mkdirSync(dir, { recursive: true });
  const key = {};
  let idx = 0;
  const label = () => String.fromCharCode(65 + (idx++ % 26)) + (idx > 26 ? Math.floor((idx - 1) / 26) : "");
  const shuffled = done.filter((r) => r.draft?.slides && r.slides === 10).sort(() => Math.random() - 0.5);
  for (const r of shuffled) {
    const id = label();
    key[id] = { model: r.model, topic: r.topic, slides: r.slides };
    writeFileSync(join(dir, `${id}.json`), JSON.stringify({ topic: r.topic, draft: r.draft }, null, 2));
  }
  writeFileSync(join(dir, "KEY.json"), JSON.stringify(key, null, 2));

  // один компактный текстовый файл со всем нужным (таблица + черновики), чтобы его можно было просто переслать
  const pack = { v: 1, usdRub, key, drafts: Object.fromEntries(shuffled.map((r, i) => [Object.keys(key)[i], { topic: r.topic, draft: r.draft }])), runs: done.map((r) => ({ provider: r.provider, model: r.model, topic: r.topic, slides: r.slides, phases: r.phases, valid: r.valid })) };
  const packFile = outFile.replace(/\.md$/, "") + "-pack.txt";
  writeFileSync(packFile, gzipSync(JSON.stringify(pack)).toString("base64"));
  console.log(`Таблица: ${outFile}\nЧерновики для слепого сравнения (10 слайдов): ${dir}/ (ключ — KEY.json, не открывайте до выбора)\nФайл для передачи мне: ${packFile} (откройте и скопируйте весь текст целиком)`);
}

/* ---------------------------------------------------------------------------- запуск */

const mode = positional[0];
try {
  if (mode === "merge") runMerge();
  else if (flags["list-models"]) await runList();
  else if (flags.check) await runCheck();
  else await runBench();
} catch (e) {
  console.error("Ошибка стенда:", e?.message ?? e);
  process.exit(1);
}
