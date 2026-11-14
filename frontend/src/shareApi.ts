/** M5a 分享链接 API：管理侧（沿用 X-API-Key）+ 公开侧（免登录、严格只读）。
 *
 * 单独成文件（不改 api.ts），公开侧请求绝不允许带 X-API-Key。
 */
import type { ContentRow, PlanNode, PptSlide } from "./types";

const BASE = "/api";

export class ShareApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ShareApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, init);
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
    throw new ShareApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

function json<T>(path: string, method: "POST" | "DELETE", data?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data ?? {}),
  });
}

function passwordInit(password?: string): RequestInit | undefined {
  return password ? { headers: { "X-Share-Password": password } } : undefined;
}

export interface ShareLinkRow {
  id: string;
  course_id: string;
  label: string;
  permission: string;
  expires_at: string | null;
  revoked_at: string | null;
  has_password: boolean;
  created_at: string;
  access_count: number;
  last_accessed_at: string | null;
  status: "active" | "revoked" | "expired" | string;
  url?: string;
  token?: string;
  notice?: string;
}

export interface ShareOverview {
  course: {
    id: string;
    title: string;
    subject: string;
    audience: string;
    duration_minutes: number;
    status: string;
  };
  chapter_count: number;
  node_count: number;
  point_count: number;
  concept_count: number;
  share: {
    label: string;
    permission: string;
    expires_at: string | null;
    has_password: boolean;
    access_count: number;
  };
}

export interface SharePlan {
  outline_id: string | null;
  title: string;
  goal: string;
  nodes: PlanNode[];
  graph: { nodes: unknown[]; edges: unknown[] };
}

export interface ShareDeckRow {
  id: string;
  title: string;
  scope: "course" | "chapter" | string;
  chapter_node_id?: string | null;
  chapter_key?: string | null;
  engine?: string | null;
  slide_count?: number | null;
  generated_at?: string | null;
  page_count: number;
  has_images: boolean;
  slides: PptSlide[];
}

export interface SharePptGrouped {
  items: ShareDeckRow[];
  course: ShareDeckRow[];
  chapters: ShareDeckRow[];
}

export interface ShareCreatePayload {
  label?: string;
  expires_in_days?: number | null;
  password?: string;
  permission?: "view" | "comment";
  base_url?: string;
}

export const shareApi = {
  /** 创建链接：明文 token / url 只在这次响应里出现一次。 */
  create: (courseId: string, data: ShareCreatePayload) =>
    json<ShareLinkRow>(`/share/course/${courseId}`, "POST", data),
  list: (courseId: string) =>
    request<{ course_id: string; items: ShareLinkRow[] }>(`/share/course/${courseId}`),
  revoke: (linkId: string) => json<ShareLinkRow>(`/share/${linkId}/revoke`, "POST"),
  remove: (linkId: string) => json<{ ok: boolean; id: string }>(`/share/${linkId}`, "DELETE"),
};

/** 答案可见性（任务 86 §B4）：与后端 `services/audience.py` 的 VIEWS 一致。 */
export type AudienceView = "teacher" | "student";

export const publicShareApi = {
  overview: (token: string, password?: string) =>
    request<ShareOverview>(`/public/${token}`, passwordInit(password)),
  plan: (token: string, password?: string) =>
    request<SharePlan>(`/public/${token}/plan`, passwordInit(password)),
  /** 任务 86 §B4：`view` = teacher（**默认 = 现状**，含答案与解析）/ student（学员版：
   * 讲义里的「答案 / 解析 / 评分要点」整行去掉、正确项不再标 ✓；题目与作答区保留）。
   * 只做确定性文本处理，不调模型。 */
  content: (token: string, password?: string, view: AudienceView = "teacher") =>
    request<ContentRow[]>(`/public/${token}/content?view=${view}`, passwordInit(password)),
  ppt: (token: string, password?: string) =>
    request<SharePptGrouped>(`/public/${token}/ppt`, passwordInit(password)),
};

/** 公开侧图片/课件图代理前缀（同源，经 vite 代理转发到后端）。 */
export function publicMediaPrefix(token: string): string {
  return `${BASE}/public/${token}`;
}