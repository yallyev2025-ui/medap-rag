/**
 * Тестовые семьи стиля этапа 1. Их три — чтобы доказать, что один и тот же текст собирается
 * в разном оформлении. Настоящие 8–12 семей рождаются в этапе 2 из ваших примеров.
 */
export const STYLES = {
    editorial: {
        id: "editorial",
        name: "Редакция",
        heading: { family: "Source Serif 4", weight: 700 },
        body: { family: "Manrope", weight: 400 },
        palette: {
            bg: "#F6F2EA",
            surface: "#ECE5D8",
            text: "#1B1B1B",
            muted: "#5E574D",
            accent: "#A63A25",
            accent2: "#1F4E5F",
            line: "#CFC6B5",
        },
        margin: 120,
        scale: 1.25,
        motifs: ["rule", "bar", "corner", "dot"],
    },
    clinic: {
        id: "clinic",
        name: "Клиника",
        heading: { family: "Manrope", weight: 800 },
        body: { family: "Manrope", weight: 400 },
        palette: {
            bg: "#0F1E2B",
            surface: "#17303F",
            text: "#F2F5F7",
            muted: "#A9BCCA",
            accent: "#4FD1B5",
            accent2: "#F2B84B",
            line: "#2B4A5E",
        },
        margin: 110,
        scale: 1.333,
        motifs: ["bar", "corner", "rule", "dot"],
    },
    calm: {
        id: "calm",
        name: "Спокойный",
        heading: { family: "Source Serif 4", weight: 600 },
        body: { family: "Manrope", weight: 400 },
        palette: {
            bg: "#FAFAF6",
            surface: "#EEF1EA",
            text: "#1E2A25",
            muted: "#56655E",
            accent: "#2F5D50",
            accent2: "#B87722",
            line: "#D5DBD0",
        },
        margin: 140,
        scale: 1.2,
        motifs: ["dot", "rule", "bar", "corner"],
    },
};
export function getStyle(id) {
    const style = STYLES[id];
    if (!style)
        throw new Error(`Неизвестная семья стиля «${id}»`);
    return style;
}
/* ------------------------------------------------------------------- контраст */
function channel(v) {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}
function luminance(hex) {
    const h = hex.replace("#", "");
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}
/** Контраст по WCAG: 1…21 */
export function contrast(a, b) {
    const la = luminance(a);
    const lb = luminance(b);
    const [hi, lo] = la > lb ? [la, lb] : [lb, la];
    return (hi + 0.05) / (lo + 0.05);
}
export function paletteColors(p) {
    return [p.accent, p.accent2, p.muted, p.line];
}
//# sourceMappingURL=styles.js.map