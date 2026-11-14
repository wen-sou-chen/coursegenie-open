import type { PlanNode } from "../types";

/** 章节点：key 里不含「.」（如 1、2、3）。 */
export function chapterNodes(nodes: PlanNode[]): PlanNode[] {
  return nodes.filter((n) => !n.key.includes("."));
}

/** 章节成员：章自身 + 其下所有以「章号.」开头的节点。 */
export function chapterMembers(nodes: PlanNode[], chapterKey: string): PlanNode[] {
  const prefix = `${chapterKey}.`;
  return nodes.filter((n) => n.key === chapterKey || n.key.startsWith(prefix));
}

/**
 * 章节下的知识点（叶子）：没有任何子节点的成员；章节只有自己时回退为成员。
 * 与后端 chapter_leaf_nodes 同口径——判定子节点必须用「前缀」，不能用「key + .」精确匹配，
 * 否则章节点会被误当成叶子（曾经导致「先生成一节样板」生成到章节点上）。
 */
export function leavesOf(nodes: PlanNode[], chapterKey: string): PlanNode[] {
  const members = chapterMembers(nodes, chapterKey);
  const leaves = members.filter((n) => !nodes.some((other) => other.key.startsWith(`${n.key}.`)));
  return leaves.length ? leaves : members;
}
