/**
 * 任务 74：第 4 步「课程预览」的筛选条 / 跳转返回 / 课件预览 / 版式诊断**纯逻辑**。
 *
 * 放在 lib 而不是组件里，是为了能在门禁里直接跑（`frontend/scripts/check-preview-ux.ts`）——
 * 与 lib/contentCounts.ts 同一套路：口径只有一处，组件只负责画。
 *
 * 起因（用户 2026-09-21 逐条点名，DSH 真机复现）：
 * ① 筛选那一排「有的点不开」、点开只弹一句「只看某某某」、**没有当前筛选回显、没有一键清除**；
 * ② 点「概念 9」跳走以后回不来（实测 scrollY 4 → 8913，页面没有回到顶部 / 浮动按钮）；
 * ③ 课件「预览」点了像没反应（其实内联展开了：滚动 0px 变化、无 loading、首行还是内部术语）；
 * ④ 「版式诊断 / 降级记录」默认全展开（实测 136 行）铺满页面。
 */
// 带扩展名：本文件会被门禁脚本用 Node 直接跑（`check-preview-ux.ts`），Node 的 ESM 解析要求显式后缀。
import { FIGURE_TERM } from "./figureTerms.ts";

export type ReviewFilter =
  | "all" | "lecture" | "text" | "image" | "flowchart" | "chart" | "quiz" | "diagram";
export type ReviewKind = Exclude<ReviewFilter, "all">;

export const FILTER_LABEL: Record<ReviewKind, string> = {
  lecture: "讲义",
  text: "正文",
  image: FIGURE_TERM.image,
  flowchart: "流程图",
  chart: FIGURE_TERM.chart,
  quiz: "作业",
  diagram: FIGURE_TERM.diagram,
};

/** 回显里的量词：流程图的条数其实是「段」，数据图是「张」。 */
export const FILTER_UNIT: Record<ReviewKind, string> = {
  lecture: "篇",
  text: "条",
  image: "张",
  flowchart: "段",
  chart: "张",
  quiz: "道",
  diagram: "处",
};

/**
 * 恒为 0 的筛选项：现代管线只写 kind=lecture（正文都在讲义里）、位图配图按策略关闭。
 * 定稿是**直接隐藏** —— 页面上已有「讲义 27」，再挂一个永远为 0 的「正文」只会误导。
 */
export const HIDDEN_FILTERS: ReviewKind[] = ["text", "image"];

export function isFilterChipHidden(kind: ReviewKind, count: number): boolean {
  return HIDDEN_FILTERS.includes(kind) && count <= 0;
}

/** 「当前只显示：流程图（12 段）」——筛选态回显；filter === "all" 时界面上不显示这条。 */
export function filterEchoText(kind: ReviewKind, count: number): string {
  return `当前只显示：${FILTER_LABEL[kind]}（${count} ${FILTER_UNIT[kind]}）`;
}

/** chip 文案：选中态除了配色再加一个 ✓（不依赖颜色也能看出来现在是哪个筛选）。 */
export function chipLabel(kind: ReviewKind, count: number, active: boolean): string {
  return `${active ? "✓ " : ""}${FILTER_LABEL[kind]} ${count}`;
}

export interface LayoutNoteLike {
  slide?: number;
  kind?: string;
  reason?: string;
}

/** 汇总一行：对外只说「自动调整了几处」，不铺开实现口径（实测修前 136 行默认全展开）。 */
export function layoutNoteSummary(notes: LayoutNoteLike[]): string {
  return `版式自动调整 ${notes.length} 处`;
}

/**
 * 明细改人话（`ppt_layout.py` 的原始文案是「已精简后放回页面」这类实现口径）。
 * 保留数字（保留几条 / 移走几条），去掉术语。
 */
export function humanLayoutNote(note: LayoutNoteLike): string {
  const head = note.slide ? `第 ${note.slide} 页：` : "全局：";
  const reason = String(note.reason ?? "");
  let m = reason.match(/^要点过多（(\d+) 条），已保留前 (\d+) 条/);
  if (m) {
    const moved = Number(m[1]) - Number(m[2]);
    return `${head}要点太多，已保留 ${m[2]} 条、其余 ${moved} 条移到备注`;
  }
  if (/^要点超长（> \d+ 字）/.test(reason)) return `${head}文字太长，已自动精简`;
  if (reason.startsWith("标题超长")) return `${head}标题太长，已自动精简`;
  if (note.kind === "figure-dropped") return `${head}图没能放进这一页，已挪到备注`;
  if (note.kind === "internal-words") return `${head}正文里的内部话术已挪到备注`;
  return `${head}版式已自动调整`;
}

/**
 * 明细行：**对外模式恒为空**（只留汇总一行）；内部模式默认也折叠，展开才给行。
 * ⇒ 定稿「默认状态下明细行数 = 0、汇总行 = 1/章」在这一个函数里钉死。
 */
export function layoutNoteRows(
  notes: LayoutNoteLike[], internal: boolean, expanded: boolean,
): string[] {
  if (!internal || !expanded) return [];
  return notes.map(humanLayoutNote);
}

export interface PreviewLike {
  ok?: boolean;
  url?: string | null;
  enabled?: boolean;
  reason?: string | null;
}

/**
 * 课件预览的首行：对外说人话；`PPT_PREVIEW_PDF=false` 这类后端原话只在内部模式追加。
 * 返回 raw 为空串时，界面不渲染那一行。
 */
export function deckPreviewNotice(
  pdf: PreviewLike | null, internal: boolean,
): { text: string; raw: string } {
  if (pdf?.ok && pdf.url) return { text: "", raw: "" };
  const raw = String(pdf?.reason ?? "");
  return { text: "本机未开启 PDF 预览，下面按页显示文字大纲", raw: internal ? raw : "" };
}

/**
 * 任务 109：网页版课件**打不开**时给用户看的那句话（`DeckPreview.openHtmlPreview` 的 catch）。
 *
 * 起因：入口提级后点它的人会变多，而原先是把 `String(e)` 直接贴到红条上 —— 用户看到的是
 * 「网页版预览打开失败：接口 500：网页版课件渲染失败：Traceback (most recent call last)…」。
 * 口径与 `deckPreviewNotice` 一致：**对外只说人话 + 下一步**，后端原话只在内部模式追加。
 *
 * 分档依据是 `api.ts::requestText` 的真实抛错形态：非 2xx 抛 `new Error(响应体原文)`
 * （FastAPI 的 detail 就在原文里，形如「课件不存在」/「网页版课件渲染失败：…」），
 * 连不上本地服务则是 fetch 自己的 `TypeError: Failed to fetch`。
 */
export function deckHtmlErrorNotice(err: unknown, internal: boolean): { text: string; raw: string } {
  const raw = String((err as { message?: unknown } | null)?.message ?? err ?? "").trim();
  const byKind: [RegExp, string][] = [
    [/401|403|Unauthorized|登录/, "登录状态可能已失效：刷新页面重新登录后再试"],
    [/404|课件不存在|课程不存在/, "这份课件在库里已经没有了（可能已删除或版本已回滚），回上一步重新生成课件"],
    [/渲染失败|HTTP 500/, "服务端渲染这份网页版课件时出错：稍后重试；一直失败就重新生成课件"],
    [/Failed to fetch|NetworkError|ECONNREFUSED|timeout|超时/i, "连不上本地服务（后端可能没启动）：确认服务在运行后重试"],
  ];
  let text = "网页版课件没能打开：稍后重试，或先用「预览」看按页文字大纲";
  for (const [re, human] of byKind) {
    if (re.test(raw)) { text = human; break; }
  }
  return { text, raw: internal ? raw : "" };
}

/** 回到跳转前的位置：夹到 >= 0，浮点取整（浏览器平滑滚动没法精确到 1px，验收给 ±50px 容差）。 */
export function clampScrollTarget(y: number): number {
  return Number.isFinite(y) ? Math.max(0, Math.round(y)) : 0;
}
