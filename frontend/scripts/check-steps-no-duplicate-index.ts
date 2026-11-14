/**
 * 任务 68 前端断言：顶部步骤条不再出现「裸序号 + 标题圈号」的重复。
 *
 * 起因（DSH 2026-09-20 走查第 1 页）：第 4 步没完成时 antd 会画序号「4」，而标题里本来就写了
 * 「④ 课程预览」⇒ 页面上是「4④ 课程预览」；新课程上更明显（「1① 原始资料 2② 大纲通过 …」）。
 *
 * 这里不启 DOM，只做**源码级**断言（与 check-panel-loading.ts 同一套路）：CourseDetail 的
 * stepItems 标题里不许再出现圈号，也不许自己带裸序号；同时自检判定函数本身（防「正则写坏了
 * 导致永远绿灯」）。真实 DOM 上的同名断言由走查脚本（Playwright）出证据。
 *
 * 运行：`node scripts/check-steps-no-duplicate-index.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const failures: string[] = [];

/** ①-⑳ / ➀-➓ / ❶-❿ / ⓫-⓴ 这类「圈号」。 */
const CIRCLED = /[\u2460-\u2473\u24EA\u24EB-\u24FF\u2776-\u277F\u2780-\u2789\u278A-\u2793\u3251-\u325F]/;
/** 行首裸序号：「1 原始资料」「1. 原始资料」「1、原始资料」。 */
const LEADING_INDEX = /^\s*\d+\s*[.、,)]?\s+/;

// 判定函数自检：写坏了就会让下面永远绿灯
const probes: Array<[string, boolean]> = [
  ["④ 课程预览", true],
  ["① 原始资料", true],
  ["课程预览", false],
  ["原始资料（6 份已就绪）", false],
];
for (const [text, expected] of probes) {
  if (CIRCLED.test(text) !== expected) {
    failures.push(`圈号判定自检失败：「${text}」应当判为 ${expected}`);
  }
}
if (!LEADING_INDEX.test("1 原始资料") || LEADING_INDEX.test("原始资料")) {
  failures.push("裸序号判定自检失败");
}

const source = readFileSync(join(here, "..", "src", "pages", "CourseDetail.tsx"), "utf8").replace(/\r+\n/g, "\n");

// 步骤条必须真的是由 stepItems 渲染出来的（否则下面的断言会看着源码绿灯、页面却是别的文案）
if (!/items=\{stepItems\.map\(\(s\) => \(\{ title: s\.title/.test(source)) {
  failures.push("CourseDetail 的 <Steps> 不再用 stepItems 的 title 渲染（下面的断言失去意义）");
}
const block = source.match(/const stepItems = \[([\s\S]*?)\] as const;/);
if (!block) {
  failures.push("找不到 CourseDetail 的 stepItems 定义");
} else {
  const titles = [...block[1].matchAll(/title:\s*"([^"]*)"/g)].map((m) => m[1]);
  if (titles.length !== 5) failures.push(`步骤条应当有 5 步，实际解析到 ${titles.length} 个标题`);
  if (new Set(titles).size !== titles.length) failures.push(`步骤标题有重复：${titles.join(" / ")}`);
  for (const title of titles) {
    if (!title.trim()) failures.push("步骤标题为空");
    if (CIRCLED.test(title)) {
      failures.push(`步骤标题「${title}」自带圈号 —— antd 已经画了序号，页面上会变成「4④ …」`);
    }
    if (LEADING_INDEX.test(title)) {
      failures.push(`步骤标题「${title}」自带裸序号 —— 与 antd 画的序号重复`);
    }
  }
}

if (failures.length > 0) {
  console.error("前端「步骤条序号不重复」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端步骤条断言通过：5 步标题都不带圈号 / 裸序号（序号只由 antd 的 Steps 画一处）");
