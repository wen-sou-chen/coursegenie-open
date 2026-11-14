import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Col, Empty, Row, Space, Statistic, Table, Tag, Tooltip, Typography } from "antd";
import { api } from "../api";
import { formatLocalTime } from "../lib/datetime";
import { fmtCost, fmtDuration, fmtTokens, PURPOSE_LABEL } from "./UsageBadge";
import type { CourseUsageItem, UsagePurposeBucket } from "../types";

const KIND_LABEL: Record<string, string> = {
  lecture: "完整讲义",
  content: "正文",
  diagram: "概念图",
  chart: "数据图",
  quiz: "随堂练习",
  concept: "概念卡",
  concept_extract: "概念抽取",
  outline: "大纲生成",
  flowchart: "流程图",
  ppt: "课件",
};

function kindLabel(kind: string) {
  return KIND_LABEL[kind] ?? kind;
}

function purposeSummary(buckets: Record<string, UsagePurposeBucket>) {
  const rows = Object.entries(buckets ?? {});
  if (rows.length === 0) return "—";
  return rows
    .map(([purpose, bucket]) => `${PURPOSE_LABEL[purpose] ?? purpose} ${bucket.calls} 次`)
    .join("、");
}

/**
 * 课程级「用量与成本」页签（任务 23-A）：当前版本 tokens/费用、平均耗时、最贵 N 篇、按 kind 分布。
 *
 * P9-4：主数字是「当前版本」——重跑会覆盖同一条目的用量（大纲/概念抽取尤其如此），
 * 所以标题里写明「重跑覆盖」，历史累计另给一行，避免把两个口径混为一谈。
 *
 * 口径与后端一致：只统计已落 meta.usage 的条目（老条目不计、也不估算）；
 * 未知模型照记 tokens，费用计 0 并显式标注「未配置单价」。
 */
export default function UsagePanel({ courseId }: { courseId: string }) {
  const report = useQuery({
    queryKey: ["courseUsage", courseId],
    queryFn: () => api.courseUsage(courseId),
  });

  if (report.isLoading) return <Card loading />;
  if (report.isError) {
    return <Alert type="error" showIcon message={`用量报告读取失败：${(report.error as Error).message}`} />;
  }
  const data = report.data;
  if (!data || data.items_measured === 0) {
    return <Empty description="本课程还没有带用量记录的生成结果，先生成讲义（第 3 步「完整讲义」）。" />;
  }
  // 任务 48x：aux_history 是汇总对象（逐项在 by_purpose 里），不是数组。
  const auxRows = data.aux_history?.by_purpose ?? [];

  const itemColumns = [
    { title: "知识点", dataIndex: "node_key", width: 90 },
    { title: "类型", dataIndex: "kind", width: 110, render: (kind: string) => kindLabel(kind) },
    { title: "标题", dataIndex: "title", ellipsis: true },
    { title: "调用", dataIndex: "llm_calls", width: 80, render: (n: number) => `${n} 次` },
    {
      title: "tokens",
      dataIndex: "total_tokens",
      width: 100,
      render: (n: number, row: CourseUsageItem) => (
        <Tooltip title={`输入 ${row.prompt_tokens} / 输出 ${row.completion_tokens}`}>{fmtTokens(n)}</Tooltip>
      ),
    },
    {
      title: "费用",
      dataIndex: "cost_cny",
      width: 110,
      render: (cost: number, row: CourseUsageItem) =>
        row.unpriced_models.length ? (
          <Tooltip title={`${row.unpriced_models.join("、")} 未配置单价，费用计 0`}>
            <Tag color="orange">未配置单价</Tag>
          </Tooltip>
        ) : (
          <Typography.Text>≈{fmtCost(cost)} 元</Typography.Text>
        ),
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      width: 100,
      render: (ms: number) => fmtDuration(ms),
    },
    {
      title: "用途分布",
      dataIndex: "by_purpose",
      width: 220,
      render: (buckets: Record<string, UsagePurposeBucket>) => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {purposeSummary(buckets)}
        </Typography.Text>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {!data.priced && (
        <Alert
          type="warning"
          showIcon
          message="部分模型未配置单价"
          description={
            data.note ??
            `以下模型未配置单价，费用计 0：${data.unpriced_models.join("、")}；可在 .env 里用 MODEL_PRICES 配置单价（元/百万 tokens）。`
          }
        />
      )}

      <Card
        title="当前版本用量（重跑覆盖，不含历史版本）"
        extra={
          <Tooltip
            title={
              auxRows.length === 0
                ? "本课程还没有大纲生成 / 概念抽取的用量记录（这两项在本版之前未采集，不会追溯）。"
                : data.history_aux_estimated
                  ? "历史账本在本版之前尚未记录（老课程），此数字是「当前版本 = 至少 1 次」的下限；今后重跑会追加。"
                  : auxRows.map((row) => `${row.title} ${row.runs} 次`).join("；")
            }
          >
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              历史累计{data.history_aux_estimated ? "（下限）" : ""}{" "}
              {data.history_aux_runs} 次 / ≈¥{data.history_aux_cost_cny.toFixed(4)}
            </Typography.Text>
          </Tooltip>
        }
      >
        <Row gutter={16} align="middle">
          <Col span={5}>
            <Statistic title="当前版本费用" value={data.cost_cny} precision={4} prefix="≈" suffix="元" />
          </Col>
          <Col span={4}>
            <Statistic title="当前版本 tokens" value={data.total_tokens} formatter={(v) => fmtTokens(Number(v))} />
          </Col>
          <Col span={4}>
            <Statistic title="LLM 调用" value={data.llm_calls} suffix="次" />
          </Col>
          <Col span={4}>
            <Statistic title="平均单篇耗时" value={fmtDuration(data.avg_duration_ms)} />
          </Col>
          <Col span={4}>
            <Statistic title="平均单篇费用" value={data.avg_cost_cny} precision={4} prefix="≈" suffix="元" />
          </Col>
          <Col span={3} style={{ textAlign: "right" }}>
            <Tooltip title={`统计口径：${data.items_measured} 篇内容条目${data.lectures ? `（其中完整讲义 ${data.lectures} 篇）` : ""}；平均讲义耗时 ${fmtDuration(data.avg_lecture_duration_ms)}`}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                已统计 {data.items_measured} 篇
              </Typography.Text>
            </Tooltip>
          </Col>
        </Row>
      </Card>

      <Card size="small" title="按内容类型分布">
        <Table
          rowKey="kind"
          size="small"
          pagination={false}
          dataSource={data.by_kind}
          columns={[
            { title: "类型", dataIndex: "kind", render: (kind: string) => kindLabel(kind) },
            { title: "篇数", dataIndex: "items", width: 80 },
            { title: "调用", dataIndex: "llm_calls", width: 90, render: (n: number) => `${n} 次` },
            { title: "tokens", dataIndex: "total_tokens", width: 110, render: (n: number) => fmtTokens(n) },
            {
              title: "费用",
              dataIndex: "cost_cny",
              width: 120,
              render: (cost: number) => `≈${fmtCost(cost)} 元`,
            },
            {
              title: "耗时",
              dataIndex: "duration_ms",
              width: 110,
              render: (ms: number) => fmtDuration(ms),
            },
          ]}
        />
      </Card>

      <Card size="small" title={`最贵 ${data.top_items.length} 篇（优先看这里）`}>
        <Table rowKey="item_id" size="small" pagination={false} dataSource={data.top_items} columns={itemColumns} />
      </Card>

      <Card size="small" title="全部内容用量">
        <Table
          rowKey="item_id"
          size="small"
          dataSource={[...data.items].sort((a, b) => b.cost_cny - a.cost_cny)}
          columns={itemColumns}
        />
      </Card>

      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        报告生成于 {formatLocalTime(data.generated_at)}；单价默认 DeepSeek 官方价（元/百万 tokens），可用 MODEL_PRICES 覆盖。
      </Typography.Text>
    </Space>
  );
}
