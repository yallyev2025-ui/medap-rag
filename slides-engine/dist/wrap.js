import { textWidth } from "./measure.js";
/**
 * Перенос строк с русской типографикой.
 *
 * Правила, которые отличают вёрстку от «как получилось»:
 * - предлог или союз из 1–2 букв не остаётся в конце строки (приклеивается к следующему слову);
 * - тире не начинает строку (приклеивается к предыдущему слову);
 * - числа с единицей («120 мм рт. ст.») не рвутся между числом и единицей.
 * Склейка делается неразрывным пробелом U+00A0 — его понимают и PowerPoint, и браузер.
 */
export const NBSP = " ";
const DASHES = new Set(["—", "–", "-"]);
export function glue(text) {
    const words = text.split(" ");
    const out = [];
    for (let i = 0; i < words.length; i++) {
        const word = words[i] ?? "";
        const prev = out.length > 0 ? out[out.length - 1] : undefined;
        if (prev !== undefined && DASHES.has(word)) {
            out[out.length - 1] = prev + NBSP + word;
            continue;
        }
        if (prev !== undefined && /^[\p{L}]{1,2}$/u.test(prev.split(NBSP).pop() ?? "") && !prev.endsWith(".")) {
            // предыдущее слово было коротким — оно уже ждёт это слово
            out[out.length - 1] = prev + NBSP + word;
            continue;
        }
        if (prev !== undefined && /^\d+([.,]\d+)?$/.test(prev.split(NBSP).pop() ?? "") && /^[\p{L}%°]/u.test(word)) {
            out[out.length - 1] = prev + NBSP + word;
            continue;
        }
        out.push(word);
    }
    return out.join(" ");
}
/** Жадный перенос по ширине. Явные переводы строки `\n` сохраняются */
export function wrap(text, font, size, maxWidth) {
    const lines = [];
    let maxLineWidth = 0;
    let brokeWord = false;
    for (const paragraph of text.split("\n")) {
        const words = paragraph.split(" ").filter((w) => w.length > 0);
        if (words.length === 0) {
            lines.push("");
            continue;
        }
        let line = "";
        for (const word of words) {
            const candidate = line === "" ? word : `${line} ${word}`;
            if (textWidth(candidate, font, size) <= maxWidth) {
                line = candidate;
                continue;
            }
            if (line !== "") {
                lines.push(line);
                maxLineWidth = Math.max(maxLineWidth, textWidth(line, font, size));
            }
            if (textWidth(word, font, size) <= maxWidth) {
                line = word;
                continue;
            }
            // слово шире строки: режем по символам и честно отмечаем
            brokeWord = true;
            let piece = "";
            for (const ch of word) {
                if (textWidth(piece + ch, font, size) > maxWidth && piece !== "") {
                    lines.push(piece);
                    maxLineWidth = Math.max(maxLineWidth, textWidth(piece, font, size));
                    piece = ch;
                }
                else {
                    piece += ch;
                }
            }
            line = piece;
        }
        lines.push(line);
        maxLineWidth = Math.max(maxLineWidth, textWidth(line, font, size));
    }
    return { lines, maxLineWidth, brokeWord };
}
//# sourceMappingURL=wrap.js.map