import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background, Controls, MarkerType, Panel, ReactFlow, applyEdgeChanges, applyNodeChanges,
} from "@xyflow/react";
import type { Edge, EdgeChange, Node, NodeChange } from "@xyflow/react";
import type { ReactFlowInstance } from "@xyflow/react";
import { Button, Space, Typography } from "antd";
import { ClearOutlined } from "@ant-design/icons";
import type { GraphData } from "../types";
import {
  LABEL_FONT_PX, MIN_READABLE_ZOOM, NODE_H, NODE_W, knowledgeGraphLayout,
} from "../lib/knowledgeGraphLayout";

/**
 * fitView 口径（任务 71 ①）：minZoom 就是可读下限 —— fitView 算出来再小也不缩到它以下，
 * 放不下就让人拖动 / 自己缩小（手动缩放仍可到 0.25）。纯函数口径见 eadableZoom()。
 */
const FIT_VIEW_OPTIONS = { padding: 0.12, maxZoom: 1.1, minZoom: MIN_READABLE_ZOOM };

/**
 * 大纲知识图谱（任务 68）。
 *
 * 布局算法（层级边、间距口径、章子树码放）在 `../lib/knowledgeGraphLayout.ts` 的纯函数
 * `knowledgeGraphLayout`（旧名 `dagreLayout`，本轮不再用 dagre，见该文件头注释）里 ——
 * 抽出去是为了让 `scripts/check-knowledge-graph-layout.ts` 能用真图谱
 * fixture 直接断言坐标。本组件只负责把坐标变成 ReactFlow 的节点与边：
 *   · 章 = 蓝底 / 知识点 = 绿底（沿用原样式，高度固定成布局用的 46px，几何与断言一致）；
 *   · 「章 → 章」前置边 = 原样的实线 + 箭头；
 *   · 「父章 → 知识点」层级边 = 浅色细虚线（仅渲染用，不写库、不改接口）。
 *
 * 任务 71 追加：默认缩放不低于可读下限（FIT_VIEW_OPTIONS.minZoom）、右上角显示当前缩放、
 * 多一个「100%」按钮（原来的「重置布局」= 回到 fitView，「适应画布」按钮在 ReactFlow 自带的 Controls 里）。
 */
export default function KnowledgeGraph({ graph }: { graph: GraphData }) {
  const initial = useMemo(() => {
    const layout = knowledgeGraphLayout(graph);

    const nodes: Node[] = layout.nodes.map((n) => {
      const chapter = n.kind === "chapter";
      return {
        id: n.id,
        position: { x: n.x, y: n.y },
        data: {
          label: (
            <Typography.Text
              style={{ fontSize: LABEL_FONT_PX, color: chapter ? "#1f3d7a" : "#256029" }}
            >
              {n.key} {n.label}
            </Typography.Text>
          ),
        },
        style: {
          background: chapter ? "#e8f0fe" : "#eefaf0",
          border: chapter ? "1.5px solid #4f8cff" : "1.5px solid #57b26a",
          borderRadius: 10,
          padding: "4px 8px",
          width: NODE_W,
          height: NODE_H,
          display: "flex",
          alignItems: "center",
          boxShadow: "0 1px 4px rgba(15,23,42,0.08)",
        },
      };
    });

    const edges: Edge[] = layout.edges.map((e) => (
      e.kind === "hierarchy"
        ? {
            id: e.id,
            source: e.source,
            target: e.target,
            type: "smoothstep",
            style: { stroke: "#cbd5e1", strokeWidth: 1, strokeDasharray: "4 4" },
          }
        : {
            id: e.id,
            source: e.source,
            target: e.target,
            label: e.label,
            markerEnd: { type: MarkerType.ArrowClosed },
            type: "smoothstep",
            style: { stroke: "#94a3b8" },
          }
    ));

    return { nodes, edges };
  }, [graph]);

  const sig = useMemo(() => graph.nodes.map((n) => n.id).join("|"), [graph]);
  const prevSig = useRef(sig);

  const [nodes, setNodes] = useState<Node[]>(initial.nodes);
  const [edges, setEdges] = useState<Edge[]>(initial.edges);
  // 任务 71 ①：拿到 ReactFlow 实例（调 fitView / zoomTo）并跟踪当前缩放，好在角落显示出来
  const [rf, setRf] = useState<ReactFlowInstance | null>(null);
  const [zoom, setZoom] = useState<number | null>(null);

  /** 同步「当前缩放」显示（程序化 fitView / zoomTo 之后补读一次，onMove 只覆盖手动操作）。 */
  const syncZoom = () => {
    if (rf) setZoom(rf.getZoom());
  };

  // 大纲重建（节点 id 集合变化）时自动恢复初始布局
  useEffect(() => {
    if (prevSig.current !== sig) {
      prevSig.current = sig;
      setNodes(initial.nodes);
      setEdges(initial.edges);
    }
  }, [sig, initial]);

  const onNodesChange = (changes: NodeChange[]) =>
    setNodes((nds) => applyNodeChanges(changes, nds) as Node[]);
  const onEdgesChange = (changes: EdgeChange[]) =>
    setEdges((eds) => applyEdgeChanges(changes, eds) as Edge[]);

  const resetLayout = () => {
    prevSig.current = sig;
    setNodes(initial.nodes);
    setEdges(initial.edges);
    // 回到「适应画布 + 可读下限」（与首次打开同一条口径）
    void rf?.fitView(FIT_VIEW_OPTIONS).then(syncZoom);
  };

  /** 回到 100%（1 像素 = 1 图坐标单位）——「一屏放不下」时的第二档：看得清、自己拖。 */
  const zoomTo100 = () => {
    void rf?.zoomTo(1, { duration: 200 }).then(syncZoom);
  };

  /** 适应画布：把整幅图放进画布，但**不缩到可读下限以下**（与首次打开同一条口径）。 */
  const fitToCanvas = () => {
    void rf?.fitView(FIT_VIEW_OPTIONS).then(syncZoom);
  };

  if (graph.nodes.length === 0) {
    return <Typography.Text type="secondary">暂无图谱数据：生成大纲后自动出现。</Typography.Text>;
  }

  return (
    <div style={{ height: "78vh", width: "100%" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onInit={(inst) => {
          setRf(inst);
          setZoom(inst.getZoom());
        }}
        onMove={(_, viewport) => setZoom(viewport.zoom)}
        fitView
        fitViewOptions={FIT_VIEW_OPTIONS}
        nodesDraggable
        minZoom={0.25}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#e2e8f0" />
        <Controls showInteractive={false} showFitView={false} />
        <Panel position="top-right">
          <Space>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {graph.nodes.length} 节点 · 可拖拽调整
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              缩放 {zoom === null ? "—" : `${Math.round(zoom * 100)}%`}
              {zoom !== null && zoom < 1 ? "（可拖动 / 自己缩小）" : ""}
            </Typography.Text>
            {/* 不用 ReactFlow 自带的 fitView 按钮：它不吃 fitViewOptions（实测会缩到 0.556 < 可读下限） */}
            <Button size="small" onClick={fitToCanvas}>
              适应画布
            </Button>
            <Button size="small" onClick={zoomTo100}>
              100%
            </Button>
            <Button size="small" icon={<ClearOutlined />} onClick={resetLayout}>
              重置布局
            </Button>
          </Space>
        </Panel>
      </ReactFlow>
    </div>
  );
}
