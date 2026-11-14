import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, List, Space, Tag, Typography, message } from "antd";
import { DiffOutlined, ReloadOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { ConsistencyNode, ConsistencyReport } from "../types";

const STATUS_META: Record<ConsistencyNode["status"], { color: string; text: string }> = {
  consistent: { color: "green", text: "一致" },
  mismatch: { color: "orange", text: "不一致" },
  missing: { color: "red", text: "笔记缺页" },
};

const PART_LABEL: Record<string, string> = {
  lecture: "讲义页",
  quiz: "习题页",
  asset: "图表库资产",
  index: "课程概览",
  "course-level": "课程级内容",
  material: "素材页",
};

const FINGERPRINT_HINT: Record<string, string> = {
  absent: "旧文件没有版本指纹",
  broken: "文件在 Obsidian 里被改过",
  unreadable: "文件读不出来",
};

/** 第 4/5 步共用：校验「网页端讲义」与「Obsidian 笔记」是否一致，并可按网页端修复。 */
export default function ConsistencyCheck({ courseId }: { courseId: string }) {
  const qc = useQueryClient();
  const [report, setReport] = useState<ConsistencyReport | null>(null);

  const check = useMutation({
    mutationFn: () => api.kbVerify(courseId),
    onSuccess: (res) => {
      setReport(res);
      if (!res.ok) message.warning(res.reason ?? "暂时无法校验");
      else if (res.inconsistent === 0) {
        message.success(`一致性检查通过：${res.checked} 个知识点与网页端完全一致`);
      } else {
        message.warning(`有 ${res.inconsistent} 个知识点与笔记不一致，见下方清单`);
      }
    },
    onError: (e) => message.error(String(e)),
  });

  const repair = useMutation({
    mutationFn: () => api.kbRepair(courseId),
    onSuccess: (res) => {
      setReport(res.after);
      message.success(
        `已按网页端修复 ${res.repaired.length} 个知识点` +
          (res.failed.length ? `，${res.failed.length} 个失败，请重试` : ""),
      );
      qc.invalidateQueries({ queryKey: ["content", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });

  const bad = (report?.nodes ?? []).filter((n) => n.status !== "consistent");

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="small">
      <Space wrap>
        <Button icon={<DiffOutlined />} loading={check.isPending} onClick={() => check.mutate()}>
          一致性检查（网页 vs 笔记）
        </Button>
        {report?.ok && bad.length > 0 && (
          <Button
            danger
            icon={<ReloadOutlined />}
            loading={repair.isPending}
            onClick={() => repair.mutate()}
          >
            以网页为准修复不一致（{bad.length}）
          </Button>
        )}
        {report?.ok && (
          <Tag color={report.inconsistent ? "orange" : "green"}>
            {report.inconsistent
              ? `不一致 ${report.inconsistent}/${report.checked}`
              : `全部一致 ${report.consistent}/${report.checked}`}
          </Tag>
        )}
      </Space>

      {report && !report.ok && (
        <Alert type="warning" showIcon message="暂时无法校验" description={report.reason} />
      )}

      {report?.ok && bad.length > 0 && (
        <>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            下列知识点的笔记内容与网页端讲义不一致。点上方「以网页为准修复」会用网页端内容覆盖这些笔记页（含在 Obsidian 里手改过的）。
          </Typography.Text>
          <List
            size="small"
            bordered
            dataSource={bad}
            renderItem={(n) => (
              <List.Item key={n.node_id}>
                <Space direction="vertical" size={2} style={{ width: "100%" }}>
                  <Space wrap>
                    <Tag color={STATUS_META[n.status].color}>{STATUS_META[n.status].text}</Tag>
                    {n.part && PART_LABEL[n.part] && <Tag>{PART_LABEL[n.part]}</Tag>}
                    <Typography.Text strong>
                      {n.node_key} {n.title}
                    </Typography.Text>
                    {n.fingerprint && FINGERPRINT_HINT[n.fingerprint] && (
                      <Tag>{FINGERPRINT_HINT[n.fingerprint]}</Tag>
                    )}
                  </Space>
                  {n.diff.map((d) => (
                    <Typography.Text key={d} type="secondary" style={{ fontSize: 12 }}>
                      · {d}
                    </Typography.Text>
                  ))}
                  {n.note && (
                    <Typography.Text type="secondary" style={{ fontSize: 11, fontFamily: "monospace" }}>
                      {n.note}
                    </Typography.Text>
                  )}
                </Space>
              </List.Item>
            )}
          />
        </>
      )}
    </Space>
  );
}
