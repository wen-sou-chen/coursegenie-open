import { Popover, Space, Tag, Tooltip, Typography } from "antd";
import { ThunderboltOutlined } from "@ant-design/icons";
import type { UsageSummary } from "../types";

/** 调用用途中文名（与后端 usage.py 的 purpose 标注一致）。 */
export const PURPOSE_LABEL: Record<string, string> = {
  lecture: "讲义正文",
  cards: "概念卡/习题",
  diagram: "概念图",
  chart: "数据图",
  flowchart: "流程图",
  quiz: "随堂练习",
  concept: "概念库",
  planner: "大纲规划",
  outline: "大纲生成",
  material: "素材处理",
  other: "其它",
};

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

export function fmtCost(cny: number): string {
  return cny >= 1 ? cny.toFixed(2) : cny.toFixed(4);
}

export function fmtDuration(ms: number): string {
  if (ms >= 60000) return `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`;
  return `${(ms / 1000).toFixed(1)} 秒`;
}

/** 「X 次调用 · Y tokens · ≈Z 元 · 耗时 W 秒」——任务面板与讲义卡共用一句话口径。 */
export function usageText(usage: UsageSummary | null | undefined, withDuration = true): string | null {
  if (!usage || !usage.llm_calls) return null;
  const parts = [
    `${usage.llm_calls} 次调用`,
    `${fmtTokens(usage.total_tokens)} tokens`,
    `≈${fmtCost(usage.cost_cny)} 元`,
  ];
  if (withDuration) parts.push(`耗时 ${fmtDuration(usage.duration_ms)}`);
  return parts.join(" · ");
}

/**
 * 讲义卡用量徽标（任务 23-A）：点开看这次生成的调用明细。
 * 老讲义（meta.usage 缺失）不显示任何东西，避免噪声。
 */
export default function UsageBadge({ usage }: { usage: UsageSummary | null | undefined }) {
  const line = usageText(usage, false);
  if (!line || !usage) return null;
  const buckets = Object.entries(usage.by_purpose ?? {});

  const content = (
    <Space direction="vertical" size={4} style={{ maxWidth: 360 }}>
      <Typography.Text>{usageText(usage)}</Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens} tokens
        {(usage.models ?? []).length ? ` · 模型 ${(usage.models ?? []).join("、")}` : ""}
      </Typography.Text>
      {buckets.length > 0 && (
        <Space direction="vertical" size={2}>
          {buckets.map(([purpose, bucket]) => (
            <Typography.Text key={purpose} style={{ fontSize: 12 }}>
              {PURPOSE_LABEL[purpose] ?? purpose}：{bucket.calls} 次 · {bucket.total_tokens} tokens ·{" "}
              ≈{fmtCost(bucket.cost_cny)} 元
            </Typography.Text>
          ))}
        </Space>
      )}
      {usage.retried_calls ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          含重试 {usage.retried_calls} 次调用
        </Typography.Text>
      ) : null}
      {usage.estimated_calls ? (
        <Typography.Text type="warning" style={{ fontSize: 12 }}>
          其中 {usage.estimated_calls} 次无 usage 返回，tokens 为估算值
        </Typography.Text>
      ) : null}
      {usage.note ? (
        <Typography.Text type="warning" style={{ fontSize: 12 }}>
          {usage.note}
        </Typography.Text>
      ) : null}
    </Space>
  );

  return (
    <Popover content={content} title="本篇用量与成本" trigger="click">
      <Tooltip title="点击查看本次生成的调用明细">
        <Tag icon={<ThunderboltOutlined />} color={usage.unpriced_models?.length ? "orange" : "blue"}>
          {line}
          {usage.unpriced_models?.length ? "（未配置单价）" : ""}
        </Tag>
      </Tooltip>
    </Popover>
  );
}

/** 从讲义 meta 里取用量（写入端见 backend services/usage.py attach）。 */
export function itemUsage(meta: Record<string, unknown> | undefined | null): UsageSummary | null {
  const raw = meta?.usage;
  if (!raw || typeof raw !== "object") return null;
  return raw as UsageSummary;
}
