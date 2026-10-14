#!/usr/bin/env node
/**
 * Собирает PPTX и PDF из черновиков, которые написали модели на стенде.
 *
 *   node render-drafts.mjs <drafts.json> <папка-вывода> [стиль]
 *
 * drafts.json — массив [{ "name": "med-luna", "topic": "med", "draft": { "slides": [...] } }, ...].
 * В папке вывода появляются <name>.pptx и <name>.pdf и report.json (что не разобралось, что не влезло).
 * Один и тот же стиль для всех файлов, чтобы сравнивать именно тексты моделей.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { buildPptx, checkDeck, draftToDeck, lintDeck, placeDeck, renderPdf } from "./dist/index.js";

const [draftsPath, outDir, style = "editorial"] = process.argv.slice(2);
if (!draftsPath || !outDir) {
  console.error("Использование: node render-drafts.mjs <drafts.json> <папка-вывода> [стиль]");
  process.exit(2);
}

const items = JSON.parse(readFileSync(draftsPath, "utf8"));
mkdirSync(outDir, { recursive: true });
const report = {};
let failed = 0;

for (const it of items) {
  try {
    const { deck, problems } = draftToDeck(it.draft, { styleId: style, signature: 1 });
    const placed = placeDeck(deck);
    const errors = checkDeck(placed).map((v) => `${v.slideId}: ${v.message}`);
    const warnings = lintDeck(deck, placed).map((v) => `${v.slideId}: ${v.message}`);
    writeFileSync(join(outDir, `${it.name}.pptx`), await buildPptx(placed));
    writeFileSync(join(outDir, `${it.name}.pdf`), await renderPdf(placed));
    report[it.name] = { slides: deck.slides.length, parse: problems, errors, warnings };
    console.log(`✓ ${it.name}: слайдов ${deck.slides.length}, не разобралось ${problems.length}, ошибок вёрстки ${errors.length}`);
  } catch (e) {
    failed++;
    report[it.name] = { error: String(e?.message ?? e) };
    console.log(`✗ ${it.name}: ${String(e?.message ?? e).slice(0, 200)}`);
  }
}

writeFileSync(join(outDir, "report.json"), JSON.stringify(report, null, 2));
process.exit(failed === items.length && items.length > 0 ? 1 : 0);
