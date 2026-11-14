import { useMemo, useState } from "react";
import SectionCard from "../ui/SectionCard";
import {
  Button, Checkbox, Empty, Input, Popconfirm, Space, Spin, Tag, Typography, message,
} from "antd";
import { BarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { api } from "../../api";
import type { ContentRow } from "../../types";
import ChartRenderer, { ChartDataIssuesTag } from "../ChartRenderer";
import VaultStatusTag from "../VaultStatusTag";
export const CHART_KINDS = ["bar", "line", "pie", "mindmap", "timeline", "table", "quadrant"] as const;
export const CHART_TYPE_OPTIONS = [
  { value: "bar", label: "柱状图" },
  { value: "line", label: "折线图" },
  { value: "pie", label: "饼图" },
  { value: "mindmap", label: "思维导图" },
  { value: "timeline", label: "时间轴" },
  { value: "table", label: "表格" },
  { value: "quadrant", label: "象限图" },
];
const TYPE_LABEL: Record<string, string> = {
  bar: "柱状图", line: "折线图", pie: "饼图",
  mindmap: "思维导图", timeline: "时间轴", table: "表格", quadrant: "象限图",
};
const TYPE_COLOR: Record<string, string> = {
  bar: "blue", line: "cyan", pie: "magenta",
  mindmap: "purple", timeline: "geekblue", table: "green", quadrant: "volcano",
};
export default function ChartSection({
  courseId, nodeId, approved, rows, refresh,
}: {
  courseId: string;
  nodeId: string;
  approved: boolean;
  rows: ContentRow[];
  refresh: () => void;
}) {
  const [picked, setPicked] = useState<string[]>(["bar"]);
  const [instruction, setInstruction] = useState("");
  const [running, setRunning] = useState<string[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const chartRows = useMemo(
    () => rows.filter((r) => r.kind.startsWith("chart_") && r.node_id === nodeId),
    [rows, nodeId],
  );
  const groups = useMemo(() => {
    const map = new Map<string, ContentRow[]>();
    for (const r of chartRows) {
      const k = r.kind.replace(/^chart_/, "");
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(r);
    }
    return CHART_KINDS.filter((k) => map.has(k)).map((k) => ({ kind: k, items: map.get(k)! }));
  }, [chartRows]);
  async function runGenerate(types: string[]) {
    setRunning(types);
    const failures: string[] = [];
    let ok = 0;
    // 顺序生成：并发请求容易撞上免费档限流（429），逐个来更稳
    for (const t of types) {
      try {
        await api.generateChart({
          course_id: courseId,
          node_id: nodeId,
          chart_type: t,
          instruction: instruction.trim() || undefined,
        });
        ok += 1;
      } catch (e) {
        failures.push(`${TYPE_LABEL[t] ?? t}：${e instanceof Error ? e.message : String(e)}`);
      }
    }
    if (ok > 0) {
      message.success(`已生成 ${ok} 个数据图`);
      setInstruction("");
      refresh();
    }
    failures.forEach((f) => message.error(`生成失败：${f}`));
    if (types.length > 0 && ok === 0 && failures.length === 0) {
      message.error("数据图生成失败，请稍后重试");
    }
    setRunning([]);
  }
  const handleGenerate = () => {
    if (!approved) {
      message.warning("请先在大纲步骤点「大纲通过」，锁定大纲后再生成数据图");
      return;
    }
    if (picked.length === 0) {
      message.warning("请先勾选要生成的图形类型");
      return;
    }
    void runGenerate(picked);
  };
  const redoOne = async (row: ContentRow) => {
    const k = row.kind.replace(/^chart_/, "");
    setBusyId(row.id);
    try {
      await api.generateChart({
        course_id: courseId,
        node_id: nodeId,
        chart_type: k,
        instruction: String((row.body as { instruction?: string })?.instruction ?? row.title),
        item_id: row.id,
      });
      message.success(`已重新生成${TYPE_LABEL[k] ?? "数据图"}`);
      refresh();
    } catch (e) {
      message.error(`重新生成失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusyId(null);
    }
  };
  const removeOne = async (row: ContentRow) => {
    await api.deleteChart(row.id);
    message.success("已删除该数据图");
    refresh();
  };
  const totalCharts = chartRows.length;
  return (
    <SectionCard
      size="small"
      title={`数据图（${totalCharts}）· 柱状/折线/饼图/思维导图/时间轴/表格`}
      extra={
        <Button
          type="primary"
          size="small"
          icon={<BarChartOutlined />}
          disabled={!approved}
          onClick={handleGenerate}
          loading={running.length > 0}
        >
          生成所选图形（{picked.length}）
        </Button>
      }
    >
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        <SectionCard size="small" type="inner" title="① 勾选图形类型（可多选）">
          <Space direction="vertical" style={{ width: "100%" }} size={8}>
            <Checkbox.Group
              value={picked}
              onChange={(vals) => setPicked(vals.map(String))}
              options={CHART_TYPE_OPTIONS}
            />
            <Space wrap style={{ width: "100%" }}>
              <Input.TextArea
                rows={2}
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="输入数据图的自然语言要求，如：近五年全球气温变化（每个类型都会按此要求生成）"
              />
            </Space>
          </Space>
        </SectionCard>
        <SectionCard size="small" type="inner" title="② 生成结果（按类型分组）">
          {running.length > 0 && (
            <Space style={{ marginBottom: 8 }}>
              <Spin size="small" />
              <Typography.Text type="secondary">
                正在生成：{running.map((t) => TYPE_LABEL[t]).join("、")}…
              </Typography.Text>
            </Space>
          )}
          {groups.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="还没有本节数据图。勾选上方类型并点「生成所选图形」，生成后自动增量写入 Obsidian 笔记（若已绑定 Vault）。"
            />
          ) : (
            <Space direction="vertical" style={{ width: "100%" }} size="middle">
              {groups.map((g) => (
                <div key={g.kind}>
                  <Space wrap style={{ marginBottom: 8 }}>
                    <Tag color={TYPE_COLOR[g.kind]}>{TYPE_LABEL[g.kind]}</Tag>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {g.items.length} 个
                    </Typography.Text>
                  </Space>
                  <Space direction="vertical" style={{ width: "100%" }} size="middle">
                    {g.items.map((row) => (
                      <SectionCard
                        key={row.id}
                        size="small"
                        type="inner"
                        title={
                          <Space wrap>
                            <Typography.Text strong>{row.title}</Typography.Text>
                          </Space>
                        }
                        extra={
                          <Space size={6} wrap>
                            <ChartDataIssuesTag meta={row.meta as Record<string, unknown> | undefined} />
                            <VaultStatusTag
                              vault={row.meta?.vault as Record<string, unknown> | undefined}
                              itemId={row.id}
                              kind="content"
                              onChanged={refresh}
                            />
                            <Button
                              size="small"
                              type="link"
                              icon={<ReloadOutlined />}
                              loading={busyId === row.id}
                              onClick={() => redoOne(row)}
                            >
                              重新生成
                            </Button>
                            <Popconfirm title="删除该数据图？" onConfirm={() => removeOne(row)}>
                              <Button size="small" type="link" danger>
                                删除
                              </Button>
                            </Popconfirm>
                          </Space>
                        }
                      >
                        <ChartRenderer row={row} />
                      </SectionCard>
                    ))}
                  </Space>
                </div>
              ))}
            </Space>
          )}
        </SectionCard>
      </Space>
    </SectionCard>
  );
}
