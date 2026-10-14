import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as fontkit from "fontkit";
const FAMILIES = {
    Manrope: { prefix: "manrope", weights: [400, 600, 700, 800] },
    "Source Serif 4": { prefix: "source-serif-4", weights: [400, 600, 700] },
    Caveat: { prefix: "caveat", weights: [400, 700] },
};
export const KNOWN_FAMILIES = Object.keys(FAMILIES);
/** Диапазоны юникода, как в стилях Google Fonts */
export const UNICODE_RANGE = {
    cyrillic: "U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116",
    latin: "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD",
};
export function isCyrillic(codePoint) {
    return (codePoint === 0x301 ||
        (codePoint >= 0x400 && codePoint <= 0x45f) ||
        codePoint === 0x490 ||
        codePoint === 0x491 ||
        codePoint === 0x4b0 ||
        codePoint === 0x4b1 ||
        codePoint === 0x2116);
}
const FONT_DIR = fileURLToPath(new URL("../fonts/", import.meta.url));
/** Ближайшая доступная жирность семейства: 500 → 400, 900 → 800 */
export function nearestWeight(family, weight) {
    const entry = FAMILIES[family];
    if (!entry)
        throw new Error(`Неизвестный шрифт «${family}». Известные: ${KNOWN_FAMILIES.join(", ")}`);
    return entry.weights.reduce((best, w) => Math.abs(w - weight) < Math.abs(best - weight) ? w : best);
}
export function fontFile(font, subset) {
    const entry = FAMILIES[font.family];
    if (!entry)
        throw new Error(`Неизвестный шрифт «${font.family}»`);
    return `${FONT_DIR}${entry.prefix}-${subset}-${nearestWeight(font.family, font.weight)}-normal.woff2`;
}
const cache = new Map();
export function loadFont(path) {
    let font = cache.get(path);
    if (!font) {
        font = fontkit.create(readFileSync(path));
        cache.set(path, font);
    }
    return font;
}
/** Файл woff2 в base64 — для встраивания в HTML, чтобы страница не зависела от путей */
export function fontBase64(path) {
    return readFileSync(path).toString("base64");
}
//# sourceMappingURL=fonts.js.map