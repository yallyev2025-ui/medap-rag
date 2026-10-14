import { baselineOffset } from "./measure.js";
import { SLIDE_H, SLIDE_W, } from "./types.js";
export function paintSlide(ps, p) {
    p.rect(0, 0, SLIDE_W, SLIDE_H, { fill: ps.bg });
    for (const e of ps.elements) {
        if (e.kind === "rect")
            p.rect(e.box.x, e.box.y, e.box.w, e.box.h, { fill: e.fill, rx: e.radius, stroke: e.stroke });
        else if (e.kind === "line")
            p.line(e.x1, e.y1, e.x2, e.y2, e.color, e.width);
        else if (e.kind === "photo")
            photo(e, p);
        else if (e.kind === "chart")
            chart(e, p);
        else if (e.kind === "table")
            table(e, p);
    }
    for (const e of ps.elements)
        if (e.kind === "text")
            text(e, p);
}
function text(e, p) {
    const font = { family: e.family, weight: e.weight };
    const lh = e.size * e.lineHeight;
    const base = baselineOffset(font, e.size, lh);
    const x = e.align === "left" ? e.box.x : e.align === "center" ? e.box.x + e.box.w / 2 : e.box.x + e.box.w;
    const anchor = e.align === "left" ? "start" : e.align === "center" ? "middle" : "end";
    e.lines.forEach((line, i) => {
        if (line !== "")
            p.text(line, x, e.box.y + i * lh + base, font, e.size, e.color, anchor);
    });
}
function photo(e, p) {
    const { x, y, w, h } = e.box;
    const cx = x + w / 2;
    const cy = y + h / 2;
    p.rect(x, y, w, h, { fill: e.fill });
    p.rect(x + 40, y + 40, w - 80, h - 80, { stroke: { color: e.stroke, width: 3, dash: true } });
    p.rect(cx - 44, cy - 60, 88, 64, { rx: 10, stroke: { color: e.textColor, width: 4 } });
    p.rect(cx - 16, cy - 44, 32, 32, { rx: 16, stroke: { color: e.textColor, width: 4 } });
    if (e.caption)
        p.text(e.caption, cx, cy + 70, { family: e.family, weight: 400 }, 30, e.textColor, "middle");
}
function table(e, p) {
    const bold = { family: e.family, weight: 700 };
    const regular = { family: e.family, weight: 400 };
    const base = baselineOffset(regular, e.fontSize, e.rowH);
    [e.head, ...e.rows].forEach((row, r) => {
        const y = e.box.y + r * e.rowH;
        const fill = r === 0 ? e.headFill : r % 2 === 0 ? e.zebra : undefined;
        if (fill)
            p.rect(e.box.x, y, e.box.w, e.rowH, { fill });
        p.line(e.box.x, y + e.rowH - 1, e.box.x + e.box.w, y + e.rowH - 1, e.lineColor, 2);
        let x = e.box.x;
        row.forEach((cell, c) => {
            p.text(cell, x + 24, y + base, r === 0 ? bold : regular, e.fontSize, r === 0 ? e.headColor : e.bodyColor, "start");
            x += e.colW[c] ?? 0;
        });
    });
}
function chart(e, p) {
    const { x, y, w, h } = e.box;
    const font = { family: e.fontFamily, weight: 400 };
    const plot = { x: x + 70, y: y + 20, w: w - 80, h: h - 90 };
    const maxV = Math.max(1, ...e.series.flatMap((s) => s.values));
    if (e.chartType === "pie") {
        const values = e.series[0]?.values ?? [];
        const total = values.reduce((a, b) => a + b, 0) || 1;
        const r = Math.min(w, h) / 2 - 20;
        const cx = x + w / 2;
        const cy = y + h / 2;
        let a0 = -Math.PI / 2;
        values.forEach((v, i) => {
            const a1 = a0 + (v / total) * Math.PI * 2;
            p.path(`M${cx} ${cy} L${cx + r * Math.cos(a0)} ${cy + r * Math.sin(a0)} A${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${cx + r * Math.cos(a1)} ${cy + r * Math.sin(a1)} Z`, e.colors[i % e.colors.length] ?? "#000000");
            a0 = a1;
        });
        return;
    }
    for (let g = 0; g <= 4; g++) {
        const gy = plot.y + plot.h - (plot.h * g) / 4;
        p.line(plot.x, gy, plot.x + plot.w, gy, e.gridColor, 2);
        p.text(String(Math.round((maxV * g) / 4)), plot.x - 14, gy + 8, font, e.fontSize, e.textColor, "end");
    }
    const n = Math.max(e.labels.length, 1);
    const slot = plot.w / n;
    e.labels.forEach((label, i) => {
        p.text(label, plot.x + slot * i + slot / 2, plot.y + plot.h + 40, font, e.fontSize, e.textColor, "middle");
    });
    e.series.forEach((s, si) => {
        const color = e.colors[si % e.colors.length] ?? "#000000";
        if (e.chartType === "bar") {
            const bw = (slot * 0.6) / e.series.length;
            s.values.forEach((v, i) => {
                const bh = (v / maxV) * plot.h;
                p.rect(plot.x + slot * i + slot * 0.2 + bw * si, plot.y + plot.h - bh, bw, bh, { fill: color });
            });
        }
        else {
            p.polyline(s.values.map((v, i) => [plot.x + slot * i + slot / 2, plot.y + plot.h - (v / maxV) * plot.h]), color, 5);
        }
    });
}
//# sourceMappingURL=paint.js.map