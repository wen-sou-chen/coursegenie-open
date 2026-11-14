/**
 * 「流程图」计数唯一口径（后端 app/services/content_flow.py 与之一字对齐）。
 *
 * 真实情况（任务 27 复核）：内容条目里 `kind='flowchart'` 是 0 条 —— 流程图**内嵌在讲义
 * 正文的 ```mermaid 代码块里**（真实库共 4 段：1.2 / 2.1 / 2.2 / 3.2 各 1 段）。所以任何
 * 「本课 / 本节有几张流程图」都必须数正文里的 mermaid 段，**不能按 kind 计数**，否则界面
 * 永远显示 0，还和顶部「流程图 4」自相矛盾。
 *
 * 取值顺序：优先用后端算好的 `meta.mermaid_blocks`（后端 build_content_rows 用同一个函数
 * 现算写入，口径一致）；老数据没有该字段时，按下面的正则解析正文兜底，结果相同。
 */
import type { ContentRow } from "../types";

export const MERMAID_BLOCKS_KEY = "mermaid_blocks";

/** 一段 Markdown 里的 ```mermaid 段数（与后端 _FENCE_RE 同口径、忽略大小写）。 */
export function countMermaid(markdown?: unknown): number {
  return (String(markdown ?? "").match(/```\s*mermaid/gi) ?? []).length;
}

function markdownOf(row: ContentRow): string {
  return String((row.body as { markdown?: unknown } | undefined)?.markdown ?? "");
}

/** 单个条目的流程图段数：后端 meta 优先，否则解析正文（两者结果一致）。 */
export function mermaidCountOfRow(row: ContentRow): number {
  const cached = row.meta?.[MERMAID_BLOCKS_KEY];
  if (typeof cached === "number") return cached;
  return countMermaid(markdownOf(row));
}

/** 一批条目的流程图总段数（讲义 + 正文；独立的 kind='flowchart' 条目贡献 0，不看它计数）。 */
export function flowchartCount(rows: ContentRow[] | undefined | null): number {
  return (rows ?? []).reduce((n, r) => n + mermaidCountOfRow(r), 0);
}

/** 取出正文里每一段 mermaid 源码（「只看 → 流程图」直接把这些段渲染成流程图）。 */
export function mermaidBlocks(markdown?: unknown): string[] {
  const text = String(markdown ?? "");
  const re = /```\s*mermaid[ \t]*\r?\n([\s\S]*?)(?:```|$)/gi;
  const out: string[] = [];
  let m = re.exec(text);
  while (m !== null) {
    const code = String(m[1] ?? "").trim();
    if (code) out.push(code);
    m = re.exec(text);
  }
  return out;
}

/** 一批条目里所有内嵌 mermaid 段（附来源标题，用于定位到具体讲义）。 */
export function mermaidBlockList(
  rows: ContentRow[] | undefined | null,
): { title: string; code: string }[] {
  const out: { title: string; code: string }[] = [];
  for (const row of rows ?? []) {
    for (const code of mermaidBlocks(markdownOf(row))) {
      out.push({ title: row.title, code });
    }
  }
  return out;
}
