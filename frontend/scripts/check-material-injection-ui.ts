/**
 * 任务 69 前端断言：素材注入的「超限提示」与「入库卫生提示」在界面上真的接上了。
 *
 * 起因（DSH 2026-09-20 走查第 2 步）：素材注入是全项目唯一的口子，却有两条硬上限
 * （最多 6 份 × 每份前 2500 字）——超出部分静默丢弃、接口与界面都没有任何提示；
 * 实测素材开头还带着「生成详细版 / ###### ChatGPT 说：」这类对话外壳、页数也没落库。
 *
 * 后端行为由 `backend/tests/test_material_injection.py` 断言；这里只做**源码级**断言，
 * 保证接口新加的字段在界面上有落点（否则会出现「后端修好了、用户还是看不见」）。
 *
 * 运行：`node scripts/check-material-injection-ui.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const failures: string[] = [];

function read(rel: string): string {
  return readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");
}

/** ① 类型：后端返回的字段必须在 types.ts 里声明（否则前端读不到、tsc 也不拦）。 */
const types = read("src/types.ts");
for (const field of ["material_inject_notice", "material_inject", "cleaned_lines", "cleaned_rules",
                     // 任务 69 §B1.5.1 / §B1.5.3 新加的字段（来源类型 / 检测 / 有来处）
                     "origin_label", "must_clean", "detected_lines", "detected_rules",
                     "grounded_count", "ungrounded_count", "ungrounded", "paragraphs"]) {
  if (!new RegExp(`^\\s*${field}\\??:`, "m").test(types)) {
    failures.push(`types.ts 里没有声明 ${field}（后端返回了、前端读不到）`);
  }
}

/** ② 大纲自检：`check.material_inject_notice` 必须真的渲染（超限不再静默）。 */
const NOTICE = /check\.material_inject_notice/;
const outline = read("src/components/OutlinePanel.tsx");
if (!NOTICE.test(outline)) {
  failures.push("OutlinePanel 没有渲染 check.material_inject_notice —— 注入超限用户仍然看不见");
}

/** ③ 素材行：「已清理 N 行」提示必须挂在 m.cleaned_lines 上。 */
const CLEANED = /已清理\s*\{m\.cleaned_lines\}\s*行/;
const materials = read("src/components/steps/MaterialsStep.tsx");
if (!CLEANED.test(materials)) {
  failures.push("MaterialsStep 没有渲染「已清理 N 行」提示（入库卫生对用户不可见）");
}

/**
 * ④ 任务 69 §B1.5.1：上传原件**保持原样** —— 界面必须给「检测到 N 行」提示 + 「一键清理」入口
 *    （用户 2026-09-21 口径：原始上传的可以接受，只提示、不强制清理）。
 */
const DETECTED = /检测到\s*\{m\.detected_lines\}\s*行/;
const OPTIONAL_CLEAN = /clean\.mutate\(m\.id\)/;
if (!DETECTED.test(materials)) {
  failures.push("MaterialsStep 没有渲染「检测到 N 行」（上传原件的外壳对用户不可见）");
}
if (!OPTIONAL_CLEAN.test(materials)) {
  failures.push("MaterialsStep 没有「一键清理」入口（用户口径要求可选清理，默认不动原稿）");
}
if (!/cleanMaterial:[\s\S]{0,200}?\/material\/\$\{id\}\/clean/.test(read("src/api.ts"))) {
  failures.push("api.ts 没有 cleanMaterial（一键清理点了没反应）");
}

/**
 * ⑤ 任务 69 §B1.5.3：「有来处」必须在卡片上看得见 —— 有依据 N / 无依据 M，且无依据标红，
 *    还能一键跳到原素材第 N 段核对（numbered 模式）。
 */
const GROUND_BADGE = /有依据\s*\{m\.grounded_count\s*\?\?\s*0\}\s*条\s*\/\s*无依据\s*\{m\.ungrounded_count\s*\?\?\s*0\}\s*条/;
const GROUND_RED = /type="danger"/;
const GROUND_JUMP = /jumpTo\(g\.material,\s*g\.para\)/;
const NUMBERED = /numbered:\s*true/;
if (!GROUND_BADGE.test(materials)) {
  failures.push("MaterialsStep 没有渲染「有依据 N 条 / 无依据 M 条」");
}
if (!GROUND_RED.test(materials)) {
  failures.push("MaterialsStep 没有把无来处的条目标红（用户看不出哪条不能当依据）");
}
if (!GROUND_JUMP.test(materials)) {
  failures.push("MaterialsStep 没有「跳到原文核对」（AI 补充的依据无法回查）");
}
if (!NUMBERED.test(materials) || !/target=\{fullPara\}/.test(materials)) {
  failures.push("MaterialFullText 没有接 numbered（跳过去看不到第 N 段）");
}

/**
 * ⑥ 任务 69 §B1.5.1：**清过之后不再说「保持原样」** —— 否则点了一键清理的素材行会同时挂着
 *    「检测到 N 行（保持原样）」和「已清理 N 行」两种标签，自相矛盾、还能被点第二次。
 */
const DETECTED_ONLY_WHEN_DIRTY = /!m\.must_clean && !m\.cleaned_lines && \(m\.detected_lines \?\? 0\) > 0/;
if (!DETECTED_ONLY_WHEN_DIRTY.test(materials)) {
  failures.push("MaterialsStep 的「检测到 N 行」没有排除已清理的素材（清过之后仍说「保持原样」）");
}

// 判定自检：把字段从源码里抹掉后必须变红 —— 否则这两条断言等于永远绿灯
if (NOTICE.test(outline.replace(/check\.material_inject_notice/g, "check.material_notice"))) {
  failures.push("自检失败：抹掉 material_inject_notice 后判定仍然通过");
}
if (CLEANED.test(materials.replace(/已清理 \{m\.cleaned_lines\} 行/g, ""))) {
  failures.push("自检失败：抹掉「已清理 N 行」后判定仍然通过");
}
if (DETECTED.test(materials.replace(/检测到 \{m\.detected_lines\} 行/g, ""))) {
  failures.push("自检失败：抹掉「检测到 N 行」后判定仍然通过");
}
if (OPTIONAL_CLEAN.test(materials.replace(/clean\.mutate\(m\.id\)/g, ""))) {
  failures.push("自检失败：抹掉「一键清理」后判定仍然通过");
}
if (GROUND_BADGE.test(materials.replace(/有依据 \{m\.grounded_count \?\? 0\} 条 \/ 无依据 \{m\.ungrounded_count \?\? 0\} 条/g, ""))) {
  failures.push("自检失败：抹掉「有依据 / 无依据」后判定仍然通过");
}
if (GROUND_RED.test(materials.replace(/type="danger"/g, ""))) {
  failures.push("自检失败：抹掉无依据标红后判定仍然通过");
}
if (GROUND_JUMP.test(materials.replace(/jumpTo\(g\.material, g\.para\)/g, ""))) {
  failures.push("自检失败：抹掉「跳到原文核对」后判定仍然通过");
}
if (NUMBERED.test(materials.replace(/numbered: true/g, ""))) {
  failures.push("自检失败：抹掉 numbered 模式后判定仍然通过");
}
if (DETECTED_ONLY_WHEN_DIRTY.test(materials.replace(
    /!m\.must_clean && !m\.cleaned_lines && \(m\.detected_lines \?\? 0\) > 0/g,
    "!m.must_clean && (m.detected_lines ?? 0) > 0"))) {
  failures.push("自检失败：把「已清理后不再提示」的条件去掉后判定仍然通过");
}

if (failures.length > 0) {
  console.error("前端「素材注入提示」接线断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端素材注入断言通过：接口的注入/卫生字段在 types.ts 有声明，"
  + "OutlinePanel 渲染超限提示，MaterialsStep 渲染「已清理 N 行」「检测到 N 行」+ 一键清理、"
  + "AI 补充的「有依据 / 无依据」标红与跳转核对、清过之后不再提示「保持原样」");
