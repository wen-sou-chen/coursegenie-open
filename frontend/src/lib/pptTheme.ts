/**
 * 任务 145：课程级 PPT 主题（`course.meta.ppt_theme`）的前端口径。
 *
 * 后端是唯一出处（`backend/app/services/ppt_theme_setting.py`）：
 * 优先级 = **调用方显式传的 theme（非空）> 课程 meta.ppt_theme > 后端配置 PPT_THEME > 内置默认**。
 * 本文件只做三件事：① 把后端给的「本课当前主题 + 来源」翻成人话；
 * ② 把「本课当前」那一项标进下拉；③ 记「改了要重新生成才生效」这句提示。
 *
 * ⚠️ `FALLBACK_THEMES`（PptPanel 的首屏兜底）**只是兜底**：后端 `/api/ppt/themes` 到位后一律
 * 以它为准；兜底表里**不含**「本课当前主题」（那是每门课各不相同的，写死必然错），所以后端
 * 没答复时下拉**不选第一项**、而是显示「读取中」。
 */

export type PptThemeOption = { id: string; name: string; description?: string };

/** 后端 `/api/ppt/themes?course_id=` 的 `course` 块（本课当前主题 + 来源）。 */
export type PptThemeCourse = {
  id: string;
  name: string;
  source: string;
  setting?: string | null;
  error?: string | null;
};

export type PptThemesPayload = {
  items?: PptThemeOption[];
  course?: PptThemeCourse | null;
};

/**
 * 来源 key → 人话。与后端 `ppt_theme_setting.SOURCE_LABELS` **逐字一致**
 * （门禁 `scripts/check-ppt-course-theme.ts` 会读两个文件逐条比对）。
 */
export const THEME_SOURCE_LABELS: Record<string, string> = {
  param: "本次调用指定",
  course: "本课设置",
  config: "后端配置",
  default: "内置默认",
};

/** 改主题的后果（与后端 `ppt_theme_setting.APPLY_NOTE` 同句；界面提示的唯一出处）。 */
export const THEME_APPLY_NOTE = "只改外观，不改内容；改了主题要重新生成课件才会生效";

/** 后端还没答复时的占位（**不猜**成某一套、也不显示第一个）。 */
export const THEME_LOADING_LINE = "课程主题读取中…";

/** 来源 key → 人话；未知 key 返回空串（**不猜**）。 */
export function themeSourceLabel(source?: string | null): string {
  return THEME_SOURCE_LABELS[String(source ?? "")] ?? "";
}

/** 本课当前主题 id（后端没答复 ⇒ 空串，**不拿第一个主题顶替**）。 */
export function courseThemeId(data?: PptThemesPayload | null): string {
  return String(data?.course?.id ?? "");
}

/** 「当前：深蓝商务（本课设置）」；后端没答复 / 没有课程 ⇒ 空串（界面不印半截话）。 */
export function currentThemeLine(data?: PptThemesPayload | null): string {
  const course = data?.course;
  if (!course?.id || !course?.name) return "";
  const label = themeSourceLabel(course.source);
  return label ? `当前：${course.name}（${label}）` : `当前：${course.name}`;
}

/** 下拉选项：把「本课当前主题」那一项标出来（**只标后端说的那一项**）。 */
export function themeSelectOptions(
  options: PptThemeOption[] | undefined,
  currentId?: string | null,
): { value: string; label: string }[] {
  const current = String(currentId ?? "");
  return (options ?? []).map((item) => ({
    value: item.id,
    label: item.id === current ? `${item.name}（本课当前）` : item.name,
  }));
}
