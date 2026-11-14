/**
 * 前端口径断言（任务 27 · E.2）：`顶部流程图数 == Σ 各章流程图数`。
 *
 * 直接调 `src/lib/contentCounts.ts` 的**同一个函数**（不是另写一套解析），样例数据按真实
 * 形状造：讲义正文里 4 段 ```mermaid（真实库就是 4 段：1.2 / 2.1 / 2.2 / 3.2 各 1 段），
 * 另外塞几条 `kind='flowchart'` 的条目 —— 它们**不该**多算，证明「不依赖 kind」。
 *
 * 运行：`node scripts/check-content-counts.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { countMermaid, flowchartCount, mermaidBlocks } from "../src/lib/contentCounts.ts";
import type { ContentRow } from "../src/types.ts";

const failures: string[] = [];

function check(name: string, actual: unknown, expected: unknown): void {
  if (actual !== expected) failures.push(`${name}：期望 ${String(expected)}，实际 ${String(actual)}`);
}

function lecture(id: string, nodeId: string, markdown: string, extra: Partial<ContentRow> = {}): ContentRow {
  return {
    id, node_id: nodeId, kind: "lecture", title: `讲义 ${id}`,
    body: { markdown }, meta: {}, ...extra,
  } as ContentRow;
}

const flow = (code: string) => `\`\`\`mermaid\n${code}\n\`\`\`\n`;

// 真实形状：两章，讲义里分别内嵌 2 段与 2 段流程图
const rows: ContentRow[] = [
  lecture("l1", "n11", `# 1.1\n\n${flow("flowchart TD\n  A --> B")}正文\n${flow("flowchart LR\n  C --> D")}`),
  lecture("l2", "n21", `# 2.1\n\n${flow("flowchart TD\n  E --> F")}正文`),
  lecture("l3", "n22", `# 2.2\n\n${flow("flowchart TD\n  G --> H")}`),
  // 干扰项：独立流程图条目（库里 kind='flowchart' 是 0 条，即便有也不算进 mermaid 段数）
  { id: "f1", node_id: "n22", kind: "flowchart", title: "独立流程图", body: { mermaid: "flowchart TD\n X --> Y" }, meta: {} } as ContentRow,
  // 没有正文的条目不该贡献任何段数
  { id: "q1", node_id: "n11", kind: "quiz", title: "习题", body: {}, meta: {} } as ContentRow,
];

const chapters = [
  { key: "1", nodeIds: ["n1", "n11"] },
  { key: "2", nodeIds: ["n2", "n21", "n22"] },
];

check("单段讲义", countMermaid(flow("flowchart TD\n  A --> B")), 1);
check("两段讲义", countMermaid(rows[0].body.markdown as string), 2);
check("顶部总数", flowchartCount(rows), 4);
check("kind='flowchart' 不多算", flowchartCount([rows[3]]), 0);
check("meta.mermaid_blocks 优先", flowchartCount([lecture("l9", "n1", flow("flowchart TD\n A-->B"), { meta: { mermaid_blocks: 3 } })]), 3);

const perChapter = chapters.map((ch) => flowchartCount(rows.filter((r) => ch.nodeIds.includes(String(r.node_id)))));
check("Σ 各章 == 顶部", perChapter.reduce((a, b) => a + b, 0), flowchartCount(rows));
check("第 1 章", perChapter[0], 2);
check("第 2 章", perChapter[1], 2);
check("能取出 mermaid 源码", mermaidBlocks(rows[0].body.markdown as string).length, 2);

if (failures.length > 0) {
  console.error("前端口径断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`前端口径断言通过：顶部流程图 ${flowchartCount(rows)} 段 == Σ 各章 ${perChapter.join(" + ")} 段`);
