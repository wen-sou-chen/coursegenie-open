/**
 * 任务 80 前端断言：「不含配图」这类入口的**文案与口径统一**（按钮 / 卡片 / 筛选条 / 导出说明）。
 *
 * 起因（用户 2026-09-22 原话）：「在章节生成那个页面，有一个一键生成本章正文加习题，不含配图，
 * 既然它允许生成文章的正文加习题，为什么又不含配图」。DSH 只读查清：
 * ① 那个按钮走旧管线 `POST /api/content/generate`，产出「纯文本正文 + 习题」——**一张图都没有**
 *    （连概念图 / 数据图 / 流程图也没有），「不含配图」只说了三分之一；
 * ② 「配图」两种含义混用：权威口径（`PreviewStep` 的 tooltip）= **位图**（image.py + IMAGE_MODEL），
 *    但 `lecture.py` 把概念图也叫「自动配图」；统计口径以 `lecture.py:945` 为准
 *    （images = AI 位图、diagrams = 概念图、charts = 数据图）；
 * ③ 全库 `kind="image"` = 0 条 ⇒ 位图配图从没生成过（2026-09-08「讲义零 AI 位图、全矢量」口径）。
 *
 * ⇒ 三类名字只在 `src/lib/figureTerms.ts` 定义一处，组件从这里读；这里跑**源码级**接线断言 +
 *   变异自检，专门防「后端没变、界面又说反话」这类回退。
 *
 * 运行：`node scripts/check-wording-image-vs-diagram.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import {
  FIGURE_TERM, PLAIN_TEXT_BUTTON_LABEL, PLAIN_TEXT_BUTTON_TIP, PLAIN_TEXT_DONE,
} from "../src/lib/figureTerms.ts";

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = join(here, "..", "src");
const read = (rel: string) =>
  readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");
/** 扫「界面文案里还有没有旧说法」时要跳过注释（本文件的文档注释里引用了用户原话）。 */
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ---------------- A. 唯一出处：figureTerms.ts（三类名字 + 按钮文案 + 完成提示） ---------------- */
function auditTerms(src: string): string[] {
  const bad: string[] = [];
  const one = (name: string) => src.match(new RegExp(`export const ${name} = "([^"]*)"`))?.[1] ?? "";
  const field = (name: string) => src.match(new RegExp(`${name}: "([^"]*)"`))?.[1] ?? "";
  const label = one("PLAIN_TEXT_BUTTON_LABEL");
  const done = one("PLAIN_TEXT_DONE");
  const image = field("image");
  const imageClosed = field("imageClosed");
  const diagram = field("diagram");
  const chart = field("chart");
  if (!image || !imageClosed || !diagram || !chart) {
    bad.push("figureTerms 缺三类名字（image / imageClosed / diagram / chart）");
  }
  if (new Set([image, diagram, chart]).size !== 3) {
    bad.push(`三类名字有重复，同屏分不清：${image} / ${diagram} / ${chart}`);
  }
  if (diagram === "配图" || image === "配图") bad.push(`三类名字又退回含糊的「配图」：${diagram}`);
  if (!label) bad.push("figureTerms 里找不到 PLAIN_TEXT_BUTTON_LABEL");
  if (/不含配图/.test(label)) bad.push(`按钮文案还在说「不含配图」：${label}`);
  if (/配图/.test(label)) bad.push(`按钮文案还带「配图」字样：${label}`);
  if (!/纯文本/.test(label)) bad.push(`按钮文案没说清产出是纯文本正文：${label}`);
  if (!/习题/.test(label)) bad.push(`按钮文案没说清含习题：${label}`);
  if (/不含配图/.test(stripComments(src))) bad.push("figureTerms 的代码里又出现「不含配图」");
  if (!done) bad.push("figureTerms 里找不到 PLAIN_TEXT_DONE");
  if (!/纯文本正文/.test(done) || !/不含图/.test(done)) {
    bad.push(`完成提示分不出走的是哪条管线（要含「纯文本正文」+「不含图」）：${done}`);
  }
  if (!/完整讲义/.test(done)) bad.push("完成提示没告诉用户图文混排该走「完整讲义」");
  const tip = src.match(/PLAIN_TEXT_BUTTON_TIP = \[([\s\S]*?)\] as const;/)?.[1] ?? "";
  if (!tip) bad.push("figureTerms 里找不到 PLAIN_TEXT_BUTTON_TIP（Tooltip 三段没了）");
  else {
    if (!/不含任何图/.test(tip)) bad.push("Tooltip 第①段没说「不含任何图」");
    if (!/完整讲义/.test(tip)) bad.push("Tooltip 第②段没指向「完整讲义」");
    if (!/2026-09-08/.test(tip) || !/位图配图/.test(tip)) {
      bad.push("Tooltip 第③段没写位图配图口径（2026-09-08 关闭）");
    }
  }
  return bad;
}

const terms = read("src/lib/figureTerms.ts");
failures.push(...auditTerms(terms));
check(FIGURE_TERM.image === "位图配图", `FIGURE_TERM.image 不是「位图配图」：${FIGURE_TERM.image}`);
check(FIGURE_TERM.diagram === "概念图", `FIGURE_TERM.diagram 不是「概念图」：${FIGURE_TERM.diagram}`);
check(FIGURE_TERM.chart === "数据图", `FIGURE_TERM.chart 不是「数据图」：${FIGURE_TERM.chart}`);
check(FIGURE_TERM.imageClosed === "位图配图（已按口径关闭）", `imageClosed 不对：${FIGURE_TERM.imageClosed}`);
check(PLAIN_TEXT_BUTTON_TIP.length === 3, `Tooltip 不是三段：${PLAIN_TEXT_BUTTON_TIP.length}`);
check(!PLAIN_TEXT_DONE.includes("整章内容已生成"), "完成提示还是旧的「整章内容已生成」");

/* ---------------- B. 按钮与卡片接线（WorkbenchStep.tsx） ---------------- */
function auditWorkbench(src: string): string[] {
  const bad: string[] = [];
  if (!/\{PLAIN_TEXT_BUTTON_LABEL\}/.test(src)) {
    bad.push("旧管线按钮没有用 PLAIN_TEXT_BUTTON_LABEL（文案又写死成一处）");
  }
  if (/一键生成本章正文\+习题/.test(src)) bad.push("旧字面量「一键生成本章正文+习题」还在（按钮没换名）");
  if (/不含配图/.test(src)) bad.push("WorkbenchStep 里还有「不含配图」");
  if (!/PLAIN_TEXT_BUTTON_TIP\.map\(/.test(src)) {
    bad.push("按钮没有把 Tooltip 三段接到界面上（PLAIN_TEXT_BUTTON_TIP 没人用）");
  }
  if (!/message\.success\(PLAIN_TEXT_DONE\)/.test(src)) bad.push("生成完成提示没走 PLAIN_TEXT_DONE");
  if (/整章内容已生成/.test(src)) bad.push("还有旧的「整章内容已生成」提示");
  if (!/图文混排：正文、概念图、流程图、随堂练习一次生成/.test(src)) {
    bad.push("「完整讲义」卡片没改成「正文、概念图、流程图、随堂练习」");
  }
  if (/图文混排：正文、配图、流程图/.test(src)) bad.push("「完整讲义」卡片还写着「配图」");
  if (!/diagram: FIGURE_TERM\.diagram/.test(src)) bad.push("KIND_LABEL 的 diagram 没走 FIGURE_TERM.diagram");
  if (!/image: FIGURE_TERM\.image/.test(src)) bad.push("KIND_LABEL 的 image 没走 FIGURE_TERM.image");
  if (!/chart: FIGURE_TERM\.chart/.test(src)) bad.push("KIND_LABEL 的 chart 没走 FIGURE_TERM.chart");
  // 不改行为：点按钮仍走旧接口，请求体一字不动
  if (!/api\.generateContent\(\{ course_id: courseId, node_ids: ids, tone: "正式", include_quiz: true \}\)/.test(src)) {
    bad.push("旧管线按钮的请求体变了（include_quiz: true 不许动）");
  }
  if (!/onClick=\{\(\) => genWholeChapter\.mutate\(nodeIds\)\}/.test(src)) {
    bad.push("旧管线按钮的点击入口被改了");
  }
  if (!/本节位图配图（\$\{images\.length\}）/.test(src)) bad.push("「本节配图」区块标题没改成「位图配图」");
  if (!/还没有本节\$\{FIGURE_TERM\.image\}/.test(src)) bad.push("位图配图空态文案没走 FIGURE_TERM.image");
  return bad;
}

/* ---------------- C. 预览页（筛选条 / 区块标题 / tooltip） ---------------- */
function auditPreview(src: string): string[] {
  const bad: string[] = [];
  // 精确到那个静态 Tag（注释里也引用了这句话，别让注释把断言喂饱）
  if (!/(?<!位图)配图 已关闭\s*<\/Tag>/.test(src)) {
    bad.push("预览页「配图 已关闭」静态说明被改了（口径要求原样保留）");
  }
  if (!/位图配图（旧称「配图 \/ 拍照图」）按既定策略关闭/.test(src)) {
    bad.push("ImageClosedTag 的 tooltip 没把「配图 = 位图配图」说清");
  }
  if (!/label: FIGURE_TERM\.image/.test(src)) bad.push("筛选条 image 标签没走 FIGURE_TERM.image");
  if (!/label: FIGURE_TERM\.chart/.test(src)) bad.push("筛选条 chart 标签没走 FIGURE_TERM.chart");
  if (!/label: FIGURE_TERM\.diagram/.test(src)) bad.push("筛选条 diagram 标签没走 FIGURE_TERM.diagram");
  if (!/title=\{FIGURE_TERM\.image\}/.test(src)) bad.push("位图配图区块标题没走 FIGURE_TERM.image");
  if (!/title=\{FIGURE_TERM\.chart\}/.test(src)) bad.push("数据图区块标题没走 FIGURE_TERM.chart");
  if (!/数据图 \$\{totalChart\}/.test(src)) bad.push("逐章审阅的分段 chip 还是旧名（应为「数据图 N」）");
  if (/label="数据图"[^>]*tooltip="点一下 = 下面只看「图表」/.test(src)) {
    bad.push("「数据图」chip 的 tooltip 还写着「图表」");
  }
  if (!/image: `\$\{FIGURE_TERM\.imageClosed\}：改用概念图/.test(src)) {
    bad.push("image 的 0 条说明没走 FIGURE_TERM.imageClosed");
  }
  return bad;
}

/* ---------------- D. 其它落点：导出说明 / kind 映射表 ---------------- */
function auditNote(src: string): string[] {
  const bad: string[] = [];
  if (!/无讲义自动拼接正文 \/ 概念图 \/ 数据图 \/ 流程图/.test(src)) {
    bad.push("导出说明没改成「正文 / 概念图 / 数据图 / 流程图」");
  }
  if (/正文\/流程图\/图表\/配图/.test(src)) bad.push("导出说明还留着「正文/流程图/图表/配图」");
  if (/assets 配图/.test(src)) bad.push("ZIP 说明还把章节 assets 里的图叫「配图」");
  return bad;
}

/** 映射表里「概念图」不许再叫「配图」，也统一到 `expected` 那种写法。 */
function auditDiagramLabel(name: string, src: string, expected: RegExp, legacy: RegExp): string[] {
  const bad: string[] = [];
  if (legacy.test(src)) bad.push(`${name}: 又把概念图标成「配图」了`);
  if (!expected.test(src)) bad.push(`${name}: diagram 标签不是「概念图」（口径漂了）`);
  return bad;
}

const workbench = read("src/components/steps/WorkbenchStep.tsx");
const preview = read("src/components/steps/PreviewStep.tsx");
const note = read("src/components/steps/NoteStep.tsx");
const outline = read("src/components/OutlinePanel.tsx");
const usageBadge = read("src/components/UsageBadge.tsx");
const usagePanel = read("src/components/UsagePanel.tsx");
const deck = read("src/components/PptDeckBlocks.tsx");
const modal = read("src/components/FigureZoomModal.tsx");

failures.push(...auditWorkbench(workbench));
failures.push(...auditPreview(preview));
failures.push(...auditNote(note));
failures.push(...auditDiagramLabel("OutlinePanel", outline, /diagram:\s*"概念图"/, /diagram:\s*"配图"/));
failures.push(...auditDiagramLabel("UsageBadge", usageBadge, /diagram:\s*"概念图"/, /diagram:\s*"配图"/));
failures.push(...auditDiagramLabel("UsagePanel", usagePanel, /diagram:\s*"概念图"/, /diagram:\s*"配图"/));
if (!/image:\s*"位图配图"/.test(outline)) failures.push("OutlinePanel: image 还叫「图片」（应与「位图配图」统一）");
if (/chart:\s*"图表"/.test(usageBadge) || /chart:\s*"图表"/.test(usagePanel)) {
  failures.push("用量面板的 chart 还叫「图表」（应与「数据图」统一）");
}
if (/配图/.test(deck)) failures.push("PptDeckBlocks 里出现「配图」（课件积木只说「图 / 幻灯片」）");
const modalPeitu = (modal.match(/配图/g) ?? []).length;
const modalQualified = (modal.match(/位图配图/g) ?? []).length;
if (modalPeitu !== modalQualified) {
  failures.push(`FigureZoomModal 里有 ${modalPeitu - modalQualified} 处单独一个「配图」（应为「位图配图」）`);
}

/* ---------------- E. 全站扫一遍：旧说法不许再出现 ---------------- */
function srcFiles(dir: string): string[] {
  const out: string[] = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...srcFiles(p));
    else if (/\.tsx?$/.test(e.name)) out.push(p);
  }
  return out;
}
const stillSaysNoImage: string[] = [];
const stillCallsDiagramPeitu: string[] = [];
for (const abs of srcFiles(srcRoot)) {
  const body = stripComments(readFileSync(abs, "utf8").replace(/\r+\n/g, "\n"));
  const rel = relative(join(here, ".."), abs).replace(/\\/g, "/");
  if (/不含配图/.test(body)) stillSaysNoImage.push(rel);
  if (/diagram:\s*["']配图["']/.test(body)) stillCallsDiagramPeitu.push(rel);
}
check(stillSaysNoImage.length === 0, `还有文件在说「不含配图」：${stillSaysNoImage.join("、")}`);
check(stillCallsDiagramPeitu.length === 0, `还有文件把概念图标成「配图」：${stillCallsDiagramPeitu.join("、")}`);

/* ---------------- F. 判定自检：改回旧说法 / 摘掉接线 ⇒ 必须变红 ---------------- */
const mutations: [string, string, string, (src: string) => string[]][] = [
  ["把按钮文案改回「不含配图」",
    terms, terms.replace('export const PLAIN_TEXT_BUTTON_LABEL = "生成本章纯文本正文 + 习题";',
      'export const PLAIN_TEXT_BUTTON_LABEL = "一键生成本章正文+习题（不含配图）";'), auditTerms],
  ["把完成提示改回「整章内容已生成」",
    terms, terms.replace('export const PLAIN_TEXT_DONE = "已生成本章纯文本正文 + 习题（不含图）；要图文混排请用「完整讲义」";',
      'export const PLAIN_TEXT_DONE = "整章内容已生成";'), auditTerms],
  ["把 Tooltip 第③段（位图口径）删掉",
    terms, terms.replace(/  "③ AI 位图配图已按既定口径关闭[^"]*",\n/, ""), auditTerms],
  ["把三类名字里的 diagram 退回「配图」",
    terms, terms.replace('diagram: "概念图",', 'diagram: "配图",'), auditTerms],
  ["把按钮里的常量换回旧字面量",
    workbench, workbench.replace("{PLAIN_TEXT_BUTTON_LABEL}", "一键生成本章正文+习题（不含配图）"), auditWorkbench],
  ["把「完整讲义」卡片文案改回「配图」",
    workbench, workbench.replace("图文混排：正文、概念图、流程图、随堂练习一次生成",
      "图文混排：正文、配图、流程图、随堂练习一次生成"), auditWorkbench],
  ["把完成提示换成旧文案",
    workbench, workbench.replace("message.success(PLAIN_TEXT_DONE)", 'message.success("整章内容已生成")'), auditWorkbench],
  ["把旧管线的 include_quiz 改成 false（行为漂了）",
    workbench, workbench.replace("tone: \"正式\", include_quiz: true", "tone: \"正式\", include_quiz: false"), auditWorkbench],
  ["把预览页「配图 已关闭」说明改字",
    preview, preview.replace("        配图 已关闭\n", "        位图配图 已关闭\n"), auditPreview],
  ["把 OutlinePanel 的概念图标签退回「配图」",
    outline, outline.replace('diagram: "概念图",', 'diagram: "配图",'),
    (m) => auditDiagramLabel("OutlinePanel", m, /diagram:\s*"概念图"/, /diagram:\s*"配图"/)],
  ["把导出说明改回「正文/流程图/图表/配图」",
    note, note.replace("无讲义自动拼接正文 / 概念图 / 数据图 / 流程图", "无讲义自动拼接正文/流程图/图表/配图"), auditNote],
];
for (const [name, base, mutated, run] of mutations) {
  if (mutated === base) {
    failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
  } else if (run(mutated).length === 0) {
    failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  }
}

if (failures.length > 0) {
  console.error("前端「位图配图 / 概念图 / 数据图」口径断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端「不含配图」文案与口径断言通过："
  + `旧管线按钮叫「${PLAIN_TEXT_BUTTON_LABEL}」+ 三段 Tooltip（纯文本正文 / 完整讲义 / 位图配图 2026-09-08 关闭）；`
  + `三类名字一处定义（${FIGURE_TERM.image} / ${FIGURE_TERM.diagram} / ${FIGURE_TERM.chart}，含关闭态「${FIGURE_TERM.imageClosed}」）；`
  + "完成提示能区分管线、旧接口（include_quiz: true）与点击入口一字未动；"
  + `全站 0 处「不含配图」、0 处把概念图标成「配图」；${mutations.length} 条变异自检全红`);
