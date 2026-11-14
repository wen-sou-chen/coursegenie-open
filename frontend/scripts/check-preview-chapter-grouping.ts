/**
 * 任务 113 前端断言：第 4 步「课程预览」的**编排** —— 默认「按章节聚合」，「按类型」原样保留为第二种视图。
 *
 * 用户 2026-09-29 睡前提的原话：「看 AI 幻觉那节课第 4 部分的课程预览，我觉得在预览的安排上有问题：
 * **先列出了全部的课件，下面才是正文**，容易让人看得一头雾水，**编排上要考虑每个章节的内容应该放在一起**。」
 *
 * 改造前：**按「类型」分区**堆（完整讲义 → 正文 → 图 → 流程图 → 数据图 → 作业，课件在另一个区块），
 * 一节课的「讲义 + 正文 + 图 + 课件 + 习题」被拆到相距很远的区块里。
 * 改造后：默认**按章节聚合** —— 一个知识点一张卡，卡内固定顺序
 * 「讲义 → 正文 → 位图配图 → 概念图 → 流程图 → 数据图 → 习题」（同类内保持原顺序），
 * **本章课件就地放进本章卡**（默认折叠 + 「N 页」角标）；课程级只有一块「整课课件」。
 * 位图配图按既定口径关闭、恒为 0 条 ⇒ 只在真有内容时才出现（一条都不丢）。
 *
 * 这里跑的是**纯逻辑**（`src/lib/previewGrouping.ts`，与组件共用同一份口径）+ **源码接线**断言；
 * 真机上的 DOM / 折叠 / 记忆由 DSH 复验，数字与证据见 `backend/docs/task113-preview-chapter-grouping.md`。
 *
 * 运行：`node scripts/check-preview-chapter-grouping.ts`
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { FIGURE_TERM } from "../src/lib/figureTerms.ts";
import {
  CHAPTER_DECK_KEY, CHAPTER_PANEL_ORDER, DEFAULT_PREVIEW_VIEW, DECK_FOLDED_BY_DEFAULT, PANEL_TITLE,
  PREVIEW_VIEW_KEY, PREVIEW_VIEW_OPTIONS, TYPE_VIEW_ORDER, chapterCardPlan, countOf,
  deckPanelActiveKeys, deckPanelTitle, defaultOpenPanels, describeChapterPlan, normalizePreviewView,
  pointCounts, readPreviewView, sumPointCounts, visiblePanels, writePreviewView,
} from "../src/lib/previewGrouping.ts";
import type { PanelKey, PointCounts, PreviewView, ViewStore } from "../src/lib/previewGrouping.ts";
import type { ContentRow } from "../src/types";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");
const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };
const eq = (got: unknown, want: unknown, msg: string) =>
  check(JSON.stringify(got) === JSON.stringify(want), `${msg}（实际 ${JSON.stringify(got)}，应为 ${JSON.stringify(want)}）`);

/* ============================ ① 造真实形状的语料 ============================ */

const MOCK_MERMAID = "```mermaid\ngraph TD\n  A --> B\n```";

function row(id: string, kind: string, title: string, body: Record<string, unknown> = {}, meta: Record<string, unknown> = {}): ContentRow {
  return { id, kind, title, node_id: id, node_key: "1.1", body, meta } as unknown as ContentRow;
}

/** 知识点 1.1：讲义 2（其中 1 篇里内嵌 1 段 mermaid）/ 正文 1 / 概念图 2 / 数据图 2 / 作业 3。 */
const POINT_11: ContentRow[] = [
  row("l1", "lecture", "1.1 讲义 A", { markdown: `正文…\n${MOCK_MERMAID}\n更多正文` }),
  row("l2", "lecture", "1.1 讲义 B", { markdown: "正文…" }),
  row("t1", "text", "1.1 旧管线正文", { markdown: "纯文本正文" }),
  row("d1", "diagram", "1.1 概念图甲", {}, {}),
  row("d2", "diagram", "1.1 概念图乙", {}, {}),
  row("c1", "chart_bar", "1.1 数据图甲", {}, {}),
  row("c2", "chart_line", "1.1 数据图乙", {}, {}),
  row("q1", "quiz", "1.1 作业 1", { question: "?" }, {}),
  row("q2", "quiz", "1.1 作业 2", { question: "?" }, {}),
  row("q3", "quiz", "1.1 作业 3", { question: "?" }, {}),
];
/** 知识点 1.2：没有讲义（老数据/旧管线），只有正文 + 流程图条目 + 作业。 */
const POINT_12: ContentRow[] = [
  row("t2", "text", "1.2 正文", { markdown: `x\n${MOCK_MERMAID}` }),
  row("f1", "flowchart", "1.2 库里的流程图", { mermaid: "graph LR\n  A --> B" }),
  row("q4", "quiz", "1.2 作业", { question: "?" }),
];

const counts11 = pointCounts(POINT_11);
const counts12 = pointCounts(POINT_12);
const ALL_ROWS = [...POINT_11, ...POINT_12];

/* ============================ ② 计数口径（两种视图同一个出处） ============================ */

eq(counts11, { lecture: 2, text: 1, image: 0, diagram: 2, flowchart: 1, chart: 2, quiz: 3, other: 0 }, "知识点 1.1 的分类计数不对");
eq(counts12, { lecture: 0, text: 1, image: 0, diagram: 0, flowchart: 2, chart: 0, quiz: 1, other: 0 }, "知识点 1.2 的分类计数不对（流程图 = 内嵌 mermaid 1 段 + 库里条目 1 条）");
eq(sumPointCounts([counts11, counts12]), pointCounts(ALL_ROWS), "逐点相加 != 全量统计（有内容没归到卡里 / 被算重）");
check(countOf(counts11, "chart") === 2 && countOf(counts11, "diagram") === 2, "单类取数不对");

/* ============================ ③ 默认视图 = 按章节（反证据就是改它） ============================ */

eq(DEFAULT_PREVIEW_VIEW, "chapter", "默认视图不是「按章节」");
eq(normalizePreviewView(null), "chapter", "没存过时没有回落到默认视图");
eq(normalizePreviewView(undefined), "chapter", "没存过时没有回落到默认视图");
eq(normalizePreviewView("legacy-view"), "chapter", "脏值没有回落到默认视图");
eq(normalizePreviewView("type"), "type", "「按类型」存进去读不回来");
eq(normalizePreviewView("chapter"), "chapter", "「按章节」读不回来");

function memoryStore(seed?: string): ViewStore & { get(key: string): string | null } {
  const data = new Map<string, string>();
  if (seed !== undefined) data.set(PREVIEW_VIEW_KEY, seed);
  return {
    getItem: (key) => (data.has(key) ? String(data.get(key)) : null),
    setItem: (key, value) => { data.set(key, value); },
    get: (key) => (data.has(key) ? String(data.get(key)) : null),
  };
}

/* ============================ ④ 切换记忆（localStorage） ============================ */

const emptyStore = memoryStore();
eq(readPreviewView(emptyStore), "chapter", "从没切过时不是「按章节」");
eq(PREVIEW_VIEW_KEY, "coursegenie.preview.view", "存储键变了（老选择会丢）");
const mem = memoryStore();
writePreviewView("type", mem);
eq(mem.get(PREVIEW_VIEW_KEY), "type", "切换没有写进 localStorage");
eq(readPreviewView(mem), "type", "记不住上次的视图（刷新又回默认）");
writePreviewView("chapter", mem);
eq(readPreviewView(mem), "chapter", "切回「按章节」没记住");
const legacy = memoryStore("上次乱写的值");
eq(readPreviewView(legacy), "chapter", "老/脏值没有回落到默认视图");
const blocked: ViewStore = {
  getItem: () => { throw new Error("localStorage 被禁用"); },
  setItem: () => { throw new Error("localStorage 被禁用"); },
};
eq(readPreviewView(blocked), "chapter", "存储被禁用时读取会抛（隐私模式白屏）");
writePreviewView("type", blocked);   // 不许抛
eq(PREVIEW_VIEW_OPTIONS.map((o) => o.value), ["chapter", "type"], "开关不是两档：按章节 / 按类型");
eq(PREVIEW_VIEW_OPTIONS.map((o) => o.label), ["按章节", "按类型"], "开关文案不对");

/* ============================ ⑤ 章内聚合的固定顺序 ============================ */

const chapterPanels11 = visiblePanels("chapter", counts11, "all");
eq(chapterPanels11, ["lecture", "text", "diagram", "flowchart", "chart", "quiz"], "「按章节」卡内顺序不对");
eq(visiblePanels("chapter", counts12, "all"), ["lecture", "text", "flowchart", "quiz"], "没有讲义的点卡：「讲义」恒在（空态告诉你去哪生成），其余只列真有内容的");
const withImage = { ...counts11, image: 3 };
eq(visiblePanels("chapter", withImage, "all"), ["lecture", "text", "image", "diagram", "flowchart", "chart", "quiz"], "真有位图配图时被丢掉了（一条都不许丢）");
for (const key of CHAPTER_PANEL_ORDER) {
  const at = CHAPTER_PANEL_ORDER.indexOf(key);
  if (at >= 0 && !["image"].includes(key)) {
    check(chapterPanels11.includes(key) === (countOf(counts11, key) > 0), `「${key}」在章节视图里的可见性不对（0 条的不该占位）`);
  }
}
eq(visiblePanels("chapter", counts11, "quiz"), ["quiz"], "「只看作业」时章内还混着别的类型");
eq(visiblePanels("chapter", counts11, "diagram"), ["diagram"], "「只看概念图」时章内还混着别的类型");

/* ============================ ⑥ 类型视图：改造前的分区原样（一个块都不丢） ============================ */

eq(visiblePanels("type", counts12, "all"), ["lecture", "text", "image", "flowchart", "chart", "quiz"], "「按类型」视图的区块集合/顺序变了（空态区块也不许丢）");
eq(visiblePanels("type", counts11, "all"), ["lecture"], "有讲义时按类型的既有规则变了（正文/图/习题都在讲义里）");
eq(visiblePanels("type", counts11, "diagram"), ["diagram"], "「只看概念图」在按类型视图里没有单列概念图");
eq(visiblePanels("type", counts11, "chart"), ["chart"], "「只看数据图」在按类型视图里不对");
for (const key of TYPE_VIEW_ORDER) {
  check(TYPE_VIEW_ORDER.indexOf(key) >= 0, `按类型视图缺了「${key}」这一块`);
}
/** 「按章节」把有讲义时被折叠掉的图 / 作业也收进卡里 —— 这正是用户说的「每个章节的内容应该放在一起」。 */
for (const key of ["diagram", "flowchart", "chart", "quiz"] as PanelKey[]) {
  check(chapterPanels11.includes(key), `「按章节」卡里没把「${key}」收进来（又得往回翻）`);
}

/* ============================ ⑦ 折叠默认值（讲义展开 / 其余折叠 + 展开全部 / 收起全部） ============================ */

eq(defaultOpenPanels(["lecture", "text", "chart"], "all", "auto"), ["lecture"], "默认策略不是「讲义展开、其余折叠」");
eq(defaultOpenPanels(["lecture", "text", "chart"], "chart", "auto"), ["chart"], "筛选态没有自动展开命中的那一类");
eq(defaultOpenPanels(["lecture", "text", "chart"], "all", "all"), ["lecture", "text", "chart"], "「展开全部」没有全开");
eq(defaultOpenPanels(["lecture", "text", "chart"], "all", "none"), [], "「收起全部」没有全收");
eq(DECK_FOLDED_BY_DEFAULT, true, "课件列表不是默认折叠（一节课 13 份课件会铺成一面墙）");
eq(deckPanelActiveKeys("deck-body-x"), [], "课件正文折叠块默认是展开的");
// 任务 114：默认折叠不变，但**跳转时**要能自己打开（用户点「课件」链接没反应＝目标折着）
eq(deckPanelActiveKeys("deck-body-x", true), ["deck-body-x"], "跳转时没有把课件折叠块打开");
eq(deckPanelTitle(12), "本章课件 · 12 页", "课件折叠行的角标文案不对");
eq(deckPanelTitle(null), "本章课件 · 未生成", "没生成课件时的文案不对");
eq(deckPanelTitle(undefined), "本章课件 · 未生成", "没生成课件时的文案不对");

/* ============================ ⑧ 章卡 = 本章全部东西（同一章不跨卡） ============================ */

const plan1 = chapterCardPlan({
  key: "1",
  label: "第1章 幻觉是什么",
  points: [
    { key: "1.1", label: "1.1 什么是 AI 幻觉", rows: POINT_11 },
    { key: "1.2", label: "1.2 幻觉为什么发生", rows: POINT_12 },
  ],
  deckPages: 12,
});
eq(plan1.points.map((p) => p.key), ["1.1", "1.2"], "章卡里没有把本章全部知识点收进来");
eq(plan1.deck.key, CHAPTER_DECK_KEY, "章卡里没有「本章课件」这一块");
eq(plan1.deck.title, "本章课件 · 12 页", "章卡里的课件角标不对");
eq(plan1.points[0].panels, chapterPanels11, "章卡里知识点的面板与单独算的不一致");
eq(sumPointCounts(plan1.points.map((p) => p.counts)), pointCounts([...POINT_11, ...POINT_12]), "章卡里的内容有丢 / 有重");
const tree1 = describeChapterPlan(plan1).join("\n");
check(tree1.includes("第1章 幻觉是什么（章卡）"), "结构树没有章卡这一层");
check(tree1.includes("1.1 什么是 AI 幻觉") && tree1.includes("1.2 幻觉为什么发生"), "结构树里缺了知识点");
check(tree1.includes("完整讲义（图文混排） 2") && tree1.includes("数据图 2") && tree1.includes("作业与习题 3"), "结构树里每类的条目数不对");
check(tree1.includes("本章课件 · 12 页（章级，默认折叠）"), "结构树里章级课件没有就地放在章卡里");
// 任务 114：课件是**章卡第一块**（用户：「每一章节的课件，应该放在这个章节开头的部分」）
const treeLines = describeChapterPlan(plan1);
const iDeckLine = treeLines.findIndex((l) => l.includes("本章课件 · 12 页"));
const iPointLine = treeLines.findIndex((l) => l.includes("1.1 什么是 AI 幻觉"));
check(iDeckLine > 0 && iPointLine > iDeckLine, "结构树里课件不在章卡最前面");
/** 第二章只有一篇讲义、没课件（另一门课的「未生成」形态）。 */
const POINT_21 = [row("l21", "lecture", "2.1 方法", { markdown: "正文…" })];
const plan2 = chapterCardPlan({ key: "2", label: "第2章 怎么减少幻觉", points: [{ key: "2.1", label: "2.1 方法", rows: POINT_21 }] });
check(plan2.deck.title === "本章课件 · 未生成", "没生成课件的章卡角标不对");
/** 两种视图的每类条数必须一致（这里两边的口径本来就同一个函数，钉住它别被改漂）。 */
const typeSide = pointCounts([...ALL_ROWS, ...POINT_21]);
const chapterSide = sumPointCounts([...plan1.points, ...plan2.points].map((p) => p.counts));
eq(chapterSide, typeSide, "「按章节」与「按类型」两边的每类条数不一致");
eq(
  { 讲义: chapterSide.lecture, 正文: chapterSide.text, 概念图: chapterSide.diagram, 流程图: chapterSide.flowchart, 数据图: chapterSide.chart, 作业: chapterSide.quiz },
  { 讲义: typeSide.lecture, 正文: typeSide.text, 概念图: typeSide.diagram, 流程图: typeSide.flowchart, 数据图: typeSide.chart, 作业: typeSide.quiz },
  "两边的计数对照不一致",
);

/* ============================ ⑨ 面板命名不另造（一律沿用既有口径） ============================ */

eq(PANEL_TITLE.diagram, FIGURE_TERM.diagram, "概念图的写法不是既有口径");
eq(PANEL_TITLE.chart, FIGURE_TERM.chart, "数据图的写法不是既有口径");
eq(PANEL_TITLE.image, FIGURE_TERM.image, "位图配图的写法不是既有口径");
eq(PANEL_TITLE.lecture, "完整讲义（图文混排）", "讲义的写法不是既有口径");
eq(PANEL_TITLE.flowchart, "流程图", "流程图的写法不是既有口径");
eq(PANEL_TITLE.quiz, "作业与习题", "作业的写法不是既有口径");

/* ============================ ⑩ 组件接线（源码级） ============================ */

const preview = read("src/components/steps/PreviewStep.tsx");
const lib = read("src/lib/previewGrouping.ts");

function auditPreview(src: string): string[] {
  const bad: string[] = [];
  // 开关：顶部 Segmented 两档 + 默认按章节 + 切换即落盘 + 多看一眼的 tooltip
  if (!/data-testid="preview-view-switch"/.test(src)) bad.push("顶部没有「按章节 / 按类型」开关");
  if (!/options=\{PREVIEW_VIEW_OPTIONS\}/.test(src)) bad.push("开关的两档不是共用 PREVIEW_VIEW_OPTIONS");
  if (!/switchView\(normalizePreviewView\(v\)\)/.test(src)) bad.push("开关切换没有走 normalizePreviewView（脏值会漏）");
  if (!/writePreviewView\(next\)/.test(src)) bad.push("切换后没有写进 localStorage（记不住选择）");
  if (!/useState<PreviewView>\(\(\) => readPreviewView\(\)\)/.test(src)) bad.push("初始视图没有从 localStorage 读（刷新回默认）");
  // 默认视图 = 按章节聚合：每章一张卡 → 知识点卡（卡内各类型折叠面板）→ 本章课件就地
  if (!/const reviewItems = \(view: PreviewView\)/.test(src)) bad.push("没有按视图组织卡片的 reviewItems");
  if (!/const chapterItem = \(ch: PlanNode, view: PreviewView\)/.test(src)) bad.push("没有「一章一张卡」的 chapterItem");
  // 任务 114：章卡内顺序改成「课件第一块 → 知识点卡」（原来课件在最后，用户看不到本章课件）
  if (!/view === "chapter" \? \(\s*<div data-chapter-anchor=\{ch\.key\}>\s*<Space direction="vertical" style=\{\{ width: "100%" \}\} size="small">[\s\S]{0,200}?\{renderDeckRow\(ch, true\)\}\s*\{pointCards\(members, view\)\}/.test(src)) {
    bad.push("本章课件没有就地放进章卡最前面（还在页顶单列 / 排在正文后面？）");
  }
  if (!/const pointCards = \(members: PlanNode\[\], view: PreviewView\)/.test(src)) bad.push("没有知识点卡构造器");
  if (!/defaultActiveKey=\{defaultOpenPanels\(shownPanels, filter, openMode\)\}/.test(src)) {
    bad.push("章内各类型没有用 Collapse（折叠默认策略没接线）");
  }
  if (!/const shownPanels = visiblePanels\(view, counts, filter\);/.test(src)) bad.push("面板可见性/顺序没有走 lib/previewGrouping.visiblePanels");
  if (!/const counts = pointCounts\(rows\);/.test(src)) bad.push("条数没有走 lib/previewGrouping.pointCounts（两视图会各算一套）");
  if (!/setOpenMode\("all"\)/.test(src) || !/setOpenMode\("none"\)/.test(src)) bad.push("没有「展开全部 / 收起全部」");
  if (!/activeKey=\{deckPanelActiveKeys\(bodyKey, deckOpen\)\}/.test(src)) {
    bad.push("章内课件折叠块不是「受控 + 默认折叠」（跳转打不开它）");
  }
  if (!/deckPanelTitle\(deck\?\.slide_count\)/.test(src)) bad.push("章内课件折叠行没有页数角标");
  // 两种视图都要有的东西（一个区块 / 一个入口都不许丢）
  for (const needle of [
    "课件（PPT）", "概念（概念库）", "逐章逐节审阅", "按章节审阅（一章一块）", "整课课件", "审核要点",
    "修改这一节", "↑ 回到课件 / 预览", "← 返回预览（回到刚才的位置）", "回到顶部", "整课课件（课程级）",
  ]) {
    if (!src.includes(needle)) bad.push(`页面上少了既有区块 / 入口：「${needle}」`);
  }
  // 既有交互与断言依赖的属性
  if (!/onZoom=\{openZoom\}/.test(src)) bad.push("NodeReview 没接 onZoom（点图放大丢了）");
  if (!/resolveZoomTarget\(e\.target\)/.test(src)) bad.push("图没有接 onClick → resolveZoomTarget");
  if (!/onClick=\{\(\) => onOpenChapter\(member\.id\)\}/.test(src)) bad.push("「修改这一节」没有接 onOpenChapter");
  if (!/data-figure-title=\{/.test(src)) bad.push("图卡片上的 data-figure-title 丢了（弹窗标题会只剩「图」）");
  if (!/data-diagram-anchor=\{diagrams\.length > 0 \? "1" : undefined\}/.test(src)) bad.push("概念图锚点丢了 / 没有图时也设锚");
  const marks = (src.match(/data-fig-enter=""/g) ?? []).length;
  if (marks !== 5) bad.push(`PreviewStep 里 data-fig-enter 有 ${marks} 处（应为 5 处：图表 / 配图 / 内嵌流程图 / 库流程图 / 概念图）`);
  if (!/filter === "all" \|\| member\.key\.includes\("\."\)/.test(src)) bad.push("任务 76 的章容器筛选过滤被改掉了");
  if (!/本节暂无讲义/.test(src) || !/本节暂无概念图：第 3 步/.test(src) || !/本节暂无流程图（讲义内嵌 0 段）/.test(src)) {
    bad.push("空态文案被删了（两种视图都要保留）");
  }
  // 「只看」筛选与回显：同一份口径，两处落点（顶部 + 审阅区）都还在
  const echoes = (src.match(/<FilterEcho filter=\{filter\} count=\{filterCount\(filter\)\} onClear=\{\(\) => setFilter\("all"\)\} \/>/g) ?? []).length;
  if (echoes !== 2) bad.push(`FilterEcho 落点 ${echoes} 处（应页面顶部 + 审阅区各一处）`);
  const backIcons = (src.match(/<ArrowLeftOutlined \/>/g) ?? []).length;
  if (backIcons !== 1) bad.push(`返回按钮图标用了 ${backIcons} 处（另造了新按钮？）`);
  // 单一出处：面板标题只在 PanelHead / 面板表里写一次
  if (!/title="完整讲义（图文混排）"/.test(src) || !/title="作业与习题"/.test(src)) bad.push("面板标题没有沿用既有名字");
  if (!/chapters\.map\(\(ch\) => renderDeckRow\(ch\)\)/.test(src)) bad.push("按类型视图的课件行把数组下标当成了 folded 参数（folded 会误判）");
  return bad;
}

function auditLib(src: string): string[] {
  const bad: string[] = [];
  if (!/export const DEFAULT_PREVIEW_VIEW: PreviewView = "chapter";/.test(src)) bad.push("默认视图不是「按章节」");
  if (!/export const CHAPTER_PANEL_ORDER: PanelKey\[\] = \["lecture", "text", "image", "diagram", "flowchart", "chart", "quiz"\];/.test(src)) {
    bad.push("章内固定顺序不是 讲义 → 正文 → 位图配图 → 概念图 → 流程图 → 数据图 → 习题");
  }
  if (!/export const TYPE_VIEW_ORDER: PanelKey\[\] = \["lecture", "text", "image", "flowchart", "chart", "quiz", "diagram"\];/.test(src)) {
    bad.push("「按类型」的区块顺序被改了（不是改造前那套）");
  }
  if (!/export const DECK_FOLDED_BY_DEFAULT = true;/.test(src)) bad.push("课件列表默认折叠被改掉了");
  if (!/if \(kind\.startsWith\("chart_"\)\)/.test(src)) bad.push("数据图不是按 chart_* 前缀统计（会算成 0 条）");
  if (!/flowchartCount\(list\.filter\(\(r\) => r\.kind === "lecture" \|\| r\.kind === "text"\)\)/.test(src)) {
    bad.push("流程图没有按「讲义/正文里的 mermaid 段」统计（口径与既有不一致）");
  }
  if (!/export const PREVIEW_VIEW_KEY = "coursegenie\.preview\.view";/.test(src)) bad.push("存储键变了");
  return bad;
}

failures.push(...auditPreview(preview).map((m) => `PreviewStep.tsx：${m}`));
failures.push(...auditLib(lib).map((m) => `previewGrouping.ts：${m}`));

/* ============================ ⑪ 变异自检：把关键口径改坏 ⇒ 上面的审计必须红 ============================ */

const mutations: [string, string, (src: string) => string[]][] = [
  ["把默认视图改回「按类型」", lib.replace('export const DEFAULT_PREVIEW_VIEW: PreviewView = "chapter";', 'export const DEFAULT_PREVIEW_VIEW: PreviewView = "type";'), auditLib],
  ["把章内顺序改回按类型的老顺序（概念图挪到最后）", lib.replace('["lecture", "text", "image", "diagram", "flowchart", "chart", "quiz"]', '["lecture", "text", "image", "flowchart", "chart", "quiz", "diagram"]'), auditLib],
  ["课件列表改成默认展开", lib.replace("export const DECK_FOLDED_BY_DEFAULT = true;", "export const DECK_FOLDED_BY_DEFAULT = false;"), auditLib],
  ["数据图按 kind === \"chart\" 统计（真实库恒为 0 条）", lib.replace('if (kind.startsWith("chart_")) {', 'if (kind === "chart") {'), auditLib],
  ["把章内折叠面板的默认策略接线摘掉", preview.replace("defaultActiveKey={defaultOpenPanels(shownPanels, filter, openMode)}", "defaultActiveKey={[]}"), auditPreview],
  ["把「本章课件」从章卡里挪走（又变成页顶单列）", preview.replace("        {renderDeckRow(ch, true)}\n", ""), auditPreview],
  ["把顶部「按章节 / 按类型」开关摘掉", preview.replace('data-testid="preview-view-switch"', ""), auditPreview],
  ["把概念图锚点摘掉（点「概念图 N」白跳）", preview.replace('data-diagram-anchor={diagrams.length > 0 ? "1" : undefined}', "data-diagram-anchor={undefined}"), auditPreview],
];
for (const [name, mutated, run] of mutations) {
  if (mutated === (run === auditLib ? lib : preview)) {
    failures.push(`变异「${name}」没改到源码（断言自己失效了）`);
    continue;
  }
  if (run(mutated).length === 0) failures.push(`变异「${name}」应该判红，实际还是绿的（这条口径没被覆盖）`);
}

/* ============================ ⑫ 结论 ============================ */

if (failures.length) {
  console.error("前端预览页「按章节聚合」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端预览页「按章节聚合」断言通过：默认「按章节」（一个知识点一张卡：讲义 → 正文 → 图 → 习题，本章课件就地放章卡、默认折叠 + N 页角标）、「按类型」原样保留为第二视图（区块与空态一个不丢）、两视图每类条数一致、顶部开关两档 + 选择记 localStorage（脏值/禁用回落默认）、章内 Collapse 默认「讲义展开、其余折叠」+ 展开全部 / 收起全部、点图放大 / 章节跳转 / 空态 / data-figure-title / data-fig-enter / 概念图锚点全部保留；8 条变异自检全红");
console.log("结构证据（章 → 卡内各类型 → 条目数）：");
for (const line of describeChapterPlan(plan1)) console.log(`  ${line}`);
for (const line of describeChapterPlan(plan2)) console.log(`  ${line}`);
