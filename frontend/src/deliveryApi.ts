/** M5c 交付包 API（任务 31）：清单预览 / 一键构建 / 下载。
 *
 * 单独成文件（不动 api.ts）：交付包构建是纯本地渲染与打包，0 次 LLM 调用。
 */
import { authHeader, handleUnauthorized } from "./auth";

const BASE = "/api";

export class DeliveryApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "DeliveryApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // 任务 48u：交付包接口同样要带 X-API-Key（没 key 时空对象，单机照旧）
  const headers = { ...authHeader(), ...((init?.headers as Record<string, string> | undefined) ?? {}) };
  const resp = await fetch(`${BASE}${path}`, { ...init, headers });
  if (resp.status === 401) handleUnauthorized();
  if (!resp.ok) {
    const text = await resp.text();
    let detail = text.slice(0, 200);
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (parsed?.detail !== undefined) {
        detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      }
    } catch {
      // 非 JSON 响应体：按原文截断展示
    }
    throw new DeliveryApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export interface DeliveryProblemRow {
  item: string;
  reason: string;
}

export interface DeliveryCounts {
  chapters: number;
  points: number;
  lectures: number;
  decks: number;
  figures: number;
  charts: number;
  diagrams: number;
  quizzes: number;
  concepts: number;
  materials: number;
  notes_files: number;
  pdf_ok?: number;
  pdf_failed?: number;
  files?: number;
  svg?: number;
  png?: number;
}

export interface DeliveryPlanItem {
  key: string;
  label: string;
  present: boolean;
}

/** 任务 87：按**课程场景**会多出哪些料（默认八件套之外的那几项）。
 *
 * 判定全部来自后端 `recording_kit` / `scenario`（界面不另写一份规则）：
 * recorded ⇒ 录课包（逐字稿 / 时长 / 字幕 / 分镜）；classroom|live ⇒ 互动点；
 * self_study ⇒ 学员版讲义；lms ⇒ SCORM 按课时打包。
 */
export interface DeliveryExtraItem {
  key: string;
  label: string;
  present: boolean;
  scene: string;
  count: number;
}

/** 任务 87：SCORM 导出前的自检结果（导出接口原样返回）。 */
export interface ScormSelfCheck {
  ok: boolean;
  items: number;
  resources: number;
  files: number;
  total_bytes: number;
  problems: string[];
  lessons?: number;
  estimated_seconds?: number;
  file?: string;
}

/** 任务 48p：交付模式（默认对外 = 客户包里不含 LLM 用量与金额）。 */
export interface DeliveryMode {
  external: boolean;
  mode: "external" | "internal";
  label: string;
}

export interface DeliveryPreview {
  course_id: string;
  course_title: string;
  counts: DeliveryCounts;
  items: DeliveryPlanItem[];
  extras?: DeliveryExtraItem[];
  recording_kit?: { enabled: boolean } & Record<string, number | boolean>;
  missing: DeliveryProblemRow[];
  pdf: { available: boolean; engine: string };
  delivery_mode?: DeliveryMode;
  mermaid: { available: boolean };
}

export interface DeliveryBuildRow {
  build_id: string;
  generated_at?: string | null;
  dir: string;
  zip: string;
  zip_bytes: number;
  counts: DeliveryCounts;
  missing: DeliveryProblemRow[];
  degraded: DeliveryProblemRow[];
  llm_calls: number;
  delivery_mode?: DeliveryMode;
  duration_ms: number;
  acceptance?: Record<string, unknown>;
}

export interface DeliveryBuildResult extends DeliveryBuildRow {
  course_id: string;
  files: number;
  bytes: number;
  rebuilt: boolean;
  notes_sync?: DeliveryNotesSync;
  vault?: { writes: number; created_dirs_cleaned: number };
  pdf: { generated: number; failed: number; engine: string };
}

/** 任务 38：生成前的一致性状态（笔记库 ↔ 网页端）。 */
export interface DeliveryConsistency {
  ok: boolean;
  reason: string;
  checked: number;
  inconsistent: number;
  consistent: number;
  vault_root?: string | null;
  hint: string;
}

/** 任务 38：出包前的笔记同步结果（验收单 / manifest 里的同一组数字）。 */
export interface DeliveryNotesSync {
  synced: boolean;
  checked: number;
  inconsistent_before: number;
  inconsistent_after: number;
  exported: boolean;
  export_files: number;
  export_preserved: number;
  export_error: string;
  forced: boolean;
  snapshot_at?: string | null;
  snapshot_label?: string | null;
}

export interface DeliveryOverview {
  course_id: string;
  course_title: string;
  preview: DeliveryPreview;
  builds: DeliveryBuildRow[];
  /** 任务 48z：默认 null —— 首屏不再同步跑整库一致性检查，由 consistency() 补。 */
  consistency?: DeliveryConsistency | null;
  /** 任务 48z：true = 本次响应没带一致性，需再请求 /consistency。 */
  consistency_deferred?: boolean;
  delivery_mode?: DeliveryMode;
}

export const deliveryApi = {
  /** 清单预览（不写盘）+ 历史交付包（新 → 旧）+ 交付模式。
   *
   * 任务 48z：**默认不再带一致性**（那一步要逐项比对整个笔记库，AI 课 4.33 秒），
   * 面板改为并行调 consistency()。需要「一次拿齐」的老口径传 withConsistency=true。
   */
  overview: (courseId: string, withConsistency = false) =>
    request<DeliveryOverview>(
      `/delivery/course/${courseId}${withConsistency ? "?with_consistency=1" : ""}`),
  /** 任务 48z：生成前的笔记一致性（独立接口，口径 = 任务 38，一个字没改）。 */
  consistency: (courseId: string) =>
    request<DeliveryConsistency>(`/delivery/course/${courseId}/consistency`),
  /** 一键生成：**同一份内容 = 同一次交付** —— 重复构建重建同一个包（跨分钟也复用，任务 126）。
   *
   * external（任务 48p）：true = 对外（不含用量与金额）/ false = 内部复盘；
   * 传了就写回课程交付模式，省略则用课程里已保存的模式（后端默认对外）。
   *
   * 默认（allowInconsistent=false）构建前先自检一致性：不一致就先自动导出一次笔记，
   * 导出后仍不一致会被后端拒绝（400，message 里带指引）。
   *
   * studentLectures（任务 86 §B4）：true = 额外产出 `学员版讲义/`（去答案与解析）；
   * **省略** = 按课程场景自动（含 `self_study` 才出）。默认八件套结构不变，**0 LLM**。
   */
  build: (courseId: string, pdf: "auto" | "edge" | "off" = "auto",
          allowInconsistent = false, external?: boolean, studentLectures?: boolean) =>
    request<DeliveryBuildResult>(`/delivery/course/${courseId}/build`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pdf, allow_inconsistent: allowInconsistent, external,
                             student_lectures: studentLectures }),
    }),
  /** 某个包的文件清单（sha1 / 字节数 / 生成参数）。 */
  manifest: (buildId: string) => request<Record<string, unknown>>(`/delivery/${buildId}`),
  /** zip 下载地址（同源，经 vite 代理转发到后端）。 */
  zipUrl: (buildId: string) => `${BASE}/delivery/${buildId}/download`,
  /** 任务 87：按课时打 SCORM 1.2 包（0 LLM；导出前自检，不通过不落盘）。 */
  exportScorm: (courseId: string) =>
    request<{ ok: boolean; file: string; format: string; url: string;
              self_check: ScormSelfCheck }>(
      "/publish/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ course_id: courseId, format: "scorm" }),
      }),
};
