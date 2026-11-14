/**
 * 任务 70 前端断言：大纲自检「落库常显」+ 覆盖检查扩面在界面上真的接上了。
 *
 * 起因（DSH 2026-09-20 走查）：`outline_check()` 算得很细，但结论只作为
 * `POST /api/plan/generate` 的响应返回、OutlinePanel 用 `setCheck(r.check)` 存进组件 state
 * ⇒ 刷新页面 / 换设备就消失，第二次打开这门课再也看不到「当初哪几个素材要点没覆盖」。
 *
 * 后端行为由 `backend/tests/test_outline_selfcheck.py` 断言（落库两次读取、扩面要点数、
 * 建议文案、语义默认关不调 LLM）；这里做**源码级**接线断言，防止「后端修好了、界面还是看不到」。
 *
 * 运行：`node scripts/check-outline-selfcheck.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const failures: string[] = [];

function read(rel: string): string {
  return readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");
}

/** ① 类型 + 接口：后端新字段必须在 types.ts / api.ts 有落点，否则前端读不到。 */
const types = read("src/types.ts");
for (const field of ["check_generated_at", "semantic_enabled", "material_points_by_kind",
                     "material_uncovered_detail"]) {
  if (!new RegExp(`^\\s*${field}\\??:`, "m").test(types)) {
    failures.push(`types.ts 里没有声明 ${field}（后端返回了、前端读不到）`);
  }
}
// 未覆盖明细是内联对象类型：字段名不在行首，单独按字面串查
for (const inlineField of ["kind_label: string", "suggestion: string"]) {
  if (!types.includes(inlineField)) {
    failures.push(`types.ts 的未覆盖明细里没有 ${inlineField}（界面上给不出类型与建议）`);
  }
}
if (!/export interface PlanCheckStored/.test(types)) {
  failures.push("types.ts 没有 PlanCheckStored（落库自检记录的返回形状没声明）");
}
const api = read("src/api.ts");
const GET_CHECK = /getPlanCheck:[\s\S]{0,200}?\/plan\/\$\{courseId\}\/check/;
if (!GET_CHECK.test(api)) {
  failures.push("api.ts 没有 getPlanCheck（界面没有读落库自检的入口）");
}
if (!/generatePlan:[\s\S]{0,400}?semantic\?: boolean/.test(api)) {
  failures.push("api.ts 的 generatePlan 没有 semantic 参数（语义等价开关传不到后端）");
}

/** ② OutlinePanel：自检结论必须来自**落库查询**，不能再存组件 state。 */
const outline = read("src/components/OutlinePanel.tsx");
const QUERY_KEY = /queryKey:\s*\["planCheck",\s*courseId\]/;
const READS_API = /queryFn:\s*\(\)\s*=>\s*api\.getPlanCheck\(courseId\)/;
const READS_STORED = /const check: PlanCheck \| null = planCheck\?\.check \?\? plan\?\.check/;
const KEEPS_STATE = /useState<PlanCheck\s*\|\s*null>/;
const SET_CHECK = /setCheck\(/;
if (!QUERY_KEY.test(outline) || !READS_API.test(outline)) {
  failures.push("OutlinePanel 没有查 GET /plan/{id}/check（刷新后自检仍然看不到）");
}
if (!READS_STORED.test(outline)) {
  failures.push("OutlinePanel 没有优先用落库的自检结论（planCheck?.check）");
}
if (KEEPS_STATE.test(outline) || SET_CHECK.test(outline)) {
  failures.push("OutlinePanel 仍然把自检结论存在组件 state 里（刷新 / 换设备即丢）");
}

/** ③ 常显：空态 + 上次自检时间 + 未覆盖建议 + 按类型汇总，四样都要真的渲染。 */
// 任务 81 §A3：空态文案从「还没有自检记录 / 在本页生成一次大纲后…」改成分清两种情况
// （「这份大纲还没跑过自检」+「立即自检」/「还没有大纲…」）—— 这里钉的是「有明确空态」这件事本身。
const EMPTY_STATE = /这份大纲还没跑过自检|还没有大纲，所以也没有自检结论/;
// 任务 84 §B2：这处时间从裸切字符串改走唯一出处 `formatLocalTime`（本地时间口径）
// ⇒ 断言只钉「界面上有这个时间」，不再钉它是不是裸的 `{checkGeneratedAt}`。
const STAMP = /上次自检\s*\{(?:formatLocalTime\()?checkGeneratedAt/;
const SUGGESTION = /\{row\.suggestion\}/;
const BY_KIND = /material_points_by_kind\.map/;
// 任务 75 §B3 起，明细先按类型分组、默认只渲染前 8 条（任务 75 的断言脚本单独管细节）
const DETAIL = /\{check\.material_uncovered_detail\.length > 0 && \(/;
const DETAIL_GROUP = /uncoveredGroups\.map\(\(group\)/;
for (const [name, pattern, why] of [
  ["空态", EMPTY_STATE, "没有自检记录时没有明确空态"],
  ["上次自检时间", STAMP, "界面上看不出这份结论是什么时候生成的"],
  ["未覆盖建议", SUGGESTION, "未覆盖要点只列名字、没有可操作建议"],
  ["按类型汇总", BY_KIND, "看不出扩面识别到了哪些类型的要点"],
  ["未覆盖明细", DETAIL, "未覆盖清单没有走新的 material_uncovered_detail"],
  ["未覆盖明细按类型分组", DETAIL_GROUP, "明细没有按类型分组渲染"],
] as [string, RegExp, string][]) {
  if (!pattern.test(outline)) failures.push(`OutlinePanel 没有渲染${name}（${why}）`);
}

/** ④ 语义等价开关：界面上必须是**可选、默认关**。 */
const SWITCH = /name="semantic"[\s\S]{0,200}?valuePropName="checked"/;
const DEFAULT_OFF = /name="semantic"[\s\S]{0,260}?initialValue=\{false\}/;
const SWITCH_LABEL = /语义等价判定（默认关）/;
const PASSES_THROUGH = /semantic: v\.semantic === true/;
if (!SWITCH.test(outline) || !SWITCH_LABEL.test(outline)) {
  failures.push("OutlinePanel 没有「语义等价判定」开关（可选但界面上找不到）");
}
if (!DEFAULT_OFF.test(outline)) {
  failures.push("「语义等价判定」不是默认关闭（默认路径不该走语义判定）");
}
if (!PASSES_THROUGH.test(outline)) {
  failures.push("开关没有随生成请求传给后端（勾了也不生效）");
}

/** ⑤ 后端：自检必须落库，且自检本身不调 LLM（默认路径不产生用量）。 */
const planner = readFileSync(join(repo, "backend", "app", "services", "planner.py"), "utf8");
const RECORD_KEY = /OUTLINE_CHECK_KEY = "outline_check"/;
const WRITES_META = /course\.meta = \{\*\*\(course\.meta or \{\}\), OUTLINE_CHECK_KEY: record\}/;
const HOOKED = /record_outline_check\(db, course, outline_check\(/;
if (!RECORD_KEY.test(planner) || !WRITES_META.test(planner)) {
  failures.push("planner.py 没有把自检结论写进 course.meta[outline_check]（刷新就丢）");
}
if (!HOOKED.test(planner)) {
  failures.push("generate_outline 没有落库自检结论（重新生成时不会更新）");
}
const outlineCheckBody = planner.slice(planner.indexOf("def outline_check("),
                                       planner.indexOf("def materials_fingerprint("));
if (/build_llm_client/.test(outlineCheckBody)) {
  failures.push("outline_check 里出现 LLM 调用（自检不该产生用量）");
}

/** ⑥ 后端测试真的存在（断言落库两次读取 / 扩面 / 建议 / 语义默认关）。 */
const testPath = join(repo, "backend", "tests", "test_outline_selfcheck.py");
if (!existsSync(testPath)) {
  failures.push("backend/tests/test_outline_selfcheck.py 不存在（落库与扩面没有回归测试）");
} else {
  const testSrc = readFileSync(testPath, "utf8");
  for (const needle of ["stored_outline_check", "survives_reopen", "语义等价", "material_uncovered_detail"]) {
    if (!testSrc.includes(needle)) failures.push(`test_outline_selfcheck.py 缺少 ${needle} 相关断言`);
  }
}

// 判定自检：把关键接线抹掉后必须变红 —— 否则这些断言等于永远绿灯
if (QUERY_KEY.test(outline.replace(/\["planCheck",\s*courseId\]/g, "[]"))) {
  failures.push("自检失败：抹掉 planCheck 查询键后判定仍然通过");
}
if (READS_STORED.test(outline.replace(/planCheck\?\.check \?\? plan\?\.check/g, "null"))) {
  failures.push("自检失败：抹掉落库优先读后判定仍然通过");
}
if (EMPTY_STATE.test(outline.replace(/这份大纲还没跑过自检/g, "")
                                 .replace(/还没有大纲，所以也没有自检结论/g, "") )) {
  failures.push("自检失败：抹掉空态文案后判定仍然通过");
}
if (SWITCH.test(outline.replace(/name="semantic"/g, "name=\"seman\""))) {
  failures.push("自检失败：抹掉语义开关后判定仍然通过");
}
if (DEFAULT_OFF.test(outline.replace(/initialValue=\{false\}/g, "initialValue={true}"))) {
  failures.push("自检失败：把开关默认值改成 true 后判定仍然通过");
}
if (PASSES_THROUGH.test(outline.replace(/semantic: v\.semantic === true/g, "semantic: false"))) {
  failures.push("自检失败：抹掉开关透传后判定仍然通过");
}
if (HOOKED.test(planner.replace(/record_outline_check\(db, course, outline_check\(/g, "outline_check("))) {
  failures.push("自检失败：抹掉落库那步后判定仍然通过");
}

if (failures.length > 0) {
  console.error("前端「大纲自检常显」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端大纲自检断言通过：结论读落库（GET /plan/{id}/check）而非组件 state，"
  + "空态 / 上次自检时间 / 未覆盖建议 / 按类型汇总都渲染，语义等价开关默认关且随请求透传，"
  + "后端把结论写进 course.meta[outline_check] 且自检不调 LLM");
