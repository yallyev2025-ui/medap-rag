import { textWidth } from "./measure.js";
import { contrast } from "./styles.js";
import { SLIDE_H, SLIDE_W } from "./types.js";
/**
 * Проверка уровня 1 (код): всё, что можно проверить без модели и без картинки.
 * Ошибка = файл выдавать нельзя, предупреждение = показать в редакторе.
 */
const SAFE = 40;
/** Допуск на наложение рамок текста (пиксели): рамки строк чуть шире самих букв */
const OVERLAP_TOLERANCE = 6;
function isLarge(el) {
    return el.size >= 48 || (el.size >= 36 && el.weight >= 600);
}
function intersects(a, b) {
    return (a.x + OVERLAP_TOLERANCE < b.x + b.w &&
        b.x + OVERLAP_TOLERANCE < a.x + a.w &&
        a.y + OVERLAP_TOLERANCE < b.y + b.h &&
        b.y + OVERLAP_TOLERANCE < a.y + a.h);
}
export function checkDeck(placed) {
    const out = [];
    for (const ps of placed.slides) {
        const id = ps.slide.id;
        const texts = ps.elements.filter((e) => e.kind === "text");
        for (const el of texts) {
            if (el.overflow) {
                out.push({ slideId: id, rule: "overflow", severity: "error", message: `Текст «${el.text.slice(0, 40)}…» не влез даже на минимальном размере` });
            }
            const floor = el.tag === "title" ? 36 : el.tag === "decor" ? 0 : 20;
            if (el.size < floor) {
                out.push({ slideId: id, rule: "min-size", severity: "error", message: `Кегль ${el.size}px меньше допустимых ${floor}px (${el.tag})` });
            }
            if (el.tag !== "decor") {
                const ratio = contrast(el.color, el.onBg);
                const need = isLarge(el) ? 3 : 4.5;
                if (ratio < need) {
                    out.push({ slideId: id, rule: "contrast", severity: "error", message: `Контраст ${ratio.toFixed(2)} ниже ${need} (${el.tag})` });
                }
            }
        }
        for (const e of ps.elements) {
            const box = boxOf(e);
            if (!box)
                continue;
            const bleed = e.kind === "photo";
            if (!bleed && (box.x < SAFE - 1 || box.y < 0 || box.x + box.w > SLIDE_W - SAFE + 1 || box.y + box.h > SLIDE_H - SAFE + 1)) {
                if (e.kind === "text" || e.kind === "table" || e.kind === "chart") {
                    out.push({ slideId: id, rule: "bounds", severity: "error", message: `Элемент (${e.kind}) выходит за безопасные поля` });
                }
            }
        }
        for (const e of ps.elements) {
            if (e.kind !== "table")
                continue;
            [e.head, ...e.rows].forEach((row, r) => {
                row.forEach((cell, c) => {
                    const font = { family: e.family, weight: r === 0 ? 700 : 400 };
                    if (textWidth(cell, font, e.fontSize) > (e.colW[c] ?? 0) - 40) {
                        out.push({ slideId: id, rule: "table-cell", severity: "error", message: `Ячейка таблицы «${cell.slice(0, 30)}» не умещается в колонку` });
                    }
                });
            });
        }
        const real = texts.filter((t) => t.tag !== "decor");
        for (let i = 0; i < real.length; i++) {
            for (let j = i + 1; j < real.length; j++) {
                const a = real[i];
                const b = real[j];
                if (a && b && intersects(a.box, b.box)) {
                    out.push({ slideId: id, rule: "overlap", severity: "error", message: `Тексты «${a.tag}» и «${b.tag}» налезают друг на друга` });
                }
            }
        }
    }
    return out;
}
function boxOf(e) {
    if (e.kind === "line")
        return undefined;
    return e.box;
}
//# sourceMappingURL=checks.js.map