/** 笔记导出「未更新文件」的原因文案与展示拼装（保存到笔记 / 一致性提示共用）。 */
import type { PreservedEntry } from "./types";

export const PRESERVED_REASON_TEXT: Record<string, string> = {
  "manual-edit": "在 Obsidian 里手动改过，已跳过覆盖",
  "unknown-drift": "与导出记录不一致且无版本指纹，保守跳过",
};

export function preservedReason(reason: string): string {
  return PRESERVED_REASON_TEXT[reason] ?? (reason || "未知原因");
}

export function preservedFileText(entry: PreservedEntry): string {
  return `${entry.file} —— ${preservedReason(entry.reason)}`;
}
