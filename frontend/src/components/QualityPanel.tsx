import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { ReloadOutlined, WarningOutlined } from "@ant-design/icons";
import { api } from "../api";
import { formatLocalTime } from "../lib/datetime";
import { QC_CHECK_LABEL } from "./QcBadge";
import AuditPanel from "./AuditPanel";
import type { CourseQcItem } from "../types";

function scoreColor(score: number) {
  if (score >= 90) return "#16a34a";
  if (score >= 75) return "#d97706";
  return "#dc2626";
}

function levelTag(score: number) {
  if (score >= 90) return <Tag color="green">良好</Tag>;
  if (score >= 75) return <Tag color="orange">待改进</Tag>;
  return <Tag color="red">问题较多</Tag>;
}

/**
 * 课程级「质量报告」页签（任务 22-A）：平均分、各检查项失败分布、最差 N 篇，
 * 并可直接对某篇「按质检建议重生成」。
 */
export default function QualityPanel({ courseId }: { courseId: string }) {
  const queryClient = useQueryClient();
  const report = useQuery({
    queryKey: ["courseQc", courseId],
    queryFn: () => api.courseQc(courseId),
  });

  const recheck = useMutation({
    mutationFn: () => api.courseQc(courseId, { refresh: true }),
    onSuccess: (data) => {
      queryClient.setQueryData(["courseQc", courseId], data);
      message.success(`已按当前正文复检 ${data.checked} 篇讲义`);
    },
    onError: (e: Error) => message.error(`复检失败：${e.message}`),
  });

  const regenerate = useMutation({
    mutationFn: (row: CourseQcItem) => api.regenerateWithQc(row.item_id),
    onSuccess: () => {
      message.success("已按质检建议重生成该篇讲义");
      recheck.mutate();
    },
    onError: (e: Error) => message.error(`重生成失败：${e.message}`),
  });

  if (report.isLoading) return <Card loading />;
  if (report.isError) {
    return <Alert type="error" showIcon message={`质量报告读取失败：${(report.error as Error).message}`} />;
  }
  const data = report.data;
  if (!data || data.checked === 0) {
    return (
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <AuditPanel courseId={courseId} />
        <Empty description="本课程还没有讲义，先生成讲义再做整课质检（第 3 步「完整讲义」）。" />
      </Space>
    );
  }

  const columns = [
    { title: "知识点", dataIndex: "node_key", width: 90 },
    { title: "标题", dataIndex: "title", ellipsis: true },
    {
      title: "得分",
      dataIndex: "score",
      width: 200,
      render: (score: number) => (
        <Space>
          <Progress
            percent={score}
            size="small"
            strokeColor={scoreColor(score)}
            style={{ width: 90, marginBottom: 0 }}
          />
          {levelTag(score)}
        </Space>
      ),
    },
    {
      title: "待改项",
      key: "issues",
      width: 300,
      render: (_: unknown, row: CourseQcItem) =>
        row.issues.length === 0 && (row.skipped_count ?? 0) === 0 ? (
          <Tag color="green">无</Tag>
        ) : (
          <Space direction="vertical" size={2}>
            {row.issues.slice(0, 3).map((issue) => (
              <Tooltip key={issue.key} title={issue.detail}>
                <Tag color={issue.level === "fail" ? "red" : "orange"}>
                  {QC_CHECK_LABEL[issue.key] ?? issue.key}
                </Tag>
              </Tooltip>
            ))}
            {(row.skipped_count ?? 0) > 0 ? (
              <Tooltip title={(row.skipped_keys ?? []).map((k) => QC_CHECK_LABEL[k] ?? k).join("、")}>
                <Tag color="default">{row.skipped_count} 项未执行</Tag>
              </Tooltip>
            ) : null}
            {(row.downgraded ?? []).map((item) => (
              <Tooltip key={`dg-${item.key}`} title={item.hint}>
                <Tag color="blue">{QC_CHECK_LABEL[item.key] ?? item.key}（提示）</Tag>
              </Tooltip>
            ))}
            {row.issues.length > 3 ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                还有 {row.issues.length - 3} 项
              </Typography.Text>
            ) : null}
          </Space>
        ),
    },
    {
      title: "操作",
      key: "action",
      width: 170,
      render: (_: unknown, row: CourseQcItem) => (
        <Button
          size="small"
          icon={<ReloadOutlined />}
          disabled={row.issues.length === 0}
          loading={regenerate.isPending && regenerate.variables?.item_id === row.item_id}
          onClick={() => regenerate.mutate(row)}
        >
          按质检建议重生成
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <AuditPanel courseId={courseId} />
      {data.skipped_items > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={`${data.skipped_items} 篇有未执行项`}
          description={data.skipped_items_notice}
        />
      ) : null}
      {data.material_notices && data.material_notices.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={`${data.material_notices.length} 条素材的图片文字需要人工抽查`}
          description={(
            <ul style={{ marginBottom: 0, paddingLeft: 18 }}>
              {data.material_notices.map((n) => (
                <li key={n.material_id}>{n.title}：{n.reason}</li>
              ))}
            </ul>
          )}
        />
      ) : null}
      {data.downgraded_items ? (
        <Alert
          type="info"
          showIcon
          message={`${data.downgraded_items} 篇有按提示处理的项`}
          description={data.downgraded_notice}
        />
      ) : null}
      <Card>
        <Row gutter={16} align="middle">
          <Col span={5}>
            <Statistic
              title={data.profile ? `平均分（${data.profile.label}）` : "平均分"}
              value={data.average_score} suffix="分"
              valueStyle={{ color: scoreColor(data.average_score) }} />
          </Col>
          <Col span={4}>
            <Statistic title="已质检讲义" value={data.checked} suffix="篇" />
          </Col>
          <Col span={4}>
            <Statistic title="最低分" value={data.min_score} />
          </Col>
          <Col span={4}>
            <Statistic title="最高分" value={data.max_score} />
          </Col>
          <Col span={7} style={{ textAlign: "right" }}>
            <Space>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {/* 任务 81 §B2：本地时间 + 人类可读（原先原样渲染 ISO：2026-09-22T13:56:02+00:00）。 */}
                生成于 {formatLocalTime(data.generated_at)}
              </Typography.Text>
              <Button
                icon={<ReloadOutlined />}
                loading={recheck.isPending}
                onClick={() => recheck.mutate()}
              >
                按当前正文复检
              </Button>
            </Space>
          </Col>
        </Row>
      </Card>

      <Card size="small" title="各检查项失败分布（整课）">
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          dataSource={data.failure_distribution}
          columns={[
            {
              title: "检查项",
              dataIndex: "key",
              render: (key: string) => QC_CHECK_LABEL[key] ?? key,
            },
            { title: "不合格", dataIndex: "fail", width: 100 },
            { title: "待改进", dataIndex: "warn", width: 100 },
            {
              title: "未执行",
              dataIndex: "skipped",
              width: 100,
              render: (count: number) =>
                count > 0 ? (
                  <Tooltip title="这一项本次没执行（例如概念库为空时的术语比对），不计入分母">
                    <Tag>{count}</Tag>
                  </Tooltip>
                ) : (
                  <Typography.Text type="secondary">0</Typography.Text>
                ),
            },
            {
              title: "有问题的篇数",
              dataIndex: "failed",
              width: 140,
              render: (count: number, row) => (
                <Tag color={count === 0 ? "green" : row.fail > 0 ? "red" : "orange"}>
                  {count} / {data.checked}
                </Tag>
              ),
            },
          ]}
        />
      </Card>

      <Card
        size="small"
        title={
          <Space>
            <WarningOutlined />
            最差 {data.worst.length} 篇（优先修这些）
          </Space>
        }
      >
        {data.worst.map((row) => (
          <div key={row.item_id} style={{ marginBottom: 8 }}>
            <Space wrap>
              <Tag color={scoreColor(row.score)}>{row.score} 分</Tag>
              <Typography.Text strong>
                {row.node_key} {row.title}
              </Typography.Text>
              {row.locked ? <Tag color="gold">已审核锁定</Tag> : null}
              {row.issues.map((issue) => (
                <Tooltip key={issue.key} title={issue.detail}>
                  <Tag color={issue.level === "fail" ? "red" : "orange"}>
                    {QC_CHECK_LABEL[issue.key] ?? issue.key}
                  </Tag>
                </Tooltip>
              ))}
            </Space>
          </div>
        ))}
      </Card>

      <Card size="small" title="全部讲义">
        <Table
          rowKey="item_id"
          size="small"
          dataSource={[...data.items].sort((a, b) => a.score - b.score)}
          columns={columns}
        />
      </Card>
    </Space>
  );
}
