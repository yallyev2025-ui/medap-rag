import { Canvas, FontLibrary, Path2D } from "skia-canvas";
import { KNOWN_FAMILIES, fontFile, nearestWeight } from "./fonts.js";
import { paintSlide } from "./paint.js";
import { SLIDE_H, SLIDE_W } from "./types.js";
/**
 * Рендер БЕЗ БРАУЗЕРА: PDF и PNG-превью из одной раскладки через skia-canvas.
 *
 * Выбран по замеру этапа 1 (13.10.2026, см. README «Замеры рендера»): та же картинка, что у
 * Chromium, в 4 раза быстрее, втрое легче по памяти, без установки браузера и системных
 * библиотек в образ. Текст в PDF остаётся текстом (выделяется и ищется).
 */
const WEIGHTS = {
    Manrope: [400, 600, 700, 800],
    "Source Serif 4": [400, 600, 700],
    Caveat: [400, 700],
};
const alias = (f, subset) => `${f.family} ${nearestWeight(f.family, f.weight)} ${subset}`;
let ready = false;
function registerFonts() {
    if (ready)
        return;
    for (const family of KNOWN_FAMILIES) {
        for (const weight of WEIGHTS[family] ?? []) {
            for (const subset of ["cyrillic", "latin"]) {
                FontLibrary.use(alias({ family, weight }, subset), [fontFile({ family, weight }, subset)]);
            }
        }
    }
    ready = true;
}
/** Масштаб PDF: 1920 px → 1440 pt = размер слайда PowerPoint 13,333 × 7,5 дюйма */
const PDF_SCALE = 0.75;
function makePainter(getCtx) {
    return {
        rect(x, y, w, h, o) {
            const ctx = getCtx();
            const path = new Path2D();
            if (o.rx)
                path.roundRect(x, y, w, h, o.rx);
            else
                path.rect(x, y, w, h);
            if (o.fill) {
                ctx.fillStyle = o.fill;
                ctx.fill(path);
            }
            if (o.stroke) {
                ctx.strokeStyle = o.stroke.color;
                ctx.lineWidth = o.stroke.width;
                ctx.setLineDash(o.stroke.dash ? [14, 10] : []);
                ctx.stroke(path);
                ctx.setLineDash([]);
            }
        },
        line(x1, y1, x2, y2, color, width) {
            const ctx = getCtx();
            ctx.strokeStyle = color;
            ctx.lineWidth = width;
            ctx.beginPath();
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);
            ctx.stroke();
        },
        polyline(points, color, width) {
            const ctx = getCtx();
            ctx.strokeStyle = color;
            ctx.lineWidth = width;
            ctx.beginPath();
            points.forEach((p, i) => (i === 0 ? ctx.moveTo(p[0], p[1]) : ctx.lineTo(p[0], p[1])));
            ctx.stroke();
        },
        path(d, fill) {
            const ctx = getCtx();
            ctx.fillStyle = fill;
            ctx.fill(new Path2D(d));
        },
        text(str, x, baselineY, font, size, color, anchor) {
            const ctx = getCtx();
            ctx.font = `${size}px "${alias(font, "cyrillic")}", "${alias(font, "latin")}"`;
            ctx.fillStyle = color;
            ctx.textAlign = anchor === "start" ? "left" : anchor === "middle" ? "center" : "right";
            ctx.textBaseline = "alphabetic";
            ctx.fillText(str, x, baselineY);
        },
    };
}
function paintAll(placed, width, height, scale) {
    registerFonts();
    const canvas = new Canvas(width, height);
    let ctx = canvas.getContext("2d");
    const painter = makePainter(() => ctx);
    placed.slides.forEach((slide, i) => {
        if (i > 0)
            ctx = canvas.newPage(width, height);
        ctx.scale(scale, scale);
        paintSlide(slide, painter);
    });
    return canvas;
}
/** PDF всей колоды: страница 1440 × 810 pt, текст остаётся текстом */
export async function renderPdf(placed) {
    const canvas = paintAll(placed, SLIDE_W * PDF_SCALE, SLIDE_H * PDF_SCALE, PDF_SCALE);
    return canvas.toBuffer("pdf");
}
/** PNG-превью каждого слайда в 1920 × 1080 */
export async function renderPngs(placed) {
    const canvas = paintAll(placed, SLIDE_W, SLIDE_H, 1);
    const out = [];
    for (let i = 0; i < placed.slides.length; i++)
        out.push(await canvas.toBuffer("png", { page: i + 1 }));
    return out;
}
//# sourceMappingURL=skia.js.map