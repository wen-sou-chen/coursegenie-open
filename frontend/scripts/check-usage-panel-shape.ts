/**
 * 任务 48x 前端口径断言：`aux_history` 是汇总对象，面板必须走 `.by_purpose`。
 *
 * 起因：后端 `GET /api/analytics/{id}/usage` 的 `aux_history` 一直是**对象**
 * （`{runs, llm_calls, total_tokens, cost_cny, by_purpose, estimated}`），
 * 而 types.ts 把它声明成数组、UsagePanel 直接 `.map()` ⇒ 页签一打开就
 * `data.aux_history.map is not a function`，整页白屏；且 tsc / build 全绿拦不住
 * （错的类型把编译器骗过去了）。
 *
 * 这里不启 DOM，只做形状断言，防「类型与真值不符」这类静默失效：
 * 1. types.ts：aux_history 指向 CourseUsageAuxHistory（对象），且不许再写成 `{...}[]`；
 * 2. UsagePanel.tsx：只能通过 `aux_history?.by_purpose ?? []` 取行，不许直接 .map/.length；
 * 3. CourseDetail.tsx：10 个高级工具面板都包了 ErrorBoundary（再坏也只坏一格）。
 *
 * 运行：`node scripts/check-usage-panel-shape.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = (rel: string) => readFileSync(join(here, "..", "src", rel), "utf8");

const failures: string[] = [];

const types = src("types.ts");
if (!/aux_history:\s*CourseUsageAuxHistory;/.test(types)) {
  failures.push("types.ts：aux_history 不再指向 CourseUsageAuxHistory（形状又漂了）");
}
if (!/by_purpose:\s*CourseUsageAuxRow\[\];/.test(types)) {
  failures.push("types.ts：CourseUsageAuxHistory 丢了 by_purpose: CourseUsageAuxRow[]");
}
if (/aux_history:\s*\{[^}]*\}\[\]/.test(types)) {
  failures.push("types.ts：aux_history 又被写成数组了（真值是汇总对象 ⇒ 面板必然白页）");
}

const panel = src("components/UsagePanel.tsx");
if (!/aux_history\?\.by_purpose\s*\?\?\s*\[\]/.test(panel)) {
  failures.push("UsagePanel：没有从 aux_history?.by_purpose ?? [] 取行");
}
if (/\baux_history\s*\.\s*(length|map)\b/.test(panel)) {
  failures.push("UsagePanel：又在 aux_history 上直接 .length / .map（对象没有这两个）");
}

const detail = src("pages/CourseDetail.tsx");
if (!detail.includes("<ErrorBoundary name={name}>")) {
  failures.push("CourseDetail：panel() 里没有 ErrorBoundary（单个面板崩溃会再白整页）");
}
const wrapped = detail.split('panel("').length - 1;
if (wrapped !== 10) {
  failures.push(`CourseDetail：高级工具面板包了 ErrorBoundary 的是 ${wrapped} 个，应为 10 个`);
}

if (failures.length > 0) {
  console.error("前端用量面板形状断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端用量面板形状断言通过：aux_history 是对象、面板走 .by_purpose、10 个面板都有 ErrorBoundary");
