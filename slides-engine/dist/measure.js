import { fontFile, isCyrillic, loadFont } from "./fonts.js";
/**
 * Ширина текста по метрикам шрифта.
 *
 * Считается как сумма advance-ширин глифов (без кернинга пар): это даёт небольшой запас
 * (кернинг обычно сужает), а не недобор. Точность относительно Chromium проверяется тестом
 * в `render-bench` и записывается в README пределов.
 */
const advances = new Map();
function pickSubset(codePoint) {
    return isCyrillic(codePoint) ? "cyrillic" : "latin";
}
/** Ширина одного символа в единицах размера (em-доли) */
function emWidth(font, codePoint) {
    const key = `${font.family}|${font.weight}|${codePoint}`;
    const hit = advances.get(key);
    if (hit !== undefined)
        return hit;
    const preferred = pickSubset(codePoint);
    const order = preferred === "cyrillic" ? ["cyrillic", "latin"] : ["latin", "cyrillic"];
    let value = 0.5;
    for (const subset of order) {
        const file = loadFont(fontFile(font, subset));
        if (file.hasGlyphForCodePoint(codePoint)) {
            value = file.glyphForCodePoint(codePoint).advanceWidth / file.unitsPerEm;
            break;
        }
    }
    advances.set(key, value);
    return value;
}
export function textWidth(text, font, size) {
    let sum = 0;
    for (const ch of text)
        sum += emWidth(font, ch.codePointAt(0) ?? 0);
    return sum * size;
}
/**
 * Высота строки по правилам CSS: базовая линия лежит на «половине интерлиньяжа + ascent»
 * от верха строки. Нужна рисовальщикам без браузера, чтобы поставить текст туда же, где его
 * поставил бы Chromium.
 */
export function baselineOffset(font, size, lineHeightPx) {
    const file = loadFont(fontFile(font, "cyrillic"));
    const ascent = (file.ascent / file.unitsPerEm) * size;
    const descent = (-file.descent / file.unitsPerEm) * size;
    return (lineHeightPx - (ascent + descent)) / 2 + ascent;
}
//# sourceMappingURL=measure.js.map