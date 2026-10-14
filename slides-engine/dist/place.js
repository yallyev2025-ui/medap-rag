import { LAYOUTS, mix } from "./compose.js";
import { getStyle } from "./styles.js";
/**
 * Раскладка колоды: для каждого слайда — выбор макета по роли и расчёт прямоугольников.
 * Дальше эти прямоугольники без изменений уходят в PPTX и в HTML/PDF.
 */
export function placeDeck(deck) {
    const style = getStyle(deck.styleId);
    const p = style.palette;
    const slides = deck.slides.map((slide) => {
        const layout = LAYOUTS[slide.role];
        // инверсные слайды (тезис, цитата, финал) задают ритм: фон и текст меняются местами
        const inverse = layout.inverse;
        const ctx = {
            style,
            signature: deck.signature,
            slide,
            els: [],
            bg: inverse ? p.text : p.bg,
            fg: inverse ? p.bg : p.text,
            muted: inverse ? mix(p.bg, p.text, 0.28) : p.muted,
            surface: inverse ? mix(p.text, p.bg, 0.12) : p.surface,
        };
        layout.compose(ctx);
        return { slide, layoutId: layout.id, bg: ctx.bg, elements: ctx.els };
    });
    return { deck, style, slides };
}
//# sourceMappingURL=place.js.map