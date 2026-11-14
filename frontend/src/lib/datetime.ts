/**
 * 时间戳统一成「本地时间 + 人类可读」（YYYY-MM-DD HH:mm）—— **全仓唯一出处**。
 *
 * 任务 81 §B2：版本面板 / 质量报告原先直接把后端的 ISO 原样渲染，用户看到的是
 * `2026-09-21T06:24:46.708384` / `2026-09-22T13:56:02+00:00` 这种技术格式；
 * 这里统一按**浏览器本地时区**格式化（带时区的 ISO 会先换算到本地）。
 *
 * 任务 83 §B2：**没有时区标记的裸时间戳按 UTC 解释**。按 ES 规范 `new Date("2026-09-21T06:24:46")`
 * 会当**本地时间**（少 8 小时，日期都可能变），而本仓的裸时间戳全部来自后端的 `utcnow()` 落库
 * （SQLite 不存时区）⇒ 必须先补 `Z` 再解析。带 `Z` / `±hh:mm` 的串行为不变。
 */
const BARE_LOCAL_SHAPE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

export function formatLocalTime(iso?: string | null, fallback = "-"): string {
  if (!iso) return fallback;
  const text = String(iso).trim();
  const normalized = BARE_LOCAL_SHAPE.test(text) ? `${text.replace(" ", "T")}Z` : text;
  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
