import PptxModule from "pptxgenjs";
import { LAYOUTS } from "./compose.js";
/**
 * Раскладка → настоящий PPTX: текст остаётся текстом, фигуры — фигурами, диаграммы — диаграммами
 * PowerPoint (их можно править правой кнопкой). Картинок слайдов внутри нет.
 *
 * ПЕРЕВОД ЕДИНИЦ. 1 логический пиксель = 1/144 дюйма = 0,5 пункта шрифта.
 *
 * ЧЕГО PPTX НЕ УМЕЕТ ТАК ЖЕ, КАК PDF (честные пределы, см. README):
 * - шрифт, не установленный у человека, подменяется (pptxgenjs не встраивает шрифты);
 * - PowerPoint переносит строки по своим метрикам: мы даём запас по ширине (WIDTH_SAFETY),
 *   фиксируем межстрочный интервал в пунктах и отключаем автоподгонку;
 * - макеты-мастера названы по нашим макетам, но слайды не используют заполнители (placeholders),
 *   поэтому «сменить макет» в PowerPoint меняет только фон, а не раскладку.
 */
/*
 * Типы pptxgenjs описаны как CommonJS (`export default` внутри объекта), а при запуске, в
 * зависимости от загрузчика (tsx, node, сборка), приходит то сам класс, то объект с `default`.
 * Приводим один раз здесь, чтобы остальной код был обычным.
 */
const wrapped = PptxModule;
const PptxGenJS = (typeof wrapped.default === "function" ? wrapped.default : PptxModule);
const IN = 1 / 144;
const hex = (c) => c.replace("#", "").toUpperCase();
const inch = (px) => Math.round(px * IN * 1000) / 1000;
export async function buildPptx(placed) {
    const pptx = new PptxGenJS();
    pptx.layout = "LAYOUT_WIDE";
    pptx.title = placed.deck.title;
    pptx.author = "Medap Slide";
    pptx.subject = `Стиль «${placed.style.name}»`;
    for (const layout of Object.values(LAYOUTS)) {
        pptx.defineSlideMaster({ title: layout.id, background: { color: hex(placed.style.palette.bg) } });
    }
    for (const ps of placed.slides)
        addSlide(pptx, ps);
    const out = await pptx.write({ outputType: "nodebuffer" });
    return out;
}
function addSlide(pptx, ps) {
    const slide = pptx.addSlide({ masterName: ps.layoutId });
    slide.background = { color: hex(ps.bg) };
    for (const e of ps.elements) {
        switch (e.kind) {
            case "rect":
                addRect(pptx, slide, e);
                break;
            case "line":
                slide.addShape(pptx.ShapeType.line, {
                    x: inch(Math.min(e.x1, e.x2)),
                    y: inch(Math.min(e.y1, e.y2)),
                    w: inch(Math.abs(e.x2 - e.x1)),
                    h: inch(Math.abs(e.y2 - e.y1)),
                    line: { color: hex(e.color), width: e.width * 0.5 },
                });
                break;
            case "text":
                addText(slide, e);
                break;
            case "table":
                addTable(slide, e);
                break;
            case "chart":
                addChart(pptx, slide, e);
                break;
            case "photo":
                addPhoto(pptx, slide, e);
                break;
        }
    }
    if (ps.slide.notes)
        slide.addNotes(ps.slide.notes);
}
function addRect(pptx, slide, e) {
    const round = e.radius !== undefined && e.radius > 0;
    const circle = round && e.radius !== undefined && e.radius * 2 >= Math.min(e.box.w, e.box.h);
    slide.addShape(circle ? pptx.ShapeType.ellipse : round ? pptx.ShapeType.roundRect : pptx.ShapeType.rect, {
        x: inch(e.box.x),
        y: inch(e.box.y),
        w: inch(e.box.w),
        h: inch(e.box.h),
        fill: e.fill ? { color: hex(e.fill) } : { type: "none" },
        line: e.stroke ? { color: hex(e.stroke.color), width: e.stroke.width * 0.5, dashType: e.stroke.dash ? "dash" : "solid" } : { type: "none" },
        ...(round && !circle ? { rectRadius: inch(e.radius ?? 0) } : {}),
    });
}
function addText(slide, e) {
    slide.addText(e.text, {
        x: inch(e.box.x),
        y: inch(e.box.y),
        w: inch(e.box.w),
        h: inch(e.box.h),
        fontFace: e.family,
        fontSize: e.size * 0.5,
        bold: e.weight >= 600,
        color: hex(e.color),
        align: e.align,
        valign: "top",
        margin: 0,
        lineSpacing: Math.round(e.size * e.lineHeight * 0.5 * 10) / 10,
        paraSpaceAfter: 0,
        paraSpaceBefore: 0,
        fit: "none",
        wrap: true,
    });
}
function addTable(slide, e) {
    const border = { type: "solid", color: hex(e.lineColor), pt: 1 };
    const head = e.head.map((t) => ({
        text: t,
        options: { bold: true, color: hex(e.headColor), fill: { color: hex(e.headFill) }, border },
    }));
    const body = e.rows.map((row, r) => row.map((t) => ({
        text: t,
        options: { color: hex(e.bodyColor), fill: r % 2 === 1 ? { color: hex(e.zebra) } : undefined, border },
    })));
    slide.addTable([head, ...body], {
        x: inch(e.box.x),
        y: inch(e.box.y),
        w: inch(e.box.w),
        colW: e.colW.map(inch),
        rowH: inch(e.rowH),
        fontFace: e.family,
        fontSize: e.fontSize * 0.5,
        valign: "middle",
        margin: [0, 0.16, 0, 0.16],
    });
}
function addChart(pptx, slide, e) {
    const type = e.chartType === "bar" ? pptx.ChartType.bar : e.chartType === "line" ? pptx.ChartType.line : pptx.ChartType.pie;
    const data = e.series.map((s) => ({ name: s.name, labels: e.labels, values: s.values }));
    slide.addChart(type, data, {
        x: inch(e.box.x),
        y: inch(e.box.y),
        w: inch(e.box.w),
        h: inch(e.box.h),
        chartColors: e.chartType === "pie" ? e.colors.map(hex) : e.colors.slice(0, e.series.length).map(hex),
        catAxisLabelFontFace: e.fontFamily,
        valAxisLabelFontFace: e.fontFamily,
        catAxisLabelFontSize: e.fontSize * 0.5,
        valAxisLabelFontSize: e.fontSize * 0.5,
        catAxisLabelColor: hex(e.textColor),
        valAxisLabelColor: hex(e.textColor),
        valGridLine: { color: hex(e.gridColor), size: 1 },
        showLegend: e.series.length > 1,
        legendColor: hex(e.textColor),
        legendFontFace: e.fontFamily,
        barGapWidthPct: 60,
    });
}
function addPhoto(pptx, slide, e) {
    const { x, y, w, h } = e.box;
    slide.addShape(pptx.ShapeType.rect, { x: inch(x), y: inch(y), w: inch(w), h: inch(h), fill: { color: hex(e.fill) }, line: { type: "none" } });
    slide.addShape(pptx.ShapeType.rect, {
        x: inch(x + 40),
        y: inch(y + 40),
        w: inch(w - 80),
        h: inch(h - 80),
        fill: { type: "none" },
        line: { color: hex(e.stroke), width: 1.5, dashType: "dash" },
    });
    if (e.caption) {
        slide.addText(e.caption, {
            x: inch(x + 40),
            y: inch(y + h / 2 + 40),
            w: inch(w - 80),
            h: inch(70),
            align: "center",
            fontFace: e.family,
            fontSize: 15,
            color: hex(e.textColor),
            margin: 0,
        });
    }
}
//# sourceMappingURL=build-pptx.js.map