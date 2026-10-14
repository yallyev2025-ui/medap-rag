import { LAYOUTS_VERSION, SCHEMA_VERSION } from "./types.js";
const ROLE = {
    title: "title",
    statement: "statement",
    text: "list",
    compare: "compare",
    stat: "stat",
    quote: "quote",
    steps: "steps",
    timeline: "timeline",
    table: "table",
    photo: "photo",
    end: "end",
};
export function draftToDeck(raw, opts) {
    const problems = [];
    const slides = [];
    (raw.slides ?? []).forEach((r, i) => {
        const id = `s${String(i + 1).padStart(2, "0")}`;
        const role = ROLE[r.blockType ?? ""];
        if (!role) {
            problems.push(`слайд ${i + 1}: неизвестный тип «${r.blockType}»`);
            return;
        }
        const title = (r.title ?? "").trim();
        if (!title) {
            problems.push(`слайд ${i + 1}: нет заголовка`);
            return;
        }
        const content = { title };
        if (r.text)
            content.text = r.text;
        if (r.items)
            content.items = r.items;
        if (r.stat?.value)
            content.stat = { value: r.stat.value, label: r.stat.label ?? "" };
        if (r.quote?.text)
            content.quote = { text: r.quote.text, author: r.quote.author };
        if (r.steps)
            content.steps = r.steps.filter((s) => s.title).map((s) => ({ title: s.title ?? "", text: s.text }));
        if (r.columns)
            content.columns = r.columns.map((c) => ({ title: c.title ?? "", items: c.items ?? [] }));
        if (r.table?.head && r.table.rows)
            content.table = { head: r.table.head, rows: r.table.rows };
        if (role === "photo")
            content.photo = { caption: r.photo?.caption ?? "Здесь будет ваше фото" };
        slides.push({ id, role, content });
    });
    return {
        problems,
        deck: {
            schemaVersion: SCHEMA_VERSION,
            layoutsVersion: LAYOUTS_VERSION,
            kind: opts.kind ?? "lecture",
            title: opts.title ?? slides[0]?.content.title ?? "Презентация",
            styleId: opts.styleId,
            signature: opts.signature ?? 0,
            slides,
        },
    };
}
//# sourceMappingURL=draft-to-deck.js.map