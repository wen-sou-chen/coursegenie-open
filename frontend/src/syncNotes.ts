/**
 * 笔记自动同步：课程内容变更后防抖自动导出到 Obsidian 笔记，
 * 并通过 window 事件通知 CourseDetail 顶部的同步状态。
 */
import { api } from "./api";

export interface SyncState {
  state: "idle" | "syncing" | "synced" | "error";
  time?: string;
  error?: string;
}

export interface SyncEvent {
  courseId: string;
  state: SyncState;
}

const EVENT = "coursegenie:notes-sync";
const DEBOUNCE_MS = 600;

let timer: ReturnType<typeof setTimeout> | undefined;

function now(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function emit(courseId: string, state: SyncState): void {
  window.dispatchEvent(new CustomEvent<SyncEvent>(EVENT, { detail: { courseId, state } }));
}

async function exportAndEmit(courseId: string): Promise<{ ok: boolean; msg: string }> {
  try {
    emit(courseId, { state: "syncing" });
    const res = await api.kbExport(courseId, false);
    const warn = res.warnings?.length ? `（${res.warnings.length} 条提示）` : "";
    const preserved = res.preserved.length ? `，保留手改 ${res.preserved.length} 个` : "";
    const msg = `已保存到笔记：${res.output_rel}（${res.files.length} 个文件${warn}${preserved}）`;
    emit(courseId, { state: "synced", time: now() });
    return { ok: true, msg };
  } catch (e) {
    const msg = String((e as Error)?.message ?? e);
    emit(courseId, { state: "error", time: now(), error: msg });
    return { ok: false, msg };
  }
}

/** 内容变更后调用：防抖合并，自动导出到 Obsidian 笔记。 */
export function requestAutoSync(courseId: string): void {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = undefined;
    void exportAndEmit(courseId);
  }, DEBOUNCE_MS);
}

/** 手动"保存到 Obsidian 笔记"（取消待执行的防抖）。 */
export function manualSync(courseId: string): Promise<{ ok: boolean; msg: string }> {
  if (timer) {
    clearTimeout(timer);
    timer = undefined;
  }
  return exportAndEmit(courseId);
}

export function subscribeSync(cb: (ev: SyncEvent) => void): () => void {
  const handler = (e: Event) => cb((e as CustomEvent<SyncEvent>).detail);
  window.addEventListener(EVENT, handler);
  return () => window.removeEventListener(EVENT, handler);
}
