import { fitText } from "./fit.js";
import { glue } from "./wrap.js";
import { textWidth } from "./measure.js";
import { wrap } from "./wrap.js";
import { SLIDE_H as H, SLIDE_W as W, } from "./types.js";
const BOTTOM = 90;
/** Шаг шкалы размеров: 0 → 30 px (15 pt) */
export function sz(style, step) {
    return Math.round(30 * style.scale ** step);
}
export function mix(a, b, t) {
    const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
    const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
    const out = pa.map((v, i) => Math.round(v + ((pb[i] ?? 0) - v) * t));
    return `#${out.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}
function put(ctx, o) {
    const lh = o.lh ?? 1.25;
    const text = glue(o.text);
    const fit = fitText({
        text,
        font: o.font,
        maxWidth: o.w,
        maxHeight: o.h,
        maxSize: o.maxSize,
        minSize: o.minSize,
        lineHeight: lh,
    });
    let y = o.y;
    if (o.valign === "middle")
        y = o.y + (o.h - fit.height) / 2;
    if (o.valign === "bottom")
        y = o.y + o.h - fit.height;
    const el = {
        kind: "text",
        box: { x: o.x, y, w: o.w, h: fit.height },
        text,
        lines: fit.lines,
        size: fit.size,
        weight: o.font.weight,
        family: o.font.family,
        color: o.color,
        align: o.align ?? "left",
        lineHeight: lh,
        onBg: o.onBg ?? ctx.bg,
        overflow: !fit.fits,
        minSize: o.minSize,
        tag: o.tag,
    };
    ctx.els.push(el);
    return el;
}
const bottom = (el) => el.box.y + el.box.h;
/* ---------------------------------------------------------------- общие части */
/** Заголовок слайда и мотив подписи. Возвращает нижнюю границу заголовка */
function titleBlock(ctx, maxStep = 4) {
    const { style, slide } = ctx;
    const M = style.margin;
    const title = put(ctx, {
        text: slide.content.title,
        x: M,
        y: 96,
        w: W - 2 * M,
        h: 200,
        font: style.heading,
        maxSize: sz(style, maxStep),
        minSize: 44,
        lh: 1.12,
        color: ctx.fg,
        tag: "title",
    });
    motif(ctx, bottom(title));
    return bottom(title);
}
/** Мотив подписи: у двоих людей на одной семье стиля слайды различаются */
function motif(ctx, titleBottom) {
    const { style } = ctx;
    const M = style.margin;
    const kind = style.motifs[ctx.signature % style.motifs.length];
    const accent = style.palette.accent;
    if (kind === "bar") {
        ctx.els.push({ kind: "rect", box: { x: 0, y: 96, w: 18, h: Math.max(80, titleBottom - 96) }, fill: accent });
    }
    else if (kind === "corner") {
        ctx.els.push({ kind: "rect", box: { x: 0, y: 0, w: 220, h: 14 }, fill: accent });
    }
    else if (kind === "dot") {
        ctx.els.push({ kind: "rect", box: { x: M, y: 58, w: 22, h: 22 }, fill: accent, radius: 11 });
    }
    else {
        ctx.els.push({ kind: "line", x1: M, y1: titleBottom + 26, x2: M + 180, y2: titleBottom + 26, color: accent, width: 6 });
    }
}
/** Верхняя граница содержимого под заголовком */
const contentTop = (titleBottom) => titleBottom + 70;
/** Список с короткой чертой вместо точки-маркера */
function list(ctx, items, box, opts) {
    const { style } = ctx;
    const indent = opts.numbered ? 0 : 44;
    const gapOf = (s) => Math.round(s * 0.6);
    const cleaned = items.map((t, i) => (opts.numbered ? `${i + 1}. ${t}` : t));
    let size = opts.maxSize;
    for (; size > opts.minSize; size -= 2) {
        let total = 0;
        for (const t of cleaned) {
            const lines = wrap(glue(t), style.body, size, (box.w - indent) * 0.96).lines.length;
            total += lines * size * 1.3 + gapOf(size);
        }
        if (total - gapOf(size) <= box.h)
            break;
    }
    let y = box.y;
    for (const t of cleaned) {
        const el = put(ctx, {
            text: t,
            x: box.x + indent,
            y,
            w: box.w - indent,
            h: box.h,
            font: style.body,
            maxSize: size,
            minSize: size,
            lh: 1.3,
            color: opts.color ?? ctx.fg,
            onBg: opts.onBg,
            tag: "item",
        });
        if (!opts.numbered) {
            ctx.els.push({
                kind: "rect",
                box: { x: box.x, y: y + Math.round(size * 0.62), w: 28, h: 5 },
                fill: style.palette.accent,
            });
        }
        y = bottom(el) + gapOf(size);
    }
    return y;
}
const title = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const t = put(ctx, {
        text: slide.content.title,
        x: M,
        y: 180,
        w: W * 0.74,
        h: 480,
        font: style.heading,
        maxSize: sz(style, 7),
        minSize: 56,
        lh: 1.05,
        color: ctx.fg,
        valign: "bottom",
        tag: "title",
    });
    ctx.els.push({ kind: "line", x1: M, y1: bottom(t) + 36, x2: M + 220, y2: bottom(t) + 36, color: style.palette.accent, width: 8 });
    let y = bottom(t) + 80;
    if (slide.content.text) {
        const sub = put(ctx, {
            text: slide.content.text,
            x: M,
            y,
            w: W * 0.6,
            h: 160,
            font: style.body,
            maxSize: sz(style, 2),
            minSize: 28,
            color: ctx.muted,
            tag: "subtitle",
        });
        y = bottom(sub) + 24;
    }
    const meta = slide.content.meta ?? [];
    if (meta.length > 0) {
        put(ctx, {
            text: meta.join("\n"),
            x: M,
            y: H - BOTTOM - 120,
            w: W * 0.6,
            h: 120,
            font: style.body,
            maxSize: sz(style, 0),
            minSize: 22,
            color: ctx.muted,
            valign: "bottom",
            tag: "meta",
        });
    }
};
const thesis = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const statement = put(ctx, {
        text: slide.content.title,
        x: M,
        y: 150,
        w: W - 2 * M - 120,
        h: slide.content.text ? 560 : 700,
        font: style.heading,
        maxSize: sz(style, 6),
        minSize: 48,
        lh: 1.1,
        color: ctx.fg,
        valign: slide.content.text ? "top" : "middle",
        tag: "title",
    });
    ctx.els.push({ kind: "rect", box: { x: M, y: 96, w: 120, h: 8 }, fill: style.palette.accent });
    if (slide.content.text) {
        put(ctx, {
            text: slide.content.text,
            x: M,
            y: bottom(statement) + 56,
            w: W * 0.55,
            h: H - BOTTOM - bottom(statement) - 56,
            font: style.body,
            maxSize: sz(style, 2),
            minSize: 26,
            color: ctx.muted,
            tag: "text",
        });
    }
};
const text = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx);
    const top = contentTop(tb);
    const h = H - BOTTOM - top;
    const items = slide.content.items ?? [];
    if (items.length > 0) {
        const half = Math.ceil(items.length / 2);
        const gap = 80;
        const colW = (W - 2 * M - gap) / 2;
        if (items.length <= 3) {
            list(ctx, items, { x: M, y: top, w: W * 0.7, h }, { maxSize: sz(style, 3), minSize: 26 });
        }
        else {
            list(ctx, items.slice(0, half), { x: M, y: top, w: colW, h }, { maxSize: sz(style, 2), minSize: 24 });
            list(ctx, items.slice(half), { x: M + colW + gap, y: top, w: colW, h }, { maxSize: sz(style, 2), minSize: 24 });
        }
        return;
    }
    const paragraphs = (slide.content.text ?? "").split("\n\n").filter(Boolean);
    const body = (t, x, w) => {
        put(ctx, { text: t, x, y: top, w, h, font: style.body, maxSize: sz(style, 2), minSize: 24, lh: 1.4, color: ctx.fg, tag: "text" });
    };
    if (paragraphs.length >= 2) {
        const gap = 80;
        const colW = (W - 2 * M - gap) / 2;
        const mid = Math.ceil(paragraphs.length / 2);
        body(paragraphs.slice(0, mid).join("\n\n"), M, colW);
        body(paragraphs.slice(mid).join("\n\n"), M + colW + gap, colW);
    }
    else {
        body(paragraphs[0] ?? "", M, W * 0.62);
    }
};
const compare = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx, 3);
    const top = contentTop(tb);
    const cols = (slide.content.columns ?? []).slice(0, 3);
    const n = Math.max(cols.length, 1);
    const gap = 40;
    const cardW = (W - 2 * M - gap * (n - 1)) / n;
    const cardH = H - BOTTOM - top;
    const pad = 40;
    cols.forEach((col, i) => {
        const x = M + i * (cardW + gap);
        ctx.els.push({ kind: "rect", box: { x, y: top, w: cardW, h: cardH }, fill: ctx.surface, radius: 18 });
        const head = put(ctx, {
            text: col.title,
            x: x + pad,
            y: top + pad,
            w: cardW - 2 * pad,
            h: 120,
            font: style.heading,
            maxSize: sz(style, 3),
            minSize: 28,
            lh: 1.15,
            color: ctx.fg,
            onBg: ctx.surface,
            tag: "col-title",
        });
        ctx.els.push({ kind: "line", x1: x + pad, y1: bottom(head) + 18, x2: x + pad + 90, y2: bottom(head) + 18, color: style.palette.accent, width: 5 });
        list(ctx, col.items, { x: x + pad, y: bottom(head) + 52, w: cardW - 2 * pad, h: top + cardH - pad - (bottom(head) + 52) }, { maxSize: sz(style, 1), minSize: 22, onBg: ctx.surface });
    });
};
const stat = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx, 3);
    const top = contentTop(tb);
    const s = slide.content.stat ?? { value: "—", label: "" };
    const value = put(ctx, {
        text: s.value,
        x: M,
        y: top,
        w: W * 0.52,
        h: 420,
        font: style.heading,
        maxSize: sz(style, 9),
        minSize: 120,
        lh: 1,
        color: style.palette.accent,
        tag: "stat",
    });
    put(ctx, {
        text: s.label,
        x: M,
        y: bottom(value) + 28,
        w: W * 0.5,
        h: 200,
        font: style.body,
        maxSize: sz(style, 2),
        minSize: 26,
        color: ctx.fg,
        tag: "stat-label",
    });
    ctx.els.push({ kind: "line", x1: W * 0.6, y1: top + 20, x2: W * 0.6, y2: H - BOTTOM - 40, color: style.palette.line, width: 3 });
    if (slide.content.text) {
        put(ctx, {
            text: slide.content.text,
            x: W * 0.6 + 60,
            y: top + 20,
            w: W - M - (W * 0.6 + 60),
            h: H - BOTTOM - top - 60,
            font: style.body,
            maxSize: sz(style, 2),
            minSize: 24,
            lh: 1.4,
            color: ctx.muted,
            tag: "text",
        });
    }
};
const quote = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const q = slide.content.quote ?? { text: slide.content.title };
    put(ctx, {
        text: "«",
        x: M - 10,
        y: 70,
        w: 300,
        h: 300,
        font: style.heading,
        maxSize: 300,
        minSize: 300,
        lh: 1,
        color: mix(ctx.fg, ctx.bg, 0.5),
        onBg: ctx.bg,
        tag: "decor",
    });
    const body = put(ctx, {
        text: q.text,
        x: M + 40,
        y: 330,
        w: W - 2 * M - 200,
        h: 480,
        font: { family: style.heading.family, weight: 600 },
        maxSize: sz(style, 5),
        minSize: 36,
        lh: 1.2,
        color: ctx.fg,
        tag: "quote",
    });
    if (q.author) {
        put(ctx, {
            text: `— ${q.author}`,
            x: M + 40,
            y: bottom(body) + 40,
            w: W * 0.6,
            h: 80,
            font: style.body,
            maxSize: sz(style, 1),
            minSize: 24,
            color: ctx.muted,
            tag: "author",
        });
    }
};
const steps = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx, 3);
    const top = contentTop(tb) + 20;
    const items = (slide.content.steps ?? []).slice(0, 5);
    const n = Math.max(items.length, 1);
    const colW = (W - 2 * M) / n;
    const pad = 36;
    const avail = H - BOTTOM - top;
    ctx.els.push({ kind: "line", x1: M, y1: top + 56, x2: W - M, y2: top + 56, color: style.palette.line, width: 3 });
    items.forEach((st, i) => {
        const x = M + i * colW;
        put(ctx, {
            text: String(i + 1).padStart(2, "0"),
            x,
            y: top,
            w: colW - pad,
            h: 110,
            font: style.heading,
            maxSize: sz(style, 5),
            minSize: 56,
            lh: 1,
            color: style.palette.accent,
            tag: "step-no",
        });
        const t = put(ctx, {
            text: st.title,
            x,
            y: top + 140,
            w: colW - pad,
            h: 180,
            font: { family: style.body.family, weight: 700 },
            maxSize: sz(style, 3),
            minSize: 24,
            lh: 1.2,
            color: ctx.fg,
            tag: "step-title",
        });
        if (st.text) {
            put(ctx, {
                text: st.text,
                x,
                y: bottom(t) + 20,
                w: colW - pad,
                h: top + avail - (bottom(t) + 20),
                font: style.body,
                maxSize: sz(style, 1),
                minSize: 20,
                lh: 1.35,
                color: ctx.muted,
                tag: "step-text",
            });
        }
    });
};
const timeline = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx, 3);
    const top = contentTop(tb);
    const items = (slide.content.steps ?? []).slice(0, 6);
    const n = Math.max(items.length, 1);
    const rowH = Math.min(150, (H - BOTTOM - top) / n);
    ctx.els.push({ kind: "line", x1: M + 14, y1: top + 14, x2: M + 14, y2: top + rowH * (n - 1) + 14, color: style.palette.line, width: 4 });
    items.forEach((st, i) => {
        const y = top + i * rowH;
        ctx.els.push({ kind: "rect", box: { x: M, y, w: 28, h: 28 }, fill: style.palette.accent, radius: 14 });
        put(ctx, {
            text: st.title,
            x: M + 70,
            y: y - 2,
            w: 640,
            h: rowH - 10,
            font: { family: style.body.family, weight: 700 },
            maxSize: sz(style, 2),
            minSize: 24,
            lh: 1.2,
            color: ctx.fg,
            tag: "tl-title",
        });
        if (st.text) {
            put(ctx, {
                text: st.text,
                x: M + 760,
                y: y - 2,
                w: W - M - (M + 760),
                h: rowH - 10,
                font: style.body,
                maxSize: sz(style, 1),
                minSize: 20,
                lh: 1.3,
                color: ctx.muted,
                tag: "tl-text",
            });
        }
    });
};
const table = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx, 3);
    const top = contentTop(tb);
    const t = slide.content.table ?? { head: [], rows: [] };
    const width = W - 2 * M;
    const bold = { family: style.body.family, weight: 700 };
    const needFor = (size) => t.head.map((h, c) => Math.max(textWidth(h, bold, size), ...t.rows.map((r) => textWidth(r[c] ?? "", style.body, size))) + 56);
    // сначала самый крупный кегль, при котором все ячейки умещаются в одну строку; ниже 22 не идём
    let fs = Math.max(22, Math.min(40, Math.round(46 - t.rows.length * 2.2)));
    while (fs > 22 && needFor(fs).reduce((a, b) => a + b, 0) > width)
        fs -= 2;
    const rowH = Math.round(fs * 2.2);
    const weights = needFor(fs).map((n) => Math.max(n, 140));
    const total = weights.reduce((a, b) => a + b, 0) || 1;
    ctx.els.push({
        kind: "table",
        box: { x: M, y: top, w: width, h: rowH * (t.rows.length + 1) },
        head: t.head,
        rows: t.rows,
        colW: weights.map((w) => (w / total) * width),
        rowH,
        fontSize: fs,
        family: style.body.family,
        headFill: style.palette.accent2,
        headColor: ctx.bg,
        bodyColor: ctx.fg,
        lineColor: style.palette.line,
        zebra: ctx.surface,
    });
};
const chart = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx, 3);
    const top = contentTop(tb);
    const c = slide.content.chart ?? { type: "bar", labels: [], series: [] };
    const cw = W * 0.56;
    ctx.els.push({
        kind: "chart",
        box: { x: M, y: top, w: cw, h: H - BOTTOM - top },
        chartType: c.type,
        labels: c.labels,
        series: c.series,
        colors: [style.palette.accent, style.palette.accent2, style.palette.muted, style.palette.line],
        textColor: ctx.fg,
        fontFamily: style.body.family,
        fontSize: 26,
        gridColor: style.palette.line,
    });
    if (slide.content.text) {
        put(ctx, {
            text: slide.content.text,
            x: M + cw + 80,
            y: top + 20,
            w: W - M - (M + cw + 80),
            h: H - BOTTOM - top - 40,
            font: style.body,
            maxSize: sz(style, 3),
            minSize: 24,
            lh: 1.35,
            color: ctx.fg,
            tag: "takeaway",
        });
    }
};
const photo = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const leftW = W * 0.4;
    const t = put(ctx, {
        text: slide.content.title,
        x: M,
        y: 140,
        w: leftW,
        h: 420,
        font: style.heading,
        maxSize: sz(style, 4),
        minSize: 40,
        lh: 1.12,
        color: ctx.fg,
        tag: "title",
    });
    motif(ctx, bottom(t));
    if (slide.content.text) {
        put(ctx, {
            text: slide.content.text,
            x: M,
            y: bottom(t) + 70,
            w: leftW,
            h: H - BOTTOM - bottom(t) - 70,
            font: style.body,
            maxSize: sz(style, 2),
            minSize: 22,
            lh: 1.4,
            color: ctx.muted,
            tag: "text",
        });
    }
    ctx.els.push({
        kind: "photo",
        box: { x: W * 0.5, y: 0, w: W * 0.5, h: H },
        caption: slide.content.photo?.caption ?? "Здесь будет ваше фото",
        fill: ctx.surface,
        stroke: style.palette.line,
        textColor: ctx.muted,
        family: style.body.family,
    });
};
const refs = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const tb = titleBlock(ctx, 3);
    const top = contentTop(tb) - 20;
    const items = slide.content.items ?? [];
    const h = H - BOTTOM - top;
    if (items.length > 7) {
        const half = Math.ceil(items.length / 2);
        const gap = 70;
        const colW = (W - 2 * M - gap) / 2;
        list(ctx, items.slice(0, half), { x: M, y: top, w: colW, h }, { maxSize: 32, minSize: 20, numbered: true });
        const second = items.slice(half).map((t, i) => `${half + i + 1}. ${t}`);
        list(ctx, second, { x: M + colW + gap, y: top, w: colW, h }, { maxSize: 32, minSize: 20 });
    }
    else {
        list(ctx, items, { x: M, y: top, w: W - 2 * M, h }, { maxSize: 36, minSize: 20, numbered: true });
    }
};
const end = (ctx) => {
    const { style, slide } = ctx;
    const M = style.margin;
    const t = put(ctx, {
        text: slide.content.title,
        x: M,
        y: 200,
        w: W * 0.7,
        h: 460,
        font: style.heading,
        maxSize: sz(style, 7),
        minSize: 56,
        lh: 1.05,
        color: ctx.fg,
        valign: "bottom",
        tag: "title",
    });
    ctx.els.push({ kind: "rect", box: { x: M, y: bottom(t) + 40, w: 220, h: 10 }, fill: style.palette.accent });
    if (slide.content.text) {
        put(ctx, {
            text: slide.content.text,
            x: M,
            y: bottom(t) + 100,
            w: W * 0.6,
            h: 200,
            font: style.body,
            maxSize: sz(style, 2),
            minSize: 24,
            color: ctx.muted,
            tag: "text",
        });
    }
};
export const LAYOUTS = {
    title: { id: "title", version: 1, compose: title, textHeavy: false, inverse: false },
    statement: { id: "statement", version: 1, compose: thesis, textHeavy: false, inverse: true },
    text: { id: "text", version: 1, compose: text, textHeavy: true, inverse: false },
    list: { id: "list", version: 1, compose: text, textHeavy: true, inverse: false },
    compare: { id: "compare", version: 1, compose: compare, textHeavy: false, inverse: false },
    stat: { id: "stat", version: 1, compose: stat, textHeavy: false, inverse: false },
    quote: { id: "quote", version: 1, compose: quote, textHeavy: false, inverse: true },
    steps: { id: "steps", version: 1, compose: steps, textHeavy: false, inverse: false },
    timeline: { id: "timeline", version: 1, compose: timeline, textHeavy: false, inverse: false },
    table: { id: "table", version: 1, compose: table, textHeavy: false, inverse: false },
    chart: { id: "chart", version: 1, compose: chart, textHeavy: false, inverse: false },
    photo: { id: "photo", version: 1, compose: photo, textHeavy: false, inverse: false },
    refs: { id: "refs", version: 1, compose: refs, textHeavy: true, inverse: false },
    end: { id: "end", version: 1, compose: end, textHeavy: false, inverse: true },
};
//# sourceMappingURL=compose.js.map