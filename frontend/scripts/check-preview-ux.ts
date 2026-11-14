/**
 * 任务 74 前端断言：第 4 步「课程预览」的交互体检（原任务 73 已并入）。
 *
 * 用户 2026-09-21 逐条点名、DSH 真机只读复现过的真实数字：
 * ① 筛选一排「有的点不开」、点开只弹一句「只看某某某」、**没有回显、没有一键清除**；
 * ② 点「概念 9」跳走后 scrollY **4 → 8913**、回不去（页面无回到顶部、无浮动按钮）；
 * ③ 课件「预览」点了像没反应（内联展开、滚动 0px 变化、无 loading、首行还是 PPT_PREVIEW_PDF）；
 * ④ 「版式诊断 / 降级记录」默认全展开 **136 行**；
 * ⑤ 课件卡片露出未插值占位符 `{deck.slides_per_point}`（第 3、4 页都可见）；
 * ⑥ 质检提示带**别的课**（面点课）的例句（27 篇里 17 篇）。
 *
 * 这里跑**纯逻辑**（`src/lib/previewFilters.ts`，与组件共用同一份口径）+ **源码接线**断言；
 * 真机上的 scrollY / 抽屉 / 截图由 DSH 复验，数字与证据见 `backend/docs/task74-preview-ux.md`。
 *
 * 运行：`node scripts/check-preview-ux.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  chipLabel, clampScrollTarget, deckPreviewNotice, filterEchoText, humanLayoutNote,
  isFilterChipHidden, layoutNoteRows, layoutNoteSummary,
} from "../src/lib/previewFilters.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };
const read = (rel: string) => readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");

/** ① 筛选态回显：回显文本必须带**被点项的名称**与数量（验收「回显 == 被点项」的口径）。 */
check(filterEchoText("flowchart", 12) === "当前只显示：流程图（12 段）",
  `filterEchoText 口径不对：${filterEchoText("flowchart", 12)}`);
check(filterEchoText("chart", 35).includes("数据图") && filterEchoText("chart", 35).includes("35"),
  "回显里没带被点项的名称 / 数量");
check(filterEchoText("quiz", 78).includes("78"), "回显里没带数量");

/** ② 恒为 0 的「正文 0」定稿直接隐藏；讲义等真筛选项不许被误隐藏（0 条仍走灰掉 + 说明）。 */
check(isFilterChipHidden("text", 0), "「正文 0」没有被隐藏");
check(!isFilterChipHidden("text", 5), "真有正文时被误隐藏");
check(isFilterChipHidden("image", 0), "「配图 已关闭」没有被隐藏");
check(!isFilterChipHidden("lecture", 0), "讲义 0 条被误隐藏（原本是灰掉 + 说明去哪生成）");

/** ③ 选中态可区分：选中 chip 带 ✓，未选中不带。 */
check(chipLabel("flowchart", 12, true).startsWith("✓ "), "选中 chip 没有 ✓ 标记");
check(!chipLabel("flowchart", 12, false).startsWith("✓ "), "未选中 chip 也带 ✓");
check(chipLabel("flowchart", 12, true) !== chipLabel("flowchart", 12, false), "选中 / 未选中文案一样");

/** ④ 版式诊断收敛：汇总 1 行、明细默认 0 行、且只在内部模式展开。 */
const NOTES = [
  { slide: 1, reason: "要点过多（6 条），已保留前 4 条，其余 2 条移入备注" },
  { slide: 2, reason: "要点超长（> 22 字），已精简后放回页面" },
  { slide: 3, reason: "标题超长（> 24 字），已精简为「某标题」" },
  { slide: 4, kind: "figure-dropped", reason: "原计划配图 1 张，未能放进页面" },
  { slide: 5, kind: "internal-words", reason: "命中内部话术，已挪进备注" },
  { slide: 6, reason: "要点过多（5 条），已保留前 4 条，其余 1 条移入备注" },
  { slide: 7, reason: "要点超长（> 22 字），已精简后放回页面" },
  { slide: 8, reason: "要点超长（> 22 字），已精简后放回页面" },
  { slide: 9, reason: "要点超长（> 22 字），已精简后放回页面" },
];
check(layoutNoteSummary(NOTES) === "版式自动调整 9 处",
  `汇总行文案不对：${layoutNoteSummary(NOTES)}`);
check(layoutNoteRows(NOTES, false, true).length === 0, "对外模式居然渲染了逐条明细");
check(layoutNoteRows(NOTES, true, false).length === 0, "内部模式默认没折叠（明细仍会铺满页面）");
check(layoutNoteRows(NOTES, true, true).length === NOTES.length, "内部模式展开后明细条数不对");
check(humanLayoutNote(NOTES[0]) === "第 1 页：要点太多，已保留 4 条、其余 2 条移到备注",
  `「要点过多」没改成人话：${humanLayoutNote(NOTES[0])}`);
check(humanLayoutNote(NOTES[1]) === "第 2 页：文字太长，已自动精简",
  `「要点超长」没改成人话：${humanLayoutNote(NOTES[1])}`);
for (const row of layoutNoteRows(NOTES, true, true)) {
  check(!row.includes("已精简后放回页面") && !row.includes("移入备注"),
    `明细里仍有实现口径：${row}`);
}

/** ⑤ 课件预览首行：对外说人话，内部术语只在内部模式。 */
const CLOSED = { ok: false, enabled: false, url: null, reason: "PDF 预览未开启（PPT_PREVIEW_PDF=false），已回退页级大纲" };
const noticeOut = deckPreviewNotice(CLOSED, false);
check(noticeOut.text.includes("本机未开启 PDF 预览"), "首行没改成人话");
check(!noticeOut.text.includes("PPT_PREVIEW_PDF"), "对外模式仍露出 PPT_PREVIEW_PDF");
check(noticeOut.raw === "", "对外模式仍带后端原话");
check(deckPreviewNotice(CLOSED, true).raw.includes("PPT_PREVIEW_PDF"), "内部模式没保留后端原话（排查要用）");
check(deckPreviewNotice({ ok: true, url: "/x.pdf" }, false).text === "", "PDF 可用时不该再显示回退提示");

/** ⑥ 返回原位的落点：夹到 >= 0 且取整（浏览器平滑滚动给 ±50px 容差）。 */
check(clampScrollTarget(-5) === 0 && clampScrollTarget(8913.4) === 8913 && clampScrollTarget(4) === 4,
  "clampScrollTarget 口径不对");

/** ⑦ 组件接线：PreviewStep.tsx */
const preview = read("src/components/steps/PreviewStep.tsx");
const ECHO = /<FilterEcho filter=\{filter\} count=\{filterCount\(filter\)\} onClear=\{\(\) => setFilter\("all"\)\} \/>/g;
const echoes = (preview.match(ECHO) || []).length;
check(echoes === 2, `FilterEcho 落点 ${echoes} 处（应页面顶部 + 筛选条右侧各一处）`);
const CLEAR = /✕ 清除筛选/;
check(CLEAR.test(preview), "回显旁边没有「✕ 清除筛选」按钮");
const NO_TEXT_CHIP = /KindBadge kind="text"/;
check(!NO_TEXT_CHIP.test(preview), "章节头上还挂着「正文 0」chip");
const HIDDEN_CHIP = /isFilterChipHidden\("text", total\("text"\)\)/;
check(HIDDEN_CHIP.test(preview), "分段的「正文」chip 没有走隐藏口径");
const REMEMBER_Y = /setPreviewY\(clampScrollTarget\(window\.scrollY\)\)/;
check(REMEMBER_Y.test(preview), "跳转前没记下 scrollY（回不到刚才的位置）");
const BACK_BTN = /← 返回预览（回到刚才的位置）/;
const TOP_BTN = /回到顶部/;
const CONCEPT_ANCHOR = /↑ 回到课件 \/ 预览/;
check(BACK_BTN.test(preview), "没有「← 返回预览（回到刚才的位置）」浮动按钮");
check(TOP_BTN.test(preview), "没有「回到顶部」浮动按钮");
check(CONCEPT_ANCHOR.test(preview), "概念库区块底部没有「↑ 回到课件 / 预览」锚点");
const INTERNAL = /internal=\{internalMode\}/g;
const internalUses = (preview.match(INTERNAL) || []).length;
check(internalUses === 4, `internal={internalMode} 落点 ${internalUses} 处（LayoutNotes / DeckPreview 各 2）`);
check(/delivery\?\.external === false/.test(preview), "内部模式判定没接 course.meta.delivery.external");
check(/跳到：/.test(preview) && /只看：/.test(preview), "顶部没有把「跳转类」与「筛选类」分组");

/** ⑧ 组件接线：PptDeckBlocks.tsx */
const deck = read("src/components/PptDeckBlocks.tsx");
const PLACEHOLDER = /\$\{deck\.slides_per_point\}/;
check(PLACEHOLDER.test(deck), "figureTag 的插值没修（仍是 {deck.slides_per_point}）");
check(!/每点 \{deck\.slides_per_point\}/.test(deck), "页面上仍会渲染出未插值的占位符");
const DRAWER = /<Drawer/;
check(DRAWER.test(deck), "课件预览没有改成抽屉");
check(/<Spin/.test(deck) && /openDrawer/.test(deck), "抽屉打开时没有 loading 态");
check(/关闭预览/.test(deck), "打开后按钮文案没有变化");
const NOTES_ROWS = /layoutNoteRows\(notes, internal, expanded\)/;
check(NOTES_ROWS.test(deck) && /layoutNoteSummary\(notes\)/.test(deck),
  "LayoutNotes 没有走「汇总 1 行 + 明细按内部模式折叠」的口径");
check(!deck.includes("已精简后放回页面"), "组件里还写着实现口径「已精简后放回页面」");
check(!deck.includes("版式自动精简 / 降级记录（不静默）"), "旧的默认展开诊断块还在");

/** ⑨ 后端：例句必须摘自本篇正文（QC 判据不动）。 */
const qcPy = readFileSync(join(repo, "backend", "app", "services", "lecture_qc.py"), "utf8");
check(!qcPy.includes("当温度低于"), "质检提示里仍有别课（面点课）的例句");
check(/_abstract_example\(candidates\)/.test(qcPy), "actionable 没有摘本篇正文那句抽象描述");
check(qcPy.includes("_ACTIONABLE_NEUTRAL_EXAMPLE"), "缺「摘不出时的中性例子」兜底");
const testPath = join(repo, "backend", "tests", "test_actionable_example.py");
check(existsSync(testPath), "backend/tests/test_actionable_example.py 不存在（例句与判分无关没有回归）");

// 判定自检：把关键接线抹掉后必须变红 —— 否则这些断言等于永远绿灯
if (CLEAR.test(preview.replace(/✕ 清除筛选/g, "清除"))) {
  failures.push("自检失败：抹掉清除按钮后判定仍然通过");
}
if (HIDDEN_CHIP.test(preview.replace(/isFilterChipHidden\("text", total\("text"\)\)/g, "false"))) {
  failures.push("自检失败：抹掉「正文 0」隐藏口径后判定仍然通过");
}
if (REMEMBER_Y.test(preview.replace(/setPreviewY\(clampScrollTarget\(window\.scrollY\)\)/g, "void 0"))) {
  failures.push("自检失败：抹掉「跳转前记 scrollY」后判定仍然通过");
}
if (BACK_BTN.test(preview.replace(/← 返回预览（回到刚才的位置）/g, "返回"))) {
  failures.push("自检失败：抹掉返回按钮文案后判定仍然通过");
}
if (INTERNAL.test(preview.replace(/internal=\{internalMode\}/g, "internal={false}"))) {
  failures.push("自检失败：把内部模式透传改成 false 后判定仍然通过");
}
if (DRAWER.test(deck.replace(/<Drawer/g, "<div"))) {
  failures.push("自检失败：把抽屉换回普通容器后判定仍然通过");
}
if (PLACEHOLDER.test(deck.replace(/\$\{deck\.slides_per_point\}/g, "{deck.slides_per_point}"))) {
  failures.push("自检失败：把插值改回 {…} 后判定仍然通过");
}
if (NOTES_ROWS.test(deck.replace(/layoutNoteRows\(notes, internal, expanded\)/g, "notes.map(String)"))) {
  failures.push("自检失败：把明细改回默认全展开后判定仍然通过");
}
if (/_abstract_example\(candidates\)/.test(qcPy.replace(/_abstract_example\(candidates\)/g, "\"（例如「当温度低于 12℃ 时」）\""))) {
  failures.push("自检失败：把例句改回硬编码后判定仍然通过");
}

if (failures.length > 0) {
  console.error("前端「课程预览」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端课程预览断言通过：筛选回显「当前只显示：X（N）」+ 一键清除 + 选中 ✓ + 「正文 0」隐藏 + 筛选/跳转分组；"
  + "跳转后「← 返回预览（回到刚才的位置）」按 scrollY 回原位、全局「回到顶部」、概念区块有回程锚点；"
  + "课件预览改抽屉（有 loading、首行说人话、PPT_PREVIEW_PDF 只在内部模式）；"
  + "版式诊断汇总 1 行/章、明细对外 0 行且内部默认折叠、措辞去实现口径；"
  + "figureTag 插值已修；后端 actionable 例句摘自本篇正文且不参与判分");