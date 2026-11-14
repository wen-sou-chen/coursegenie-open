export interface Course {


  id: string;

  title: string;

  subject: string;

  audience: string;

  duration_minutes: number;

  status: string;

  meta: Record<string, unknown>;

  /** 任务 51：K-12 教材元数据（后端把 meta 里的散写/嵌套块归一化后透出）。 */
  k12?: CourseK12;

  /** 任务 85：使用场景（五选多 + 主场景）。**没选过就是空对象**（绝不补默认场景）。 */
  scenario?: CourseScenario;

  created_at: string;

  updated_at: string;

}



/** 任务 51：K-12 学段/教材元数据。字段全部可空，留空即按普通课程口径生成。 */
export interface CourseK12 {

  stage?: string;

  grade?: string;

  textbook?: string;

  volume?: string;

  unit?: string;

  class_hours?: number;

}



/**
 * 任务 85：课程「使用场景」。
 * `primary` 必须 ∈ `selected`；`selected` 为空 = 未选择（生成口径与从前逐字一致）。
 */
export interface CourseScenario {

  primary: string;

  selected: string[];

  updated_at?: string;

}


export interface PlanNode {

  id: string;

  key: string;

  type: string;

  title: string;

  objective: string;

  teaching_strategy: string;

  assessment: string;

  prerequisites: string[];

}



/** 大纲节点影响面（P3-3 删除/移动前的二次确认）。 */

export interface PlanNodeRef {

  id: string;

  key: string;

  title: string;

  type: string;

}



export interface PlanNodeImpact {

  node: PlanNodeRef;

  nodes: PlanNodeRef[];

  node_count: number;

  content_total: number;

  content_counts: Record<string, number>;

  content_items: { id: string; kind: string; title: string }[];

  quizzes: number;

  locked: number;

  notes: { bound: boolean; files: string[]; count: number; out_dir: string; note: string };

  key_changes: { id: string; title: string; from: string; to: string }[];

  key_change_count: number;

}



/** 增 / 删 / 移大纲节点后的返回（含重排后的完整节点列表）。 */

export interface PlanNodeChange {

  node: PlanNode;

  renumbered: Record<string, string>;

  impact?: PlanNodeImpact;

  nodes: PlanNode[];

  graph: GraphData;

  status: string;

  notice: string;

  deleted?: { id: string; key: string; title: string }[];

  removed_content?: number;

  removed_quizzes?: number;

}



export interface GraphData {

  nodes: { id: string; key: string; label: string; type: string }[];

  edges: { source: string; target: string; label?: string }[];

}



export interface Plan {

  outline_id: string;

  title: string;

  goal: string;

  nodes: PlanNode[];

  graph: GraphData;

  /** 本次大纲生成的 LLM 用量（P3-4：原先这段调用没有采集器、被静默丢弃）。 */

  usage?: UsageSummary | null;

  /** 生成后自检：章节数「请求 vs 实际」+ 素材要点覆盖（P3-2）。 */

  check?: PlanCheck;

  /** 任务 70 §B2：自检结论的生成时间（落库值）—— 刷新 / 换设备后界面上仍标注「上次自检」。 */

  check_generated_at?: string;

}



/** 大纲生成后的自检（POST /api/plan/generate 的 check 字段，P3-2）。 */

/** 任务 69 §B1.4：本次注入里单份素材的用量（course.meta.material_inject.used[i]）。 */

export interface MaterialInjectItem {

  id: string;

  /** 引用时用的素材名（AI 补充带「AI补充——」前缀）。 */

  title: string;

  /** 课程素材序号（与导出「NN-标题.md」一致）。 */

  index: number;

  /** 来源类型：空串 = 教师素材、ai = AI 补充资料。 */

  source: string;

  injected_chars: number;

  original_chars: number;

  paragraphs_total: number;

  paragraphs_injected: number;

}

/** 任务 69 §B1.3/§B1.4：本次素材注入的口径（用了 N/M 份、注入/原始字数、未参与清单）。 */

export interface MaterialInject {

  at: string;

  /** 生成场景：outline（大纲）/ lecture（讲义）。 */

  scope: string;

  /** 这次注入的总预算（字符）。 */

  budget: number;

  used: MaterialInjectItem[];

  /** 没参与本次注入的素材 + 原因（超限绝不静默）。 */

  unused: { id: string; title: string; original_chars: number; reason: string }[];

  injected_chars: number;

  original_chars: number;

  used_count: number;

  total_materials: number;

  /** 人话提示（界面上「生成自检」那一条）。 */

  notice: string;

}
export interface PlanCheck {

  requested_chapters: number;

  actual_chapters: number;

  chapter_match: boolean;

  chapter_notice: string;

  suggested_chapters: number;

  material_points_total: number;

  material_covered_count: number;

  material_uncovered: string[];

  material_uncovered_count: number;

  material_points: { point: string; covered: boolean; matched_by: string }[];

  /** 任务 70 §B3：语义等价判定是否开启（默认关；开启也只走本地词面相似度、不调 LLM）。 */

  semantic_enabled: boolean;

  /** 任务 70 §B3：按类型汇总的要点数（方法/步骤/编号/枚举/定义/概念/案例）。 */

  material_points_by_kind: { kind: string; label: string; total: number; covered_count: number }[];

  /** 任务 70 §B3 + 任务 75 §B3：未覆盖要点 + 每条的**可操作建议**（建议并入第 N 章「章名」/ 建议新增知识点《X》）。 */

  material_uncovered_detail: { point: string; kind: string; kind_label: string; suggestion: string }[];

  /** 任务 75 §B3：未覆盖明细里被过滤掉的句子碎片条数（后端如实给数，不静默丢弃）。 */

  filtered_out_count?: number;

  /** 任务 75 §B3：被过滤条目的样例（最多 5 条，供界面说明「过滤了什么」）。 */

  filtered_out_samples?: string[];

  /** 任务 75 §B3：过滤后真正展示在明细里的条数（= material_uncovered_count − filtered_out_count）。 */

  material_uncovered_shown_count?: number;

  material_notice: string;

  /** 任务 69 §B1.3：本次素材注入情况的人话提示（用了 N/M 份、注入/原始字数、未参与清单）。 */

  material_inject_notice: string;

  /** 任务 69 §B1.4：本次注入的落库明细（每份素材注入多少字 / 原始多少字）。 */

  material_inject: MaterialInject | null;

  action_hint: string;

}



/** 任务 70 §B1/§B2：自检结论的落库记录（course.meta.outline_check，GET /plan/{id}/check 返回）。
 *  带「素材指纹」—— 能看出这份结论是不是基于当前这批素材算出来的。 */

export interface PlanCheckStored {

  course_id: string;

  /** 有没有大纲（false 时 check 也是 null ⇒ 界面给明确空态）。 */

  has_outline: boolean;

  check: PlanCheck | null;

  check_generated_at: string;

  check_at: string;

  check_semantic_enabled: boolean;

  check_materials_fingerprint: { id: string; title: string; chars: number; source: string }[];

  check_materials_fingerprint_hash: string;

}

export interface ContentRow {

  id: string;

  node_id: string | null;

  node_key?: string;

  kind: string;

  title: string;

  body: Record<string, unknown>;

  meta: Record<string, unknown>;

}



export type ChartType = "bar" | "line" | "pie" | "mindmap" | "timeline" | "table" | "quadrant";



export interface PptSlide {

  index: number;

  title: string;

  bullets: string[];

  notes_excerpt: string;

  image?: string;

  image_url?: string;

}


export interface PptPreview {

  ok: boolean;

  enabled: boolean;

  reason?: string | null;

  fallback?: string | null;

  url?: string | null;

  slides: PptSlide[];

}


export interface PptFigureFailure {

  node_key: string;

  figure: string;

  reason: string;

}


export interface PptLayoutNote {

  slide: number;

  layout?: string;

  kind?: string;

  reason: string;

}


export interface PptRow {

  id: string;

  title: string;

  body: { pptx?: string; pdf?: string; html?: string; engine?: string; source?: string };

  meta: Record<string, unknown>;

  urls?: { pptx?: string; pdf?: string; html?: string };

  slides?: PptSlide[];

  slides_error?: string | null;

  images_error?: string | null;

  figure_count?: number | null;

  figure_failures?: PptFigureFailure[] | null;

  slides_per_point?: number | null;

  theme?: string | null;

  theme_name?: string | null;

  theme_error?: string | null;

  layouts?: string[];

  layout_notes?: PptLayoutNote[] | null;

}



export interface PptDeck extends PptRow {
  scope: "course" | "chapter";
  chapter_node_id?: string | null;
  chapter_key?: string | null;
  /** 章节点 id（后端补齐：老数据按 meta.chapter_key 匹配大纲节点得到，只读兜底）。 */
  node_id?: string | null;
  /** 与 lib/chapterLabel.ts 同口径的展示标签：「第1章 发酵原理与微生物基础」/「整课课件」。 */
  label?: string | null;
  /** 节点标签：章 = 「第N章 名」，知识点 = 「N.N 名」。 */
  sub_label?: string | null;
  chapter_title?: string | null;
  engine?: string;
  slide_count?: number | null;
  generated_at?: string | null;
  engine_error?: string | null;
}

export interface PptGrouped {
  items: PptDeck[];
  course: PptDeck[];
  chapters: PptDeck[];
}

export interface ConceptSource {
  kind: string;
  ref: string;
  quote?: string;
}

export interface ConceptRow {
  id: string;
  concept_id: string;
  name: string;
  aliases: string[];
  chapters: string[];
  edited: boolean;
  body: {
    definition?: string;
    points?: string[];
    relations?: Record<string, string[]>;
    pitfalls?: string[];
    sources?: ConceptSource[];
  };
}

export interface ConceptLinkIssue {
  concept: string;
  section?: string;
  target: string;
}

export interface ConceptStats {
  count: number;
  linked_relations: number;
  broken_links: number;
  unresolved_relations: number;
  broken: ConceptLinkIssue[];
  unresolved: ConceptLinkIssue[];
}

export interface ConceptList {
  course_id: string;
  count: number;
  concepts: ConceptRow[];
  chapters: Record<string, string[]>;
  stats: ConceptStats;
}

export interface VersionRow {

  id: string;

  version_no: number;

  note: string;

  created_at: string;

}



export interface Analytics {

  course_id: string;

  status: string;

  outline_nodes: number;

  content_items: number;

  node_coverage: number;

  words_total: number;

  /** 任务 81 §A1：去空白后的字符数（含标点）—— 与字数并列，便于对账口径。 */
  chars_total: number;

  quizzes: number;

  quiz_by_difficulty: Record<string, number>;

  low_score_alert: string;

  high_exit_alert: string;

}



/** 任务 54：图片素材的 OCR 元信息（本地 rapidocr 识别，数据不出本机）。 */
export interface MaterialOcr {

  /** 识别引擎（rapidocr-onnxruntime）。 */
  engine?: string;

  version?: string;

  /** 识别出的文字行数（= 素材段落数）。 */
  lines?: number;

  /** 平均置信度（0-1；低于 0.75 建议人工抽查）。 */
  confidence?: number | null;

  /** 没有文字时的原因（未装组件 / 识别失败 / 图里没有文字）。 */
  skipped?: string;

}

/** 任务 102：音视频素材的本地语音转写元信息（faster-whisper，本机跑、不上传云端）。 */
export interface MaterialAsr {

  /** 转写引擎（faster-whisper）。 */
  engine?: string;

  version?: string;

  /** 模型名（默认 small；可用 `.env` 的 ASR_MODEL（推荐）或环境变量配成 medium / large-v3）。 */
  model?: string;

  /** 媒体时长（秒）。 */
  duration_sec?: number | null;

  /** 转写分成了几段（每段行首带 [mm:ss]，也就是素材页的 [N] 段）。 */
  segments?: number;

  /** 识别出的语种（en / zh …；引擎没识别出来就是空串）。 */
  language?: string;

  /** 平均对数概率（越接近 0 越有把握；低于 -1 建议人工抽查）。 */
  avg_logprob?: number | null;

  /** pending（已入队）/ running（转写中）/ done（完成）/ failed（跳过或失败，原因见 skipped_reason）。 */
  status?: string;

  /** 跳过 / 失败的原因（人话：没装组件、没有音轨、文件损坏…）。 */
  skipped_reason?: string;

  /** 任务 103：转写时给的引导语（中文 = 简体引导语，非中文 = 空串）；落库如实记录，便于回溯。 */
  initial_prompt?: string;

}


/** 任务 118：从链接导入（YouTube）时，**一条视频**的抓取结果（用了哪条路 / 语言 / 段数）。 */
export interface MaterialLinkItem {

  id?: string;
  title?: string;
  duration_sec?: number | null;
  /** subtitle（抓的字幕）/ asr（下音频走本机转写）/ none（这条没抓成）。 */
  path?: string;
  language?: string;
  segments?: number;
}

/**
 * 任务 118：「从链接导入」的元信息 —— 素材的第三种来源（上传文件 / 粘贴文本 / **从链接导入**）。
 *
 * 硬事实都记在这里：用了哪条路（字幕 or 本机转写）、语言、时长、来源 URL、抓取时间、
 * 限流等待、失败原因。cookie 的**路径与值都不落库**，只有「带没带 cookie / 域对不对」。
 */
export interface MaterialLink {

  /** 来源（目前只有 youtube）。 */
  source?: string;
  /** 规范化后的来源 URL（**重新导入**用它，不必再贴一次链接）。 */
  url?: string;
  /** video（单个视频）/ playlist（播放列表）。 */
  kind?: string;
  video_id?: string;
  playlist_id?: string;
  /** pending（排队中）/ done / failed。 */
  status?: string;
  /** 这次用了哪条路：subtitle（抓字幕）/ asr（下音频本机转写）/ mixed / none。 */
  path?: string;
  language?: string;
  duration_sec?: number | null;
  /** 抓成几条 / 一共几条 / 本次没导的条数（播放列表默认只导前若干条）。 */
  videos?: number;
  videos_all?: number;
  items_skipped?: number;
  items?: MaterialLinkItem[];
  failed?: { title?: string; reason?: string }[];
  /** 抓取时间（带时区）。 */
  fetched_at?: string;
  /** 有没有带 cookies.txt（**路径与值都不落库、不进日志**）。 */
  cookies?: boolean;
  cookies_youtube_domain?: boolean;
  /** 限流记录：请求数 + 每次实际等待的秒数（429 退避一目了然）。 */
  rate?: { requests?: number; waits?: number[] };
  /** 失败 / 部分失败的人话原因（可直接贴给人看）。 */
  skipped_reason?: string;
  error_code?: string;
  notes?: string[];
}

export interface MaterialRow {

  id: string;

  title: string;

  kind: string;

  file_rel: string;

  chars: number;

  /** 解析出的原始字数（存库会截断，超限时 truncated=true）。 */

  raw_chars?: number;

  truncated?: boolean;

  /** 文件型素材的页数（pptx = 幻灯片数、pdf = 页数）；纯文本为 null。 */

  pages?: number | null;

  /** 任务 69 §B1.5：入库时剥离的对话外壳 / 页眉页脚行数（0 = 没剥；剥不了的只提示、不硬改）。 */

  cleaned_lines?: number;

  /** 任务 69 §B1.5：命中的清理规则名（素材行 tooltip 用）。 */

  cleaned_rules?: string[];

  /**
   * 任务 72 §B1：库里的正文**有没有真的改过**（分三态文案靠它，别再只看 cleaned_lines）。
   *
   * true  = 入库时已剥 / 老师点过「一键清理」⇒「已清理 N 行（库内正文已更新）」；
   * false = 老数据只记了行数、库内保留原文 ⇒「出库时会剥离 N 行（库内保留原文）」。
   */

  shell_stripped?: boolean;

  /** 任务 69 §B1.5.1：来源类型 —— upload=人工上传 / paste=粘贴文本(疑似 AI 产物) / ai=AI 补充。 */

  origin?: string;

  /** 任务 69 §B1.5.1：来源类型的中文标签（人工上传 / 粘贴文本(疑似 AI 产物) / AI 补充）。 */

  origin_label?: string;

  /** 任务 69 §B1.5.1：是否属于用户口径里「必须干净」的两类（AI 补充 + 粘贴件）。 */

  must_clean?: boolean;

  /** 任务 69 §B1.5.1：上传原件**检测到但没清理**的外壳行数（只提示 + 可选一键清理）。 */

  detected_lines?: number;

  /** 任务 69 §B1.5.1：检测命中的规则名（剥不了时带原因，界面只提示不硬改）。 */

  detected_rules?: string[];

  ext: string;

  original: string;

  preview: string;

  source?: string;

  /** 任务 69 §B1.5.3：AI 补充素材的「有来处」条目（claim=事实性表述 / material=来源素材名 / para=段号）。 */

  grounded?: { claim: string; material: string; para: number; ai?: boolean }[];

  /** 任务 69 §B1.5.3：有来处的条数。 */

  grounded_count?: number;

  /** 任务 69 §B1.5.3：模型自己都说「没有来处」的表述（(无依据·模型补充)）—— 界面标红。 */

  ungrounded?: string[];

  /** 任务 69 §B1.5.3：无来处的条数。 */

  ungrounded_count?: number;

  /** 任务 54：图片素材的 OCR 结果（非图片素材为 null）。 */
  ocr?: MaterialOcr | null;

  /** 任务 102：音视频素材的转写结果（其它素材为 null）。 */
  asr?: MaterialAsr | null;
  /** 任务 118：从链接导入的元信息（不是链接素材就是 null）。 */
  link?: MaterialLink | null;

}

/** 素材解析全文（分页，P2-1：文件型素材也能读）。 */

export interface MaterialFull {

  id: string;

  title: string;

  kind: string;

  ext: string;

  original: string;

  chars: number;

  raw_chars: number;

  truncated: boolean;

  pages: number | null;

  /** 任务 54：图片素材的 OCR 结果（非图片素材为 null）。 */
  ocr?: MaterialOcr | null;

  /** 任务 102：音视频素材的转写结果（其它素材为 null）。 */
  asr?: MaterialAsr | null;

  offset: number;

  limit: number;

  total_chars: number;

  text: string;

  has_more: boolean;

  next_offset: number | null;

  /** 任务 69 §B1.5.3：numbered=true 时按段落返回（offset/limit 这时是段号/段数）。 */

  numbered?: boolean;

  /** 任务 69 §B1.5.3：总段数（numbered 模式）。 */

  total_paragraphs?: number;

  /** 任务 69 §B1.5.3：段落窗口 [{n, text}]（n = 注入与素材页的 `[N]`）。 */

  paragraphs?: { n: number; text: string }[];

}



export interface VaultState {

  configured: boolean;

  vault_root: string | null;

  exists: boolean;

}



export interface KbInfo {

  course_id: string;

  vault: VaultState;

  kb: { vault_root?: string; output_rel?: string };

}



export interface PreservedEntry {

  file: string;

  reason: string;

}



export interface DiagramFailure {

  id: string;

  slot?: string;

  stage?: string;

  reason?: string;

  at?: string;

}



export interface ConsistencyNode {

  node_id: string;

  node_key: string;

  title: string;

  note: string | null;

  part?: "lecture" | "quiz" | "asset" | "index" | "course-level" | "material";

  status: "consistent" | "mismatch" | "missing";

  fingerprint?: "ok" | "absent" | "broken" | "unreadable";

  refs?: { note: Record<string, number>; expected: Record<string, number> };

  diff: string[];

}



export interface ConsistencyReport {

  course_id: string;

  ok: boolean;

  reason: string | null;

  vault_root: string | null;

  output_rel: string | null;

  checked: number;

  consistent: number;

  inconsistent: number;

  nodes: ConsistencyNode[];

}



export interface KbExportResult {

  vault_root: string;

  output_rel: string;

  files: string[];

  preserved: PreservedEntry[];

  charts: string[];

  warnings?: string[];

}

export interface StyleAnchor {

  source_item_id?: string;

  source_node_id?: string | null;

  source_node_key: string;

  source_title: string;

  tone: string;

  depth: string;

  figure_style: string;

  quiz_difficulty: string;

  terminology: string[];

  created_at?: string;

}
/** 任务 22：单项质检结果（key 见后端 lecture_qc.CHECK_KEYS）。 */

export interface QcCheck {

  key: string;

  level: "ok" | "warn" | "fail" | "skipped";

  detail: string;

  issues?: { alias: string; standard: string; context?: string }[];

  /** P5-2：缺来源标注的具体句子（含上下文与行号），前端可跳到正文。 */

  sentences?: { text: string; context: string; line?: number; section?: string }[];

  /** 任务 52：本档位按「提示」处理的项（判据没放宽，只是不计入扣分）。 */
  downgraded?: boolean;

  /** 任务 52：降级前的原始结论（人话解释「这条本来要说的是什么」）。 */
  hint?: string;
}



export interface QcMetrics {

  chars: number;

  sections: number;

  points: number;

  examples: number;

  figures: number;

  tables: number;

  quizzes: number;

  sources: number;

  links: number;

}



/** 任务 52：质检档位（按学段/学科选判据口径；判据本身一律没放宽）。 */
export interface QcProfile {
  key: string;
  label: string;
  stage: string;
  subject: string;
  actionable_tier: "required" | "hint";
  objective_mode: "coverage" | "lesson";
  note: string;
}

export interface LectureQc {

  version: number;

  score: number;

  checks: QcCheck[];

  metrics: QcMetrics;

  /** P5-1：本次未执行的检查项（概念库为空时术语比对不计入分母）。 */

  skipped_count?: number;

  skipped_keys?: string[];

  executed_checks?: number;

  total_checks?: number;

  /** 有未执行项时的人话说明（分数已按已执行项归一）。 */

  note?: string;

  generated_at?: string;

  /** 任务 52：本档位按「提示」处理的项（如小学语文的 actionable）。 */
  downgraded_keys?: string[];

  /** 任务 52：本次质检所用的档位（老师据此理解「这条为什么不算问题」）。 */
  profile?: QcProfile;
}



/** 来源锚定：一条 (来源: 素材名 #段落号) 的解析结果。 */

export interface SourceAnchor {

  node_key: string;

  section: string;

  material_id: string | null;

  material_name: string;

  quote: string;

  paragraph?: number;

  material_quote?: string;

}



export interface LectureQcPayload {

  item_id: string;

  node_id: string | null;

  node_key?: string | null;

  title: string;

  locked: boolean;

  sources: SourceAnchor[];

  issues: QcCheck[];

  qc: LectureQc;

}



export interface CourseQcItem {

  item_id: string;

  node_id: string | null;

  node_key: string;

  title: string;

  score: number;

  metrics: QcMetrics;

  issues: { key: string; level: string; detail: string }[];

  /** 任务 52：本档位按「提示」处理的项（判据没放宽，只是不计入扣分）。 */
  downgraded?: { key: string; hint: string }[];
  failed: { key: string; detail: string }[];

  warned: { key: string; detail: string }[];

  locked: boolean;

  /** P5-1：这篇有几项没执行（分数已按已执行项归一）。 */

  skipped_count?: number;

  skipped_keys?: string[];

  executed_checks?: number;

  total_checks?: number;

}



/** 任务 54：OCR 的素材级提示（低置信度 / 未装组件），不进 10 项判据。 */
export interface MaterialNotice {

  material_id: string;

  title: string;

  level: string;

  confidence?: number | null;

  reason: string;

}


/** 整课质量报告（POST /api/lecture/course/{id}/qc）。 */

export interface CourseQcReport {

  course_id: string;

  course_title: string;

  checked: number;

  average_score: number;

  min_score: number;

  max_score: number;

  checks: { key: string; ok: number; warn: number; fail: number; skipped: number }[];

  failure_distribution: { key: string; warn: number; fail: number; failed: number; skipped: number }[];

  /** P5-1：整课有多少篇存在「未执行」项（通常是概念库为空）。 */

  skipped_items: number;

  skipped_items_notice: string;

  /** 任务 54：OCR 素材级提示（低置信度 / 未装组件），不属于 10 项判据。 */
  material_notices?: MaterialNotice[];

  /** 任务 52：当前档位 + 按提示处理的说明（“这条为什么不算问题”）。 */
  profile?: QcProfile;
  downgraded_items?: number;
  downgraded_notice?: string;
  items: CourseQcItem[];

  worst: CourseQcItem[];

  generated_at: string;

}
// ---------------------------------------------------------------------------
// 任务 24：特色三件套（知识图谱 / 风格库 / 课程资产体检报告）
// ---------------------------------------------------------------------------

/** 概念图谱节点（GET /api/concept/course/{id}/graph）。 */
export interface ConceptGraphNode {
  id: string;
  name: string;
  chapter: string;
}

export interface ConceptGraphStats {
  nodes: number;
  edges: number;
  isolated: number;
  dropped: number;
}

export interface ConceptGraphDropped {
  from: string;
  kind: string;
  target: string;
}

export interface CanvasNode {
  id: string;
  type: string;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  color?: string;
}

export interface CanvasEdge {
  id: string;
  fromNode: string;
  toNode: string;
  label: string;
  fromSide?: string;
  toSide?: string;
}

/** 一键补抽结果（POST /api/concept/course/{id}/supplement，P6-1）。 */

export interface ConceptSupplementResult {

  ok: boolean;

  model: string;

  requested: string[];

  concepts: number;

  names: string[];

  missing: string[];

  filtered_count: number;

  created: number;

  updated: number;

  kept_edited: number;

  removed: number;

}

export interface ConceptGraph {
  course_id: string;
  course_title: string;
  format: "mermaid" | "canvas";
  stats: ConceptGraphStats;
  isolated: string[];
  dropped_relations: ConceptGraphDropped[];
  chapters: { key: string; title: string }[];
  nodes: ConceptGraphNode[];
  files: string[];
  mermaid?: string;
  text?: string;
  canvas?: { nodes: CanvasNode[]; edges: CanvasEdge[] };
}

export interface ConceptGraphExport {
  ok: boolean;
  vault_path: string;
  output_rel: string;
  files: string[];
  preserved: PreservedEntry[];
  stats: ConceptGraphStats;
  isolated: string[];
  dropped_relations: ConceptGraphDropped[];
  chapters: { key: string; title: string }[];
}

/** .excalidrawlib 风格库（GET /api/kb/course/{id}/style-library）。 */
export interface StyleLibraryItem {
  id: string;
  name: string;
  created: number;
  elements: Record<string, unknown>[];
}

export interface StyleLibraryFile {
  type: string;
  version: number;
  libraryItems: StyleLibraryItem[];
}

export interface StyleLibraryStats {
  items: number;
  elements: number;
  groups: Record<string, number>;
  theme: string;
  theme_name: string;
  theme_error?: string | null;
  names: string[];
}

export interface StyleLibraryPreview {
  course_id: string;
  file: string;
  stats: StyleLibraryStats;
  library: StyleLibraryFile;
}

export interface StyleLibraryExportResult {
  ok: boolean;
  vault_path: string;
  output_rel: string;
  file: string;
  files: string[];
  preserved: PreservedEntry[];
  stats: StyleLibraryStats;
}

/** 课程资产体检报告（GET/POST /api/audit/course/{id}/report）。 */
export interface AuditAction {
  level: "ok" | "warn" | "fail";
  text: string;
  action: string;
}

export interface AuditConclusion {
  score: number;
  level: string;
  badge: "ok" | "warn" | "fail";
  deductions: { reason: string; value: number; cut: number }[];
}

export interface AuditCoverage {
  chapters: number;
  points: number;
  lectures: number;
  concepts: number;
  diagrams: number;
  charts: number;
  quizzes: number;
  ppt: number;
  lecture_coverage: number;
  missing_lectures: string[];
  missing_lectures_count: number;
}

export interface AuditLinks {
  ok: boolean;
  reason?: string | null;
  output_rel?: string | null;
  links: number;
  wiki_links: number;
  file_links: number;
  broken: number;
  broken_links: { file: string; target: string; kind: string }[];
}

export interface AuditExportHealth {
  ok: boolean;
  reason?: string | null;
  files: number;
  notes: number;
  charts: number;
  assets: number;
  concept_pages: number;
  ppt: number;
  orphan_count: number;
  orphans: string[];
  preserved_count: number;
  preserved: PreservedEntry[];
  decks: number;
  deck_missing: string[];
}

export interface CourseAudit {
  course_id: string;
  course_title: string;
  conclusion: AuditConclusion;
  coverage: AuditCoverage;
  consistency: ConsistencyReport;
  links: AuditLinks;
  quality: CourseQcReport;
  usage: CourseUsage;
  export_health: AuditExportHealth;
  actions: AuditAction[];
  summary: string;
  markdown: string;
}

/**
 * 任务 48z：体检「首屏轻量概览」——完整报告里**不需要整库比对**的那几段
 * （覆盖度 / 链接健康 / 质检 / 用量 / 导出健康），实测 0.29 秒出图；
 * 一致性、结论、建议动作留空，由完整报告补齐。
 */
export interface CourseAuditOverview {
  course_id: string;
  course_title: string;
  partial: true;
  deferred: string[];
  coverage: AuditCoverage;
  links: AuditLinks;
  quality: CourseQcReport;
  usage: CourseUsage;
  export_health: AuditExportHealth;
  consistency: null;
  conclusion: null;
  actions: AuditAction[];
  summary: string;
  markdown: string;
}

export interface CourseAuditExport extends CourseAudit {
  written: boolean;
  file: string;
  vault_path: string;
  output_rel: string;
  files: string[];
  preserved: PreservedEntry[];
}



/** 一次生成的用量汇总（讲义 meta.usage / 任务 result.usage 同构；任务级多 per_node）。 */

export interface UsageSummary {

  llm_calls: number;

  prompt_tokens: number;

  completion_tokens: number;

  total_tokens: number;

  cost_cny: number;

  duration_ms: number;

  models?: string[];

  unpriced_models?: string[];

  estimated_calls?: number;

  retried_calls?: number;

  by_purpose?: Record<string, UsagePurposeBucket>;

  note?: string;

  updated_at?: string;

  per_node?: UsageNodeRow[];

  item_count?: number;

}



export interface UsagePurposeBucket {

  calls: number;

  prompt_tokens: number;

  completion_tokens: number;

  total_tokens: number;

  cost_cny: number;

  latency_ms: number;

}



/** 任务级用量里每个知识点一行（result.usage.per_node）。 */

export interface UsageNodeRow {

  node_key: string;

  item_id: string | null;

  llm_calls: number;

  tokens: number;

  cost_cny: number;

  ms: number;

}



/** 课程级用量报告里的一篇内容（GET /api/analytics/{course_id}/usage）。 */

export interface CourseUsageItem {

  item_id: string;

  node_id: string | null;

  node_key: string;

  kind: string;

  title: string;

  locked: boolean;

  llm_calls: number;

  prompt_tokens: number;

  completion_tokens: number;

  total_tokens: number;

  cost_cny: number;

  duration_ms: number;

  by_purpose: Record<string, UsagePurposeBucket>;

  unpriced_models: string[];

  updated_at?: string;

}



export interface CourseUsageByKind {

  kind: string;

  items: number;

  llm_calls: number;

  prompt_tokens: number;

  completion_tokens: number;

  total_tokens: number;

  cost_cny: number;

  duration_ms: number;

}



/** 整课用量与成本（任务 23-A）。 */

export interface CourseUsageAuxRow {

  purpose: string;

  kind: string;

  title: string;

  runs: number;

  llm_calls: number;

  total_tokens: number;

  cost_cny: number;

  first_at?: string | null;

  last_at?: string | null;

}



/**
 * P9-4：辅助用量的历史账本汇总。
 *
 * 任务 48x 修正：后端返回的是**对象**（逐项在 `by_purpose` 里），
 * 此前 types.ts 把它声明成了数组，把 tsc 骗过 ⇒ 面板一打开就
 * `data.aux_history.map is not a function`，整页白屏。
 */

export interface CourseUsageAuxHistory {

  runs: number;

  llm_calls: number;

  total_tokens: number;

  cost_cny: number;

  by_purpose: CourseUsageAuxRow[];

  estimated: boolean;

}



/** 整课用量与成本（任务 23-A）。 */

export interface CourseUsage {

  course_id: string;

  course_title: string;

  items_measured: number;

  lectures: number;

  llm_calls: number;

  prompt_tokens: number;

  completion_tokens: number;

  total_tokens: number;

  cost_cny: number;

  avg_cost_cny: number;

  duration_ms: number;

  avg_duration_ms: number;

  avg_lecture_duration_ms: number;

  priced: boolean;

  unpriced_models: string[];

  /** P9-4：辅助用量（大纲/概念抽取）的历史账本（含被重跑覆盖的版本）。 */

  aux_history: CourseUsageAuxHistory;

  /** 历史累计（跑次 / 费用 / tokens）；老课程没账本时取当前版本下限。 */

  history_aux_runs: number;

  history_aux_cost_cny: number;

  history_aux_tokens: number;

  /** true = 历史账本缺失，下面的数字只是下限（至少 N 次）。 */

  history_aux_estimated: boolean;

  by_kind: CourseUsageByKind[];

  top_items: CourseUsageItem[];

  items: CourseUsageItem[];

  note?: string;

  generated_at: string;

}

/** 大纲历史版本（P3-1）：每版的生成参数、节点数与用量。 */

export interface PlanVersion {

  id: string;

  at: string;

  reason: string;

  title: string;

  goal: string;

  params: Record<string, unknown>;

  usage?: PlanUsageSummary | null;

  nodes: number;

}



/** 一次辅助生成（大纲 / 概念抽取）的用量摘要。 */

export interface PlanUsageSummary {

  llm_calls?: number;

  total_tokens?: number;

  cost_cny?: number;

  duration_ms?: number;

  updated_at?: string;

}



export interface PlanHistory {

  course_id: string;

  current: { nodes: number; params: Record<string, unknown>; status: string };

  versions: PlanVersion[];

}



/** 历史版本与当前大纲的节点级差异。 */

export interface PlanDiff {

  added: { key: string; title: string }[];

  removed: { key: string; title: string }[];

  renamed: { key: string; from: string; to: string }[];

  unchanged: number;

}



/** 回滚影响面（P3-1）：现版大纲上绑定的内容 / 习题 / 笔记文件。 */

export interface PlanRestoreImpact {

  nodes: number;

  content_total: number;

  content_by_kind: Record<string, number>;

  quizzes: number;

  notes: { count: number; sample: string[]; note: string };

  notice: string;

}



export interface PlanVersionDetail {

  course_id: string;

  id: string;

  at: string;

  reason: string;

  title: string;

  goal: string;

  params: Record<string, unknown>;

  usage?: PlanUsageSummary | null;

  nodes: PlanNode[];

  diff: PlanDiff;

  impact: PlanRestoreImpact;

}



export interface PlanRestoreResult {

  ok: boolean;

  restored: string;

  outline_id: string;

  nodes: number;

  status: string;

}
