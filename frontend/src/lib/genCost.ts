/**
 * 任务 86 §B3：生成一篇讲义的**预估花费**（只做提示，**不接支付**）。
 *
 * 为什么要有它：用户「不知道点一下要花钱、花多少」。界面上的金额**只有这一个出处**，
 * 不许在按钮 / 弹窗里各写一个常量（那样迟早对不上）。
 *
 * 口径：`¥0.033 / 篇`（与 HANDOFF「成本纪律」一致）。金额一律按 2 位小数显示，
 * 与单篇按钮上的 `≈¥0.03` 同一个数，不会出现两个说法。
 */
export const LECTURE_COST_YUAN = 0.033;

/** 单篇按钮上的金额文案（≈¥0.03）。 */
export const LECTURE_COST_ONE = "≈¥0.03";

/** 单价的人话（弹窗里「N 篇 × ¥0.033/篇」用）—— 与上面两个文案同源，别处不许再写这个数。 */
export const LECTURE_COST_UNIT = `¥${LECTURE_COST_YUAN.toFixed(3)}/篇`;

/** n 篇的预估金额文案；n ≤ 0 ⇒ 空串（调用方据此决定要不要渲染）。 */
export function estimateCostText(count: number): string {
  const n = Math.max(0, Math.floor(count));
  if (!n) return "";
  return `≈¥${(n * LECTURE_COST_YUAN).toFixed(2)}`;
}
