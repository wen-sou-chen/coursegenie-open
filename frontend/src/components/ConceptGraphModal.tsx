import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Modal, Space, Statistic, Tabs, Tag, Typography, message } from "antd";
import { CloudDownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import FlowDiagram from "./FlowDiagram";
import { api } from "../api";

/**
 * 概念知识图谱预览（任务 24-A）：页面内直接渲染 Mermaid，并可一键导出到笔记库
 * （概念库/_图谱.md + _图谱.canvas）。孤立概念 / 库外关系目标显式列出，不静默丢弃。
 */
export default function ConceptGraphModal({
  courseId,
  open,
  onClose,
}: {
  courseId: string;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [tab, setTab] = useState("mermaid");

  const graph = useQuery({
    queryKey: ["conceptGraph", courseId, "mermaid"],
    queryFn: () => api.conceptGraph(courseId, "mermaid"),
    enabled: open,
    retry: false,
  });

  const canvas = useQuery({
    queryKey: ["conceptCanvas", courseId],
    queryFn: () => api.conceptGraph(courseId, "canvas"),
    enabled: open && tab === "canvas",
    retry: false,
  });

  const exportToNotes = useMutation({
    mutationFn: () => api.exportConceptGraph(courseId),
    onSuccess: (res) => {
      message.success(`已导出到笔记：${res.files.length} 个文件（概念库/_图谱.md + _图谱.canvas）`);
      void qc.invalidateQueries({ queryKey: ["concepts", courseId] });
    },
    onError: (e: Error) => message.error(`导出失败：${e.message}`),
  });

  const data = graph.data;
  const stats = data?.stats;

  return (
    <Modal
      title="概念知识图谱"
      open={open}
      onCancel={onClose}
      width={1000}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
        <Button
          key="export"
          type="primary"
          icon={<CloudDownloadOutlined />}
          loading={exportToNotes.isPending}
          disabled={!data || data.stats.nodes === 0}
          onClick={() => exportToNotes.mutate()}
        >
          导出到笔记
        </Button>,
      ]}
    >
      {graph.isLoading && <Typography.Text type="secondary">读取图谱中…</Typography.Text>}
      {graph.isError && (
        <Alert
          type="warning"
          showIcon
          message="暂时画不出图谱"
          description={(graph.error as Error).message}
        />
      )}
      {data && (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Space wrap size="large">
            <Statistic title="概念" value={stats?.nodes ?? 0} />
            <Statistic title="关系" value={stats?.edges ?? 0} />
            <Statistic title="孤立概念" value={stats?.isolated ?? 0} />
            <Statistic title="库外目标" value={stats?.dropped ?? 0} />
          </Space>
          <Space wrap size={4}>
            {(data.chapters ?? []).map((chapter) => (
              <Tag key={chapter.key} color="blue">第{chapter.key}章 {chapter.title}</Tag>
            ))}
          </Space>
          {data.isolated.length > 0 && (
            <Alert
              type="info"
              showIcon
              message={`${data.isolated.length} 个概念还没有任何关系（图谱里是孤点）`}
              description={data.isolated.join("、")}
            />
          )}
          {data.dropped_relations.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message={`${data.dropped_relations.length} 条关系指向概念库里不存在的名字（未画边，仅列在这里）`}
              description={data.dropped_relations
                .map((row) => `${row.from} → ${row.target}（${row.kind}）`)
                .join("；")}
            />
          )}
          <Tabs
            activeKey={tab}
            onChange={setTab}
            items={[
              {
                key: "mermaid",
                label: "图谱渲染（Mermaid）",
                children: <FlowDiagram code={data.mermaid ?? ""} />,
              },
              {
                key: "canvas",
                label: "Obsidian Canvas",
                children: canvas.isLoading ? (
                  <Typography.Text type="secondary">读取中…</Typography.Text>
                ) : canvas.isError ? (
                  <Alert type="warning" showIcon message={(canvas.error as Error).message} />
                ) : (
                  <Space direction="vertical" style={{ width: "100%" }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      Canvas 节点 {canvas.data?.canvas?.nodes.length ?? 0} 个 · 边{" "}
                      {canvas.data?.canvas?.edges.length ?? 0} 条；导出后可在 Obsidian 里拖拽编辑。
                    </Typography.Text>
                    <pre style={{ maxHeight: 360, overflow: "auto", background: "#f6f8fa", padding: 8, borderRadius: 6, fontSize: 12 }}>
                      {JSON.stringify(canvas.data?.canvas ?? {}, null, 2)}
                    </pre>
                  </Space>
                ),
              },
            ]}
          />
          <Space>
            <Button
              icon={<ReloadOutlined />}
              size="small"
              onClick={() => {
                void graph.refetch();
                void canvas.refetch();
              }}
            >
              刷新预览
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              导出落点：{data.files.join("、")}
            </Typography.Text>
          </Space>
        </Space>
      )}
    </Modal>
  );
}
