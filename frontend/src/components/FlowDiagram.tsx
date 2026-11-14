import { useEffect, useRef, useState } from "react";
import { Spin, Typography } from "antd";
import mermaid from "mermaid";

let initialized = false;

function ensureMermaid() {
  if (initialized) return;
  initialized = true;
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "default",
    fontFamily: '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", "Source Han Sans SC", sans-serif',
    flowchart: { useMaxWidth: true, htmlLabels: true },
  });
}

export default function FlowDiagram({ code }: { code: string }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<"loading" | "done" | "error">("loading");
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    ensureMermaid();
    const host = hostRef.current;
    if (!host) return;
    const source = code || "flowchart TD\n    A([等待内容]) --> B([生成中])";
    const pre = document.createElement("div");
    pre.className = "mermaid";
    pre.textContent = source;
    host.replaceChildren(pre);
    setState("loading");
    setError("");
    mermaid
      .run({ nodes: [pre] })
      .then(() => {
        if (alive) setState("done");
      })
      .catch((e: unknown) => {
        if (alive) {
          setState("error");
          setError(String(e instanceof Error ? e.message : e));
        }
      });
    return () => {
      alive = false;
    };
  }, [code]);

  return (
    <div style={{ minHeight: 48, position: "relative" }}>
      {state === "loading" && <Spin size="small" />}
      {state === "error" && (
        <div style={{ marginBottom: 8 }}>
          <Typography.Text type="warning" style={{ fontSize: 12 }}>
            流程图渲染失败（{String(error).slice(0, 120)}），下方保留源码供检查：
          </Typography.Text>
          <pre style={{ whiteSpace: "pre-wrap", background: "#f6f8fa", padding: 8, borderRadius: 6, fontSize: 12 }}>
            {code}
          </pre>
        </div>
      )}
      <div
        ref={hostRef}
        // 任务 76：点开放大要拿回源码（mermaid 的 svg 序列化成 `<img>` 后 HTML 标签会错位）
        data-figure-mermaid={code}
        style={{ maxWidth: "100%", overflowX: "auto", background: "#fff", borderRadius: 8, padding: 8 }}
      />
    </div>
  );
}
