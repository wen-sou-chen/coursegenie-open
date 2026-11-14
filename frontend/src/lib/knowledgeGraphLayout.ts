import type { GraphData } from "../types";

/**
 * 知识图谱（大纲树）的纯布局函数（任务 68）。
 *
 * 起因（DSH 2026-09-20 走查第 1 页只读取证）：AI 课 39 个节点里，第 1 章 + 全部 27 个知识点被挤在
 * 同一条水平带（y=636）上 —— 同层净间隙只有 28px、fitView 之后整幅图缩到 0.25 倍（节点 43px、
 * 字号 3.2px），视觉上糊成一条、字读不出来；第 2–12 章各自一行斜向排下去。根因：`GET /api/plan/{id}`
 * 的 `graph.edges` 只有 11 条「章 → 章」前置边（27 个知识点零边），旧实现把 `graph.edges` 直接交给
 * dagre ⇒ 孤立节点全落进第一层横排；再叠加「节点数 ≥15 就把间距压到 28px」（小于节点高 46px）。
 *
 * 做法：
 * ① 按 key 前缀补「父章 → 知识点」的层级边（`2.3` → `2`；仅渲染用，不写库、不改接口），真实前置边
 *    照旧保留，两类边由组件用不同样式画（前置 = 实线带箭头 / 层级 = 浅色细虚线）；
 * ② 每一章排成一块子树：章在最上，它的知识点按 `POINTS_PER_ROW` 换行排在章**正下方**
 *    （间距 NODESEP / RANKSEP，都按节点尺寸算），章水平居中于整块之上；
 * ③ 再把这些子树逐列往下码放（列数取「整幅图周长最小」的取值）—— 章的前置链在列内自上而下读，
 *    整图不再是一条斜向阶梯。
 *
 * 为什么不用 dagre（实测，详见 backend/docs/task68-knowledge-graph-layout.md §3）：
 * · `compound`（setParent 让章成为 cluster）：本图谱上 `dagre.layout` 直接抛
 *   `TypeError: Cannot set properties of undefined (setting 'rank')`；
 * · 只补层级边让 dagre 全图排：实测 3692×1438 的斜向阶梯，而「大纲与知识图谱」这块画布实测只有
 *   745×814（fitView 后 0.20 倍 ⇒ 字号 2.6px），比修之前还小 ⇒ 不可用。
 * 因此本函数自己算坐标（等价于「层级边 + 章先定 rank、子节点落下一 rank」，只是把「孤立节点别挤成一排」
 * 这件事做在码放这一层）。不要 dagre 之后它在本仓库已无使用点（见文档 §7）。
 *
 * 本文件不依赖 React/antd，好让 `scripts/check-knowledge-graph-layout.ts` 用真图谱 fixture 直接断言
 * 坐标（Node ≥ 22.18 自带类型擦除）。
 */

/** 节点尺寸（与组件样式一致，也是 DSH 取证里的 172×46）。 */
export const NODE_W = 172;
export const NODE_H = 46;
/** 同层相邻节点的水平净间隙下限：≥ 节点宽 + 8。 */
export const NODESEP = NODE_W + 8;
/** 相邻层的垂直净间隙下限：≥ 节点高 + 24。 */
export const RANKSEP = NODE_H + 24;
/** 整幅图四周留白。 */
export const MARGIN = 24;
/**
 * 每章的知识点每行最多放几个，多的换行（任务 71 ③ 实测后**保持 2**，理由与数据见文档 §3.3）：
 * 均匀 2 个/行 ⇒ 单块宽 524、整幅图 1276×1452 —— 在 0.8 倍视图里（可见约 1840 宽）**整幅宽度都放得下**，
 * 只需上下拖动；而「5 个点排 3+2」把单块撑到 876 ⇒ 整幅图 1628×1336、面积 +17%、周长 +236，
 * 按「让包围盒更紧凑」的原意并不更优，故保持 2（列数仍自动取，实测 2 列）。
 */
export const POINTS_PER_ROW = 2;
/**
 * 列内「上一块最后一行 → 下一块的章」的间距（任务 71 ③）：从 RANKSEP(70) 收到 40 —— 块**内**
 * 章→知识点仍用 RANKSEP（校验 ③ 的下限不许动），这里只是两块子树之间的过道，收紧只影响观感与包围盒高度。
 */
export const BLOCK_GAP = 40;

// ── 可读性（任务 71 ①）───────────────────────────────────────────────────────────
// 68 修完结构后，真正的问题变成「fitView 之后字太小」（0.468 × 13px ≈ 6.1px）。这里把「渲染后可读」
// 这件事写成**可断言的纯函数**：默认缩放不得低于 MIN_READABLE_ZOOM，字号 = LABEL_FONT_PX × 缩放。
/** 节点标签字号（与组件里 Typography.Text 的 fontSize 一致）。 */
export const LABEL_FONT_PX = 13;
/**
 * 默认缩放下限。取 **0.8** 而不是 0.75：0.75 × 13px = **9.75px，严格小于 10px**；
 * 0.8 × 13px = **10.4px** ⇒ 「缩放 ≥ 0.75」与「字号 ≥ 10px」两条判据同时成立。
 */
export const MIN_READABLE_ZOOM = 0.8;
/** 可读字号下限（px）。 */
export const MIN_READABLE_FONT_PX = 10;
/**
 * 默认（fitView）缩放的可读兜底：fitView 算出来的缩放再小也不低于 MIN_READABLE_ZOOM ——
 * 放不下就让人拖动 / 自己缩小，而不是把 39 个节点缩成一团糊。
 * 组件里用的是同一套口径（fitViewOptions.minZoom = MIN_READABLE_ZOOM），
 * scripts/check-knowledge-graph-readability.ts 既断言本函数、也断言组件确实接了这个常量。
 */
export function readableZoom(fitZoom: number): number {
  return Math.max(fitZoom, MIN_READABLE_ZOOM);
}

/** 有效字号（px）= 标签字号 × 渲染缩放。 */
export function effectiveFontPx(zoom: number, fontPx: number = LABEL_FONT_PX): number {
  return zoom * fontPx;
}

export type LayoutNodeKind = "chapter" | "point";
export type LayoutEdgeKind = "prereq" | "hierarchy";

export interface LayoutNode {
  id: string;
  key: string;
  label: string;
  kind: LayoutNodeKind;
  /** 节点左上角坐标（ReactFlow 的 `position`）。 */
  x: number;
  y: number;
  width: number;
  height: number;
  /** 所属章 id（知识点才有；章与孤立节点为 null）。 */
  parentId: string | null;
}

export interface LayoutEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  kind: LayoutEdgeKind;
}

export interface GraphLayout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  /** 布局包围盒（含留白），便于断言。 */
  width: number;
  height: number;
}

type GraphNode = GraphData["nodes"][number];

interface Block {
  nodes: LayoutNode[];
  width: number;
  height: number;
}

/** 章 = key 里没有「.」（`1`、`12`）；知识点 = `1.1` 这种（与旧组件口径一致）。 */
export function isChapterKey(key: string): boolean {
  return !key.includes(".");
}

/** 知识点按 key 前缀找父章的候选：`2.3` → [`2`]；`2.3.1` → [`2.3`, `2`]。 */
export function parentKeyCandidates(key: string): string[] {
  const out: string[] = [];
  const lastDot = key.lastIndexOf(".");
  if (lastDot > 0) out.push(key.slice(0, lastDot));
  const firstDot = key.indexOf(".");
  if (firstDot > 0) {
    const head = key.slice(0, firstDot);
    if (!out.includes(head)) out.push(head);
  }
  return out;
}

/** 在已有章 key 里取第一个命中的前缀；找不到返回 null（按孤立节点处理，不丢节点）。 */
export function parentKeyOf(key: string, chapterKeys: ReadonlySet<string>): string | null {
  for (const candidate of parentKeyCandidates(key)) {
    if (chapterKeys.has(candidate)) return candidate;
  }
  return null;
}

/** 一行 `count` 个节点时的行宽（节点之间留 NODESEP）。 */
function rowWidth(count: number): number {
  return count === 0 ? 0 : count * NODE_W + (count - 1) * NODESEP;
}

function makeNode(node: GraphNode, x: number, y: number): LayoutNode {
  return {
    id: node.id,
    key: node.key,
    label: node.label,
    kind: isChapterKey(node.key) ? "chapter" : "point",
    x,
    y,
    width: NODE_W,
    height: NODE_H,
    parentId: null,
  };
}

/**
 * 排一块子树：`chapter`（可为 null，表示「找不到父章的知识点」这一块）+ 它的知识点。
 * 章在最上、水平居中；知识点按 `POINTS_PER_ROW` 换行排在章正下方（相邻两行隔 RANKSEP）。
 */
function layoutBlock(chapter: GraphNode | null, points: GraphNode[]): Block {
  const rows: GraphNode[][] = [];
  for (let i = 0; i < points.length; i += POINTS_PER_ROW) {
    rows.push(points.slice(i, i + POINTS_PER_ROW));
  }
  const width = Math.max(chapter ? NODE_W : 0, ...rows.map((r) => rowWidth(r.length)), 0);
  const nodes: LayoutNode[] = [];
  let y = 0;
  if (chapter) {
    nodes.push(makeNode(chapter, (width - NODE_W) / 2, y));
    y += NODE_H + RANKSEP;
  }
  for (const row of rows) {
    let x = (width - rowWidth(row.length)) / 2;
    for (const p of row) {
      nodes.push(makeNode(p, x, y));
      x += NODE_W + NODESEP;
    }
    y += NODE_H + RANKSEP;
  }
  const height = nodes.length === 0 ? 0 : Math.max(...nodes.map((n) => n.y + n.height));
  return { nodes, width, height };
}

/** 按「每列 `perColumn` 块」码放时整幅图的宽高（不含留白）。 */
function packedSize(blocks: Block[], columns: number): { width: number; height: number } {
  const perColumn = Math.ceil(blocks.length / columns);
  let width = 0;
  let height = 0;
  for (let start = 0; start < blocks.length; start += perColumn) {
    const group = blocks.slice(start, start + perColumn);
    width += Math.max(...group.map((b) => b.width));
    const columnHeight = group.reduce((sum, b) => sum + b.height, 0) + (group.length - 1) * BLOCK_GAP;
    height = Math.max(height, columnHeight);
  }
  return { width: width + (columns - 1) * NODESEP, height };
}

/** 列数取「整幅图周长最小」的取值（越小越方）。实测 12 章 ⇒ 2 列。 */
function pickColumnCount(blocks: Block[]): number {
  let best = 1;
  let bestScore = Number.POSITIVE_INFINITY;
  for (let columns = 1; columns <= blocks.length; columns += 1) {
    const { width, height } = packedSize(blocks, columns);
    const score = width + height;
    if (score < bestScore) {
      bestScore = score;
      best = columns;
    }
  }
  return best;
}

/**
 * 主入口：大纲图谱 → 带坐标的节点 + 要画的边。
 * 纯函数（同输入同输出、不碰 DOM），返回的节点坐标都是节点左上角。
 */
export function knowledgeGraphLayout(graph: GraphData): GraphLayout {
  const chapters = graph.nodes.filter((n) => isChapterKey(n.key));
  const chapterKeys = new Set(chapters.map((c) => c.key));

  const pointsOf = new Map<string, GraphNode[]>();
  const orphans: GraphNode[] = [];
  for (const n of graph.nodes) {
    if (isChapterKey(n.key)) continue;
    const parentKey = parentKeyOf(n.key, chapterKeys);
    if (parentKey === null) {
      // 异常数据：找不到父章 ⇒ 退回孤立节点，但不许静默丢节点
      orphans.push(n);
      continue;
    }
    const list = pointsOf.get(parentKey) ?? [];
    list.push(n);
    pointsOf.set(parentKey, list);
  }

  const blocks: Block[] = [];
  const parentOf = new Map<string, string>();
  for (const chapter of chapters) {
    const points = pointsOf.get(chapter.key) ?? [];
    for (const p of points) parentOf.set(p.id, chapter.id);
    blocks.push(layoutBlock(chapter, points));
  }
  if (orphans.length > 0) blocks.push(layoutBlock(null, orphans));

  const columns = blocks.length > 1 ? pickColumnCount(blocks) : 1;
  const perColumn = Math.ceil(blocks.length / columns);
  const nodes: LayoutNode[] = [];
  let columnX = MARGIN;
  for (let start = 0; start < blocks.length; start += perColumn) {
    const group = blocks.slice(start, start + perColumn);
    const columnWidth = Math.max(...group.map((b) => b.width));
    let blockY = MARGIN;
    for (const block of group) {
      for (const n of block.nodes) {
        nodes.push({ ...n, x: columnX + n.x, y: blockY + n.y, parentId: parentOf.get(n.id) ?? null });
      }
      blockY += block.height + BLOCK_GAP;
    }
    columnX += columnWidth + NODESEP;
  }

  const edges: LayoutEdge[] = [];
  const pairKeys = new Set<string>();
  const rendered = new Set<string>();
  for (const e of graph.edges) {
    pairKeys.add(`${e.source}->${e.target}`);
    pairKeys.add(`${e.target}->${e.source}`);
  }
  for (const e of graph.edges) {
    const id = `${e.source}->${e.target}`;
    if (rendered.has(id)) continue;
    rendered.add(id);
    edges.push({ id, source: e.source, target: e.target, label: e.label, kind: "prereq" });
  }
  for (const [pointId, chapterId] of parentOf) {
    const id = `${chapterId}->${pointId}`;
    if (pairKeys.has(id) || rendered.has(id)) continue;
    rendered.add(id);
    edges.push({ id, source: chapterId, target: pointId, kind: "hierarchy" });
  }

  const width = nodes.length === 0 ? 0 : Math.max(...nodes.map((n) => n.x + n.width)) + MARGIN;
  const height = nodes.length === 0 ? 0 : Math.max(...nodes.map((n) => n.y + n.height)) + MARGIN;
  return { nodes, edges, width, height };
}
