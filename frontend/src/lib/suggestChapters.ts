/**
 * 「章节数」建议值（P3-2）：按素材规模给一个**上限建议**，不再写死 4 章。
 *
 * 依据（够用就好、可解释）：① 每份素材至少摊 3 章（一份 PPT 通常就是一串并列要点，
 * 例如「方法一…方法五」）；② 每 ~1200 字再补 1 章（PPT 一页大约 100-200 字）。
 * 取两者较大值，夹在 4-12（与后端 PlanRequest.num_chapters 的 1-12 对齐）。
 * 这只是「上限建议」：不保证覆盖全部素材要点，生成后由大纲自检兜底提示。
 */
export const MIN_CHAPTERS = 4;
export const MAX_CHAPTERS = 12;
export const CHAPTERS_PER_FILE = 3;
export const CHARS_PER_CHAPTER = 1200;

export function suggestChapters(materials: { chars?: number }[] | undefined): number {
  const rows = materials ?? [];
  const chars = rows.reduce((sum, row) => sum + (Number(row.chars) || 0), 0);
  if (rows.length === 0 || chars <= 0) return MIN_CHAPTERS;
  const byFiles = rows.length * CHAPTERS_PER_FILE;
  const byChars = Math.ceil(chars / CHARS_PER_CHAPTER) + 1;
  return Math.min(MAX_CHAPTERS, Math.max(MIN_CHAPTERS, byFiles, byChars));
}
