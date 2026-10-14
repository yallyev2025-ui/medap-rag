import { UNICODE_RANGE, fontBase64, fontFile, nearestWeight } from "./fonts.js";
import { SLIDE_H, SLIDE_W, } from "./types.js";
/**
 * Раскладка → HTML. Каждая строка текста приходит уже с переносами из измерителя и рисуется
 * без повторного переноса (`white-space: pre`): PDF поэтому совпадает с расчётом, а не с тем,
 * что решит браузер. PPTX получает исходный текст и переносит его сам — это и есть
 * неизбежное расхождение, которое мы сдерживаем запасом по ширине.
 */
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
function fontFaces(deck) {
    const used = new Set();
    for (const s of deck.slides) {
        for (const e of s.elements) {
            if (e.kind === "text")
                used.add(`${e.family}|${e.weight}`);
            if (e.kind === "table" || e.kind === "chart" || e.kind === "photo")
                used.add(`${e.kind === "table" ? e.family : e.kind === "chart" ? e.fontFamily : e.family}|400`);
        }
    }
    const faces = [];
    for (const key of used) {
        const [family, w] = key.split("|");
        const weight = nearestWeight(family, Number(w));
        for (const subset of ["cyrillic", "latin"]) {
            const file = fontFile({ family, weight }, subset);
            faces.push(`@font-face{font-family:"${family}";font-weight:${weight};font-style:normal;src:url(data:font/woff2;base64,${fontBase64(file)}) format("woff2");unicode-range:${UNICODE_RANGE[subset]};}`);
        }
    }
    return faces.join("\n");
}
function shapes(elements) {
    const out = [];
    for (const e of elements) {
        if (e.kind === "rect") {
            const stroke = e.stroke ? ` stroke="${e.stroke.color}" stroke-width="${e.stroke.width}"${e.stroke.dash ? ' stroke-dasharray="14 10"' : ""}` : "";
            out.push(`<rect x="${e.box.x}" y="${e.box.y}" width="${e.box.w}" height="${e.box.h}" rx="${e.radius ?? 0}" fill="${e.fill ?? "none"}"${stroke}/>`);
        }
        else if (e.kind === "line") {
            out.push(`<line x1="${e.x1}" y1="${e.y1}" x2="${e.x2}" y2="${e.y2}" stroke="${e.color}" stroke-width="${e.width}" stroke-linecap="butt"/>`);
        }
        else if (e.kind === "photo") {
            out.push(photoSvg(e));
        }
        else if (e.kind === "chart") {
            out.push(chartSvg(e));
        }
    }
    return out.join("");
}
function photoSvg(e) {
    const { x, y, w, h } = e.box;
    const cx = x + w / 2;
    const cy = y + h / 2;
    const caption = e.caption
        ? `<text x="${cx}" y="${cy + 70}" text-anchor="middle" fill="${e.textColor}" font-family="${e.family}" font-size="30">${esc(e.caption)}</text>`
        : "";
    return (`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${e.fill}"/>` +
        `<rect x="${x + 40}" y="${y + 40}" width="${w - 80}" height="${h - 80}" fill="none" stroke="${e.stroke}" stroke-width="3" stroke-dasharray="16 12"/>` +
        `<rect x="${cx - 44}" y="${cy - 60}" width="88" height="64" rx="10" fill="none" stroke="${e.textColor}" stroke-width="4"/>` +
        `<circle cx="${cx}" cy="${cy - 28}" r="16" fill="none" stroke="${e.textColor}" stroke-width="4"/>` +
        caption);
}
function chartSvg(e) {
    const { x, y, w, h } = e.box;
    const font = `font-family="${e.fontFamily}" font-size="${e.fontSize}" fill="${e.textColor}"`;
    const padL = 70;
    const padB = 60;
    const plot = { x: x + padL, y: y + 20, w: w - padL - 10, h: h - padB - 30 };
    const maxV = Math.max(1, ...e.series.flatMap((s) => s.values));
    const out = [];
    if (e.chartType === "pie") {
        const values = e.series[0]?.values ?? [];
        const total = values.reduce((a, b) => a + b, 0) || 1;
        const r = Math.min(w, h) / 2 - 20;
        const cx = x + w / 2;
        const cy = y + h / 2;
        let a0 = -Math.PI / 2;
        values.forEach((v, i) => {
            const a1 = a0 + (v / total) * Math.PI * 2;
            const large = a1 - a0 > Math.PI ? 1 : 0;
            out.push(`<path d="M${cx} ${cy} L${cx + r * Math.cos(a0)} ${cy + r * Math.sin(a0)} A${r} ${r} 0 ${large} 1 ${cx + r * Math.cos(a1)} ${cy + r * Math.sin(a1)} Z" fill="${e.colors[i % e.colors.length]}"/>`);
            a0 = a1;
        });
        return out.join("");
    }
    for (let g = 0; g <= 4; g++) {
        const gy = plot.y + plot.h - (plot.h * g) / 4;
        out.push(`<line x1="${plot.x}" y1="${gy}" x2="${plot.x + plot.w}" y2="${gy}" stroke="${e.gridColor}" stroke-width="2"/>`);
        out.push(`<text x="${plot.x - 14}" y="${gy + 8}" text-anchor="end" ${font}>${Math.round((maxV * g) / 4)}</text>`);
    }
    const n = Math.max(e.labels.length, 1);
    const slot = plot.w / n;
    e.labels.forEach((label, i) => {
        out.push(`<text x="${plot.x + slot * i + slot / 2}" y="${plot.y + plot.h + 40}" text-anchor="middle" ${font}>${esc(label)}</text>`);
    });
    e.series.forEach((s, si) => {
        const color = e.colors[si % e.colors.length];
        if (e.chartType === "bar") {
            const bw = (slot * 0.6) / e.series.length;
            s.values.forEach((v, i) => {
                const bh = (v / maxV) * plot.h;
                out.push(`<rect x="${plot.x + slot * i + slot * 0.2 + bw * si}" y="${plot.y + plot.h - bh}" width="${bw}" height="${bh}" fill="${color}"/>`);
            });
        }
        else {
            const pts = s.values.map((v, i) => `${plot.x + slot * i + slot / 2},${plot.y + plot.h - (v / maxV) * plot.h}`);
            out.push(`<polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="5"/>`);
        }
    });
    return out.join("");
}
function textHtml(e) {
    const px = Math.round(e.size * e.lineHeight);
    return (`<div style="position:absolute;left:${e.box.x}px;top:${e.box.y}px;width:${e.box.w}px;height:${Math.ceil(e.box.h)}px;` +
        `font-family:'${e.family}';font-weight:${e.weight};font-size:${e.size}px;line-height:${px}px;color:${e.color};` +
        `text-align:${e.align};white-space:pre;overflow:visible;font-kerning:none">${esc(e.lines.join("\n"))}</div>`);
}
function tableHtml(e) {
    const cell = (t, c, head) => `<div style="position:absolute;left:${e.colW.slice(0, c).reduce((a, b) => a + b, 0)}px;width:${e.colW[c]}px;` +
        `padding:0 24px;box-sizing:border-box;line-height:${e.rowH}px;height:${e.rowH}px;white-space:nowrap;overflow:hidden;` +
        `font-weight:${head ? 700 : 400};color:${head ? e.headColor : e.bodyColor}">${esc(t)}</div>`;
    const rows = [e.head, ...e.rows].map((row, r) => {
        const bg = r === 0 ? e.headFill : r % 2 === 0 ? e.zebra : "transparent";
        return (`<div style="position:absolute;left:0;top:${r * e.rowH}px;width:${e.box.w}px;height:${e.rowH}px;background:${bg};border-bottom:2px solid ${e.lineColor}">` +
            row.map((t, c) => cell(t, c, r === 0)).join("") +
            `</div>`);
    });
    return (`<div style="position:absolute;left:${e.box.x}px;top:${e.box.y}px;width:${e.box.w}px;height:${e.box.h}px;` +
        `font-family:'${e.family}';font-size:${e.fontSize}px">${rows.join("")}</div>`);
}
function slideHtml(ps) {
    const texts = ps.elements.filter((e) => e.kind === "text").map(textHtml).join("");
    const tables = ps.elements.filter((e) => e.kind === "table").map(tableHtml).join("");
    return (`<section class="slide" style="background:${ps.bg}">` +
        `<svg width="${SLIDE_W}" height="${SLIDE_H}" viewBox="0 0 ${SLIDE_W} ${SLIDE_H}" style="position:absolute;left:0;top:0">${shapes(ps.elements)}</svg>` +
        tables +
        texts +
        `</section>`);
}
export function renderHtml(deck) {
    return (`<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>${esc(deck.deck.title)}</title><style>` +
        `${fontFaces(deck)}\n@page{size:${SLIDE_W}px ${SLIDE_H}px;margin:0}` +
        `html,body{margin:0;padding:0;background:#888}` +
        `.slide{width:${SLIDE_W}px;height:${SLIDE_H}px;position:relative;overflow:hidden;page-break-after:always;break-after:page}` +
        `</style></head><body>${deck.slides.map(slideHtml).join("")}</body></html>`);
}
//# sourceMappingURL=render-html.js.map