import type {

  Analytics,

  ContentRow,

  Course,

  ConsistencyReport,

  KbExportResult,

  KbInfo,

  MaterialFull,
  MaterialLink,
  MaterialRow,

  PlanHistory,

  PlanVersionDetail,

  PlanRestoreResult,

  Plan,
  PlanCheckStored,

  PlanNodeChange,
  PlanNodeImpact,

  PlanNode,

  PptDeck,

  PptGrouped,

  PptRow,

  PptPreview,

  ConceptList,

  StyleAnchor,

  VersionRow,

  CourseQcReport,

  LectureQcPayload,

  QcCheck,

  CourseUsage,

  ConceptGraph,

  ConceptGraphExport,

  ConceptSupplementResult,

  CourseAudit,

  CourseAuditExport,

  CourseAuditOverview,

  StyleLibraryExportResult,

  StyleLibraryPreview,

} from "./types";

import { authHeader, handleUnauthorized } from "./auth";



type LectureResult = {

  id: string;

  kind: string;

  title: string;

  body: { markdown?: string; summary?: string; keywords?: string[]; tone?: string };

  meta: Record<string, unknown>;

};



type NotesZipResult = {

  ok: boolean;

  file: string;

  url: string;

  scope: "all" | "nodes";

};



const BASE = "/api";


async function request<T>(path: string, init?: RequestInit): Promise<T> {

  const isForm = init?.body instanceof FormData;

  const resp = await fetch(`${BASE}${path}`, {

    ...init,

    // 任务 48u：带上 X-API-Key（没有 key 时 authHeader() 是空对象，

    // 单机 AUTH_ENABLED=false 依旧免登录可用）；FormData 不能设 Content-Type。

    headers: {

      ...(isForm ? {} : { "Content-Type": "application/json" }),

      ...authHeader(),

      ...((init?.headers as Record<string, string> | undefined) ?? {}),

    },

  });

  if (resp.status === 401) handleUnauthorized();   // 任务 48u：key 失效 ⇒ 回登录页

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

    throw new Error(detail);

  }

  return resp.json() as Promise<T>;

}



/** 取「文本」响应（网页版课件预览）：鉴权 / 401 / 错误提示与 request 同一条路。 */

async function requestText(path: string, init?: RequestInit): Promise<string> {

  const resp = await fetch(`${BASE}${path}`, {

    ...init,

    headers: { ...authHeader(), ...((init?.headers as Record<string, string> | undefined) ?? {}) },

  });

  if (resp.status === 401) handleUnauthorized();   // 任务 48u：key 失效 ⇒ 回登录页

  if (!resp.ok) {

    const text = await resp.text();

    throw new Error(text.slice(0, 200) || `HTTP ${resp.status}`);

  }

  return resp.text();

}



function json<T>(path: string, method: "POST" | "PATCH" | "DELETE", data?: unknown): Promise<T> {

  return request<T>(path, { method, body: JSON.stringify(data ?? {}) });

}



type JobStatusPayload = {
  job_id: string;
  status: string;
  result: Record<string, unknown>;
  error: string;
};

async function pollJobResult(jobId: string, tries: number, failMessage: string) {
  for (let i = 0; i < tries; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    const s = await request<JobStatusPayload>(`/content/jobs/${jobId}`);
    if (s.status === "failed") throw new Error(s.error || failMessage);
    if (s.status === "success") return s;
  }
  throw new Error("讲义生成仍在进行，请稍后到「课程预览」查看结果");
}

/** 任务 102：音视频素材上传的返回 —— 素材行（含 `asr` 状态）+ 转写作业号。 */
export type MaterialUploadResult = MaterialRow & { job_id?: string | null; size_bytes?: number };

export type CourseDeleteResult = {
  ok: boolean;
  removed_files: number;
  removed_dirs: number;
  counts: {
    outlines: number;
    content_items: number;
    quizzes: number;
    materials: number;
  };
};

export const api = {

  // 课程

  listCourses: () => request<Course[]>("/course/"),

  createCourse: (data: Partial<Course>) => request<Course>("/course/", { method: "POST", body: JSON.stringify(data) }),

  getCourse: (id: string) => request<Course>(`/course/${id}`),

  /** 任务 85：改课程（PATCH）。`meta` 是**合并**语义 ⇒ 只带 `scenario` 不会清掉 kb 绑定等既有键。 */
  updateCourse: (id: string, data: Partial<Course>) =>
    request<Course>(`/course/${id}`, { method: "PATCH", body: JSON.stringify(data) }),



  /** 删除课程：confirm 必须是课程全名（后端不一致会返回 400）。 */
  deleteCourse: (id: string, confirm: string) =>
    request<CourseDeleteResult>(`/course/${id}?confirm=${encodeURIComponent(confirm)}`,
      { method: "DELETE" }),

  // 大纲

  generatePlan: (data: { course_id: string; audience: string; goal: string; num_chapters: number;
                   style: string; semantic?: boolean }) =>

    request<Plan>("/plan/generate", { method: "POST", body: JSON.stringify(data) }),

  getPlan: (courseId: string) => request<Plan>(`/plan/${courseId}`),

  /** 任务 70 §B2：大纲自检的**落库结论** —— 刷新 / 换设备后仍在（没生成过时 check 为 null）。 */

  getPlanCheck: (courseId: string) => request<PlanCheckStored>(`/plan/${courseId}/check`),

  /** 任务 81 §B3：对**当前大纲**补跑一次自检并落库（不重新生成大纲、不调模型）。 */

  runPlanCheck: (courseId: string) => json<PlanCheckStored>(`/plan/${courseId}/check`, "POST", {}),

  /** 大纲历史版本（P3-1）：每版参数 + 节点数 + 用量，最新在前。 */
  planHistory: (courseId: string) => request<PlanHistory>(`/plan/${courseId}/history`),

  /** 某个历史版本的节点快照 + 与当前大纲的 diff + 回滚影响面。 */
  planVersion: (courseId: string, versionId: string) =>
    request<PlanVersionDetail>(`/plan/${courseId}/history/${versionId}`),

  /** 回滚到某一版；confirm=false 时后端返回 409 + 影响面（二次确认）。 */
  restorePlanVersion: (courseId: string, versionId: string, confirm = false) =>
    json<PlanRestoreResult>(`/plan/${courseId}/history/${versionId}/restore`, "POST", { confirm }),

  createPlanNode: (data: {
    course_id: string;
    title: string;
    parent_id?: string | null;
    position?: number;
    node_type?: string;
    objective?: string;
  }) => json<PlanNodeChange>("/plan/node", "POST", data),

  planNodeImpact: (nodeId: string) => request<PlanNodeImpact>(`/plan/node/${nodeId}/impact`),

  movePlanNode: (nodeId: string, data: {
    direction?: "up" | "down";
    reparent?: boolean;
    to_parent_id?: string | null;
    position?: number;
    confirm?: boolean;
  }) => json<PlanNodeChange>(`/plan/node/${nodeId}/move`, "POST", data),

  deletePlanNode: (nodeId: string, cascade = false) =>
    request<PlanNodeChange>(`/plan/node/${nodeId}?cascade=${cascade}`, { method: "DELETE" }),

  updatePlanNode: (nodeId: string, data: Partial<PlanNode>) =>

    json<PlanNode>(`/plan/node/${nodeId}`, "PATCH", data),

  approvePlan: (courseId: string, approved: boolean) =>

    json<{ ok: boolean; status: string }>("/plan/approve", "POST", { course_id: courseId, approved }),



  // 内容

  generateContent: (data: { course_id: string; node_ids?: string[]; tone: string; include_quiz: boolean }) =>

    json<{ job_id: string }>("/content/generate", "POST", data),

  listContent: (courseId: string) => request<ContentRow[]>(`/content/${courseId}`),

  jobStatus: (jobId: string) =>

    request<{ job_id: string; status: string; result: Record<string, unknown>; error: string }>(

      `/content/jobs/${jobId}`,

    ),

  updateContent: (id: string, data: { title?: string; body?: Record<string, unknown> }) =>

    json<ContentRow>(`/content/${id}`, "PATCH", data),

  deleteContent: (id: string) => json<{ ok: boolean }>(`/content/${id}`, "DELETE"),



  // PPT

  generatePpt: (data: {

    course_id: string;

    scope?: "course" | "chapter";

    chapter_node_id?: string;

    engine?: "python-pptx" | "dashi";

    theme?: string;

    item_id?: string;

  }) => json<{ job_id: string; status: string; message: string }>("/ppt/generate", "POST", data),

  /** 补齐全部章节课件：逐章串行；redo=false 时只补缺（已生成的章跳过）。 */
  generateChapterPpts: (courseId: string, opts?: {
    redo?: boolean;
    engine?: "python-pptx" | "dashi";
    theme?: string;
  }) => json<{ job_id: string; status: string; message: string }>("/ppt/generate-chapters", "POST", {
    course_id: courseId,
    redo: opts?.redo ?? false,
    engine: opts?.engine ?? "python-pptx",
    theme: opts?.theme ?? "",
  }),

  listPpt: (courseId: string) => request<PptGrouped>(`/ppt/${courseId}`),

  /**
   * 五套内置主题。传 `courseId` 时额外回**本课当前主题**与**它是哪来的**
   * （`course.source`：`course` 本课设置 / `config` 后端配置 / `default` 内置默认；
   * 任务 145：前端据此把下拉默认选中「本课当前主题」，不是第一个、也不是全局默认）。
   */
  pptThemes: (courseId?: string) =>

    request<{
      items: { id: string; name: string; description: string; accent: string; colors: string[] }[];
      /** 后端配置（PPT_THEME）解析出的主题：前端按它给图表兜底上色。 */
      active: { id: string; name: string; colors: string[]; error: string | null };
      /** 传了 courseId 才有：本课当前主题 + 来源（含课程里存脏值时的原因）。 */
      course?: {
        id: string; name: string; colors: string[]; source: string;
        source_label: string; setting: string | null; error: string | null;
      } | null;
    }>(`/ppt/themes${courseId ? `?course_id=${encodeURIComponent(courseId)}` : ""}`),

  /**
   * 任务 145：设置**本课主题**（课程级设置 `course.meta.ppt_theme`）。
   * 只改外观、不改内容；**已生成的课件不会变，必须重新生成才生效**；
   * 非法值 400 并给出合法值清单；`theme=""` = 清除本课设置（回到后端配置）。
   */
  setPptTheme: (courseId: string, theme: string) =>

    json<{
      ok: boolean; course_id: string; theme: string; theme_name: string;
      setting: string | null; source: string; source_label: string; cleared: boolean;
      note: string; message: string;
    }>("/ppt/theme", "POST", { course_id: courseId, theme }),

  pptPreview: (itemId: string) =>

    json<PptPreview>(`/ppt/${itemId}/preview`, "POST"),

  /** 任务 94：网页版课件（带入场动效）—— 单文件 HTML 原文，前端开新标签页看。 */

  pptDeckHtml: (itemId: string) => requestText(`/ppt/${itemId}/html`),

  /** 任务 150：讲师模式（逐条揭示 / 展开 / 导图 / 讲者视图）—— 单文件 HTML 原文。 */

  pptPresenterHtml: (itemId: string) => requestText(`/ppt/${itemId}/presenter`),

  /** 任务 151：学生版（地图 / 微单元 / 即时自测 / 复习卡）—— 单文件 HTML 原文。 */

  pptStudentHtml: (courseId: string) => requestText(`/ppt/${courseId}/student-html`),

  pptJobStatus: (jobId: string) =>

    request<{ job_id: string; status: string; result: Record<string, unknown>; error: string }>(

      `/ppt/jobs/${jobId}`,

    ),

  importPpt: (courseId: string, file: File, opts?: { scope?: "course" | "chapter"; chapter_node_id?: string }) => {

    const fd = new FormData();

    fd.append("course_id", courseId);

    if (opts?.scope) fd.append("scope", opts.scope);

    if (opts?.chapter_node_id) fd.append("chapter_node_id", opts.chapter_node_id);

    fd.append("file", file);

    return request<PptDeck>("/ppt/import", { method: "POST", body: fd });

  },



  // 概念库（原子笔记 v1：一概念一篇 + 双链 + 来源追溯）

  listConcepts: (courseId: string) => request<ConceptList>(`/concept/course/${courseId}`),

  generateConcepts: (courseId: string) =>

    json<{ job_id: string; status: string; message: string }>(

      `/concept/course/${courseId}/generate`,

      "POST",

      {},

    ),

  conceptJobStatus: (jobId: string) =>

    request<{ job_id: string; status: string; result: Record<string, unknown>; error: string }>(

      `/concept/jobs/${jobId}`,

    ),

  updateConcept: (id: string, data: {

    definition?: string;

    points?: string[];

    pitfalls?: string[];

    aliases?: string[];

    relations?: Record<string, string[]>;

  }) => json<{ id: string; name: string; body: Record<string, unknown>; meta: Record<string, unknown> }>(

    `/concept/${id}`,

    "PATCH",

    data,

  ),

  deleteConcept: (id: string) => json<{ ok: boolean; id: string }>(`/concept/${id}`, "DELETE"),

  /** P6-1：一键补抽指定概念（库外关系目标 / 孤点）；names 留空 = 取图谱里的库外目标。 */

  supplementConcepts: (courseId: string, names: string[]) =>

    json<ConceptSupplementResult>(`/concept/course/${courseId}/supplement`, "POST", { names }),

  // 习题

  generateQuiz: (data: { course_id: string; node_id?: string; count: number; difficulty: string }) =>

    json<unknown[]>("/quiz/generate", "POST", data),

  updateQuiz: (id: string, data: Record<string, unknown>) => json<ContentRow>(`/quiz/${id}`, "PATCH", data),

  deleteQuiz: (id: string) => json<{ ok: boolean }>(`/quiz/${id}`, "DELETE"),



  // 流程图

  generateFlowchart: (data: { course_id: string; description: string; kind: string; item_id?: string; node_id?: string }) =>

    json<ContentRow>("/flowchart/generate", "POST", data),

  importFlowchart: (data: { course_id: string; title: string; mermaid: string; item_id?: string; node_id?: string }) =>

    json<ContentRow>("/flowchart/import", "POST", data),



  // 图片

  generateImage: (data: {

    course_id: string;

    prompt: string;

    size: string;

    item_id?: string;

    node_id?: string;

    text_lang?: string;

    overlay_text?: string;

    overlay_pos?: string;

    model?: string;

  }) =>

    json<ContentRow>("/image/generate", "POST", data),

  importImage: (courseId: string, file: File, itemId?: string, nodeId?: string) => {

    const fd = new FormData();

    fd.append("course_id", courseId);

    if (itemId) fd.append("item_id", itemId);

    if (nodeId) fd.append("node_id", nodeId);

    fd.append("file", file);

    return request<ContentRow>("/image/import", { method: "POST", body: fd });

  },



  // 教学图表（bar/line/pie/mindmap/timeline/table/quadrant）

  generateChart: (data: {

    course_id: string;

    node_id?: string;

    chart_type: string;

    instruction?: string;

    item_id?: string;

  }) => json<ContentRow>("/chart/generate", "POST", data),

  deleteChart: (id: string) => json<{ ok: boolean }>(`/chart/${id}`, "DELETE"),



  // 增量写入 Vault 重试

  retryVaultWrite: (id: string) =>

    json<{ id: string; vault: Record<string, unknown> }>(`/content/${id}/vault`, "POST"),

  retryQuizVault: (id: string) =>

    json<{ id: string; vault: Record<string, unknown> }>(`/quiz/${id}/vault`, "POST"),



  // 素材（原始资料）

  listMaterials: (courseId: string) => request<MaterialRow[]>(`/material/${courseId}`),

  uploadMaterial: (courseId: string, file: File, title?: string) => {

    const fd = new FormData();

    fd.append("course_id", courseId);

    if (title) fd.append("title", title);

    fd.append("file", file);

    return request<MaterialUploadResult>("/material/upload", { method: "POST", body: fd });

  },

  pasteMaterial: (data: { course_id: string; title: string; text: string }) =>

    json<MaterialRow>("/material/paste", "POST", data),

  generateMaterials: (data: { course_id: string; directions: string[]; custom_instruction?: string }) =>

    json<MaterialRow[]>("/material/generate", "POST", data),

  getMaterialText: (id: string) => request<{ id: string; title: string; text: string }>(`/material/text/${id}`),

  /** P2-1：任意素材（含 PPT/PDF/Word）的解析全文，分页读。
   *
   *  任务 69 §B1.5.3：`numbered=true` 时按**段落**返回（offset/limit 变成段号/段数）——
   *  AI 补充卡片上「第 N 段」点进来就能定位到同一段（口径与注入/素材页一致）。
   */

  getMaterialFull: (id: string, params?: { offset?: number; limit?: number; numbered?: boolean }) =>

    request<MaterialFull>(
      `/material/full/${id}?offset=${params?.offset ?? 0}&limit=${params?.limit ?? 2000}`
      + `&numbered=${params?.numbered ? "true" : "false"}`),

  updateMaterial: (id: string, data: { title?: string; text?: string }) =>

    json<MaterialRow>(`/material/${id}`, "PATCH", data),

  deleteMaterial: (id: string) => json<{ ok: boolean }>(`/material/${id}`, "DELETE"),

  /** 任务 69 §B1.5.1：一键清理**上传原件**里的页眉页脚 / 对话外壳（默认不动原稿，点了才改）。 */

  cleanMaterial: (id: string) =>

    json<{ ok: boolean; cleaned_lines: number; material: MaterialRow }>(
      `/material/${id}/clean`, "POST", {}),

  /**
   * 任务 72 §B2：**显式**补齐老素材的页数 / 来源 / 外壳行数（只写 meta，正文不动）。
   *
   * 修前这一步藏在 `GET /material/{course_id}` 里 ⇒ 打开素材页就会悄悄写库。
   */

  backfillMaterialMeta: (courseId: string) =>

    json<{ ok: boolean; backfilled: number; total: number }>(
      `/material/${courseId}/backfill`, "POST", {}),

  /**
   * 任务 102：转写作业的状态 / 进度（**与内容生成共用** `GET /content/jobs/{job_id}` 那条链）。
   *
   * `result.progress` = 已转写时长 ÷ 总时长（0–1）；转完 / 失败后前端停轮询。
   */

  getJobStatus: (jobId: string) => request<JobStatusPayload>(`/content/jobs/${jobId}`),

  /**
   * 任务 102：音视频素材的**显式**「重新转写」（跳过 / 失败 / 换了模型后重跑）。
   *
   * 和「补齐素材信息」一样是显式入口 —— 打开素材页的 GET 不会顺手回填。
   */

  transcribeMaterial: (id: string) =>

    json<MaterialUploadResult & { ok: boolean }>(`/material/${id}/transcribe`, "POST", {}),

  /**
   * 任务 118：素材的**第三种来源** —— 从链接导入（粘贴 YouTube 视频 / 播放列表链接）。
   *
   * 后端按**写死**的顺序抓：① 抓字幕 → ② 没字幕才下音频走**本机**转写（0 模型调用）→
   * ③ 都不行就写清原因并保留素材行（可重试）。返回体里的素材行是**入队时刻**的状态
   * （`link.status = "pending"`），进度看 `job_id` 走 `GET /content/jobs/{job_id}`。
   * `cookies` 只传**路径**：后端不复制、不落库、不进日志、不进返回。
   */
  importMaterialLink: (data: { course_id: string; url: string; title?: string; cookies?: string }) =>
    json<MaterialUploadResult & { message?: string; warning?: string; link?: MaterialLink }>(
      "/material/import-link", "POST", data),

  /** 任务 118：**重新导入**（抓失败 / 装好依赖 / 换了新 cookie 后重跑；链接存在素材行里）。 */
  reimportMaterialLink: (id: string, data?: { cookies?: string }) =>
    json<MaterialUploadResult & { ok: boolean }>(`/material/${id}/import-link`, "POST", data ?? {}),



  // 版本 / 分析 / 导出

  versions: (courseId: string) => request<VersionRow[]>(`/version/${courseId}`),

  // 任务 48y：面板有「备注」列，后端也支持 ?note= ⇒ 传过去，别让备注永远是空
  snapshot: (courseId: string, note = "") =>
    json<{ version_id: string }>(
      `/version/${courseId}/snapshot${note ? `?note=${encodeURIComponent(note)}` : ""}`, "POST"),

  restore: (courseId: string, versionId: string) =>

    json<{ ok: boolean }>(`/version/${courseId}/restore/${versionId}`, "POST"),

  analytics: (courseId: string) => request<Analytics>(`/analytics/${courseId}`),



  // 笔记系统（Obsidian 成果快照）

  // 完整讲义

  /** 任务 91：目标讲义已「审核通过/人工编辑」加锁时后端返 409；
   * 界面在锁定态下先弹确认、确认后把 `allow_overwrite_locked` 置真。 */
  generateLecture: async (data: {
    course_id: string;
    node_id: string;
    tone?: string;
    item_id?: string;
    allow_overwrite_locked?: boolean;
  }) => {
    const queued = await json<{ job_id: string; status: string }>(
      "/lecture/generate",
      "POST",
      data,
    );
    const done = await pollJobResult(queued.job_id, 240, "讲义生成失败");
    const item = (done.result?.item ?? done.result) as LectureResult;
    if (!item || typeof item !== "object" || !("id" in item)) {
      throw new Error("讲义生成结果异常：缺少条目数据");
    }
    return item;
  },
  updateLecture: (itemId: string, markdown: string) =>

    json<LectureResult>(`/lecture/${itemId}`, "PATCH", { markdown }),

  deleteLecture: (itemId: string) =>

    json<{ ok: boolean; id: string }>(`/lecture/${itemId}`, "DELETE"),

  generateAllLectures: (data: { course_id: string; tone?: string; ignore_style_anchor?: boolean; force?: boolean;
    allow_anchor_overwrite?: boolean }) =>
    json<{ job_id: string; status: string }>("/lecture/generate-all", "POST", data),

  // M3 按章批量：一章一个任务，可停可续、可单章重跑
  generateChapterLectures: (data: { course_id: string; chapter_node_id: string; tone?: string; force?: boolean;
    allow_anchor_overwrite?: boolean }) =>
    json<{ job_id: string; status: string; total: number; chapter_key: string; chapter_title: string }>(
      "/lecture/generate-chapter", "POST", data),

  // M3 样板与风格锚（P4-1：设样板会自动给样板节加锁，locked/lock_notice 给界面提示）
  getStyleAnchor: (courseId: string) =>
    request<{ course_id: string; style_anchor: StyleAnchor | null; summary: string;
      locked: boolean; lock_notice: string; source_item_id?: string | null }>(
      `/lecture/style-anchor?course_id=${courseId}`),

  setStyleAnchor: (data: {
    course_id: string;
    source_item_id?: string;
    tone?: string;
    depth?: string;
    figure_style?: string;
    quiz_difficulty?: string;
    terminology?: string[];
  }) => json<{ ok: boolean; style_anchor: StyleAnchor; summary: string; locked?: boolean;
    lock_notice?: string }>(
    "/lecture/style-anchor", "POST", data),

  // P4-1 样板节一键加锁 / 解锁（换样板时先解锁才能重做那一节）
  lockStyleAnchorSource: (courseId: string, locked = true) =>
    json<{ ok: boolean; locked: boolean; source_item_id?: string; lock_notice: string }>(
      "/lecture/style-anchor/source-lock", "POST", { course_id: courseId, locked }),

  clearStyleAnchor: (courseId: string) =>
    json<{ ok: boolean }>(`/lecture/style-anchor?course_id=${courseId}`, "DELETE"),

  // M3 节点加锁：审核通过/人工编辑过的讲义批量不再覆盖
  lockLecture: (itemId: string, locked = true, reason = "") =>
    json<{ id: string; locked: boolean; meta: Record<string, unknown> }>(
      `/lecture/${itemId}/lock`, "POST", { locked, reason }),

  // 任务 22：讲义质检（单篇报告 / 整课汇总 / 按质检建议重生成）
  lectureQc: (itemId: string, refresh = false) =>
    request<LectureQcPayload>(`/lecture/${itemId}/qc${refresh ? "?refresh=true" : ""}`),

  courseQc: (courseId: string, opts: { worst_n?: number; refresh?: boolean } = {}) =>
    json<CourseQcReport>(`/lecture/course/${courseId}/qc`, "POST", {
      worst_n: opts.worst_n ?? 5,
      refresh: opts.refresh ?? false,
    }),

  // 「按质检建议重生成」：异步入队 + 轮询到结束（提示词里带上了质检问题）
  regenerateWithQc: async (itemId: string, tone?: string) => {
    const queued = await json<{ job_id: string; status: string; qc_issues: QcCheck[] }>(
      `/lecture/${itemId}/regenerate-qc`, "POST", { tone });
    await pollJobResult(queued.job_id, 240, "按质检建议重生成失败");
    return queued;
  },

  // 任务 23：课程级用量与成本（累计 tokens/费用、平均耗时、最贵 N 篇、按 kind 分布）
  courseUsage: (courseId: string, topN = 5) =>
    request<CourseUsage>(`/analytics/${courseId}/usage?top_n=${topN}`),



  // 笔记 ZIP 导出

  exportNotesZip: (data: { course_id: string; scope: "all" | "nodes"; node_ids?: string[] }) =>

    json<NotesZipResult>("/publish/export-notes-zip", "POST", data),



  kbInfo: (courseId: string) => request<KbInfo>(`/kb/course/${courseId}`),
  kbBind: (courseId: string, data: { vault_root?: string | null; output_rel?: string | null }) =>

    json<{ ok: boolean; kb: Record<string, unknown> }>(`/kb/course/${courseId}/bind`, "POST", data),

  kbExport: (courseId: string, force = false) =>

    json<KbExportResult>(`/kb/course/${courseId}/export?force=${force}`, "POST"),

  retryDiagramFailure: (itemId: string, failureId: string) =>

    json<LectureResult>(`/lecture/${itemId}/failures/${failureId}/retry`, "POST"),

  kbVerify: (courseId: string, nodeIds: string[] = []) =>

    json<ConsistencyReport>(`/kb/course/${courseId}/verify`, "POST", { node_ids: nodeIds }),

  kbRepair: (courseId: string, nodeIds: string[] = []) =>

    json<{ ok: boolean; repaired: unknown[]; failed: unknown[]; after: ConsistencyReport }>(

      `/kb/course/${courseId}/repair`, "POST", { node_ids: nodeIds }),



  // 任务 24：知识图谱 / 风格库 / 课程资产体检报告

  conceptGraph: (courseId: string, format: "mermaid" | "canvas" = "mermaid") =>

    request<ConceptGraph>(`/concept/course/${courseId}/graph?format=${format}`),

  exportConceptGraph: (courseId: string) =>

    json<ConceptGraphExport>(`/concept/course/${courseId}/graph`, "POST"),

  styleLibrary: (courseId: string) =>

    request<StyleLibraryPreview>(`/kb/course/${courseId}/style-library`),

  exportStyleLibrary: (courseId: string) =>

    json<StyleLibraryExportResult>(`/kb/course/${courseId}/style-library`, "POST"),

  courseAudit: (courseId: string) =>

    request<CourseAudit>(`/audit/course/${courseId}/report`),

  /** 任务 48z：体检首屏轻量概览 —— 不跑整库笔记一致性检查（实测 0.29 秒）。 */

  courseAuditOverview: (courseId: string) =>

    request<CourseAuditOverview>(`/audit/course/${courseId}/overview`),

  exportCourseAudit: (courseId: string) =>

    json<CourseAuditExport>(`/audit/course/${courseId}/report`, "POST"),

  // 任务 48u：这两个接口本身不要 key（登录前就要能调），返回体里的 api_key 由

  // auth.setApiKey 存进 localStorage。

  health: () => request<HealthPayload>("/health"),

  login: (data: { username: string; password: string }) =>

    json<{ username: string; api_key: string }>("/auth/login", "POST", data),

};

/** 任务 48u：健康检查带回两个模式开关（前端据此决定要不要登录页 / 演示模式）。 */

/** 任务 116/118：能力清单的一项（缺什么 / 影响什么 / 不受影响 / 怎么装）。 */
export type CapabilityItem = {

  key: string;
  label: string;
  available: boolean;
  probe?: string;
  affects?: string[];
  unaffected?: string[];
  install?: string;
  reason?: string | null;
  [extra: string]: unknown;
};

export type HealthPayload = {

  ok: boolean;

  app?: string;

  auth_enabled?: boolean;

  demo_readonly?: boolean;

  /** 任务 116/118：能力清单（缺 Edge / PowerPoint / yt-dlp / JS 运行时时给的是人话）。 */
  capabilities?: {
    items: CapabilityItem[];
    all_available: boolean;
    missing: string[];
    hint?: string | null;
  };

};
