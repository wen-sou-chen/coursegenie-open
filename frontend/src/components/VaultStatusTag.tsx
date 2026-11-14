import { useMutation } from "@tanstack/react-query";
import { Button, Tag, Tooltip, message } from "antd";
import {
  CheckCircleOutlined, ExclamationCircleOutlined, InfoCircleOutlined, ReloadOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { preservedReason } from "../preserved";

export type VaultMeta = Record<string, unknown> | undefined | null;

/** 从条目 meta.vault 中读取增量写入状态（saved / skipped / failed）。 */
export function vaultState(meta: VaultMeta): "saved" | "skipped" | "failed" | "none" {
  const v = String((meta as Record<string, unknown> | undefined)?.vault_write ?? "");
  if (v === "saved") return "saved";
  if (v === "skipped") return "skipped";
  if (v) return "failed";
  return "none";
}

const CONFIG_HINT =
  "未配置 Obsidian 笔记库：请到「第 5 步 存入笔记」或「素材与笔记」面板，绑定 Vault 目录后再生成；绑定后新内容会自动增量写入。";

export default function VaultStatusTag({
  vault, itemId, kind = "content", onChanged,
}: {
  vault: VaultMeta;
  itemId: string;
  kind?: "content" | "quiz";
  onChanged?: () => void;
}) {
  const state = vaultState(vault);
  const retry = useMutation({
    mutationFn: () =>
      kind === "quiz" ? api.retryQuizVault(itemId) : api.retryVaultWrite(itemId),
    onSuccess: () => {
      message.success("已重新写入笔记");
      onChanged?.();
    },
    onError: (e: Error) => message.error(`笔记写入失败：${e.message}`),
  });

  if (state === "none") return null;

  if (state === "saved") {
    const paths = [vault?.note_path, vault?.chart_path, vault?.vault_path]
      .filter(Boolean)
      .join("\n");
    const kept = Array.isArray(vault?.preserved)
      ? (vault?.preserved as Array<Record<string, unknown>>)
      : [];
    return (
      <>
        <Tooltip title={paths ? `已同步到：\n${paths}` : "已同步到笔记库"}>
          <Tag color="green" icon={<CheckCircleOutlined />} style={{ marginInlineEnd: 4 }}>
            已保存到笔记
          </Tag>
        </Tooltip>
        {kept.length > 0 && (
          <Tooltip
            title={`本次有 ${kept.length} 个文件未更新（网页与笔记可能不一致）：\n` +
              kept.map((k) => `${String(k.file ?? "")} —— ${preservedReason(String(k.reason ?? ""))}`).join("\n")}
          >
            <Tag color="orange" icon={<ExclamationCircleOutlined />} style={{ marginInlineEnd: 4 }}>
              {kept.length} 个文件未更新
            </Tag>
          </Tooltip>
        )}
      </>
    );
  }

  if (state === "skipped") {
    return (
      <Tooltip title="未绑定 Vault，点击查看绑定说明">
        <Tag
          icon={<InfoCircleOutlined />}
          style={{ marginInlineEnd: 4, cursor: "pointer" }}
          onClick={() => message.info(CONFIG_HINT, 6)}
        >
          未配置笔记库
        </Tag>
      </Tooltip>
    );
  }

  return (
    <Tooltip title={`写入失败：${String(vault?.error ?? vault?.reason ?? "")}`}>
      <Tag color="orange" icon={<ExclamationCircleOutlined />} style={{ marginInlineEnd: 4 }}>
        笔记写入失败
      </Tag>
      <Button
        type="link"
        size="small"
        icon={<ReloadOutlined />}
        loading={retry.isPending}
        onClick={() => retry.mutate()}
        style={{ fontSize: 12, padding: 0 }}
      >
        重试
      </Button>
    </Tooltip>
  );
}
