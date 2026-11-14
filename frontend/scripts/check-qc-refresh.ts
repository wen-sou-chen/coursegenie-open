/**
 * 任务 48k 前端口径断言：「开始质检」按钮真的按当前正文现算一次。
 *
 * 根因（DSH 只读诊断）：`api.ts` 的 `lectureQc(itemId, refresh = false)` 支持
 * `?refresh=true`，但**全前端没有任何调用方传 true** ⇒ `QcBadge` 的按钮文案承诺
 * 「按当前正文现算一次」，实际拿回来的是缓存里那份 `terminology=skipped` 的旧结论。
 *
 * 这里不启 DOM、不引新依赖，做两条**形状断言**（组件里那句调用点 + URL 拼装）：
 * 1. `src/components/QcBadge.tsx` 的调用点必须显式传 `true`；
 * 2. `src/api.ts` 的 URL 拼装必须仍然只在 refresh 为真时带 `?refresh=true`。
 *
 * 运行：`node scripts/check-qc-refresh.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = (rel: string) => readFileSync(join(here, "..", "src", rel), "utf8");

const failures: string[] = [];

const api = src("api.ts");
if (!/lectureQc:\s*\(itemId: string, refresh = false\)/.test(api)) {
  failures.push("api.ts 的 lectureQc 签名变了（应保留 (itemId, refresh = false)）");
}
if (!/qc\$\{refresh \? "\?refresh=true" : ""\}/.test(api)) {
  failures.push("api.ts 里 refresh=true 不再拼出 ?refresh=true（现算拿不到了）");
}

const badge = src("components/QcBadge.tsx");
if (!badge.includes("api.lectureQc(itemId, true)")) {
  failures.push("QcBadge 的「开始质检」没传 refresh=true：拿到的是缓存，不是现算结果");
}

// 回归护栏：全前端不许再有「单参数」的 lectureQc 调用（那正是 48k 修掉的那处）
const callers = [
  ...readdirSources(join(here, "..", "src")),
].filter((rel) => {
  const text = src(rel);
  return /api\.lectureQc\(\s*[^,)]*\)/.test(text);
});
if (callers.length > 0) {
  failures.push(`还有单参数的 api.lectureQc(x) 调用：${callers.join(", ")}`);
}

function* readdirSources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      for (const nested of readdirSources(full)) yield nested;
    } else if (/\.(ts|tsx)$/.test(entry)) {
      yield full.slice(join(here, "..", "src").length + 1).replaceAll("\\", "/");
    }
  }
}

if (failures.length > 0) {
  console.error("前端质检现算断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端质检现算断言通过：QcBadge → api.lectureQc(itemId, true) → ?refresh=true，无单参数调用");
