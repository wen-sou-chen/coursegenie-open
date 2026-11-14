/**
 * 任务 64 前端口径断言：正文里已有同数据表格时，「数据卡」（chart_table）不再渲染。
 *
 * 起因：讲义正文只有 1 张表，却还 inline 引用了一张同数据的数据卡 ⇒ 同一张表在课程预览页 /
 * 分享页渲染两次（交付 HTML 里也一样，后端同时修）。
 *
 * 判定用例集读的是**共享正本** `backend/tests/fixtures/task64_chart_table_cases.json`
 * —— 后端 `tests/test_md_tables.py` 读同一份。任何一侧改了归一化口径，这里与那边会同时红，
 * 这就是「前后端同一套判定」的机器证据（别只改一边）。
 *
 * 另外做源码形状断言：`MdWithFlow` 必须真的调用 `isDuplicateTable`，且在 `chartSrcs`
 * （矢量图优先）之后判断 —— 防「改组件时把去重删掉」这类静默回归。
 *
 * 运行：`node scripts/check-chart-table-dedup.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cellCovers, isDuplicateTable, normalizeCell, parseTables } from "../src/lib/mdTables.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const failures: string[] = [];

interface Case {
  name: string;
  card_id: string;
  body: string[];
  card: string[];
  duplicate: boolean;
  why: string;
}

const fixture = JSON.parse(
  readFileSync(join(repo, "backend", "tests", "fixtures", "task64_chart_table_cases.json"), "utf8"),
) as { cases: Case[] };

for (const item of fixture.cases) {
  const body = item.body.join("\n");
  const card = item.card.join("\n");
  if (!body.includes(`chart://${item.card_id}`)) {
    failures.push(`${item.name}：用例正文必须真的引用这张卡（否则不真实）`);
    continue;
  }
  const got = isDuplicateTable(card, parseTables(body));
  if (got !== item.duplicate) {
    failures.push(`${item.name}：期望 duplicate=${item.duplicate}，实际 ${got} —— ${item.why}`);
  }
}

// 归一化口径抽查（与后端 md_tables.normalize_cell 的用例一一对应）
const norms: Array<[string, string]> = [
  [" **信任 崩塌** ", "信任崩塌"],
  ["客服问答（来源:《X》第 3 段）", "客服问答"],
  ["客服问答(来源: X #3)", "客服问答"],
  ["`40℃`", "40℃"],
  ["客服问答（含多轮）", "客服问答（含多轮）"],
];
for (const [input, expect] of norms) {
  const got = normalizeCell(input);
  if (got !== expect) failures.push(`normalizeCell(${JSON.stringify(input)})：期望 ${expect}，实际 ${got}`);
}

// 逐格「正文能否代表卡片」的两条规则（与后端 test_cell_and_table_covers_rules 对应）
const covers: Array<[string, string, boolean]> = [
  ["五法全上（反思校对+引用锚定+手动RAG+温度锁零+多模型交叉）", "五法全上", true],
  ["五法全上", "五法全上（含手动 RAG）", false],
  ["高危（最严重）", "高危（严重）", false],
  ["医疗诊断/合规报告", "医疗诊断/合规", false],
  ["客服问答（来源:《X》第 3 段）", "客服问答", true],
];
for (const [bodyCell, cardCell, expect] of covers) {
  const got = cellCovers(bodyCell, cardCell);
  if (got !== expect) {
    failures.push(`cellCovers(${JSON.stringify(bodyCell)}, ${JSON.stringify(cardCell)})：期望 ${expect}，实际 ${got}`);
  }
}

// 表格解析：代码块里的竖线不算表；`| : |` 这种没有 `-` 的分隔行不算表
if (parseTables("```text\n| a | b |\n|---|---|\n| 1 | 2 |\n```").length !== 0) {
  failures.push("代码块里的竖线被当成了表格（会误删数据卡）");
}
if (parseTables("| a | b |\n| : | : |\n| 1 | 2 |").length !== 0) {
  failures.push("没有 '-' 的分隔行被当成了表格（会误删数据卡）");
}
const one = parseTables("前言\n\n|a|b|\n|---|---|\n|1|2|\n\n后记\n");
if (one.length !== 1 || one[0].headers.join(",") !== "a,b" || one[0].rows.length !== 1) {
  failures.push(`表格解析结果不对：${JSON.stringify(one)}`);
}

// 源码形状：MdWithFlow 必须真的用上这套判定（图优先 → 表格 → 去重跳过）
const component = readFileSync(join(here, "..", "src", "components", "MdWithFlow.tsx"), "utf8");
const shapeChecks: Array<[string, string]> = [
  ['from "../lib/mdTables"', "MdWithFlow 没引用共享的判定模块"],
  ["parseTables(md)", "MdWithFlow 没按正文解析表格（只按原始 md 判定）"],
  ["isDuplicateTable(table, bodyTables)", "MdWithFlow 没有调用去重判定 —— 同一张表会渲染两次"],
];
for (const [needle, why] of shapeChecks) {
  if (!component.includes(needle)) failures.push(why);
}
const srcIdx = component.indexOf("const src = chartSrcs?.[id]");
const dupIdx = component.indexOf("isDuplicateTable(table, bodyTables)");
if (srcIdx === -1 || dupIdx === -1 || srcIdx > dupIdx) {
  failures.push("chartSrcs（矢量图）必须先于去重判定：有图的卡照旧显示图");
}

if (failures.length > 0) {
  console.error("前端「数据卡去重」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
const dups = fixture.cases.filter((c) => c.duplicate).length;
console.log(
  `前端数据卡去重断言通过：${fixture.cases.length} 条共享用例（重复 ${dups} / 不重复 ${fixture.cases.length - dups}）` +
  "两侧口径一致，MdWithFlow 图优先 + 表格去重就位",
);
