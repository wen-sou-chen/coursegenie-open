import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { REHYPE_PLUGINS, REMARK_PLUGINS } from "../lib/markdownPlugins";

/** 默认收起的答案区；按钮是原生 button，Enter / Space 可直接切换。 */
export default function AnswerReveal({ blocks }: { blocks: string[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ margin: "8px 0 12px" }}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        style={{
          cursor: "pointer", border: "1px solid #91caff", background: "#e6f4ff",
          color: "#0958d9", borderRadius: 6, padding: "2px 12px", fontSize: 12,
        }}
      >
        {open ? "收起答案" : "看答案"}
      </button>
      <div
        hidden={!open}
        data-testid="answer-reveal-body"
        style={{
          marginTop: 6, padding: "6px 10px", background: "#f6ffed",
          borderLeft: "3px solid #b7eb8f", borderRadius: 4,
        }}
      >
        {blocks.map((block, index) => (
          <ReactMarkdown
            key={index}
            remarkPlugins={REMARK_PLUGINS}
            rehypePlugins={REHYPE_PLUGINS}
          >
            {block}
          </ReactMarkdown>
        ))}
      </div>
    </div>
  );
}
