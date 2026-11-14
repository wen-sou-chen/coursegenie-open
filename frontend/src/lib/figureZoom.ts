/**
 * 任务 76：第 4 步「课程预览」**点图放大**（概念图 / 数据图 / 位图配图 / 流程图）的纯逻辑与口径。
 *
 * 起因（用户 2026-09-21 22:5x 真机原话）：「点击那些图还是不行……我建议你点击概念图的时候就把
 * 概念图那一块单独弹出来，现在有的他不出来有的是排在 PPT 的后面，总之不能够马上看到。」
 * DSH 只读定位：概念图在「只看概念图」列表里是**裸 `<img>`、没有任何 onClick**（点了完全没反应），
 * 讲义正文里内嵌的概念图同样不可点；概念图区块只在按了「概念图 N」筛选后才出现、且排在讲义/流程图/
 * 图表之后 ⇒ 体感「排在 PPT 后面，不滚看不到」。
 *
 * 与 `lib/previewFilters.ts` 同一套路：**口径只有一处**，组件只负责画，门禁直接跑这里的纯函数。
 * 弹窗尺寸沿用任务 71 的知识图谱弹窗（`92vw` 宽 / 内容区 `78vh`），不新造一套视觉。
 */

/** 弹窗宽度（与任务 71 图谱弹窗同口径：规格要求 ≥92vw）。 */
export const FIGURE_ZOOM_WIDTH = "92vw";
/** 内容区高度上限（规格要求 ≥78vh）；长图 / 1:1 时**在这个框里内部滚动**，页面不乱跳。 */
export const FIGURE_ZOOM_BODY_HEIGHT = "78vh";
/** 「适应窗口」时图的高度上限：留出标题与页脚，图本身仍是整幅可见。 */
export const FIGURE_ZOOM_FIT_MAX_HEIGHT = "72vh";

export type ZoomFit = "fit" | "actual";

/** 弹窗页脚的两种看法（规格 ①：`适应窗口` / `1:1` 可切换）。 */
export const ZOOM_FIT_OPTIONS: { value: ZoomFit; label: string }[] = [
  { value: "fit", label: "适应窗口" },
  { value: "actual", label: "1:1" },
];

/**
 * 两种看法的图样式：
 * - `fit`（默认）放大到弹窗宽度（约视口 90%+），高图整幅压进 72vh —— 一眼看清全貌；
 * - `actual` 原尺寸（1:1），超出部分由弹窗内容区自己滚（正文页面不跟着动）。
 */
export function zoomImageStyle(mode: ZoomFit): Record<string, string> {
  return mode === "actual"
    ? { width: "auto", height: "auto", maxWidth: "none", maxHeight: "none" }
    : {
      width: "100%",
      height: "auto",
      maxHeight: FIGURE_ZOOM_FIT_MAX_HEIGHT,
      objectFit: "contain",
    };
}

/** 弹窗标题：图自己的标题 + **所属章节**（老师要能知道这是哪一节的概念图）。 */
export function figureZoomHeading(
  title: string | undefined,
  chapter: string | undefined,
): { title: string; subtitle: string } {
  const t = String(title ?? "").trim();
  const c = String(chapter ?? "").trim();
  return { title: t || "图", subtitle: c ? `所属章节：${c}` : "所属章节：整课（未归章）" };
}

/** SVG 塞进 `<img>`：mermaid 流程图在页面上是 DOM，放大时序列化成 data URL 显示**同一份图**。 */
export function svgToDataUrl(svg: string): string {
  const t = String(svg ?? "").trim();
  return t ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(t)}` : "";
}

/**
 * mermaid 画出来的 `<svg>` 只带 `width="100%"`、没有 height（靠页面 CSS 撑），
 * 直接塞进 `<img>` 就**没有固有尺寸**（实测按 96×150 兜底 ⇒ 怎么看都是个图标那么大）。
 * 这里按 `viewBox` 补上真实的 width/height，放大时才真是原图的比例。
 */
export function ensureSvgIntrinsicSize(
  svg: string,
  box?: { width: number; height: number } | null,
): string {
  const head = /^<svg\b[^>]*>/.exec(String(svg ?? ""));
  if (!head) return svg;
  if (/\swidth="[\d.]+"/.test(head[0]) && /\sheight="[\d.]+"/.test(head[0])) return svg;
  const vb = head[0].match(/viewBox="([^"]+)"/);
  const parts = vb ? vb[1].trim().split(/[\s,]+/).map(Number) : [];
  let w = parts.length === 4 && parts[2] > 0 ? parts[2] : 0;
  let h = parts.length === 4 && parts[3] > 0 ? parts[3] : 0;
  if (!w || !h) { w = Math.round(box?.width ?? 0); h = Math.round(box?.height ?? 0); }
  if (!w || !h) return svg;
  const opened = head[0].replace(/\s(width|height)="[^"]*"/g, "");
  return svg.replace(head[0], opened.replace("<svg", `<svg width="${w}" height="${h}"`));
}

export interface ZoomTarget {
  kind: "img" | "svg" | "mermaid";
  src: string;
  alt: string;
  /** kind === "mermaid"：流程图源码（弹窗里**重新渲染同一份 mermaid**，不序列化 svg）。 */
  code?: string;
}

/** 小于这个尺寸的 `<svg>` 一律当成图标（antd 图标 14px），不给放大。 */
export const ZOOM_MIN_SVG_PX = 120;

/**
 * 点到的元素能不能放大 —— 三类图一次覆盖：
 * - 概念图 / 位图配图：`<img>`（src 直接用，**同一份文件**）；
 * - 数据图：Chart.js 画在 `<canvas>` 上 ⇒ 取当前像素当图看（**不改生成 / 配色口径**）；
 * - 流程图：mermaid 内联 `<svg>` ⇒ 序列化成 data URL。
 * 其余（表格、正文、按钮、图标）返回 null，点了什么也不发生。
 */
export function resolveZoomTarget(target: EventTarget | null): ZoomTarget | null {
  const el = target as Element | null;
  if (!el || typeof el.tagName !== "string") return null;
  // 图自己的标题（卡片/小节里写着的那一行）—— 数据图与流程图没有 alt，靠这个出标题
  const head = el.closest?.("[data-figure-title]") as HTMLElement | null;
  const title = head?.dataset?.figureTitle ?? "";
  if (el.tagName === "CANVAS") {
    const canvas = el as HTMLCanvasElement;
    const url = typeof canvas.toDataURL === "function" ? canvas.toDataURL("image/png") : "";
    return url ? { kind: "img", src: url, alt: title } : null;
  }
  const svg = (el.tagName === "svg" ? el : el.closest?.("svg")) as SVGElement | null;
  if (svg) {
    // 图标不是图：antd 图标 / 按钮里的 svg 一律跳过
    if (svg.closest?.(".anticon") || svg.closest?.("button") || svg.closest?.("a")) return null;
    const box = svg.getBoundingClientRect?.();
    if (box && (box.width < ZOOM_MIN_SVG_PX || box.height < ZOOM_MIN_SVG_PX * 0.5)) return null;
    // 流程图：mermaid 的标签是 <foreignObject> 里的 HTML，序列化成 `<img>` 再缩放会**文字错位**
    // （实测 276px 宽的原图放大到 1308px 后标签跑出方块）。有源码就回弹窗里重新渲染同一份。
    const host = svg.closest?.("[data-figure-mermaid]") as HTMLElement | null;
    const code = host?.dataset?.figureMermaid ?? "";
    if (code) return { kind: "mermaid", src: "", alt: title, code };
    const src = svgToDataUrl(ensureSvgIntrinsicSize(new XMLSerializer().serializeToString(svg), box));
    return src ? { kind: "svg", src, alt: title } : null;
  }
  if (el.tagName === "IMG") {
    const img = el as HTMLImageElement;
    const src = img.currentSrc || img.src || "";
    return src ? { kind: "img", src, alt: img.alt || title } : null;
  }
  return null;
}
