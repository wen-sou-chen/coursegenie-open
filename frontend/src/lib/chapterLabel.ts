/**
 * 章节编号展示的唯一口径（后端 app/services/deck_label.py 与之一字对齐）。
 *
 * 同一份大纲节点在界面上曾有三种写法（「1 发酵原理与微生物基础」、「第1章 发酵原理与微生物基础」
 * 与 DB 标题里的裸编号），用户看不出谁是谁。所有界面一律走这里：
 *
 *     章    ：第1章 发酵原理与微生物基础
 *     知识点：1.1 多菌共生体系与发酵定义
 *     整课  ：整课课件（笔记库文件名仍是 00-整课课件.pptx）
 *
 * 标题里若已自带「第N章」前缀（旧数据），先剥掉再拼，避免「第2章 第2章 …」。
 */
export const COURSE_DECK_LABEL = "整课课件";

const CHAPTER_PREFIX = /^\s*第\s*[0-9一二三四五六七八九十百]+\s*章[\s:：、.．-]*/;

/** 剥掉标题里自带的「第N章 / 第N章:」前缀。 */
export function stripChapterPrefix(title?: string | null): string {
  return String(title ?? "").replace(CHAPTER_PREFIX, "").trim();
}

/** 大纲节点 → 统一展示标签：章 = 「第N章 名」，知识点 = 「N.N 名」。 */
export function nodeLabel(
  key?: string | null,
  title?: string | null,
  type?: string | null,
): string {
  const keyText = String(key ?? "").trim();
  const isChapter = type === "chapter" || type === "section"
    || (!!keyText && !keyText.includes("."));
  if (isChapter) {
    const name = stripChapterPrefix(title) || keyText;
    return keyText ? `第${keyText}章 ${name}` : name;
  }
  return `${keyText} ${String(title ?? "").trim()}`.trim();
}

/** 章节点对象 → 「第1章 发酵原理与微生物基础」。 */
export function chapterLabel(
  node?: { key?: string | null; title?: string | null; type?: string | null } | null,
): string {
  return nodeLabel(node?.key, node?.title, node?.type ?? "chapter");
}

/** 课件 DB 标题口径：章课件 = 「第1章 名 · 课件」，整课 = 「课程名 · 整课课件」。 */
export function deckTitle(
  scope: string | null | undefined,
  courseTitle?: string | null,
  node?: { key?: string | null; title?: string | null; type?: string | null } | null,
  suffix = "",
): string {
  if (scope === "chapter" && node) return `${chapterLabel(node)} · 课件${suffix}`;
  return `${String(courseTitle ?? "").trim()} · ${COURSE_DECK_LABEL}${suffix}`;
}
