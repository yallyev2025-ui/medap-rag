import { LAYOUTS } from "./compose.js";
/**
 * Правила «против ИИ-вида». Проверяют описание колоды и раскладку.
 * Пороги — стартовые, в этапе 2 их подбираем по вашим анти-примерам.
 */
const EMOJI = /\p{Extended_Pictographic}/u;
const BREATHING = new Set(["statement", "stat", "quote", "photo", "chart", "end", "title"]);
function textOf(slide) {
    const c = slide.content;
    return [c.title, c.text, ...(c.items ?? []), c.quote?.text, ...(c.steps ?? []).flatMap((s) => [s.title, s.text])]
        .filter(Boolean)
        .join(" ");
}
export function lintDeck(deck, placed) {
    const out = [];
    const add = (slide, rule, message, severity = "warn") => {
        out.push({ slideId: slide.id, rule, message, severity });
    };
    let heavyRun = 0;
    let sameRun = 0;
    let calmRun = 0;
    deck.slides.forEach((slide, index) => {
        const layout = LAYOUTS[slide.role];
        heavyRun = layout.textHeavy ? heavyRun + 1 : 0;
        if (heavyRun > 2)
            add(slide, "text-run", "Больше двух слайдов подряд из сплошного текста — разбавьте схемой, цифрой или цитатой");
        const prev = deck.slides[index - 1];
        sameRun = prev && LAYOUTS[prev.role].id === layout.id ? sameRun + 1 : 1;
        if (sameRun > 2)
            add(slide, "same-layout", `Макет «${layout.id}» больше двух раз подряд`);
        calmRun = BREATHING.has(slide.role) ? 0 : calmRun + 1;
        if (calmRun > 3)
            add(slide, "rhythm", "Больше трёх однотипных слайдов подряд без «воздуха» (цифра, цитата, фото, диаграмма)");
        if ((slide.content.items?.length ?? 0) > 6)
            add(slide, "list-length", "В списке больше шести пунктов — это стена текста");
        if (EMOJI.test(textOf(slide)))
            add(slide, "emoji", "Эмодзи в тексте слайда — признак шаблонного ИИ-вида");
        const title = slide.content.title.trim();
        const words = title.split(/\s+/).length;
        const plain = ["title", "end", "refs", "quote", "statement", "stat"];
        if (!plain.includes(slide.role)) {
            if (words < 3)
                add(slide, "label-title", `Заголовок «${title}» — ярлык, а не утверждение`);
            if (/^что такое\b/i.test(title))
                add(slide, "label-title", `Заголовок «${title}» — вопрос-ярлык, сформулируйте вывод`);
        }
    });
    if (placed) {
        const texts = placed.slides.flatMap((s) => s.elements.filter((e) => e.kind === "text" && e.tag !== "decor"));
        const centered = texts.filter((t) => t.align === "center").length;
        if (texts.length > 0 && centered / texts.length > 0.5) {
            out.push({ slideId: "*", rule: "centered", severity: "warn", message: "Больше половины текста выровнено по центру" });
        }
    }
    return out;
}
//# sourceMappingURL=lint.js.map