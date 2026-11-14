/**
 * 任务 114：预览页的**跳转动作** = 「先展开目标折叠面板，再滚动到位」。
 *
 * 用户 2026-09-30 傍晚实测原话：「每一章节的课件，应该放在这个章节开头的部分，或者是前面的链接点的时候
 * 它能够跳出来；我去点那个课件的链接，**完全没有反应**，还是要在很后面去找。」
 *
 * 机理：目标面板**默认折叠**（折叠态看不见，锚点也落在折叠后的位置上）⇒ 浏览器滚过去什么也看不到，
 * 表现就是「点了没反应」；再叠加「同一帧里展开 → 高度变化 → 落点被顶偏」⇒ 必须在**展开之后**重新取落点。
 * 所以顺序是固定的：**先展开 → 等一帧 → 以展开后的位置为准 → 再滚**。
 *
 * 这里只放「顺序与时机」这一件事，DOM 由调用方注入：组件传真实 DOM，
 * 门禁脚本（`check-preview-anchor-jump.ts`）传桩并记录调用顺序 ⇒ 这条断言是**真跑**的，不是纯逻辑推导。
 */
// 带扩展名：本文件会被门禁脚本用 Node 直接跑（Node 的 ESM 解析要求显式后缀）。
import type { PanelKey } from "./previewGrouping.ts";

/** 落点只要「能被滚进视口」这一个能力（不依赖 DOM 类型，Node 里也能真跑）。 */
export interface ScrollTarget {
  scrollIntoView(options?: { behavior?: "auto" | "smooth"; block?: "start" | "center" | "end" | "nearest" }): void;
}

/** 跳转目标：章内课件是 `deck-body-<ch.id>`（章级），章内各类型是 PanelKey（知识点卡内的折叠面板）。 */
export type JumpKey = PanelKey | string;

export interface JumpPlan {
  /** 目标折叠面板的 key */
  key: JumpKey;
  /** 目标此刻是否已经展开（已展开就别再动它，直接滚） */
  isOpen(key: JumpKey): boolean;
  /** 展开目标（改 state；不负责滚动） */
  expand(key: JumpKey): void;
  /** 等一帧：默认两次 requestAnimationFrame */
  nextFrame?(): Promise<void>;
  /** 取**展开后**的落点（面板标题 / 面板外壳）；拿不到就返回 null */
  headOf(key: JumpKey): ScrollTarget | null;
  /** 把落点滚进视口 */
  scrollTo(el: ScrollTarget): void;
  /** 跳转前记下当前位置（可选；复用任务 74 的「← 返回预览（回到刚才的位置）」） */
  remember?(): void;
}

/** 等两帧：第一帧 React 提交 DOM，第二帧浏览器已完成布局 —— 展开后的高度这时才拿得准。 */
export function rafFrame(): Promise<void> {
  if (typeof requestAnimationFrame === "undefined") return Promise.resolve();
  return new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  });
}

export type JumpOutcome = "already-open" | "expanded" | "target-missing";

/**
 * 执行一次跳转。返回值说明这次跳转干了什么（供断言与排查用）：
 * - `already-open`：目标本来就展开着，只滚了；
 * - `expanded`：先展开再滚（用户点链接没反应的正是这条路）；
 * - `target-missing`：拿不到落点（例如目标所在的章节卡还没挂载）⇒ **不乱跳**。
 */
export async function jumpToPanel(plan: JumpPlan): Promise<JumpOutcome> {
  plan.remember?.();
  const opened = plan.isOpen(plan.key);
  if (!opened) plan.expand(plan.key);          // ① 先展开：折叠态滚过去看不见 = 「点了没反应」
  await (plan.nextFrame ?? rafFrame)();        // ② 等布局落定
  const el = plan.headOf(plan.key);            // ③ 落点必须在展开**之后**取
  if (!el) return "target-missing";
  plan.scrollTo(el);
  return opened ? "already-open" : "expanded";
}
