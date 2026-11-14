/**
 * 任务 79：预览页「图的入场动效」——**进入视口播一次**的引擎（一处实现，图自己只打标记）。
 *
 * 用户 2026-09-22 看了 HTML 课件的慢速动画后问「我们这个应用里面的图有没有这种慢速动画的效果」；
 * DSH 审计结论是**一处入场动画都没有**（`@keyframes` / `animation:` / `IntersectionObserver` 全 0 处）。
 *
 * 定稿口径（HANDOFF 任务 79 §B，只做 A+B）：
 * - **A 容器级入场**：淡入 + 上浮，1.05s、收尾很慢的缓出（`cubic-bezier(.16,.84,.24,1)`），播完不重播；
 * - **B 遮罩扫出**：同一元素叠 `clip-path` 从左到右揭示，与 A **同时开始、同时结束**
 *   （合成 app.css 里的**一条** `@keyframes cg-fig-enter`：同样两条动画效果，但只算一条 CSSAnimation
 *   ⇒ 8 个元素同时播也只有 8 条 `document.getAnimations()`，对得上 §C「同时运行的动画数 ≤ 8」）；
 * - **触发只在两处**：① 预览页的图/图表卡进入视口；② 任务 76 的放大弹窗打开时；
 * - **滚轮翻页不会半路播完**：滚动中进入视口的先挂「等待」(不可见)，等**滚动停止**再完整播一遍；
 * - **同屏 ≥20 张图也不会同时播几十个**：同时在场的上限 `MAX_CONCURRENT_ENTRANCE = 8`，
 *   超出的图**不加动画**（内容照常可见）；
 * - **默认一定播**：**不**跟随系统 `prefers-reduced-motion` 静默禁用（HTML 课件样板那轮踩过这个坑），
 *   系统开了「减少动画」只在界面上提示一句；开关状态存 `localStorage`。
 *
 * 为什么放在 lib：口径只有一处，图元件只负责给自己挂 `data-fig-enter`；
 * 门禁脚本 `frontend/scripts/check-figure-entrance.ts` 直接读这个文件断言口径。
 */
import { useEffect, useState } from "react";

/** 开关存这里（"0" = 关；其它/缺失 = 开 ⇒ 默认一定播）。 */
export const FIGURE_ANIM_KEY = "cg.figureEnterAnim";
/** 开关变化的同页广播（localStorage 的 storage 事件只在**别的标签页**触发）。 */
export const FIGURE_ANIM_EVENT = "cg-figure-anim-change";
export const ENTRANCE_CLASS = "fig-enter";
export const ENTRANCE_WAIT_CLASS = "fig-enter-wait";
export const ENTRANCE_ATTR = "data-fig-enter";
export const ENTRANCE_SELECTOR = `[${ENTRANCE_ATTR}]`;
/** 视口占比到这个数才算"进来了"（DSH §B：阈值 ~0.5）。 */
export const IO_THRESHOLD = 0.5;
/** 滚动停止的判定：最后一次 scroll 之后这么久没有新 scroll。 */
export const SETTLE_MS = 220;
/** 与 app.css 里两条动画的时长对齐（1.05s）。 */
export const ENTRANCE_MS = 1050;
/** 同时在场（等待 + 正在播）的动画上限：同屏 ≥20 张图时只让前 6–8 张动。 */
export const MAX_CONCURRENT_ENTRANCE = 8;

/** 开关当前是否开着（读不到 localStorage 也算开 —— 默认一定播）。 */
export function readFigureAnim(): boolean {
  try {
    return window.localStorage.getItem(FIGURE_ANIM_KEY) !== "0";
  } catch {
    return true;
  }
}

/** 写开关并广播（存不了也要切，不能因为隐私模式就点了没反应）。 */
export function writeFigureAnim(on: boolean): void {
  try {
    window.localStorage.setItem(FIGURE_ANIM_KEY, on ? "1" : "0");
  } catch {
    /* 存不了就只在内存里生效 */
  }
  window.dispatchEvent(new CustomEvent(FIGURE_ANIM_EVENT, { detail: on }));
}

/** 系统是否开了「减少动画」——**只用来提示**，不用来禁用（§B4）。 */
export function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** 「动画：开/关」的 React 状态（同一页多处用同一份）。 */
export function useFigureAnim(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState<boolean>(() => readFigureAnim());
  useEffect(() => {
    const sync = () => setOn(readFigureAnim());
    window.addEventListener(FIGURE_ANIM_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(FIGURE_ANIM_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return [on, writeFigureAnim];
}

interface Entry {
  /** 已经播过（不再播）。 */
  played: boolean;
  /** 最近一次交叉比。 */
  ratio: number;
  /** 滚动中进入视口、正在等滚动停止（此时元素不可见）。 */
  waiting: boolean;
}

/**
 * 扫描 `root`（默认 `document.body`）里所有 `[data-fig-enter]`，按上面的口径播入场动画。
 * `enabled=false`（开关关掉）时把已经加上的类全部摘掉 ⇒ 内容**全部可见**。
 */
export function useFigureEntrance(enabled: boolean, root?: HTMLElement | null): void {
  useEffect(() => {
    const host: HTMLElement = root ?? document.body;
    const strip = (el: Element) => el.classList.remove(ENTRANCE_CLASS, ENTRANCE_WAIT_CLASS);
    if (!enabled) {
      host.querySelectorAll(`${ENTRANCE_SELECTOR}.${ENTRANCE_CLASS}, ${ENTRANCE_SELECTOR}.${ENTRANCE_WAIT_CLASS}`)
        .forEach(strip);
      return;
    }

    const state = new WeakMap<Element, Entry>();
    const seen: Element[] = [];          // 观察过的图元（settle 时要按「谁还在视口里」补播）
    const inFlight = new Set<Element>();
    let scrolling = false;
    let settleTimer = 0;
    let disposed = false;

    /** 播完就把类摘掉：元素回到自然样式（与动画终态一致，不会闪），也不会留下残余的
     *  `transform` / `clip-path`（它们会新建包含块与层叠上下文，可能影响 antd 弹层的定位）。 */
    const release = (el: Element) => {
      el.classList.remove(ENTRANCE_CLASS);
      inFlight.delete(el);
    };

    const apply = (el: Element) => {
      const st = state.get(el);
      if (!st || st.played) return;
      st.played = true;
      st.waiting = false;
      el.classList.remove(ENTRANCE_WAIT_CLASS);
      const done = () => {
        el.removeEventListener("animationend", done);
        release(el);
      };
      el.addEventListener("animationend", done);
      // 兜底：动画被外部因素吞掉（元素被隐藏 / 被摘出文档）时也要把额度还回来，不留孤儿
      window.setTimeout(() => { if (inFlight.has(el)) release(el); }, ENTRANCE_MS + 200);
      // 强制回流：同一元素重播（补播）时动画才会真正重头走一遍
      void (el as HTMLElement).offsetWidth;
      el.classList.add(ENTRANCE_CLASS);
    };

    /** 滚动中进入视口：先占位等着（不可见），滚动停止后再完整播。 */
    const arm = (el: Element) => {
      const st = state.get(el);
      if (!st || st.played || st.waiting) return;
      if (inFlight.size >= MAX_CONCURRENT_ENTRANCE) return;
      st.waiting = true;
      inFlight.add(el);
      el.classList.add(ENTRANCE_WAIT_CLASS);
    };

    const io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const st = state.get(en.target);
        if (!st) continue;
        st.ratio = en.intersectionRatio;
        if (en.intersectionRatio < IO_THRESHOLD || st.played) continue;
        if (scrolling) {
          arm(en.target);
        } else if (inFlight.size < MAX_CONCURRENT_ENTRANCE) {
          inFlight.add(en.target);
          apply(en.target);
        }
      }
    }, { threshold: [0, IO_THRESHOLD, 1] });

    const scan = () => {
      // 图元会被重建（例如切筛选把折叠面板整块换掉）：顺手把已经离开文档的清出去
      for (let i = seen.length - 1; i >= 0; i--) {
        if (!seen[i].isConnected) { state.delete(seen[i]); seen.splice(i, 1); }
      }
      host.querySelectorAll(ENTRANCE_SELECTOR).forEach((el) => {
        if (state.has(el)) return;
        seen.push(el);
        state.set(el, { played: false, ratio: 0, waiting: false });
        io.observe(el);
      });
    };
    scan();
    const mo = new MutationObserver(() => scan());
    mo.observe(host, { childList: true, subtree: true });

    /** 滚动停止：把"等待中且还在视口里"的图补播一遍；又滚出去的撤回等待。 */
    const settle = () => {
      scrolling = false;
      for (const el of [...inFlight]) {
        const st = state.get(el);
        if (!st?.waiting) continue;
        if (st.ratio >= IO_THRESHOLD) {
          apply(el);                       // 停下时还在视口里 ⇒ 完整播一遍
        } else {
          st.waiting = false;
          inFlight.delete(el);
          el.classList.remove(ENTRANCE_WAIT_CLASS);
        }
      }
      // 快速翻了好几屏才停：前面几屏的图把 8 个额度占满了，最后停下的这一屏反而没抢到名额
      // （它们的交叉比早就在滚动中报过，之后没有新的阈值穿越 ⇒ 不会再有 IO 回调）。
      // 这里按「停住时还在视口里」再补一遍，额度仍是 MAX_CONCURRENT_ENTRANCE。
      for (const el of seen) {
        if (inFlight.size >= MAX_CONCURRENT_ENTRANCE) break;
        const st = state.get(el);
        if (!st || st.played || st.waiting || st.ratio < IO_THRESHOLD) continue;
        inFlight.add(el);
        apply(el);
      }
    };

    const onScroll = () => {
      scrolling = true;
      window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(settle, SETTLE_MS);
    };
    window.addEventListener("scroll", onScroll, { passive: true });

    return () => {
      disposed = true;
      window.removeEventListener("scroll", onScroll);
      window.clearTimeout(settleTimer);
      mo.disconnect();
      io.disconnect();
      host.querySelectorAll(`.${ENTRANCE_CLASS}, .${ENTRANCE_WAIT_CLASS}`).forEach(strip);
      void disposed;
    };
  }, [enabled, root]);
}

export default useFigureEntrance;
