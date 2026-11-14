/**
 * P5-2：把讲义正文里「某句缺来源标注」的话滚到可见处并临时高亮。
 *
 * 正文是 React 渲染出来的 Markdown，直接改 DOM 结构会和 React 打架；
 * 所以这里只做两件事：滚动 + 给所在块（<li>/<p>）加一个会淡出的高亮类。
 *
 * 匹配方式：忽略空白与 Markdown 强调符号（* ` _）后逐字比对，
 * 因为质检拿到的是 markdown 原文（带 **），而页面上已经渲染成了加粗。
 */
const IGNORED = new Set(["*", "`", "_"]);

function significantChars(text: string): string[] {
  const out: string[] = [];
  for (const ch of text) {
    if (IGNORED.has(ch) || /\s/.test(ch)) continue;
    out.push(ch);
  }
  return out;
}

export function jumpToText(container: HTMLElement | null, text: string): boolean {
  if (!container) return false;
  const needle = significantChars(text).join("");
  if (!needle) return false;

  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const haystack: string[] = [];
  const map: { node: Text; index: number }[] = [];
  let node = walker.nextNode() as Text | null;
  while (node) {
    const points = Array.from(node.data ?? "");
    for (let i = 0; i < points.length; i += 1) {
      const ch = points[i];
      if (IGNORED.has(ch) || /\s/.test(ch)) continue;
      haystack.push(ch);
      map.push({ node, index: i });
    }
    node = walker.nextNode() as Text | null;
  }
  const at = haystack.join("").indexOf(needle);
  if (at < 0) return false;

  container.querySelectorAll(".cg-jump-hit").forEach((el) => el.classList.remove("cg-jump-hit"));
  const anchor = map[at];
  const block = (anchor.node.parentElement?.closest("li, p, td, h1, h2, h3, h4") ??
    anchor.node.parentElement ??
    container) as HTMLElement;
  block.classList.add("cg-jump-hit");
  block.scrollIntoView({ block: "center", behavior: "smooth" });
  window.setTimeout(() => block.classList.remove("cg-jump-hit"), 2600);
  return true;
}

export default jumpToText;
