/**
 * 任务 113：第 4 步「课程预览」的**编排**口径（纯逻辑，放 lib 以便门禁直接跑）。
 *
 * 用户 2026-09-29 睡前提的原话：「看 AI 幻觉那节课第 4 部分的课程预览，我觉得在预览的安排上有问题：
 * **先列出了全部的课件，下面才是正文**，容易让人看得一头雾水，**编排上要考虑每个章节的内容应该放在一起**。」
 *
 * 改造前：页面**按「类型」分区**堆 —— 完整讲义 → 正文讲解 → 概念图/配图 → 流程图 → 数据图 → 作业，
 * 课件又在另一层另起区块 ⇒ 一节课的「讲义 + 正文 + 图 + 课件 + 习题」被拆到相距很远的区块里。
 * 改造后：**默认「按章节聚合」** —— 以章为主线、一个知识点一张卡，卡内固定顺序
 * 「讲义 → 正文 → 图（概念图 / 流程图 / 数据图）→ 习题」，本章课件就地放进**章卡**里；
 * 「按类型」作为**第二种视图原样保留**（顶部 Segmented 切换，选择记在 localStorage）。
 *
 * 任务 114（用户 2026-09-30 傍晚实测）：「每一章节的课件，**应该放在这个章节开头的部分**……我去点那个课件的链接，
 * **完全没有反应**」⇒ ① 课件块提到**章卡第一块**（章卡 = 课件 + 各知识点卡，见 CHAPTER_CARD_ORDER）；
 * ② 章节头的「课件」标签变成**可点**，点击 = 展开 + 滚到本章课件（见 lib/panelJump.ts）。
 *
 * 与 lib/contentCounts.ts / lib/previewFilters.ts 同一套路：口径只有一处，组件只负责画。
 */
// 带扩展名：本文件会被门禁脚本用 Node 直接跑（`check-preview-chapter-grouping.ts`），Node 的 ESM 解析要求显式后缀。
import { flowchartCount } from "./contentCounts.ts";
import { FIGURE_TERM } from "./figureTerms.ts";
import type { ReviewFilter } from "./previewFilters.ts";
import type { ContentRow } from "../types";

/* ============================ ① 视图开关（默认「按章节」+ 记住选择） ============================ */

export type PreviewView = "chapter" | "type";

/**
 * 视图开关的存储键。**全局一个键、不按课程** —— 这是本轮的选择（DSH 规格允许「按课程或全局都行」）：
 * 编排是「我怎么看这一页」的习惯，不是某一门课的数据；换一门课不该把它切回去。
 */
export const PREVIEW_VIEW_KEY = "coursegenie.preview.view";

/** 默认视图 = **按章节聚合**（用户 2026-09-29 提的那条；反证据就是把它改回 "type" ⇒ 断言必须红）。 */
export const DEFAULT_PREVIEW_VIEW: PreviewView = "chapter";

export const PREVIEW_VIEW_OPTIONS: { value: PreviewView; label: string }[] = [
  { value: "chapter", label: "按章节" },
  { value: "type", label: "按类型" },
];

/** 存进来的值只认这两种，其它（含 null / 脏值）一律回落默认视图。 */
export function normalizePreviewView(raw: unknown): PreviewView {
  return raw === "chapter" || raw === "type" ? raw : DEFAULT_PREVIEW_VIEW;
}

export interface ViewStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** 浏览器里的 localStorage；隐私模式 / 禁用存储时返回 null（不抛）。 */
function browserStore(): ViewStore | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function readPreviewView(store: ViewStore | null = browserStore()): PreviewView {
  if (!store) return DEFAULT_PREVIEW_VIEW;
  try {
    return normalizePreviewView(store.getItem(PREVIEW_VIEW_KEY));
  } catch {
    return DEFAULT_PREVIEW_VIEW;
  }
}

export function writePreviewView(view: PreviewView, store: ViewStore | null = browserStore()): void {
  if (!store) return;
  try {
    store.setItem(PREVIEW_VIEW_KEY, normalizePreviewView(view));
  } catch {
    /* 存不进去也不影响本次会话（下次进来仍是默认视图） */
  }
}

/* ============================ ② 卡内分组的固定顺序 ============================ */

export type PanelKey = "lecture" | "text" | "image" | "flowchart" | "chart" | "quiz" | "diagram";

/** 「按类型」视图里各区块的**既有顺序**（＝改造前页面上的顺序，原样保留，不许动）。 */
export const TYPE_VIEW_ORDER: PanelKey[] = ["lecture", "text", "image", "flowchart", "chart", "quiz", "diagram"];

/**
 * 「按章节」视图里**章卡内各类型的固定顺序**：
 * 讲义 → 正文 → 图（位图配图 → 概念图 → 流程图 → 数据图，同类内保持原顺序）→ 习题。
 * 位图配图按既定口径关闭（真实库恒为 0 条）⇒ 只在真有内容时才出现，但**不许被悄悄丢掉**。
 * （「课件」是章级资源，直接放**章卡**里 —— 见 CHAPTER_DECK_KEY。）
 */
export const CHAPTER_PANEL_ORDER: PanelKey[] = ["lecture", "text", "image", "diagram", "flowchart", "chart", "quiz"];

/** 「图」= 概念图 + 流程图 + 数据图。 */
export const FIGURE_PANELS: PanelKey[] = ["diagram", "flowchart", "chart"];

/** 章卡里承载「本章课件」的那一块（不给「另起一个课件区块」留活口）。 */
export const CHAPTER_DECK_KEY = "deck";

/**
 * 任务 114：**章卡内的块顺序** = 「本章课件」**第一块** → 之后是各知识点卡
 * （知识点卡内部再按上面的 CHAPTER_PANEL_ORDER 排）。顺序在组件里由 JSX 表达
 * （`renderDeckRow(ch, true)` 在 `pointCards(...)` 之前），门禁按源码顺序 + 渲染树两头钉住。
 * 用户原话（2026-09-30 傍晚）：「每一章节的课件，应该放在这个章节开头的部分」。
 */

/** 面板标题：与组件里 Section / Collapse 的标题**同一个出处**（图件名一律走 FIGURE_TERM，不另造命名）。 */
export const PANEL_TITLE: Record<PanelKey, string> = {
  lecture: "完整讲义（图文混排）",
  text: "正文讲解",
  image: FIGURE_TERM.image,
  flowchart: "流程图",
  chart: FIGURE_TERM.chart,
  quiz: "作业与习题",
  diagram: FIGURE_TERM.diagram,
};

/* ============================ ③ 计数（两种视图共用同一口径） ============================ */

export type PointCounts = Record<PanelKey, number> & { other: number };

/** 独立条目 kind → 面板。`chart_*` 归「数据图」；`flowchart` 段数另有口径（见下）。 */
const KIND_TO_PANEL: Record<string, PanelKey> = {
  lecture: "lecture",
  text: "text",
  image: "image",
  diagram: "diagram",
  quiz: "quiz",
};

/**
 * 流程图条数：**内嵌在讲义/正文里的 ```mermaid 段**（既有口径，见 lib/contentCounts.ts）
 * ＋ 独立的 `kind='flowchart'` 条目（真实库里恒为 0，但要算上，免得哪天有了就漏）。
 */
export function flowPanelCount(rows: ContentRow[] | undefined | null): number {
  const list = rows ?? [];
  const embedded = flowchartCount(list.filter((r) => r.kind === "lecture" || r.kind === "text"));
  return embedded + list.filter((r) => r.kind === "flowchart").length;
}

/** 一批条目的分类型条数（两种视图都用它 ⇒ 「两边条数一致」是同一个函数算出来的）。 */
export function pointCounts(rows: ContentRow[] | undefined | null): PointCounts {
  const list = rows ?? [];
  const counts: PointCounts = {
    lecture: 0, text: 0, image: 0, diagram: 0, flowchart: 0, chart: 0, quiz: 0, other: 0,
  };
  for (const row of list) {
    const kind = String(row.kind ?? "");
    if (kind.startsWith("chart_")) {
      counts.chart += 1;
      continue;
    }
    const panel = KIND_TO_PANEL[kind];
    if (panel) counts[panel] += 1;
    else if (kind !== "flowchart") counts.other += 1;
  }
  counts.flowchart = flowPanelCount(list);
  return counts;
}

/** 逐章/逐点计数相加（用来证明「章节聚合的合计 == 全量按类型统计」，一条都不漏）。 */
export function sumPointCounts(list: PointCounts[]): PointCounts {
  const total: PointCounts = {
    lecture: 0, text: 0, image: 0, diagram: 0, flowchart: 0, chart: 0, quiz: 0, other: 0,
  };
  for (const counts of list) {
    for (const key of Object.keys(total) as (keyof PointCounts)[]) total[key] += counts[key] ?? 0;
  }
  return total;
}

/** 某一类的条数（图 = 概念图 + 流程图 + 数据图，供「图」合计显示）。 */
export function countOf(counts: PointCounts, key: PanelKey): number {
  return counts[key] ?? 0;
}

export function figureCount(counts: PointCounts): number {
  return FIGURE_PANELS.reduce((n, key) => n + countOf(counts, key), 0);
}

/* ============================ ④ 面板的可见性与默认展开 ============================ */

/**
 * 卡内要渲染哪些面板（顺序即渲染顺序）。
 *
 * - `type`（「按类型」视图）＝**改造前的既有规则原样**：有讲义时正文/图/习题都在讲义里
 *   （`lecturePriority`，不再单列）；「概念图」只在「只看概念图」时单列。
 * - `chapter`（默认视图）＝章节聚合：`讲义` 恒在（0 条给空态文案），其余**只列真有内容的**
 *   （不然每张卡都挂一排「本节暂无…」，等于把老问题换个方向再来一次）；筛选态下只留命中那一类。
 */
export function visiblePanels(view: PreviewView, counts: PointCounts, filter: ReviewFilter): PanelKey[] {
  if (view === "type") {
    const lecturePriority = counts.lecture > 0 && (filter === "all" || filter === "lecture");
    return TYPE_VIEW_ORDER.filter((key) => {
      if (key === "lecture") return filter === "all" || filter === "lecture";
      if (key === "diagram") return filter === "diagram";
      if (filter !== "all" && filter !== key) return false;
      return !lecturePriority;
    });
  }
  if (filter !== "all") {
    return CHAPTER_PANEL_ORDER.filter((key) => key === filter);
  }
  return CHAPTER_PANEL_ORDER.filter((key) => key === "lecture" || countOf(counts, key) > 0);
}

export type OpenMode = "auto" | "all" | "none";

/**
 * 章内各类型的**默认折叠策略**（DSH 规格建议口径）：**讲义展开、其余折叠**；
 * 有筛选时展开命中的那一类；点了「展开全部 / 收起全部」按点了的来。
 */
export function defaultOpenPanels(keys: PanelKey[], filter: ReviewFilter, mode: OpenMode): PanelKey[] {
  if (mode === "all") return keys.slice();
  if (mode === "none") return [];
  if (filter !== "all") return keys.filter((key) => key === filter);
  return keys.filter((key) => key === "lecture");
}

/* ============================ ⑤ 章节聚合（章卡 = 本章全部东西） ============================ */

export interface ChapterPointInput {
  key: string;
  label: string;
  rows: ContentRow[];
}

export interface ChapterCardInput {
  key: string;
  label: string;
  points: ChapterPointInput[];
  /** 本章课件的页数；未生成传 null / 不传。 */
  deckPages?: number | null;
}

export interface ChapterPlanPoint {
  key: string;
  label: string;
  counts: PointCounts;
  panels: PanelKey[];
}

export interface ChapterPlan {
  key: string;
  label: string;
  points: ChapterPlanPoint[];
  deck: { key: string; title: string; pages: number | null };
}

/**
 * 一张**章卡**里的全部东西：本章各知识点的卡（各自带「讲义 / 正文 / 图 / 习题」面板）
 * ＋ **本章课件**（章级资源，就地放在章卡里，不给「页顶再列一遍全部课件」留口子）。
 */
export function chapterCardPlan(input: ChapterCardInput): ChapterPlan {
  const points = input.points.map((point) => {
    const counts = pointCounts(point.rows);
    return {
      key: point.key,
      label: point.label,
      counts,
      panels: visiblePanels("chapter", counts, "all"),
    };
  });
  return {
    key: input.key,
    label: input.label,
    points,
    deck: {
      key: CHAPTER_DECK_KEY,
      title: deckPanelTitle(input.deckPages),
      pages: input.deckPages ?? null,
    },
  };
}

/** 章卡里「课件」那一行的标题：**带数量角标**（页数；未生成时写清未生成）。 */
export function deckPanelTitle(pages: number | null | undefined): string {
  return pages == null ? "本章课件 · 未生成" : `本章课件 · ${pages} 页`;
}

/** 课件正文折叠面板**默认不展开**（§B4：一节课 13 份课件不许铺成一面墙）。 */
export const DECK_FOLDED_BY_DEFAULT = true;

/**
 * 课件正文折叠面板的展开项：**默认折叠**（空数组 ⇒ 头一行只有「本章课件 · N 页」）；
 * 任务 114：章节头的「课件」标签点下去时由调用方传 open=true 把它打开 —— **默认折叠的规则一字没改**，
 * 只是「跳转要能自己打开」（用户点链接没反应，就是因为目标折叠着、浏览器滚不过去）。
 */
export function deckPanelActiveKeys(key: string, open = false): string[] {
  return DECK_FOLDED_BY_DEFAULT && !open ? [] : [key];
}

/** 章内所有面板的条数合计（用来证明「本章内容都在同一张卡里」且一条不漏）。 */
export function planCounts(plan: ChapterPlan): PointCounts {
  return sumPointCounts(plan.points.map((point) => point.counts));
}

/**
 * 章卡结构的可读树（验收要的「章 → 卡内各类型 → 条目数」证据，门禁脚本直接打印）。
 * 任务 114：**第一行就是课件**（与组件里章卡的真实顺序一致 —— CHAPTER_CARD_ORDER）。
 */
export function describeChapterPlan(plan: ChapterPlan): string[] {
  const entries = [`${plan.deck.title}（章级，默认折叠）`];
  for (const point of plan.points) {
    const panels = point.panels.map((key) => `${PANEL_TITLE[key]} ${countOf(point.counts, key)}`).join(" / ");
    entries.push(`${point.label}｜卡内：${panels || "（无内容）"}`);
  }
  const lines = [`${plan.label}（章卡）`];
  entries.forEach((text, i) => lines.push(`  ${i === entries.length - 1 ? "└" : "├"} ${text}`));
  return lines;
}
