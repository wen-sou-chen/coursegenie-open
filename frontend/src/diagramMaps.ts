import { useMemo } from "react";
import type { ContentRow } from "./types";

/** 正文 Markdown 里 diagram:// / chart:// 引用 → 可显示的 SVG / Markdown 表格。 */
export interface DiagramMaps {
  diagramSrcs: Record<string, string>;
  chartSrcs: Record<string, string>;
  chartTables: Record<string, string>;
}

/**
 * 把一批内容条目里的概念图（diagram）与数据图（chart_*）整理成 MdWithFlow 需要的映射。
 * 单一实现：WorkbenchStep / PreviewStep / ContentPanel 共用，避免各处渲染口径漂移。
 */
export function useDiagramMaps(rows: ContentRow[], mediaPrefix = ""): DiagramMaps {
  const toSrc = (rel: string) => (mediaPrefix ? `${mediaPrefix}/${rel}` : `/${rel}`);
  const diagramSrcs = useMemo(() => {
    const map: Record<string, string> = {};
    for (const r of rows) {
      if (r.kind !== "diagram") continue;
      const rel = String((r.body as { svg_preview?: string })?.svg_preview ?? "");
      if (rel) map[r.id] = toSrc(rel);
    }
    return map;
  }, [rows, mediaPrefix]);
  const chartSrcs = useMemo(() => {
    const map: Record<string, string> = {};
    for (const r of rows) {
      if (!r.kind.startsWith("chart_")) continue;
      const rel = String((r.body as { svg_preview?: string })?.svg_preview ?? "");
      if (rel) map[r.id] = toSrc(rel);
    }
    return map;
  }, [rows, mediaPrefix]);
  const chartTables = useMemo(() => {
    const map: Record<string, string> = {};
    for (const r of rows) {
      if (r.kind !== "chart_table") continue;
      const table = String((r.body as { markdown_table?: string })?.markdown_table ?? "").trim();
      if (table) map[r.id] = table;
    }
    return map;
  }, [rows, mediaPrefix]);
  return { diagramSrcs, chartSrcs, chartTables };
}
