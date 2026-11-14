/**
 * 图表配色唯一口径（后端 app/services/chart_data.py 与之一字对齐）。
 *
 * 真因（任务 27）：库里 14/14 张图表的 `datasets[*].backgroundColor` 是空的，chart.js 的
 * 默认色是 `rgba(0,0,0,0.1)` 浅灰 —— 于是「数据图表点开全是灰色」，而 PPT 原生图表和概念图
 * 是有主题色的。这里按当前 PPT 主题的 6 色色板**按 dataset 序号**兜底上色：
 *
 * - 后端 `/api/ppt/themes` 的 `active.colors`（与概念图 / PPT 原生图表同一份色板）；
 * - 取不到（后端没起 / 老版本）时用 `DEFAULT_CHART_COLORS`（就是默认主题 clean 的 6 色）。
 *
 * 折线用半透明填充（同色系、不抢眼），柱状 / 饼图用实色，与后端保持一致。
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

/** 默认主题 clean 的 6 色（后端 THEMES.clean.palette.colors 同值，兜底用）。 */
export const DEFAULT_CHART_COLORS = ["4C8DF6", "F59E0B", "10B981", "8B5CF6", "EF4444", "06B6D4"];

/** 折线半透明填充的透明度（后端 chart_data.LINE_FILL_ALPHA 同值）。 */
export const LINE_FILL_ALPHA = 0.35;

/** 当前主题色板：优先后端 active 色板，取不到用内置默认 6 色。 */
export function useChartPalette(): string[] {
  const { data } = useQuery({
    queryKey: ["ppt-themes"],
    queryFn: () => api.pptThemes(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const colors = data?.active?.colors;
  return colors && colors.length ? colors : DEFAULT_CHART_COLORS;
}

/** 第 index 个数据系列的颜色（循环取色，超出 6 个系列也能上色）。 */
export function paletteColor(colors: string[], index: number): string {
  const list = colors.length ? colors : DEFAULT_CHART_COLORS;
  return list[((index % list.length) + list.length) % list.length];
}

/** 第 index 个数据系列的实色（`#RRGGBB`）。 */
export function paletteHex(colors: string[], index: number): string {
  const color = paletteColor(colors, index);
  return color.startsWith("#") ? color : `#${color}`;
}

/** `#4C8DF6` + 0.35 → `rgba(76,141,246,0.35)`（解析失败时回退到主题首色同透明度）。 */
export function hexToRgba(color: string, alpha = LINE_FILL_ALPHA): string {
  const raw = String(color ?? "").replace("#", "");
  if (raw.length === 6 && /^[0-9a-f]{6}$/i.test(raw)) {
    const r = parseInt(raw.slice(0, 2), 16);
    const g = parseInt(raw.slice(2, 4), 16);
    const b = parseInt(raw.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }
  return hexToRgba(`#${DEFAULT_CHART_COLORS[0]}`, alpha);
}

/**
 * labels 与每条 data 对齐（与后端 normalize_chart_data 同规则：以最长的一方为准，只补不删）。
 *
 * 真因：真实库里 3/14 张图表 `labels` 与 `datasets[*].data` 长度不符，网页端按 labels 画
 * → 柱子与标签错位。缺标签补 `第N项`，缺数值补 0；labels 多出来的照常保留（对应数据补 0）。
 */
export function alignChartData(
  labels: string[],
  series: number[][],
): { labels: string[]; series: number[][] } {
  const span = Math.max(labels.length, ...series.map((s) => s.length), 0);
  const outLabels = labels.slice(0, span);
  while (outLabels.length < span) outLabels.push(`第${outLabels.length + 1}项`);
  const outSeries = series.map((values) => {
    const row = values.slice(0, span);
    while (row.length < span) row.push(0);
    return row;
  });
  return { labels: outLabels, series: outSeries };
}
