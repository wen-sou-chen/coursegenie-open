import type { CourseK12 } from "../types";

/** 任务 51：K-12 元数据的一句话描述（如「小学 二年级 · 部编版 上册 · 第一单元」）。 */
export function k12Label(k12?: CourseK12): string {
  if (!k12) return "";
  const parts: string[] = [];
  const stageGrade = [k12.stage, k12.grade].filter(Boolean).join(" ");
  if (stageGrade) parts.push(stageGrade);
  const book = [k12.textbook, k12.volume].filter(Boolean).join(" ");
  if (book) parts.push(book);
  if (k12.unit) parts.push(k12.unit);
  if (k12.class_hours) parts.push(`${k12.class_hours} 课时`);
  return parts.join(" · ");
}