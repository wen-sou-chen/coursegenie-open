import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { App, Button, Popover, Space, Tag, Tooltip, Typography } from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  MinusCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import type { LectureQc, LectureQcPayload } from "../types";

/** 质检项中文名（与后端 lecture_qc.CHECK_KEYS 一一对应）。 */
export const QC_CHECK_LABEL: Record<string, string> = {
  structure: "结构完整",
  fluff: "空话套话",
  length: "篇幅匹配",
  objective: "目标对齐",
  terminology: "术语一致",
  figures: "图与表",
  quiz: "随堂练习",
  sources: "来源标注",
  // 任务 106：引用数字对账（数字必须在被引段里）
  numbers: "数字与来源不符",
  // 任务 41：含金量（一手案例 / 失败教训 / 边界声明）+ 可操作（他人能否照做）
  substance: "含金量",
  actionable: "可操作",
};

const LEVEL_TAG: Record<string, { color: string; label: string }> = {
  ok: { color: "green", label: "合格" },
  warn: { color: "orange", label: "待改进" },
  fail: { color: "red", label: "不合格" },
  // P5-1：这项本次没执行（例如概念库为空时的术语比对）—— 既不是合格也不是问题
  skipped: { color: "default", label: "未执行" },
};

/** 从讲义 meta 里取质检结果（写入端见 backend lecture_qc.build_for_item）。 */
export function lectureQc(
  meta: Record<string, unknown> | undefined | null,
): LectureQc | null {
  const raw = meta?.qc;
  if (!raw || typeof raw !== "object") return null;
  const checks = (raw as { checks?: unknown }).checks;
  return Array.isArray(checks) ? (raw as unknown as LectureQc) : null;
}

export function qcWorstLevel(qc: LectureQc): "ok" | "warn" | "fail" {
  // P5-1：「未执行」不参与最差评级——否则会把「没考」误报成「考糟」。
  if (qc.checks.some((c) => c.level === "fail")) return "fail";
  if (qc.checks.some((c) => c.level === "warn")) return "warn";
  return "ok";
}

/** 真正需要改的问题项（warn/fail）；「未执行」不算问题。 */
export function qcIssues(qc: LectureQc | null) {
  return (qc?.checks ?? []).filter((c) => c.level !== "ok" && c.level !== "skipped");
}

function checkIcon(level: string) {
  if (level === "fail") return <CloseCircleOutlined />;
  if (level === "warn") return <ExclamationCircleOutlined />;
  if (level === "skipped") return <MinusCircleOutlined />;
  return <CheckCircleOutlined />;
}

/**
 * 讲义卡质检徽标 + 明细（任务 22-A）。
 *
 * meta 里已有 qc 就直接展示；老讲义没有时点徽标现算一次（后端会落库）。
 * 明细里带「按质检建议重生成」，把 warn/fail 注入重生成提示词。
 */
export default function QcBadge({
  meta,
  itemId,
  onRegenerate,
  regenerating,
  disabled,
  onJumpToText,
}: {
  meta: Record<string, unknown> | undefined | null;
  itemId: string;
  onRegenerate?: () => void;
  regenerating?: boolean;
  disabled?: boolean;
  /** P5-2：点「跳到正文」时把这句话滚到可见处并高亮。 */
  onJumpToText?: (text: string) => void;
}) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const stored = lectureQc(meta);
  const [fetched, setFetched] = useState<LectureQcPayload | null>(null);
  const qc = fetched?.qc ?? stored;
  const issues = qcIssues(qc);

  const inspect = useMutation({
    // 任务 48k：按钮文案承诺「按当前正文现算一次」——必须真的带 refresh=true，
    // 否则拿到的是缓存（概念库为空时那份 terminology=skipped 的旧结论）。
    mutationFn: () => api.lectureQc(itemId, true),
    onSuccess: (data) => setFetched(data),
    onError: (e: Error) => message.error(`质检失败：${e.message}`),
  });

  const level = qc ? qcWorstLevel(qc) : "warn";
  const tone = LEVEL_TAG[level] ?? LEVEL_TAG.warn;

  const content = qc ? (
    <Space direction="vertical" size={6} style={{ maxWidth: 420 }}>
      <Space wrap>
        <Typography.Text strong>质检得分 {qc.score} 分</Typography.Text>
        {/* 任务 52：显示当前档位 —— 让老师知道「这条为什么不算问题」 */}
        {qc.profile?.label ? (
          <Tooltip title={qc.profile.note}>
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>
              {qc.profile.label}
            </Tag>
          </Tooltip>
        ) : null}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {qc.metrics.chars} 字 · {qc.metrics.points} 条关键点 · {qc.metrics.figures} 图/表 ·{" "}
          {qc.metrics.quizzes} 题 · {qc.metrics.sources} 条来源
        </Typography.Text>
      </Space>
      {qc.note ? (
        <Typography.Text type="warning" style={{ fontSize: 12 }}>
          {qc.note}
        </Typography.Text>
      ) : null}
      {qc.checks.map((check) => (
        <div key={check.key}>
          <Space align="start" size={6}>
            <Tag
              color={check.downgraded ? "blue" : (LEVEL_TAG[check.level] ?? LEVEL_TAG.warn).color}
              icon={checkIcon(check.level)}
              style={{ marginInlineEnd: 0 }}
            >
              {QC_CHECK_LABEL[check.key] ?? check.key}
              {check.downgraded ? "（提示）" : ""}
            </Tag>
            <Typography.Text style={{ fontSize: 12 }}>{check.detail}</Typography.Text>
          </Space>
          {/* P5-2：缺来源标注不只给条数，列出具体句子 + 上下文，可跳到正文 */}
          {check.sentences?.length ? (
            <div style={{ margin: "4px 0 0 8px" }}>
              {check.sentences.slice(0, 8).map((row, idx) => (
                <div key={`${check.key}-s${idx}`} style={{ fontSize: 12, marginBottom: 2 }}>
                  {onJumpToText ? (
                    <Typography.Link onClick={() => onJumpToText(row.text)}>
                      跳到正文
                    </Typography.Link>
                  ) : null}{" "}
                  <Typography.Text type="secondary">
                    {row.section ? `【${row.section}】` : ""}
                    {row.line ? `第 ${row.line} 行：` : ""}
                    {row.context}
                  </Typography.Text>
                </div>
              ))}
              {check.sentences.length > 8 ? (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  还有 {check.sentences.length - 8} 句，点「按质检建议重生成」可一次补齐。
                </Typography.Text>
              ) : null}
            </div>
          ) : null}
        </div>
      ))}
      {onRegenerate ? (
        <Button
          size="small"
          type="primary"
          ghost
          icon={<ReloadOutlined />}
          disabled={disabled || issues.length === 0}
          loading={regenerating}
          onClick={onRegenerate}
        >
          {issues.length === 0 ? "没有需要修正的问题" : `按质检建议重生成（${issues.length} 项）`}
        </Button>
      ) : null}
    </Space>
  ) : (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      还没有质检结果，点「开始质检」按当前正文现算一次。
    </Typography.Text>
  );

  return (
    <Popover
      content={content}
      title="讲义质检报告"
      trigger="click"
      onOpenChange={(open) => {
        if (open && !qc && !inspect.isPending) inspect.mutate();
      }}
    >
      <Tooltip title={qc ? "点击查看质检明细" : "点击开始质检"}>
        <Tag
          style={{ cursor: "pointer", marginInlineEnd: 0 }}
          color={qc ? tone.color : "default"}
          icon={qc ? checkIcon(level) : undefined}
        >
          {qc ? `质检 ${qc.score} 分 · ${tone.label}` : "未质检"}
          {qc && issues.length > 0 ? `（${issues.length} 项待改）` : ""}
          {qc && (qc.skipped_count ?? 0) > 0 ? `（${qc.skipped_count} 项未执行）` : ""}
        </Tag>
      </Tooltip>
    </Popover>
  );
}
