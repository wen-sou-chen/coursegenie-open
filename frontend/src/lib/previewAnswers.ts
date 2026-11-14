/** 预览页的答案分组：把连续的「答案 / 解析 / 评分要点」块收成一个可折叠区。 */

export type PreviewBlock =
  | { kind: "markdown"; markdown: string }
  | { kind: "answers"; blocks: string[] };

const ANSWER_HEAD = /^\s*\*\*(?:答案|解析|评分要点)\*\*\s*[：:]/;

/**
 * 按空行切块，连续以答案类标记开头的块归为一组。
 *
 * 题面等其它块原样、按原顺序返回；没有答案的正文只做切块，不会少块。
 */
export function splitPreviewAnswers(md: string): PreviewBlock[] {
  const source = String(md ?? "");
  if (!source.trim()) return [];

  const blocks: PreviewBlock[] = [];
  let answers: string[] | null = null;

  const flush = () => {
    if (!answers) return;
    blocks.push({ kind: "answers", blocks: answers });
    answers = null;
  };

  for (const chunk of source.split(/\n{2,}/)) {
    if (ANSWER_HEAD.test(chunk)) {
      answers ??= [];
      answers.push(chunk);
      continue;
    }
    flush();
    blocks.push({ kind: "markdown", markdown: chunk });
  }
  flush();
  return blocks;
}
