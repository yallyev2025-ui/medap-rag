import { wrap } from "./wrap.js";
/** Запас по ширине: PowerPoint и PDF считают чуть иначе, 4% не дают тексту вылезти в одном из них */
export const WIDTH_SAFETY = 0.96;
/**
 * Подбор кегля: самый большой размер от `maxSize` вниз до `minSize`, при котором текст
 * укладывается в ширину и высоту. Не влезло даже на минимальном — возвращает `fits: false`
 * на минимальном размере: решение (сократить, разбить слайд, сменить макет) принимает вызывающий.
 */
export function fitText(input) {
    const step = input.step ?? 2;
    const usable = input.maxWidth * WIDTH_SAFETY;
    let last;
    for (let size = input.maxSize; size >= input.minSize; size -= step) {
        const wrapped = wrap(input.text, input.font, size, usable);
        const height = wrapped.lines.length * size * input.lineHeight;
        last = { size, lines: wrapped.lines, height, fits: height <= input.maxHeight && !wrapped.brokeWord };
        if (last.fits)
            return last;
    }
    return last ?? { size: input.minSize, lines: [input.text], height: input.minSize * input.lineHeight, fits: false };
}
//# sourceMappingURL=fit.js.map