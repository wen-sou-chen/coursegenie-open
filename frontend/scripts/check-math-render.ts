/**
 * 任务 53 前端断言：讲义里的公式真的被渲染成 KaTeX。
 * 任务 62 追加断言：GFM 管道表格真的被渲染成 `<table>`（不是显示成 `| a | b |` 源码）。
 *
 * 用 `MdWithFlow` 的**同一份插件配置**（`src/lib/markdownPlugins.ts`）渲染一份含
 * 「行内公式 / 独立公式 / 表格里的公式」的讲义，断言输出里有 `<span class="katex">`
 * 且没有残留的 `$` 原文，并且那张表格是真正的 `<table>`。
 *
 * 运行：`node scripts/check-math-render.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { createElement } from "react";
import ReactMarkdown from "react-markdown";
import { renderToStaticMarkup } from "react-dom/server";

import { REHYPE_PLUGINS, REMARK_PLUGINS } from "../src/lib/markdownPlugins.ts";

const failures: string[] = [];

const markdown = [
  "# 二次方程",
  "",
  "行内公式 $x^2+y^2=r^2$ 与求根公式：",
  "",
  "$$x=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$$",
  "",
  "| 量 | 取值 |",
  "| --- | --- |",
  "| 判别式 | $\\Delta \\ge 0$ |",
].join("\n");

const html = renderToStaticMarkup(
  createElement(
    ReactMarkdown,
    { remarkPlugins: REMARK_PLUGINS, rehypePlugins: REHYPE_PLUGINS },
    markdown,
  ),
);

const katexCount = (html.match(/class="katex"/g) ?? []).length;
if (katexCount !== 3) {
  failures.push(`行内/独立/表格三处公式都要渲染：期望 3 处 <span class="katex">，实际 ${katexCount}`);
}
if (html.includes("$")) {
  failures.push("输出里还有 $ 原文：公式没被渲染，页面上会显示成 $x^2$");
}
if (!html.includes("katex-mathml")) {
  failures.push("缺少 KaTeX 的 MathML 输出（复制/无障碍要它）");
}
const tableCount = (html.match(/<table>/g) ?? []).length;
if (tableCount !== 1) {
  failures.push(`管道表格要渲染成真 <table>（任务 62，靠 remark-gfm）：期望 1 个，实际 ${tableCount}`);
}
if (/<p>\s*\|/.test(html)) {
  failures.push("表格被当普通段落输出：页面上会显示成 | 量 | 取值 | 源码（任务 62 回归）");
}
const dollars = (html.match(/\\\(/g) ?? []).length;

if (failures.length > 0) {
  console.error("前端公式渲染断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(
  `前端 Markdown 渲染断言通过：3 处公式 → <span class="katex">、1 个表格 → <table>，无 $ 残留（内联标记 ${dollars}）`,
);