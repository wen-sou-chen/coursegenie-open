import { useQuery } from "@tanstack/react-query";
import SectionCard from "./ui/SectionCard";
import { Col, Row, Statistic, Tag, Tooltip, Typography } from "antd";
import { api } from "../api";
export default function AnalyticsPanel({ courseId }: { courseId: string }) {
  const { data: stats } = useQuery({ queryKey: ["analytics", courseId], queryFn: () => api.analytics(courseId) });
  if (!stats) return <SectionCard loading />;
  return (
    <Row gutter={16}>
      <Col span={6}><SectionCard><Statistic title="大纲知识点" value={stats.outline_nodes} /></SectionCard></Col>
      <Col span={6}><SectionCard><Statistic title="内容条目" value={stats.content_items} /></SectionCard></Col>
      <Col span={6}><SectionCard><Statistic title="节点覆盖度" value={stats.node_coverage * 100} suffix="%" /></SectionCard></Col>
      {/* 任务 81 §A1：标题与算法一致 —— 原先标题写「总字数」、算的却是正文的词块数（中文连成一片只算 1 个）。 */}
      <Col span={6}>
        <SectionCard>
          <Tooltip
            title={
              "字数口径：汉字 1 个算 1 字 + 连续英文 / 数字串算 1 个词；不含标点与空白。" +
              "下方「字符数」是不去标点、只去空白的结果（旧口径是正则词块数，中文连成一片只算 1 个，已修正）。"
            }
          >
            <Statistic title="正文字数（汉字 + 英文/数字词）" value={stats.words_total} />
          </Tooltip>
          {/* chars_total 是任务 81 新加的字段：后端还没重启（跑着旧代码）时它可能缺 ——
              宁可少显示一行，也不能让整个面板白屏（真机走查时踩到过）。 */}
          {typeof stats.chars_total === "number" && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              字符数 {stats.chars_total.toLocaleString("en-US")}（含标点、不含空白）
            </Typography.Text>
          )}
        </SectionCard>
      </Col>
      <Col span={6}><SectionCard><Statistic title="习题数" value={stats.quizzes} /></SectionCard></Col>
      <Col span={18}>
        <SectionCard title="难度分布">
          {Object.entries(stats.quiz_by_difficulty ?? {}).map(([k, v]) => (
            <Tag key={k} color={k === "hard" ? "red" : k === "medium" ? "orange" : "green"}>
              {k}: {v}
            </Tag>
          ))}
          <p style={{ marginTop: 12, color: "#888" }}>{stats.low_score_alert}</p>
          <p style={{ color: "#888" }}>{stats.high_exit_alert}</p>
        </SectionCard>
      </Col>
    </Row>
  );
}
