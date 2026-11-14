import "katex/dist/katex.min.css";
import type { ReactNode } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import { REHYPE_PLUGINS, REMARK_PLUGINS } from "../lib/markdownPlugins";
import { isDuplicateTable, parseTables } from "../lib/mdTables";
import { splitPreviewAnswers } from "../lib/previewAnswers";
import AnswerReveal from "./AnswerReveal";
import FlowDiagram from "./FlowDiagram";

const mdComponents: Components = {
  img: ({ node: _node, ...rest }) => (
    // 任务 79：讲义正文里内嵌的图（概念图 / 位图配图）也纳入"进入视口播一次"的入场动效
    <img {...rest} alt={rest.alt ?? ""} data-fig-enter="" style={{ maxWidth: "100%", borderRadius: 8 }} />
  ),
};

const DIAGRAM_REF = /!\[[^\]]*\]\(\s*diagram:\/\/([A-Za-z0-9_-]+)\s*\)/g;
const CHART_REF = /!\[[^\]]*\]\(\s*chart:\/\/([A-Za-z0-9_-]+)\s*\)/g;

/**
 * 渲染讲义/正文 Markdown：mermaid 代码块单独渲染为可交互流程图，
 * 其余按普通 Markdown 渲染；图片限宽防止撑破卡片。
 * diagram://{id} 是 Excalidraw 概念图引用：传入 diagramSrcs（id → /images/*.svg）
 * 时替换为可显示的 SVG 预览；无映射则隐藏，避免出现坏图。
 * chart://{id} 是数据图（自动生成的数据卡）的引用：chartSrcs 提供 SVG 预览图
 * （bar/pie/line/mindmap/timeline），chartTables 提供 Markdown 表格（table）。
 * 任务 64：数据卡的表格与正文里某张表同数据时跳过该卡（否则同一张表渲染两次），
 * 判定口径与后端 `app/services/md_tables.py` 同一套。
 */
export default function MdWithFlow({
  md,
  diagramSrcs,
  chartSrcs,
  chartTables,
  collapseAnswers,
}: {
  md: string;
  diagramSrcs?: Record<string, string>;
  chartSrcs?: Record<string, string>;
  chartTables?: Record<string, string>;
  /** 预览页开启：答案 / 解析 / 评分要点默认收起，编辑区与导出不传。 */
  collapseAnswers?: boolean;
}) {
  let text = md;
  if (diagramSrcs && Object.keys(diagramSrcs).length > 0) {
    text = text.replace(DIAGRAM_REF, (whole: string, id: string) => {
      const src = diagramSrcs[id];
      return src ? whole.replace(/diagram:\/\/[A-Za-z0-9_-]+/, src) : "";
    });
  } else {
    text = text.replace(DIAGRAM_REF, "");
  }
  const hasChartMap =
    (chartSrcs && Object.keys(chartSrcs).length > 0) ||
    (chartTables && Object.keys(chartTables).length > 0);
  if (hasChartMap) {
    // 只按**原始正文**里的表格判定，且只对「以表格形态渲染」的卡生效
    const bodyTables = parseTables(md);
    text = text.replace(CHART_REF, (whole: string, id: string) => {
      const src = chartSrcs?.[id];
      if (src) return whole.replace(/chart:\/\/[A-Za-z0-9_-]+/, src);
      const table = chartTables?.[id];
      if (!table) return "";
      if (isDuplicateTable(table, bodyTables)) {
        console.info(`正文已有同数据表格，跳过数据卡 chart://${id}（同一张表不再渲染两次）`);
        return "";
      }
      return `\n\n${table}\n\n`;
    });
  } else {
    text = text.replace(CHART_REF, "");
  }
  const blocks: ReactNode[] = [];
  const fence = /```mermaid[^\n]*\n([\s\S]*?)```/g;
  let match: RegExpExecArray | null;
  let lastIndex = 0;
  let key = 0;
  const renderMarkdown = (chunk: string, k: string) => (
    <ReactMarkdown
      key={k}
      components={mdComponents}
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={REHYPE_PLUGINS}
    >
      {chunk}
    </ReactMarkdown>
  );
  const pushMarkdown = (chunk: string) => {
    if (!collapseAnswers) {
      blocks.push(renderMarkdown(chunk, `md-${key++}`));
      return;
    }
    splitPreviewAnswers(chunk).forEach((block, index) => {
      if (block.kind === "answers") {
        blocks.push(<AnswerReveal key={`answer-${key}-${index}`} blocks={block.blocks} />);
      } else {
        blocks.push(renderMarkdown(block.markdown, `md-${key}-${index}`));
      }
    });
    key += 1;
  };
  while ((match = fence.exec(text))) {
    if (match.index > lastIndex) {
      pushMarkdown(text.slice(lastIndex, match.index));
    }
    blocks.push(<FlowDiagram key={`flow-${key++}`} code={match[1].trim()} />);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    pushMarkdown(text.slice(lastIndex));
  }
  return <div style={{ lineHeight: 1.8, fontSize: 14 }}>{blocks}</div>;
}
