import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Descriptions, Row, Space, Statistic, Tag, Typography, message } from "antd";
import { CloudDownloadOutlined, FileSearchOutlined, ReloadOutlined } from "@ant-design/icons";
import { api } from "../api";
import { QC_CHECK_LABEL } from "./QcBadge";
import type { CourseAudit } from "../types";

const LEVEL_MARK: Record<string, string> = { ok: "✅", warn: "⚠️", fail: "❌" };

function scoreColor(score: number) {
  if (score >= 90) return "#16a34a";
  if (score >= 75) return "#d97706";
  return "#dc2626";
}

function levelColor(level: string) {
  return level === "fail" ? "red" : level === "warn" ? "orange" : "green";
}

/**
 * 课程资产体检报告（任务 24-C）：八段结构（结论卡 / 覆盖度 / 一致性 / 链接健康 /
 * 质检 / 用量与成本 / 导出健康 / 建议动作）。数字全部来自 verify / qc / usage /
 * 导出统计，前端只做展示；点「生成并导出到笔记」把同一份写入
 * 课程/<课名>/00-课程体检报告.md。
 */
export default function AuditPanel({ courseId }: { courseId: string }) {
  const queryClient = useQueryClient();
  const report = useQuery({
    queryKey: ["courseAudit", courseId],
    queryFn: () => api.courseAudit(courseId),
    retry: false,
  });

  // 任务 48z：完整报告要跑整库笔记一致性检查（AI 课 150 项、实测 4.3 秒）。
  // 首屏先请求「轻量概览」（覆盖度/链接/质检/用量/导出健康，实测 0.29 秒）并立刻渲染，
  // 完整报告并行补上；期间「结论 / 一致性 / 建议动作」三段写明「正在检查…」，
  // 绝不显示「0 分 / 没有数据」这类肯定句。
  const overview = useQuery({
    queryKey: ["courseAuditOverview", courseId],
    queryFn: () => api.courseAuditOverview(courseId),
    retry: false,
  });

  const exportReport = useMutation({
    mutationFn: () => api.exportCourseAudit(courseId),
    onSuccess: (res: CourseAudit & { output_rel: string }) => {
      message.success(`体检报告已写入 ${res.output_rel}/00-课程体检报告.md`);
      void queryClient.invalidateQueries({ queryKey: ["courseAudit", courseId] });
      void queryClient.invalidateQueries({ queryKey: ["courseAuditOverview", courseId] });
    },
    onError: (e: Error) => message.error(`导出失败：${e.message}`),
  });

  const data = report.data;
  const quick = overview.data;
  if (!data && !quick) {
    if (report.isError || overview.isError) {
      const err = (report.error ?? overview.error) as Error;
      return <Alert type="error" showIcon message={`体检报告读取失败：${err.message}`} />;
    }
    return <Card loading />;
  }

  const cover = data?.coverage ?? quick!.coverage;
  const links = data?.links ?? quick!.links;
  const quality = data?.quality ?? quick!.quality;
  const usage = data?.usage ?? quick!.usage;
  const health = data?.export_health ?? quick!.export_health;
  const conclusion = data?.conclusion ?? null;
  const actions = data?.actions ?? [];
  /** true = 完整报告还没回来（结论 / 一致性 / 建议动作三段先占位）。 */
  const checking = !data;

  return (
    <Card
      title={
        <Space>
          <FileSearchOutlined />
          课程资产体检报告
        </Space>
      }
      extra={
        <Space>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => void report.refetch()}
            loading={report.isFetching}
          >
            刷新
          </Button>
          <Button
            type="primary"
            icon={<CloudDownloadOutlined />}
            loading={exportReport.isPending}
            onClick={() => exportReport.mutate()}
          >
            生成并写入笔记
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {/* 1. 结论卡（任务 48z：完整报告未回来时先说明「正在检查」，不显示 0 分） */}
        {conclusion ? (
          <Row gutter={16} align="middle">
            <Col span={6}>
              <Statistic
                title="总分"
                value={conclusion.score}
                suffix="/ 100"
                valueStyle={{ color: scoreColor(conclusion.score) }}
              />
            </Col>
            <Col span={6}>
              <Space direction="vertical" size={2}>
                <Typography.Text type="secondary">等级</Typography.Text>
                <Tag color={levelColor(conclusion.badge)} style={{ fontSize: 14 }}>
                  {conclusion.level}
                </Tag>
              </Space>
            </Col>
            <Col span={12}>
              <Typography.Text>{data?.summary}</Typography.Text>
              {conclusion.deductions.length > 0 && (
                <div style={{ marginTop: 4 }}>
                  {conclusion.deductions.map((row) => (
                    <Tag key={row.reason} color="red">
                      {row.reason} {row.value} → −{row.cut}
                    </Tag>
                  ))}
                </div>
              )}
            </Col>
          </Row>
        ) : (
          <Alert
            type="info"
            showIcon
            message="正在检查笔记一致性与链接健康…"
            description={
              <Typography.Text type="secondary">
                这两项要逐项读整个笔记库（AI 课 150 项、约 4 秒），总分 = 100 − 它们的扣分，
                所以结论卡要等它们算完。下面的覆盖度 / 链接 / 质检 / 用量 / 导出健康是现算的，先看这些。
              </Typography.Text>
            }
          />
        )}

        {/* 任务 81 §A4：面板 = 现算，笔记库里的报告 = 出包快照 —— 两处不一样不代表哪边坏了 */}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          本面板的数字（含总分与一致性）是现算的；笔记库里的「00-课程体检报告.md」是出包时的快照 ——
          讲义 / 素材改过之后，要点「生成并写入笔记」或重新出包才会同步。
        </Typography.Text>
        {data?.consistency?.ok && data.consistency.inconsistent > 0 ? (
          <Typography.Text type="warning" style={{ fontSize: 12 }}>
            一致性不一致 {data.consistency.inconsistent} 项：点「修复不一致（以网页为准）」（在「素材与笔记」页签）
            或重新出包即可消除 —— 出包前会先自动导出一次笔记。
          </Typography.Text>
        ) : null}

        {/* 2. 覆盖度 */}
        <Descriptions title="2. 覆盖度" size="small" column={4} bordered>
          <Descriptions.Item label="章">{cover.chapters}</Descriptions.Item>
          <Descriptions.Item label="知识点">{cover.points}</Descriptions.Item>
          <Descriptions.Item label="完整讲义">
            {cover.lectures}（覆盖 {Math.round(cover.lecture_coverage * 100)}%）
          </Descriptions.Item>
          <Descriptions.Item label="概念">{cover.concepts}</Descriptions.Item>
          <Descriptions.Item label="概念/流程图条目">{cover.diagrams}</Descriptions.Item>
          <Descriptions.Item label="数据图表条目">{cover.charts}</Descriptions.Item>
          <Descriptions.Item label="图表库文件">{health.charts}</Descriptions.Item>
          <Descriptions.Item label="随堂练习">{cover.quizzes}</Descriptions.Item>
          <Descriptions.Item label="课件">{cover.ppt}</Descriptions.Item>
        </Descriptions>
        {cover.missing_lectures_count > 0 && (
          <Typography.Text type="warning" style={{ fontSize: 12 }}>
            还缺 {cover.missing_lectures_count} 个知识点的讲义：{cover.missing_lectures.join("、")}
          </Typography.Text>
        )}

        {/* 3. 一致性 */}
        <Descriptions title="3. 一致性（verify）" size="small" column={3} bordered>
          {checking ? (
            <Descriptions.Item label="状态" span={3}>
              正在检查笔记一致性…（逐项比对整个笔记库，AI 课 150 项、约 4 秒）
            </Descriptions.Item>
          ) : data!.consistency.ok ? (
            <>
              <Descriptions.Item label="已检查产出">{data.consistency.checked}</Descriptions.Item>
              <Descriptions.Item label="一致">{data.consistency.consistent}</Descriptions.Item>
              <Descriptions.Item label="不一致">
                <Tag color={data.consistency.inconsistent ? "red" : "green"}>
                  {data.consistency.inconsistent}
                </Tag>
              </Descriptions.Item>
            </>
          ) : (
            <Descriptions.Item label="状态">
              {data!.consistency.reason || "未绑定笔记库，先绑定并导出一次"}
            </Descriptions.Item>
          )}
        </Descriptions>

        {/* 4. 链接健康 */}
        <Descriptions title="4. 链接健康" size="small" column={4} bordered>
          {links.ok ? (
            <>
              <Descriptions.Item label="双链">{links.wiki_links}</Descriptions.Item>
              <Descriptions.Item label="站内链接">{links.file_links}</Descriptions.Item>
              <Descriptions.Item label="合计">{links.links}</Descriptions.Item>
              <Descriptions.Item label="坏链">
                <Tag color={links.broken ? "red" : "green"}>
                  {links.broken} {links.broken ? "❌" : "✅"}
                </Tag>
              </Descriptions.Item>
            </>
          ) : (
            <Descriptions.Item label="状态">
              {links.reason || "未绑定笔记库"}
            </Descriptions.Item>
          )}
        </Descriptions>
        {links.broken_links?.length > 0 && (
          <div>
            {links.broken_links.slice(0, 5).map((row) => (
              <div key={`${row.file}-${row.target}`} style={{ fontSize: 12, fontFamily: "monospace" }}>
                {row.file} → {row.target}（{row.kind}）
              </div>
            ))}
          </div>
        )}

        {/* 5. 质检 */}
        <Descriptions title="5. 质检" size="small" column={3} bordered>
          {quality.checked ? (
            <>
              <Descriptions.Item label="平均分">
                <span style={{ color: scoreColor(quality.average_score) }}>{quality.average_score}</span>
              </Descriptions.Item>
              <Descriptions.Item label="最低 / 最高">
                {quality.min_score} / {quality.max_score}
              </Descriptions.Item>
              <Descriptions.Item label="已质检">{quality.checked} 篇</Descriptions.Item>
            </>
          ) : (
            <Descriptions.Item label="状态">还没有讲义通过质检</Descriptions.Item>
          )}
        </Descriptions>
        {quality.checked > 0 && (
          <Space wrap size={4}>
            {quality.failure_distribution
              .filter((row) => row.failed > 0)
              .map((row) => (
                <Tag key={row.key} color={levelColor(row.fail ? "fail" : "warn")}>
                  {QC_CHECK_LABEL[row.key] ?? row.key}：{row.failed} 篇
                </Tag>
              ))}
          </Space>
        )}

        {/* 6. 用量与成本 */}
        <Descriptions title="6. 用量与成本" size="small" column={4} bordered>
          <Descriptions.Item label="累计 tokens">{usage.total_tokens}</Descriptions.Item>
          <Descriptions.Item label="费用">≈{usage.cost_cny} 元</Descriptions.Item>
          <Descriptions.Item label="平均单篇耗时">
            {(usage.avg_duration_ms / 1000).toFixed(1)} 秒
          </Descriptions.Item>
          <Descriptions.Item label="LLM 调用">{usage.llm_calls} 次</Descriptions.Item>
        </Descriptions>
        {usage.note && <Alert type="warning" showIcon message={usage.note} />}

        {/* 7. 导出健康 */}
        <Descriptions title="7. 导出健康" size="small" column={4} bordered>
          {health.ok ? (
            <>
              <Descriptions.Item label="目录文件">{health.files}</Descriptions.Item>
              <Descriptions.Item label="笔记 / 图表库">
                {health.notes} / {health.charts}
              </Descriptions.Item>
              <Descriptions.Item label="孤儿文件">
                <Tag color={health.orphan_count ? "orange" : "green"}>{health.orphan_count}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="保留（疑似手改）">{health.preserved_count}</Descriptions.Item>
              <Descriptions.Item label="课件落地">
                {health.decks}（缺 {health.deck_missing.length}）
              </Descriptions.Item>
            </>
          ) : (
            <Descriptions.Item label="状态">{health.reason || "未绑定笔记库"}</Descriptions.Item>
          )}
        </Descriptions>

        {/* 8. 建议动作 */}
        <Card size="small" title="8. 建议动作">
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            {checking && !actions.length ? (
              <Typography.Text type="secondary">
                正在检查一致性与链接健康，完成后给出建议动作…
              </Typography.Text>
            ) : null}
            {actions.map((row, index) => (
              <div key={`${row.level}-${index}`}>
                <Space align="start">
                  <Tag color={levelColor(row.level)}>{LEVEL_MARK[row.level] ?? "•"}</Tag>
                  <div>
                    <Typography.Text>{row.text}</Typography.Text>
                    <div>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        该做什么：{row.action}
                      </Typography.Text>
                    </div>
                  </div>
                </Space>
              </div>
            ))}
          </Space>
        </Card>
      </Space>
    </Card>
  );
}
