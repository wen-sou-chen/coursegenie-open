/**
 * 任务 109 前端断言：**动效入口说清楚**（入口提级 + 一句话差异常显 + 失败说人话）（**0 LLM**）。
 *
 * 起因（用户 2026-09-27 问「没有看到动画效果，是还没有做进去吗」）：PPT 文件里**一个动画都没有**
 * （`<p:timing>` / `animEffect` / `p:transition` 全 0），动效只做在**网页版课件**里；
 * 而入口是「预览」旁边一个 `size="small"` 的弱按钮、旁边没有一句解释 ⇒ 用户根本没意识到要按它。
 *
 * 钉四组：
 * ① **入口提级（源码级）**：`PptDeckBlocks.tsx::DeckPreview` 的**同一排**里，入口与「预览」同级
 *    （入口 `type="primary"`，或两个都是并列 default），且两个都不再是 `size="small"` /
 *    `type="link"` 那种弱入口；文案自解释（带动效、可放大）；
 * ② **一句话差异常显**：「PPT / PDF 是静态文稿；入场动效只在网页版课件里」必须在**抽屉外面**
 *    （不点任何按钮就能看见），且只在共用积木里写一份（PPT 面板 / 第 4 步预览页不许再写一套）；
 * ③ **失败说人话（行为级）**：`openHtmlPreview` 的 catch 走 `deckHtmlErrorNotice(e, internal)`、
 *    **不许**裸 `String(e)`；并真跑这个纯函数：5 档真实异常都出人话、对外模式不带原始异常
 *    （内部模式才追加原文）、档与档之间的话确实不一样；
 * ④ **变异**：四种「不该发生」的样子（入口退回 `type="link" size="small"` / 删掉说明句 /
 *    失败提示退回裸 `String(e)` / 说明句只写在面板里）必须逐条判红。
 *
 * ⛔ 0 LLM：只读源码 + 跑纯函数（不起后端、不碰真库 / 真 vault / 真 storage）。
 * 运行：`node scripts/check-ppt-html-entry.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deckHtmlErrorNotice } from "../src/lib/previewFilters.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
/** 源码级断言要跳过注释（代码注释里会引用 `String(e)` / 旧文案这类「反例原文」）。 */
const stripComments = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const REL_BLOCKS = "frontend/src/components/PptDeckBlocks.tsx";
const REL_PANEL = "frontend/src/components/PptPanel.tsx";
const REL_STEP = "frontend/src/components/steps/PreviewStep.tsx";

/** 入口文案（与任务 94 的断言同一处：改文案要两边一起改）。 */
const ENTRY_TEXT = "网页版预览（带动效、可放大）";
/** 一句话差异（用户 2026-09-27 的原问题就是「动效在哪」）。 */
const DIFF_NOTE = "PPT / PDF 是静态文稿；入场动效只在网页版课件里";

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/** DeckPreview 里**第一排**课件按钮（抽屉那排带 `style`，不会撞）。 */
function actionRow(src: string): string {
  const at = src.indexOf("export function DeckPreview(");
  const start = src.indexOf("<Space size={8} wrap>", at === -1 ? 0 : at);
  const end = src.indexOf("</Space>", start === -1 ? 0 : start);
  return start === -1 || end === -1 ? "" : src.slice(start, end);
}

/** 含 needle 的那个 `<Button …>…</Button>`（只看属性，不做 JSX 语法解析）。 */
function buttonOf(src: string, needle: string): string {
  const at = src.indexOf(needle);
  if (at === -1) return "";
  const start = src.lastIndexOf("<Button", at);
  const end = src.indexOf("</Button>", at);
  return start === -1 || end === -1 ? "" : src.slice(start, end + "</Button>".length);
}

/** ①②③ 源码级：共用积木里的入口 / 说明句 / 失败提示（传进来的是**去过注释**的源码）。 */
function auditEntry(blocks: string): string[] {
  const bad: string[] = [];
  if (!blocks) return ["取不到 frontend/src/components/PptDeckBlocks.tsx"];
  const row = actionRow(blocks);
  if (!row) return ["DeckPreview 里找不到课件按钮那一排（<Space size={8} wrap>）"];
  if (!row.includes("预览")) bad.push("那一排里没有「预览」按钮（同级就失去参照）");
  if (!row.includes(ENTRY_TEXT)) bad.push(`那一排里没有入口「${ENTRY_TEXT}」`);
  const entry = buttonOf(row, ENTRY_TEXT);
  const preview = buttonOf(row, "关闭预览");
  if (!entry) bad.push("取不到入口按钮的属性（文案可能被挪到按钮外面）");
  if (!preview) bad.push("取不到「预览」按钮的属性");
  if (/size="small"/.test(entry)) bad.push('入口还是 size="small"（弱入口：用户 2026-09-27 就是没看见它）');
  if (/type="link"/.test(entry)) bad.push('入口还是链接式按钮（type="link"）');
  if (/size="small"/.test(preview)) bad.push('「预览」还是 size="small"（与入口不同级）');
  const sameLevel = /type="primary"/.test(entry) || (!/type=/.test(entry) && !/type=/.test(preview));
  if (!sameLevel) bad.push('入口与「预览」不同级（要求入口 type="primary"，或两个都是并列 default）');
  if (!/api\.pptDeckHtml\(deck\.id\)/.test(blocks)) bad.push("入口没有接 api.pptDeckHtml(deck.id)（点了不会有反应）");
  // ② 一句话差异：必须在抽屉**外面**（常显）
  const drawerAt = blocks.indexOf("<Drawer");
  const noteAt = blocks.indexOf(DIFF_NOTE);
  if (noteAt === -1) bad.push(`缺一句话差异「${DIFF_NOTE}」`);
  else if (drawerAt !== -1 && noteAt > drawerAt) bad.push("说明句只在抽屉里（要点开才看得见）");
  // ③ 失败说人话：catch 里走纯函数，不许裸 String(e)
  const fnAt = blocks.indexOf("const openHtmlPreview = async");
  const fnEnd = blocks.indexOf("return (", fnAt === -1 ? 0 : fnAt);
  const fnBody = fnAt === -1 ? "" : blocks.slice(fnAt, fnEnd === -1 ? blocks.length : fnEnd);
  if (!fnBody) bad.push("找不到 openHtmlPreview（网页版入口函数）");
  if (!/setHtmlError\(/.test(fnBody)) bad.push("失败时没有落到 htmlError（红条不显示）");
  if (!/deckHtmlErrorNotice\(e, internal\)/.test(fnBody)) bad.push("失败提示没有走 deckHtmlErrorNotice(e, internal)");
  if (/String\(e\)/.test(fnBody)) bad.push("失败提示还是裸 String(e)（用户会看到一长串异常）");
  if (!/type="error"/.test(blocks)) bad.push('失败没有红条（Alert type="error"）');
  return bad;
}

/** ② 唯一出处：面板 / 预览页不许各写一份入口或说明句。 */
function auditSingleSource(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const panels: [string, string][] = [
    [REL_PANEL, files.get(REL_PANEL) ?? ""],
    [REL_STEP, files.get(REL_STEP) ?? ""],
  ];
  for (const [name, src] of panels) {
    if (src.includes(ENTRY_TEXT)) bad.push(`${name} 里又写了一份入口「${ENTRY_TEXT}」（应只在共用积木里）`);
    if (src.includes(DIFF_NOTE)) bad.push(`${name} 里又写了一份说明句（应只在共用积木里）`);
  }
  return bad;
}

/** ③ 行为级：失败提示必须是「人话 + 下一步」，机器话一律判红。 */
function auditNotice(text: string): string[] {
  if (!text || !text.trim()) return ["失败提示是空的（用户点了没反应）"];
  const machine = text.match(/Error|Traceback|接口|HTTP|https?:|\bat\s+\w|\.tsx?:\d|[{}\[\]]/);
  return machine ? [`失败文案里出现机器话「${machine[0]}」：${text}`] : [];
}

/** ③ 五档真实异常（形态取自 `api.ts::requestText` 的抛错与 fetch 自己的 TypeError）。 */
const REAL_ERRORS: [string, string][] = [
  ["登录态失效", "接口 401：Unauthorized"],
  ["课件不在库里", '{"detail":"课件不存在"}'],
  ["服务端渲染挂了", '{"detail":"网页版课件渲染失败：Traceback (most recent call last)"}'],
  ["后端没起来", "TypeError: Failed to fetch"],
  ["说不清的异常", "TypeError: Cannot read properties of undefined (reading figure)"],
];

const files = new Map<string, string>([
  [REL_BLOCKS, read(REL_BLOCKS)],
  [REL_PANEL, read(REL_PANEL)],
  [REL_STEP, read(REL_STEP)],
]);

const texts: string[] = [];
for (const [label, raw] of REAL_ERRORS) {
  const outside = deckHtmlErrorNotice(new Error(raw), false);
  const inside = deckHtmlErrorNotice(new Error(raw), true);
  failures.push(...auditNotice(outside.text).map((m) => `${label}：${m}`));
  if (outside.raw !== "") failures.push(`${label}：对外模式把原始异常也带出来了（${outside.raw}）`);
  if (inside.raw !== raw) failures.push(`${label}：内部模式没有留下原始异常（排查时看不到原因）`);
  if (inside.text !== outside.text) failures.push(`${label}：人话文案与对外模式不一致`);
  texts.push(outside.text);
}
const levels = new Set(texts).size;
check(levels >= 4, `五档异常的人话文案区分度不足（只有 ${levels} 种）`);

/* ④ 变异：四种「不该发生」的样子必须被判红（否则断言等于没写） */
const base = stripComments(files.get(REL_BLOCKS) ?? "");
failures.push(...auditEntry(base));
failures.push(...auditSingleSource(files));

const row = actionRow(base);
const weakEntry = base
  .replace(row, row.replace('type="primary"', 'type="link" size="small"'))
  .replace(' icon={<EyeOutlined />}', ' size="small" icon={<EyeOutlined />}');
const noteMoved = base.replace(DIFF_NOTE, "网页版课件");
const bareCatch = base.replace("setHtmlError(deckHtmlErrorNotice(e, internal));",
                               "setHtmlError(`网页版预览打开失败：${String(e)}`);");
const cases: [string, string, string[]][] = [
  ['入口退回弱按钮（type="link" size="small"）', weakEntry, auditEntry(weakEntry)],
  ["删掉那句差异说明", noteMoved, auditEntry(noteMoved)],
  ["失败提示退回裸异常（String(e)）", bareCatch, auditEntry(bareCatch)],
  ["说明句只写在面板里（共用积木里没有）", noteMoved,
   auditSingleSource(new Map(files).set(REL_BLOCKS, noteMoved)
     .set(REL_PANEL, (files.get(REL_PANEL) ?? "") + "\n" + DIFF_NOTE + "\n"))],
];
for (const [label, mutated, flagged] of cases) {
  if (mutated === base) failures.push(`变异「${label}」没改到源码（断言可能已失效）`);
  else if (!flagged.length) failures.push(`变异「${label}」没有被检查器判红（断言失效）`);
}
// 行为级变异：把机器话直接当人话喂进去 ⇒ 必须判红
if (!auditNotice("网页版预览打开失败：接口 500：网页版课件渲染失败：Traceback (most recent call last):").length) {
  failures.push("变异「失败文案直接贴机器话」没有被行为断言判红（断言失效）");
}

if (failures.length) {
  console.error(`\n❌ 任务 109 动效入口断言失败 ${failures.length} 条：`);
  for (const f of failures) console.error("   - " + f);
  process.exit(1);
}
console.log(`✅ 任务 109 动效入口断言全过（入口「${ENTRY_TEXT}」与「预览」同级 + `
  + `「${DIFF_NOTE}」常显在抽屉外 + 失败说人话；5 档真实异常 → ${levels} 种人话 · `
  + `${cases.length + 1} 条变异全部判红）`);
