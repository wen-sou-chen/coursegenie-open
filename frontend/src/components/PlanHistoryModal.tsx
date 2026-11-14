import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, List, Modal, Space, Tag, Typography, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { PlanDiff, PlanRestoreImpact, PlanUsageSummary } from "../types";

const REASON_LABEL: Record<string, string> = {
  regenerate: "重新生成前的旧版",
  before_restore: "回滚前自动存的现版",
};

function fmtTime(at?: string) {
  if (!at) return "时间未知";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString("zh-CN", { hour12: false });
}

function fmtParams(params: Record<string, unknown>) {
  const parts: string[] = [];
  if (params.goal) parts.push("目标：" + String(params.goal));
  if (params.num_chapters) parts.push(String(params.num_chapters) + " 章");
  if (params.style) parts.push("风格：" + String(params.style));
  if (params.audience) parts.push("受众：" + String(params.audience));
  return parts.join(" · ") || "参数未记录（这版生成于参数记录上线前）";
}

function fmtUsage(usage?: PlanUsageSummary | null) {
  if (!usage || !Number(usage.llm_calls ?? 0)) return "用量未记录";
  const cost = Number(usage.cost_cny ?? 0);
  return `${usage.llm_calls} 次调用 / ${Number(usage.total_tokens ?? 0)} tokens / ¥${cost.toFixed(4)}`;
}

function fmtKeyList(rows: { key: string; title: string }[], head: string) {
  const text = rows.slice(0, 8).map((r) => `${r.key} ${r.title}`).join("；");
  return `${head}：${text}${rows.length > 8 ? ` …共 ${rows.length} 个` : ""}`;
}

/** 后端 409 detail 是结构化对象（message + impact），前端从错误文案里还原。 */
function parseImpactError(err: unknown): { message: string; impact?: PlanRestoreImpact } | null {
  const raw = String(err instanceof Error ? err.message : err);
  try {
    const parsed = JSON.parse(raw) as { message?: string; impact?: PlanRestoreImpact };
    if (parsed && typeof parsed === "object") {
      return { message: String(parsed.message ?? ""), impact: parsed.impact };
    }
  } catch {
    // 普通字符串错误走下面的兜底
  }
  return null;
}

function renderDiff(diff: PlanDiff) {
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      <Typography.Text>
        相对当前大纲：新增 {diff.added.length} · 删除 {diff.removed.length} ·
        改名 {diff.renamed.length} · 未变 {diff.unchanged}
      </Typography.Text>
      {diff.added.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {fmtKeyList(diff.added, "回滚后会多出来的节点")}
        </Typography.Text>
      )}
      {diff.removed.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {fmtKeyList(diff.removed, "当前版有、回滚后会消失的节点")}
        </Typography.Text>
      )}
      {diff.renamed.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          同名不同题：{diff.renamed.slice(0, 6).map((r) => `${r.key}「${r.from}」→「${r.to}」`).join("；")}
          {diff.renamed.length > 6 ? ` …共 ${diff.renamed.length} 个` : ""}
        </Typography.Text>
      )}
    </Space>
  );
}

/**
 * 大纲历史版本（P3-1）：每次「重新生成」前自动存一版旧大纲（参数 + 节点快照 + 用量），
 * 这里可以逐版对比（节点级 diff）并一键回滚；回滚前先看影响面，需要二次确认。
 */
export default function PlanHistoryModal({
  courseId,
  open,
  onClose,
}: {
  courseId: string;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState<{ id: string; impact: PlanRestoreImpact } | null>(null);

  const history = useQuery({
    queryKey: ["planHistory", courseId],
    queryFn: () => api.planHistory(courseId),
    enabled: open,
    retry: false,
  });
  const versions = history.data?.versions ?? [];

  useEffect(() => {
    if (!open) {
      setSelected(null);
      setPending(null);
      return;
    }
    if (!selected && versions.length > 0) setSelected(versions[0].id);
  }, [open, selected, versions]);

  const detail = useQuery({
    queryKey: ["planVersion", courseId, selected],
    queryFn: () => api.planVersion(courseId, selected as string),
    enabled: open && !!selected,
    retry: false,
  });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["planHistory", courseId] });
    void qc.invalidateQueries({ queryKey: ["planVersion", courseId] });
    void qc.invalidateQueries({ queryKey: ["plan", courseId] });
    void qc.invalidateQueries({ queryKey: ["course", courseId] });
  };

  const restore = useMutation({
    mutationFn: (d: { id: string; confirm: boolean }) => api.restorePlanVersion(courseId, d.id, d.confirm),
    onSuccess: (r) => {
      message.success(`已回滚：${r.nodes} 个节点；大纲状态回到「待通过」，请重新点「大纲通过」`);
      setPending(null);
      refresh();
    },
    onError: (e) => message.error(String(e)),
  });

  // 先按「不确认」提交：后端会算好影响面再用 409 挡回来，用户确认后才真正回滚
  const preview = async (id: string) => {
    try {
      await api.restorePlanVersion(courseId, id);
      setPending(null);
      refresh();
    } catch (err) {
      const parsed = parseImpactError(err);
      if (parsed?.impact) {
        setPending({ id, impact: parsed.impact });
        return;
      }
      message.error(parsed?.message || String(err));
    }
  };

  return (
    <Modal
      title="大纲历史版本"
      open={open}
      onCancel={onClose}
      footer={[<Button key="close" onClick={onClose}>关闭</Button>]}
      width={900}
    >
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message={`当前大纲 ${history.data?.current.nodes ?? 0} 个节点；历史保留最近 10 版`}
          description="每次「重新生成大纲」前，旧大纲会自动存一版（含生成参数、节点快照与那次用量），方便对比「这版比上版好还是坏」。回滚会把现版也存进历史，随时可以再滚回来。"
        />
        {history.isError && (
          <Alert type="warning" showIcon message="历史版本读不出来" description={(history.error as Error).message} />
        )}
        {versions.length === 0 ? (
          <Empty description="还没有历史版本：第一次生成大纲时没有旧版可存，重新生成一次后这里就会出现上一版。" />
        ) : (
          <List
            size="small"
            dataSource={versions}
            renderItem={(v) => (
              <List.Item
                onClick={() => { setSelected(v.id); setPending(null); }}
                style={{
                  cursor: "pointer",
                  background: selected === v.id ? "rgba(22,119,255,0.06)" : undefined,
                }}
                actions={[
                  <Button
                    key="restore"
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={(e) => { e.stopPropagation(); setSelected(v.id); void preview(v.id); }}
                  >
                    回滚到这一版
                  </Button>,
                ]}
              >
                <Space direction="vertical" size={2}>
                  <Space wrap size={4}>
                    <Tag color={v.reason === "regenerate" ? "blue" : "orange"}>
                      {REASON_LABEL[v.reason] ?? v.reason}
                    </Tag>
                    <Typography.Text strong>{v.title || "（无标题）"}</Typography.Text>
                    <Typography.Text type="secondary">{fmtTime(v.at)}</Typography.Text>
                    <Typography.Text type="secondary">{v.nodes} 个节点</Typography.Text>
                  </Space>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {fmtParams(v.params)} · 用量：{fmtUsage(v.usage)}
                  </Typography.Text>
                </Space>
              </List.Item>
            )}
          />
        )}
        {selected && detail.data && (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <Typography.Text strong>这一版与当前大纲的差异</Typography.Text>
            {renderDiff(detail.data.diff)}
            {pending?.id === selected ? (
              <Alert
                type="warning"
                showIcon
                message="回滚前确认影响面"
                description={
                  <Space direction="vertical" size={4} style={{ width: "100%" }}>
                    <Typography.Text>{pending.impact.notice}</Typography.Text>
                    <Typography.Text>
                      会删掉现版大纲上绑定的内容 {pending.impact.content_total} 个、随堂习题
                      {pending.impact.quizzes} 道（现版 {pending.impact.nodes} 个节点）。
                    </Typography.Text>
                    {pending.impact.notes.count > 0 && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        笔记库文件 {pending.impact.notes.count} 个会在下次导出时重写：
                        {pending.impact.notes.sample.slice(0, 4).join("；")}
                      </Typography.Text>
                    )}
                    <Space>
                      <Button onClick={() => setPending(null)}>取消</Button>
                      <Button
                        type="primary"
                        danger
                        loading={restore.isPending}
                        onClick={() => restore.mutate({ id: pending.id, confirm: true })}
                      >
                        确认回滚
                      </Button>
                    </Space>
                  </Space>
                }
              />
            ) : (
              <Button
                type="primary"
                loading={restore.isPending}
                onClick={() => selected && void preview(selected)}
              >
                回滚到所选版本
              </Button>
            )}
          </Space>
        )}
      </Space>
    </Modal>
  );
}
