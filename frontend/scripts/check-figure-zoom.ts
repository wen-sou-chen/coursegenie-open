/**
 * 任务 76 前端断言：第 4 步「课程预览」**点图就弹出来看** + 「只看概念图」不用手动滚。
 *
 * 起因（用户 2026-09-21 22:5x 真机原话）：「点击那些图还是不行……我建议你点击概念图的时候就把
 * 概念图那一块单独弹出来，现在有的他不出来有的是排在 PPT 的后面，总之不能够马上看到。」
 * DSH 只读定位：概念图在「只看概念图」列表里是**裸 `<img>`、没有 onClick**（点了完全没反应），
 * 讲义正文里内嵌的概念图同样不可点；概念图区块只在按了「概念图 N」之后才出现、且排在
 * 讲义/流程图/图表之后（真机实测：点完「概念图 44」页面**纹丝不动**，第一处图在视口下方
 * 6815px 处）。
 *
 * 这里跑**纯逻辑**（`src/lib/figureZoom.ts`，与组件共用同一份口径）+ **源码接线**断言；
 * 真机上的截图 / rect / scrollY / 三种关法由 DSH 复验，数字与证据见 `backend/docs/task76-preview-zoom.md`。
 *
 * 运行：`node scripts/check-figure-zoom.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  FIGURE_ZOOM_BODY_HEIGHT, FIGURE_ZOOM_FIT_MAX_HEIGHT, FIGURE_ZOOM_WIDTH, ZOOM_FIT_OPTIONS,
  ZOOM_MIN_SVG_PX, ensureSvgIntrinsicSize, figureZoomHeading, svgToDataUrl, zoomImageStyle,
} from "../src/lib/figureZoom.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ---------------- A. 纯逻辑：口径只有一处 ---------------- */
check(FIGURE_ZOOM_WIDTH === "92vw", `弹窗宽度口径不是 92vw：${FIGURE_ZOOM_WIDTH}`);
check(FIGURE_ZOOM_BODY_HEIGHT === "78vh", `内容区高度口径不是 78vh：${FIGURE_ZOOM_BODY_HEIGHT}`);
check(FIGURE_ZOOM_FIT_MAX_HEIGHT === "72vh", `「适应窗口」高度上限不是 72vh：${FIGURE_ZOOM_FIT_MAX_HEIGHT}`);
check(ZOOM_MIN_SVG_PX >= 40, "没有「多小算图标」的下限（antd 图标的 svg 会被当成图弹出来）");

const fitStyle = zoomImageStyle("fit");
const actualStyle = zoomImageStyle("actual");
check(fitStyle.width === "100%" && fitStyle.maxHeight === FIGURE_ZOOM_FIT_MAX_HEIGHT,
  `「适应窗口」没有放大到弹窗宽度 / 没限高：${JSON.stringify(fitStyle)}`);
check(fitStyle.objectFit === "contain", "「适应窗口」没有 objectFit: contain（长图会变形）");
check(actualStyle.maxWidth === "none" && actualStyle.maxHeight === "none",
  `「1:1」不是原尺寸：${JSON.stringify(actualStyle)}`);
check(JSON.stringify(fitStyle) !== JSON.stringify(actualStyle), "两种看法（适应窗口 / 1:1）其实是一样的");

check(ZOOM_FIT_OPTIONS.length === 2, `看法切换不是两项：${ZOOM_FIT_OPTIONS.length}`);
check(ZOOM_FIT_OPTIONS[0].label === "适应窗口" && ZOOM_FIT_OPTIONS[1].label === "1:1",
  `看法切换文案不对：${ZOOM_FIT_OPTIONS.map((o) => o.label).join("/")}`);

const head1 = figureZoomHeading("概念图 A", "1.1 某节");
check(head1.title === "概念图 A", `弹窗标题没带图名：${head1.title}`);
check(head1.subtitle === "所属章节：1.1 某节", `弹窗没带所属章节：${head1.subtitle}`);
check(figureZoomHeading("", "1.1 某节").title === "图", "没标题时没有兜底（空标题会显示成空白）");
check(figureZoomHeading("X", "").subtitle.includes("整课"), "没章节时没有兜底文案");

const dataUrl = svgToDataUrl('<svg width="10" height="10"><rect/></svg>');
check(dataUrl.startsWith("data:image/svg+xml"), `svg 没转成 data URL：${dataUrl.slice(0, 32)}`);
check(!dataUrl.includes("<"), "svg 没做转义（含裸 `<` 会截断 data URL）");

// mermaid 的真实形状：width="100%"、没有 height、靠 viewBox 定比例（实测塞进 <img> 会缩成 96×150）
const mermaidLike = '<svg id="mermaid-1" width="100%" xmlns="http://www.w3.org/2000/svg" style="max-width: 276px;" viewBox="0 0 276 486"><style>x</style></svg>';
const fixed = ensureSvgIntrinsicSize(mermaidLike, { width: 276, height: 430 });
check(/\swidth="276"/.test(fixed) && /\sheight="486"/.test(fixed),
  `没按 viewBox 补固有尺寸：${fixed.slice(0, 90)}`);
check((fixed.match(/\swidth="/g) ?? []).length === 1, "补尺寸时留下了重复的 width 属性");
const already = '<svg width="120" height="80" viewBox="0 0 10 10"></svg>';
check(ensureSvgIntrinsicSize(already) === already, "已有真实尺寸的 svg 被改动了");

/* ---------------- B. 组件接线：点图就弹 / 不用滚 / 三种关法 ---------------- */
function auditZoom(preview: string, modal: string, lib: string): string[] {
  const bad: string[] = [];
  // ① 弹窗：× / Esc / 点遮罩三法都在（antd 默认就有，只禁止把它们关掉）
  if (!/<Modal/.test(modal)) bad.push("FigureZoomModal 没有用 Modal");
  if (!/onCancel=\{onClose\}/.test(modal)) bad.push("弹窗没有把 onCancel 接到 onClose");
  if (/keyboard=\{false\}/.test(modal)) bad.push("弹窗把 Esc 关掉了（keyboard={false}）");
  if (/maskClosable=\{false\}/.test(modal)) bad.push("弹窗把点遮罩关掉了（maskClosable={false}）");
  // ② 打开就有反馈：标题/章节可定位 + 图未到位时给 loading，不拿旧图冒充
  if (!/data-testid="figure-zoom-title"/.test(modal)) bad.push("弹窗标题没有可定位的挂点");
  if (!/data-testid="figure-zoom-chapter"/.test(modal)) bad.push("弹窗所属章节没有可定位的挂点");
  if (!/data-testid="figure-zoom-img"/.test(modal)) bad.push("弹窗里的图没有可定位的挂点");
  if (!/<Spin/.test(modal)) bad.push("弹窗没有 loading 态（打开就有反馈那条）");
  if (!/onLoad=/.test(modal)) bad.push("弹窗的图没有 onLoad（loading 永远不消失 / 拿旧图冒充）");
  if (!/onLoad=\{\(\) => setLoaded\(true\)\}/.test(modal)) {
    bad.push("弹窗的 onLoad 没有把 loading 收掉");
  }
  if (!/setFit\("fit"\)/.test(modal)) {
    bad.push("换一张图时看法没有回到「适应窗口」（上一张按过 1:1 的话，下一张就是原样的小图）");
  }
  // ③ 三处图共用一个入口：img / canvas / 内联 svg
  if (!/toDataURL\("image\/png"\)/.test(lib)) bad.push("数据图（canvas）没有走 toDataURL 取像素");
  if (!/XMLSerializer/.test(lib)) bad.push("流程图（内联 svg）没有走序列化");
  if (!/kind: "mermaid"/.test(lib)) {
    bad.push("流程图没有走「回弹窗重新渲染源码」这条路（mermaid 的 HTML 标签序列化成 <img> 会错位）");
  }
  if (!/data-figure-mermaid/.test(lib)) bad.push("没有读 FlowDiagram 挂在 DOM 上的源码（放大拿不回流程图）");
  if (!/<FlowDiagram code=/.test(modal)) bad.push("弹窗里没有真渲染流程图");
  if (!/className="figure-zoom-live"/.test(modal)) {
    bad.push("弹窗里的流程图没有挂 .figure-zoom-live（mermaid 自带 max-width ⇒ 跟页面上一样小）");
  }
  if (!/open=\{open && \(showImg \|\| isMermaid\)\}/.test(modal)) {
    bad.push("弹窗的 open 条件只看 src ⇒ 点流程图（没有 src）打不开");
  }
  if (!/el\.tagName === "IMG"/.test(lib)) bad.push("img 没有被识别成可放大的图");
  if (!/closest\?\.\("\.anticon"\)/.test(lib)) bad.push("没有排除 antd 图标（点图标也会弹图）");
  if (!/maxWidth: "none"/.test(lib) || !/maxHeight: "none"/.test(lib)) {
    bad.push("「1:1」不是原尺寸（口径常量被动过）");
  }
  // ④ 预览页接线：点一下弹出 + 挂载 + 所属章节
  if (!/<FigureZoomModal/.test(preview)) bad.push("PreviewStep 没有挂 FigureZoomModal");
  if (!/chapter=\{zoom\?\.chapter/.test(preview)) bad.push("PreviewStep 没把所属章节传给弹窗");
  if (!/resolveZoomTarget\(e\.target\)/.test(preview)) bad.push("图没有接 onClick → resolveZoomTarget（点了没反应）");
  if (!/onZoom=\{openZoom\}/.test(preview)) bad.push("NodeReview 没接 onZoom");
  // ⑤ 不用滚：概念图区块有锚点，且筛选后自动滚过去（复用任务 74 的 scrollTo ⇒ 返回按钮照旧可用）
  if (!/data-diagram-anchor=\{diagrams\.length > 0 \? "1" : undefined\}/.test(preview)) {
    bad.push("概念图区块没有锚点（或没有图时也设锚 ⇒ 会白跳）");
  }
  if (!/querySelector<HTMLDivElement>\("\[data-diagram-anchor\]"\)/.test(preview)) {
    bad.push("没有「点概念图 N 后自动滚到区块」的实现");
  }
  if (!/scrollTo\(\{ current: anchor\(\) \}\)/.test(preview)) {
    bad.push("自动滚动没有复用任务 74 的 scrollTo（返回原位会失效）");
  }
  // ⑥ 复用的返回按钮还在，且没有另造一套
  if (!/← 返回预览（回到刚才的位置）/.test(preview)) bad.push("任务 74 的返回按钮不见了");
  const backIcons = (preview.match(/<ArrowLeftOutlined \/>/g) ?? []).length;
  if (backIcons !== 1) bad.push(`返回按钮图标用了 ${backIcons} 处（另造了新按钮？）`);
  // ⑦ 章容器的误导空态收敛（用户说的「有的他不出来」）
  if (!/filter === "all" \|\| member\.key\.includes\("\."\)/.test(preview)) {
    bad.push("章容器卡在筛选态下仍然渲染（会出现「本节暂无概念图」而图就在下面的小节里）");
  }
  // ⑧ 图自己的标题（数据图 / 流程图没有 alt，靠 data-figure-title 出标题）
  if (!/data-figure-title=/.test(preview)) bad.push("图卡片上没有 data-figure-title（弹窗标题会只剩「图」）");
  return bad;
}

const preview = read("src/components/steps/PreviewStep.tsx");
const modal = read("src/components/FigureZoomModal.tsx");
const lib = read("src/lib/figureZoom.ts");
failures.push(...auditZoom(preview, modal, lib));
const css = read("src/app.css");
check(/\.figure-zoom-live svg\s*\{[^}]*max-width: 100% !important/.test(css),
  "app.css 里没有放开弹窗流程图的 max-width（流程图放大后还是原尺寸）");

/* ---------------- C. 只加「看」，没动生成 / 配色 / 去重口径 ---------------- */
for (const f of ["ChartRenderer.tsx", "FlowDiagram.tsx"]) {
  const src = read(`src/components/${f}`);
  if (/figureZoom|onZoom|FigureZoomModal/.test(src)) {
    failures.push(`${f} 被改了（本轮只加「看」的能力，不许碰生成 / 配色 / 去重口径）`);
  }
}
const chart = read("src/components/ChartRenderer.tsx");
check(/paletteHex\(colors, i\)/.test(chart) && /alignChartData\(labels, series\)/.test(chart),
  "ChartRenderer 的配色兜底 / 长度对齐口径被动过");
const flow = read("src/components/FlowDiagram.tsx");
check(/mermaid\s*\n?\s*\.run\(\{ nodes: \[pre\] \}\)/.test(flow) || /run\(\{ nodes: \[pre\] \}\)/.test(flow),
  "FlowDiagram 的 mermaid 渲染口径被动过");
check(/securityLevel: "strict"/.test(flow) && /htmlLabels: true/.test(flow) && /theme: "default"/.test(flow),
  "FlowDiagram 的 mermaid 初始化口径被动过（只允许加一个给放大用的源码挂点）");
check(/data-figure-mermaid=\{code\}/.test(flow),
  "FlowDiagram 没有把源码挂到 DOM 上（流程图放大就只能序列化 svg ⇒ 标签错位）");

/* ---------------- D. 反证据：把接线摘掉 / 把口径改回去 ⇒ 必须变红 ---------------- */
const MUTATIONS: [string, () => string[]][] = [
  ["摘掉「点图就弹」的 onClick",
    () => auditZoom(preview.replace(/onClick=\{\(e\) => \{\n\s+const target = resolveZoomTarget\(e\.target\);\n[\s\S]*?\n\s+\}\}/, ""), modal, lib)],
  ["摘掉弹窗挂载", () => auditZoom(preview.replace(/<FigureZoomModal[\s\S]*?\/>/, ""), modal, lib)],
  ["摘掉概念图区块的锚点", () => auditZoom(preview.replace('data-diagram-anchor={diagrams.length > 0 ? "1" : undefined}', ""), modal, lib)],
  ["把自动滚动从 scrollTo 换成自己滚（返回原位就废了）",
    () => auditZoom(preview.replace("scrollTo({ current: anchor() });", "anchor()?.scrollIntoView();"), modal, lib)],
  ["把章容器的筛选过滤去掉（「本节暂无概念图」又回来了）",
    () => auditZoom(preview.replace(/\n\s+\.filter\(\(member\) => filter === "all" \|\| member\.key\.includes\("\."\)\n\s+\|\| rowsOf\(member\)\.some\(\(r\) => rowInFilter\(r, filter\)\)\)/, ""), modal, lib)],
  ["把 Esc 关掉（keyboard={false}）", () => auditZoom(preview, modal.replace("onCancel={onClose}", "onCancel={onClose}\n      keyboard={false}"), lib)],
  ["把点遮罩关掉（maskClosable={false}）", () => auditZoom(preview, modal.replace("onCancel={onClose}", "onCancel={onClose}\n      maskClosable={false}"), lib)],
  ["摘掉图的 onLoad（loading 永远不消失）", () => auditZoom(preview, modal.replace("onLoad={() => setLoaded(true)}", "onLoad={() => {}}"), lib)],
  ["换图时不再回到「适应窗口」", () => auditZoom(preview, modal.replace('setFit("fit");', ""), lib)],
  ["把图标也当成图（去掉 .anticon 排除）", () => auditZoom(preview, modal, lib.replace('svg.closest?.(".anticon") || ', ""))],
  ["把流程图改回「序列化 svg」（标签会错位）", () => auditZoom(preview, modal, lib.replace('{ kind: "mermaid", src: "", alt: title, code }', '{ kind: "svg", src: svgToDataUrl(""), alt: title }'))],
  ["把弹窗 open 条件改回只看 src（点流程图打不开）", () => auditZoom(preview, modal.replace("open={open && (showImg || isMermaid)}", "open={open && Boolean(src)}"), lib)],
  ["摘掉弹窗流程图的 .figure-zoom-live（又变成原尺寸）", () => auditZoom(preview, modal.replace('className="figure-zoom-live"', ""), lib)],
  ["把 1:1 改得跟适应窗口一样", () => auditZoom(preview, modal, lib.replace('? { width: "auto", height: "auto", maxWidth: "none", maxHeight: "none" }', '? { width: "100%", height: "auto", maxHeight: FIGURE_ZOOM_FIT_MAX_HEIGHT, objectFit: "contain" }'))],
];
for (const [name, run] of MUTATIONS) {
  const bad = run();
  if (bad.length === 0) failures.push(`自检失败：变异「${name}」后判定仍然通过`);
}
// 纯逻辑的反证据：改成「两种看法一样」必须被第 ② 组断言抓到
if (zoomImageStyle("actual").maxHeight === FIGURE_ZOOM_FIT_MAX_HEIGHT) {
  failures.push("自检失败：1:1 与适应窗口同高时判定仍然通过");
}

if (failures.length > 0) {
  console.error("前端「预览页点图放大 + 只看概念图不用滚」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端点图放大断言通过：概念图（列表 + 讲义内嵌）/ 数据图（canvas）/ 配图（img）/ 流程图（mermaid 原样重渲染）"
  + "统一走 resolveZoomTarget → FigureZoomModal（92vw × 78vh、适应窗口 / 1:1、× / Esc / 遮罩三法、打开即有 loading）；"
  + "概念图区块带 data-diagram-anchor、点「概念图 N」自动滚过去且复用任务 74 的 scrollTo（返回原位照旧）；"
  + "章容器卡在筛选态下不再显示误导性的「本节暂无概念图」；ChartRenderer / FlowDiagram 的生成 / 配色 / 去重口径一字未动");
