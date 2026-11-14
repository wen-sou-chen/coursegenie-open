import { useMemo } from "react";
import {
  ArcElement, BarElement, CategoryScale, Chart as ChartJS, Legend, LinearScale,
  LineElement, PointElement, Tooltip as ChartTooltip,
} from "chart.js";
import { Bar, Line, Pie } from "react-chartjs-2";
import { Empty, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import FlowDiagram from "./FlowDiagram";
import { alignChartData, hexToRgba, paletteHex, useChartPalette } from "../lib/chartTheme";
import type { ContentRow } from "../types";

ChartJS.register(
  CategoryScale, LinearScale, BarElement, PointElement, LineElement, ArcElement,
  ChartTooltip, Legend,
);

type ChartData = {
  labels?: string[];
  datasets?: Array<Record<string, unknown>>;
  mermaid?: string;
  headers?: string[];
  rows?: unknown[][];
  svg_preview?: string;
};

/**
 * 图表「源数据」黄标：labels 与 data 长度不符（真实库里 3/14 张）时按序号补齐并显式提示。
 * 后端生成/读取层也会把问题写进 `meta.chart_data_issues`，这里只负责如实展示。
 */
export function ChartDataIssuesTag({
  meta,
}: {
  meta?: Record<string, unknown>;
}) {
  const raw = meta?.chart_data_issues;
  const issues = Array.isArray(raw)
    ? (raw as { field?: string; expected?: number; actual?: number }[])
    : [];
  if (issues.length === 0) return null;
  const detail = issues
    .map((i) => `${i.field ?? "data"}（应有 ${i.expected ?? "?"} 项，实际 ${i.actual ?? "?"} 项）`)
    .join("、");
  return (
    <Tooltip
      title={`这张图的原始数据长度不符：${detail}。已按最长的一方补齐标签（第N项）/ 补 0，不必担心画错位置；建议重做这张图拿到干净数据。`}
    >
      <Tag color="warning" style={{ cursor: "help" }}>数据长度已补齐 {issues.length} 处</Tag>
    </Tooltip>
  );
}

/**
 * 把后端数据转成 Chart.js 数据集：**按主题色板兜底上色**（库里没存颜色也不灰）+ 长度对齐。
 *
 * 兜底很重要：真实库 14/14 张老图表都没有 `backgroundColor`，chart.js 默认色是浅灰
 * `rgba(0,0,0,0.1)`，所以这里按 dataset 序号补主题色（折线用半透明填充），老数据不重生成
 * 也是彩色的。
 */
function useChartSeries(row: ContentRow, mode: "bar" | "line" | "pie") {
  const colors = useChartPalette();
  const body = (row.body ?? {}) as { data?: ChartData };
  const rawSets = body.data?.datasets ?? [];
  const labels = (body.data?.labels ?? []).map((v) => String(v));
  const series = rawSets.map((ds) =>
    Array.isArray(ds.data)
      ? ds.data.map((v) => (typeof v === "number" ? v : Number(v) || 0))
      : [],
  );
  const aligned = alignChartData(labels, series);
  const datasets = rawSets.map((ds, i) => {
    const fallback = paletteHex(colors, i);
    const background = ds.backgroundColor as string | string[] | undefined;
    return {
      label: typeof ds.label === "string" ? ds.label : "",
      data: aligned.series[i] ?? [],
      backgroundColor: background ?? (mode === "line" ? hexToRgba(fallback) : fallback),
      borderColor: (ds.borderColor as string | undefined) ?? fallback,
    };
  });
  return { labels: aligned.labels, datasets };
}

const commonOptions = (title: string) => ({
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { position: "bottom" as const },
    title: { display: true, text: title },
  },
});

function BarLineChart({ row, mode }: { row: ContentRow; mode: "bar" | "line" }) {
  const data = useChartSeries(row, mode);
  return (
    <div style={{ height: 300, position: "relative" }}>
      {mode === "bar" ? (
        <Bar data={data} options={{ ...commonOptions(row.title), scales: { y: { beginAtZero: true } } }} />
      ) : (
        <Line data={data} options={{ ...commonOptions(row.title), scales: { y: { beginAtZero: true } } }} />
      )}
    </div>
  );
}

function PieChartView({ row }: { row: ContentRow }) {
  const data = useChartSeries(row, "pie");
  return (
    <div style={{ height: 300, position: "relative" }}>
      <Pie data={data} options={commonOptions(row.title)} />
    </div>
  );
}

function TableChartView({ row }: { row: ContentRow }) {
  const body = (row.body ?? {}) as { data?: ChartData };
  const headers = body.data?.headers ?? [];
  const rows = body.data?.rows ?? [];
  const columns: ColumnsType<Record<string, string | number>> = headers.map((h, i) => ({
    title: String(h),
    dataIndex: `c${i}`,
    key: `c${i}`,
    ellipsis: true,
  }));
  const dataSource = rows.map((r, ri) => {
    const rec: Record<string, string | number> = { key: ri };
    (Array.isArray(r) ? r : []).forEach((cell, ci) => {
      rec[`c${ci}`] = typeof cell === "string" || typeof cell === "number" ? cell : String(cell);
    });
    return rec;
  });
  if (headers.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="表格数据为空（生成后仍可重新生成）" />;
  }
  return (
    <Table
      size="small"
      bordered
      columns={columns}
      dataSource={dataSource}
      pagination={false}
      scroll={{ x: true }}
    />
  );
}

function MermaidChartView({ code, preview }: { code: string; preview?: string }) {
  if (!code.trim()) {
    if (preview) {
      return (
        <div style={{ textAlign: "center" }}>
          <img src={preview} alt="数据图预览" style={{ maxWidth: "100%", height: "auto" }} />
        </div>
      );
    }
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="图源码为空（生成后仍可重新生成）" />;
  }
  return <FlowDiagram code={code} />;
}

/** 按 ContentItem 的 kind（chart_bar/chart_line/...）渲染对应数据图。 */
export default function ChartRenderer({ row }: { row: ContentRow }) {
  const kind = row.kind.replace(/^chart_/, "");
  const body = (row.body ?? {}) as { data?: ChartData; mermaid?: string; svg_preview?: string };
  const mermaidCode = useMemo(
    () => String(body.data?.mermaid ?? body.mermaid ?? ""),
    [body.data, body.mermaid],
  );

  if (kind === "bar") return <BarLineChart row={row} mode="bar" />;
  if (kind === "line") return <BarLineChart row={row} mode="line" />;
  if (kind === "pie") return <PieChartView row={row} />;
  if (kind === "table") return <TableChartView row={row} />;
  // 象限图（任务 122）与思维导图/时间轴同一条路：服务端已出 SVG 预览，直接显示。
  if (kind === "mindmap" || kind === "timeline" || kind === "quadrant") {
    const preview = body.svg_preview ? `/${body.svg_preview}` : undefined;
    const label = kind === "mindmap" ? "思维导图" : kind === "timeline" ? "时间轴" : "象限图";
    return (
      <div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {label}
        </Typography.Text>
        <MermaidChartView code={mermaidCode} preview={preview} />
      </div>
    );
  }
  return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`未知数据图类型：${row.kind}`} />;
}
