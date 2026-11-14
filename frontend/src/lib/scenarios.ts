import type { Course, CourseScenario } from "../types";

/**
 * 任务 85：课程「使用场景」五选多 —— **界面唯一出处**。
 *
 * ⚠️ 这张表与后端 `backend/app/services/scenario.py` 的 `SCENARIOS` **必须逐字一致**
 * （key 集合 + 显示名 + 一句话说明 + 短标）；门禁 `scripts/check-scenario.ts` 会读两个文件逐条比对，
 * 改了一侧忘了另一侧 ⇒ 断言变红。提示词侧只有后端那份（含「场景要求」正文）。
 *
 * 场景**只改口气与要求**，不动判定口径；未选择 = 空 ⇒ 生成行为与从前逐字一致
 * （**不许把默认值偷偷补成 classroom**）。
 *
 * 任务 86：第 4 列是**短标**（徽标 / 交付说明汇总用）；文末是「生成时的口径」读数
 * （`meta.scenario_used` / `meta.prompt_version` / `meta.generated_at`）—— 老内容没有这些键
 * ⇒ 一律「通用(未标记)」，**不许猜、不许默认成某个场景**。
 */
export const SCENARIO_TABLE = [
  ["classroom", "线下课堂面授", '照念/讲解为主，要有板书与提问点', "课堂向"],
  ["live", "线上直播", '对着镜头讲，需要稿子和互动点', "直播向"],
  ["recorded", "录成视频课", '每小节能独立成段(≤6 分钟)，不依赖手势指着说', "录课向"],
  ["self_study", "学生自学", '书面完整句，不能靠"上面说过"', "自学向"],
  ["lms", "平台投放(SCORM)", '只影响出包形态，不影响口气', "出包向"],
] as const;

export type ScenarioKey = (typeof SCENARIO_TABLE)[number][0];

export type ScenarioOption = { key: ScenarioKey; label: string; hint: string; badge: string };

/** 固定顺序的五项（options / tooltip / 文案都从这里取）。 */
export const SCENARIOS: readonly ScenarioOption[] = SCENARIO_TABLE.map(
  ([key, label, hint, badge]) => ({ key, label, hint, badge }),
);

export const SCENARIO_KEYS: readonly ScenarioKey[] = SCENARIO_TABLE.map(([key]) => key);

/** 未选场景时界面上显示的那句话（任务 85 §B3）。 */
export const SCENARIO_UNSET = "未指定场景（按通用口径生成）";

export function scenarioLabel(key?: string | null): string {
  const hit = SCENARIO_TABLE.find(([value]) => value === key);
  return hit ? hit[1] : "";
}

/**
 * 归一化：只留合法 key、去重、按固定顺序排；`primary` 必须 ∈ `selected`。
 * **空 / 缺 → null（未选择），绝不补默认场景。**
 */
export function normalizeScenario(raw?: CourseScenario | null): CourseScenario | null {
  if (!raw || typeof raw !== "object") return null;
  const wanted = Array.isArray(raw.selected) ? raw.selected.map(String) : [];
  const selected = SCENARIO_KEYS.filter((key) => wanted.includes(key));
  if (!selected.length) return null;
  const primary = selected.includes(String(raw.primary) as ScenarioKey)
    ? String(raw.primary) : selected[0];
  const out: CourseScenario = { primary, selected };
  if (raw.updated_at) out.updated_at = String(raw.updated_at);
  return out;
}

/** 课程对象的场景（优先用后端透出的 `course.scenario`，老接口只给 meta 时兜底读 meta）。 */
export function scenarioOf(course?: Course | null): CourseScenario | null {
  if (!course) return null;
  const fromMeta = (course.meta ?? {}).scenario as CourseScenario | undefined;
  return normalizeScenario(course.scenario ?? fromMeta ?? null);
}

/** 任务 85 §B3：课程页 / 出包页那一行「本次按场景生成：…」；未选 = 「未指定场景（按通用口径生成）」。 */
export function scenarioLine(course?: Course | null): string {
  const meta = scenarioOf(course);
  if (!meta) return SCENARIO_UNSET;
  const picked = meta.selected.map((key) => scenarioLabel(key)).join(" + ");
  return `本次按场景生成：${picked}（主：${scenarioLabel(meta.primary)}）`;
}

/** 场景短标（徽标 / 交付说明汇总用）；未知 key 返回空串。 */
export function scenarioBadge(key?: string | null): string {
  const hit = SCENARIO_TABLE.find(([value]) => value === key);
  return hit ? hit[3] : "";
}

/* ===========================================================================
 * 任务 86：**生成时的口径**读数（内容条目 `meta` → 徽标 / 笔记 frontmatter / 说明汇总）
 *
 * 口径的唯一写入点是后端 `services/scenario.py`（生成时写 `meta.scenario_used` +
 * `meta.prompt_version`）；前端只读、只显示。**老内容没有这两个键 ⇒ 「通用(未标记)」**，
 * 既不猜成某个场景、也不写成「未知」—— 用户看到的必须与真实生成口径一致。
 * ========================================================================= */

/** 老内容没有口径标记时显示的徽标（与后端 `UNMARKED_BADGE` 逐字一致）。 */
export const SCENARIO_UNMARKED_BADGE = "通用(未标记)";

/** 从内容条目 `meta` 读回**生成时**的场景（没有 / 脏数据 ⇒ null）。 */
export function usedScenarioOf(meta?: Record<string, unknown> | null): CourseScenario | null {
  const raw = meta?.scenario_used;
  return normalizeScenario((raw ?? null) as CourseScenario | null);
}

/** 内容条目 `meta` 的徽标：有标记 ⇒ 主场景短标；**没有标记 ⇒ 「通用(未标记)」**。 */
export function scenarioBadgeOf(meta?: Record<string, unknown> | null): string {
  const used = usedScenarioOf(meta);
  return (used ? scenarioBadge(used.primary) : "") || SCENARIO_UNMARKED_BADGE;
}

/** 内容条目 `meta` 记的提示词版本（没有 ⇒ 空串；课件/大纲可能本来就没有）。 */
export function promptVersionOf(meta?: Record<string, unknown> | null): string {
  const raw = meta?.prompt_version;
  return raw ? String(raw) : "";
}

/** 生成时间（后端写的是带时区的 ISO 串；没有 ⇒ 空串，由调用方本地化）。 */
export function generatedAtOf(meta?: Record<string, unknown> | null): string {
  const raw = meta?.generated_at;
  return raw ? String(raw) : "";
}

/** 这一篇的口径是不是就是课程当前口径（用于「口径一致，无需重跑」提示）。 */
export function sameScenario(a?: CourseScenario | null, b?: CourseScenario | null): boolean {
  const left = normalizeScenario(a ?? null);
  const right = normalizeScenario(b ?? null);
  if (!left || !right) return !left && !right;
  return left.primary === right.primary
    && left.selected.join("|") === right.selected.join("|");
}

/** 界面 → `meta.scenario` 的写入块（清空时给 `{}`，后端即按「未选择」处理）。 */
export function scenarioPayload(primary?: string, selected?: string[]): CourseScenario {
  const picked = SCENARIO_KEYS.filter((key) => (selected ?? []).includes(key));
  return {
    primary: picked.includes((primary ?? "") as ScenarioKey) ? String(primary) : (picked[0] ?? ""),
    selected: [...picked],
    updated_at: new Date().toISOString(),
  };
}
