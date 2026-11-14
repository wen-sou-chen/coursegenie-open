import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button, Space, Tag, Tooltip, message } from "antd";
import { ExclamationCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { DiagramFailure } from "../types";

const STAGE_TEXT: Record<string, string> = {
  "llm-json": "模型返回格式错误",
  layout: "排版自检未通过",
  network: "网络/接口失败",
  other: "生成失败",
};

/** 从讲义 meta 里取概念图失败记录（写入端见 backend lecture.py 的 diagram_failures）。 */
export function diagramFailures(meta: Record<string, unknown> | undefined | null): DiagramFailure[] {
  const raw = meta?.diagram_failures;
  return Array.isArray(raw) ? (raw as DiagramFailure[]).filter((r) => r && r.id) : [];
}

/** 讲义卡上的黄标：本图生成失败 + 单张重试（7c）。 */
export default function DiagramFailureTag({
  meta, itemId, onChanged,
}: {
  meta: Record<string, unknown> | undefined | null;
  itemId: string;
  onChanged?: () => void;
}) {
  const failures = diagramFailures(meta);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const retry = useMutation({
    mutationFn: (failureId: string) => api.retryDiagramFailure(itemId, failureId),
    onSettled: () => setPendingId(null),
    onSuccess: () => {
      message.success("已重新生成这张图，并同步到笔记");
      onChanged?.();
    },
    onError: (e: Error) => message.error(`重试失败：${e.message}`),
  });

  if (failures.length === 0) return null;

  return (
    <>
      {failures.map((f) => (
        <Space key={f.id} size={2} style={{ marginInlineEnd: 4 }}>
          <Tooltip title={f.reason || "该图没有生成出来"}>
            <Tag color="orange" icon={<ExclamationCircleOutlined />} style={{ marginInlineEnd: 0 }}>
              本图生成失败（{STAGE_TEXT[String(f.stage ?? "")] ?? f.slot ?? "未知"}）
            </Tag>
          </Tooltip>
          <Button
            type="link"
            size="small"
            danger
            icon={<ReloadOutlined />}
            loading={pendingId === f.id}
            onClick={() => { setPendingId(f.id); retry.mutate(f.id); }}
            style={{ fontSize: 12, padding: 0 }}
          >
            重试
          </Button>
        </Space>
      ))}
    </>
  );
}
