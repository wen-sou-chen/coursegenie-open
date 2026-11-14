/**
 * 任务 68 前端断言：知识图谱必须按「章 → 其知识点」的树形排布，而不是把孤立节点挤成一排。
 *
 * 起因（DSH 2026-09-20 走查第 1 页只读取证）：AI 课 39 个节点里，第 1 章 + 全部 27 个知识点被挤在
 * 同一条水平带（y=636）上、相邻中心只隔约 48px 而节点宽 172px ⇒ 严重重叠、文字读不了；第 2–12 章
 * 各自一行斜向排下去。根因：接口返回的 `graph.edges` 只有 11 条「章 → 章」前置边（知识点零边），
 * 旧布局把 `graph.edges` 直接交给 dagre ⇒ 孤立节点全落进第一层横排。
 *
 * 这里直接调**可测纯函数** `src/lib/knowledgeGraphLayout.ts::knowledgeGraphLayout`（旧名
 * `dagreLayout`；本轮不再用 dagre，原因见该文件头注释），跑**真图谱 fixture**
 * （`scripts/fixtures/task68-ai-course-graph.json` = `GET /api/plan/{id}` 的 `plan.graph` 原样，39 节点）
 * 断言坐标，防「改了布局又退回孤立节点横排」「层级边被删」「间距压得比节点还小」这类静默回归。
 *
 * 运行：`node scripts/check-knowledge-graph-layout.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  MARGIN, NODE_H, NODE_W, NODESEP, POINTS_PER_ROW, RANKSEP, knowledgeGraphLayout,
} from "../src/lib/knowledgeGraphLayout.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const failures: string[] = [];
const note = (msg: string) => failures.push(msg);

interface FixtureNode { id: string; key: string; label: string; type?: string }
interface FixtureEdge { source: string; target: string; label?: string }
interface Fixture { graph: { nodes: FixtureNode[]; edges: FixtureEdge[] } }

const fixture = JSON.parse(
  readFileSync(join(here, "fixtures", "task68-ai-course-graph.json"), "utf8"),
) as Fixture;
const graph = fixture.graph;
const chapters = graph.nodes.filter((n) => !n.key.includes("."));
const points = graph.nodes.filter((n) => n.key.includes("."));

// ① fixture 形状（DSH 取证口径：12 章 + 27 知识点 = 39 节点 / 11 条「章→章」前置边）
if (graph.nodes.length !== 39) note(`fixture 不是 39 个节点（实际 ${graph.nodes.length}）`);
if (chapters.length !== 12) note(`fixture 不是 12 章（实际 ${chapters.length}）`);
if (points.length !== 27) note(`fixture 不是 27 个知识点（实际 ${points.length}）`);
if (graph.edges.length !== 11) note(`fixture 不是 11 条前置边（实际 ${graph.edges.length}）`);
if (graph.edges.some((e) => !e.label)) note("fixture 的前置边应当都带 label（「前置」）");

// ② 间距下限必须按节点尺寸算（不低于节点宽 + 8 / 节点高 + 24）
if (NODESEP < NODE_W + 8) note(`相邻层水平间距 ${NODESEP} < 节点宽 + 8 = ${NODE_W + 8}`);
if (RANKSEP < NODE_H + 24) note(`相邻层垂直间距 ${RANKSEP} < 节点高 + 24 = ${NODE_H + 24}`);

const out = knowledgeGraphLayout(graph);
const byId = new Map(out.nodes.map((n) => [n.id, n]));
const byKey = new Map(out.nodes.map((n) => [n.key, n]));

// ③ 节点一个不少：id 集合与 fixture 完全一致（不丢、不重、不凭空多）
const missing = graph.nodes.filter((n) => !byId.has(n.id)).map((n) => n.key);
const extra = out.nodes.filter((n) => !graph.nodes.some((g) => g.id === n.id)).map((n) => n.key);
if (out.nodes.length !== graph.nodes.length) note(`布局输出 ${out.nodes.length} 个节点，fixture 是 ${graph.nodes.length} 个`);
if (missing.length) note(`这些节点没拿到坐标（被静默丢掉）：${missing.join("、")}`);
if (extra.length) note(`布局里多出这些节点：${extra.join("、")}`);
if (new Set(out.nodes.map((n) => n.id)).size !== out.nodes.length) note("布局输出里有重复节点 id");
for (const n of out.nodes) {
  if (!Number.isFinite(n.x) || !Number.isFinite(n.y)) note(`节点 ${n.key} 的坐标不是有限数（${n.x}, ${n.y}）`);
}

// ④ 每个知识点必须在「所属章的下方」：y 严格更大，且与本层之间留够 RANKSEP
let belowCount = 0;
const firstRowGap = new Map<string, number>();
for (const p of points) {
  const parentKey = p.key.split(".")[0];
  const parent = byKey.get(parentKey);
  const self = byKey.get(p.key);
  if (!parent || !self) {
    note(`知识点 ${p.key} 或它的父章 ${parentKey} 没有坐标`);
    continue;
  }
  if (!(self.y > parent.y)) {
    note(`知识点 ${p.key} 的 y=${self.y} 没大于所属章 ${parentKey} 的 y=${parent.y}（挤在同一水平带）`);
  } else {
    belowCount += 1;
  }
  const gap = self.y - (parent.y + parent.height);
  if (gap < 0) note(`知识点 ${p.key} 压到了所属章 ${parentKey} 上（重叠 ${-gap}px）`);
  else if (gap < RANKSEP) note(`知识点 ${p.key} 与所属章 ${parentKey} 的层间距只有 ${gap}px（下限 ${RANKSEP}px = 节点高 ${NODE_H} + 24）`);
  firstRowGap.set(parentKey, Math.min(firstRowGap.get(parentKey) ?? Number.POSITIVE_INFINITY, gap));
}
// 章的正下方一层必须是它自己的知识点：不许空出一层（那正是「知识点跑到别的章后面」的样子）
let tightCount = 0;
for (const [parentKey, gap] of firstRowGap) {
  if (gap > RANKSEP + 0.5) {
    note(`第 ${parentKey} 章正下方空着 ${gap}px（> 层距 ${RANKSEP}px）：它的知识点没紧跟在该章下面`);
  } else {
    tightCount += 1;
  }
}

// ⑤ 任意两节点矩形不相交（重叠 = 严重缺陷）
let overlapPairs = 0;
let closestPair = "";
for (let i = 0; i < out.nodes.length; i += 1) {
  for (let j = i + 1; j < out.nodes.length; j += 1) {
    const a = out.nodes[i];
    const b = out.nodes[j];
    const dx = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
    const dy = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
    if (dx > 0 && dy > 0) {
      overlapPairs += 1;
      if (!closestPair) closestPair = `${a.key} 与 ${b.key} 重叠 ${Math.round(dx)}×${Math.round(dy)}px`;
    }
  }
}
if (overlapPairs > 0) note(`有 ${overlapPairs} 对节点矩形相交（例如 ${closestPair}）`);

// ⑥ 同一水平带上的相邻节点：净间隙 ≥ 节点宽 + 8（dagre 的 nodesep 口径 = 两个矩形之间的净间隙；
//    换成中心距口径就是 ≥ 172 + 180 = 352px，比「≥ 节点宽 + 8」的中心距读法更严，两种读法都过）
const bands = new Map<number, typeof out.nodes>();
for (const n of out.nodes) {
  const key = Math.round(n.y);
  bands.set(key, [...(bands.get(key) ?? []), n]);
}
let sameBandPairs = 0;
let minSameBandGap = Number.POSITIVE_INFINITY;
for (const [y, list] of bands) {
  const sorted = [...list].sort((a, b) => a.x - b.x);
  for (let i = 1; i < sorted.length; i += 1) {
    const edgeGap = sorted[i].x - (sorted[i - 1].x + sorted[i - 1].width);
    sameBandPairs += 1;
    minSameBandGap = Math.min(minSameBandGap, edgeGap);
    if (edgeGap < NODESEP - 0.5) {
      note(`y=${y} 一带上 ${sorted[i - 1].key} → ${sorted[i].key} 的净间隙只有 ${edgeGap}px（下限 ${NODESEP}px = 节点宽 ${NODE_W} + 8）`);
    }
  }
}

// ⑦ 边：真实前置边一条不少（含 label），层级边「父章 → 知识点」每个知识点一条
const prereq = out.edges.filter((e) => e.kind === "prereq");
const hierarchy = out.edges.filter((e) => e.kind === "hierarchy");
if (prereq.length !== graph.edges.length) note(`前置边从 ${graph.edges.length} 条变成 ${prereq.length} 条（真实业务边被改没了）`);
for (const e of graph.edges) {
  const hit = prereq.find((x) => x.source === e.source && x.target === e.target);
  if (!hit) note(`前置边 ${e.source.slice(0, 6)}→${e.target.slice(0, 6)} 丢了`);
  else if (hit.label !== e.label) note(`前置边 ${e.source.slice(0, 6)}→${e.target.slice(0, 6)} 的 label 变成「${hit.label}」`);
}
if (hierarchy.length !== points.length) {
  note(`层级边应有 ${points.length} 条（每个知识点一条），实际 ${hierarchy.length} 条`);
}
for (const p of points) {
  const parentKey = p.key.split(".")[0];
  const parent = byKey.get(parentKey);
  if (!parent) continue;
  const hits = hierarchy.filter((e) => e.source === parent.id && e.target === p.id);
  if (hits.length !== 1) note(`知识点 ${p.key} 的层级边不是「${parentKey} → ${p.key}」恰好一条（实际 ${hits.length} 条）`);
}

// ⑧ 每章的「子树矩形」（章 + 它的知识点）互不相交 —— 知识点不会跑到别的章后面
const envelopes = chapters.map((c) => {
  const kids = points.filter((p) => p.key.split(".")[0] === c.key).map((p) => byId.get(p.id)!);
  const self = byId.get(c.id)!;
  const rects = [self, ...kids];
  return {
    key: c.key,
    x0: Math.min(...rects.map((r) => r.x)),
    y0: Math.min(...rects.map((r) => r.y)),
    x1: Math.max(...rects.map((r) => r.x + r.width)),
    y1: Math.max(...rects.map((r) => r.y + r.height)),
  };
});
for (let i = 0; i < envelopes.length; i += 1) {
  for (let j = i + 1; j < envelopes.length; j += 1) {
    const a = envelopes[i];
    const b = envelopes[j];
    const dx = Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0);
    const dy = Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0);
    if (dx > 0 && dy > 0) note(`第 ${a.key} 章与第 ${b.key} 章的子树矩形相交（知识点跑到别的章后面）`);
  }
}

// ⑨ 异常数据：找不到父章的知识点退回孤立节点，但**不许静默丢节点**
const orphanGraph = {
  nodes: [
    { id: "c1", key: "1", label: "第一章", type: "chapter" },
    { id: "p1", key: "1.1", label: "知识点", type: "point" },
    { id: "o1", key: "9.9", label: "野知识点", type: "point" },
  ],
  edges: [],
};
const orphanOut = knowledgeGraphLayout(orphanGraph);
if (orphanOut.nodes.length !== 3) note(`含野知识点的图谱只排出 ${orphanOut.nodes.length} 个节点（应为 3，不许丢）`);
const orphanNode = orphanOut.nodes.find((n) => n.id === "o1");
if (!orphanNode || !Number.isFinite(orphanNode.x)) note("野知识点没拿到坐标（被静默丢掉）");
if (orphanNode && orphanNode.parentId !== null) note("野知识点的 parentId 应当为 null（它没有父章）");
if (orphanOut.edges.filter((e) => e.kind === "hierarchy").length !== 1) note("含野知识点时层级边应只有 1 条（只给 1.1）");

// ⑩ 空图谱不炸
const emptyOut = knowledgeGraphLayout({ nodes: [], edges: [] });
if (emptyOut.nodes.length !== 0 || emptyOut.edges.length !== 0) note("空图谱应当返回空结果");

if (failures.length > 0) {
  console.error("前端「知识图谱树形布局」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(
  `前端知识图谱布局断言通过：${out.nodes.length} 个节点全部有坐标（${chapters.length} 章 + ${points.length} 知识点）、`
  + `前置边 ${prereq.length} 条 + 层级边 ${hierarchy.length} 条、${belowCount}/${points.length} 个知识点在所属章下方、`
  + `${tightCount}/${firstRowGap.size} 章的正下方紧跟它自己的知识点（每章知识点每行最多 ${POINTS_PER_ROW} 个）、节点矩形相交 ${overlapPairs} 对、`
  + `同层相邻最小净间隙 ${minSameBandGap}px（下限 ${NODESEP}px = 节点宽 + 8；${sameBandPairs} 对全部达标，对应中心距 ≥ ${NODE_W + NODESEP}px）、`
  + `章子树矩形两两不相交、包围盒 ${out.width}×${out.height}（留白 ${MARGIN}）`,
);
