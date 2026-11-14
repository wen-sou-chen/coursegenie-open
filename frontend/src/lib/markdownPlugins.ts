/**
 * 任务 53：Markdown 渲染插件（公式）。
 * 任务 62：补 `remark-gfm` —— react-markdown v9 默认只按 CommonMark 解析，
 * 管道表格属于 GFM 扩展，不显式加插件就会被当普通段落、在页面上显示成 `| a | b |` 源码。
 *
 * 正文公式一律 LaTeX（行内 `$…$`、独立 `$$…$$`）：
 * - `remark-math` 把 `$…$` 解析成 math 节点；
 * - `rehype-katex` 用 KaTeX 渲染成 `<span class="katex">`（含 MathML + HTML 两套输出）。
 *
 * 单独放一个 .ts（不写 JSX）是为了让 `scripts/check-math-render.ts` 能直接用 Node 跑同一套
 * 插件做断言 —— 前端渲染与断言**共用同一份配置**，不各写一套。
 * KaTeX 的字体随 `katex/dist/katex.min.css` 被 vite 打进包（CSS 里是相对路径），不依赖 CDN。
 */
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

export const REMARK_PLUGINS = [remarkGfm, remarkMath];
export const REHYPE_PLUGINS = [rehypeKatex];
