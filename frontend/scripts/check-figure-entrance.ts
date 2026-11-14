/**
 * 任务 79 前端断言：预览页「图的入场动效」（A 淡入上浮 + B 遮罩扫出、视口触发一次、可关）。
 *
 * 起因（用户 2026-09-22 看了 HTML 课件的慢速动画）：「我们这个应用里面的图有没有这种慢速动画的效果」；
 * DSH 只读审计的结论是**一处入场动画都没有**（`@keyframes` / `animation:` / `IntersectionObserver` 全 0 处）。
 *
 * 口径只有一处：`src/lib/useEnterOnce.ts`（引擎）+ `src/app.css`（A+B 合成**一条**动画）+ 图元自己挂
 * `data-fig-enter`。这里做**纯逻辑 + 源码接线**断言；真机上的 animationName / 截图 / `getAnimations()` /
 * 开关 / 滚动补播由 DSH 复验，数字与证据见 `backend/docs/task79-figure-entrance.md`。
 *
 * ⚠️ 读文件一律 `replace(/\r+\n/g, "\n")` 归一化行尾（任务 77 的教训：本机工作区是 CRLF，
 * 不归一会让所有多行匹配失效、门禁假红）。
 *
 * 运行：`node scripts/check-figure-entrance.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  ENTRANCE_ATTR, ENTRANCE_CLASS, ENTRANCE_MS, ENTRANCE_WAIT_CLASS, FIGURE_ANIM_KEY,
  IO_THRESHOLD, MAX_CONCURRENT_ENTRANCE, SETTLE_MS, prefersReducedMotion, readFigureAnim, writeFigureAnim,
} from "../src/lib/useEnterOnce.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ---------------- A. 开关：默认一定播 + 存 localStorage（不用浏览器也能验） ---------------- */
// 引擎要读 window.localStorage / matchMedia ⇒ 先造一个最小 window，再验真逻辑（不是只看源码）
const store = new Map<string, string>();
let reduced = false;
(globalThis as Record<string, unknown>).window = {
  localStorage: {
    getItem: (k: string) => (store.has(k) ? store.get(k) as string : null),
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
    removeItem: (k: string) => { store.delete(k); },
  },
  matchMedia: () => ({ matches: reduced }),
  dispatchEvent: () => true,
  addEventListener: () => {},
  removeEventListener: () => {},
};

check(FIGURE_ANIM_KEY === "cg.figureEnterAnim", `开关的 localStorage key 变了：${FIGURE_ANIM_KEY}`);
store.clear();
check(readFigureAnim() === true, "读不到开关时不是「开」（§B4：默认一定播）");
writeFigureAnim(false);
check(store.get(FIGURE_ANIM_KEY) === "0", "关掉开关没有写 localStorage（刷新后就丢了）");
check(readFigureAnim() === false, "关掉开关后仍然读到「开」");
writeFigureAnim(true);
check(store.get(FIGURE_ANIM_KEY) === "1" && readFigureAnim() === true, "再打开没有写回 localStorage");
store.clear();

check(ENTRANCE_CLASS === "fig-enter" && ENTRANCE_WAIT_CLASS === "fig-enter-wait",
  `动画类名口径变了：${ENTRANCE_CLASS} / ${ENTRANCE_WAIT_CLASS}（app.css 会对不上）`);
check(ENTRANCE_ATTR === "data-fig-enter", `图元标记口径变了：${ENTRANCE_ATTR}`);
check(MAX_CONCURRENT_ENTRANCE === 8, `同时在场动画上限不是 8：${MAX_CONCURRENT_ENTRANCE}`);
check(IO_THRESHOLD === 0.5, `「算进入视口」的阈值不是 0.5：${IO_THRESHOLD}`);
check(ENTRANCE_MS >= 900 && ENTRANCE_MS <= 1100, `入场时长 ${ENTRANCE_MS}ms 不在 §B 的 0.9–1.1s`);
check(SETTLE_MS > 0 && SETTLE_MS <= 400, `「滚动停止」的判定 ${SETTLE_MS}ms 不合理`);
check(prefersReducedMotion() === false, "系统没开「减少动画」时读数不对");
reduced = true;
check(prefersReducedMotion() === true, "系统开了「减少动画」时读数不对（界面提示就出不来）");
reduced = false;

/* ---------------- B. app.css：A+B 合成一条动画（口径与「同时运行的动画数 ≤ 8」互锁） ---------------- */
function auditCss(css: string): string[] {
  const bad: string[] = [];
  // 注释里会写到 `@media (prefers-reduced-motion: reduce) { animation: none }`（说明为什么**不写**它），
  // 所有判定都跑在去掉注释的代码上，免得把说明文字当成真的声明。
  const code = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const kf = code.match(/@keyframes\s+cg-fig-enter\s*\{([\s\S]*?)\n\}/);
  if (!kf) {
    bad.push("没有 @keyframes cg-fig-enter（入场动画被删了）");
  } else {
    const body = kf[1];
    if (!/from\s*\{[\s\S]*?opacity:\s*0/.test(body) || !/to\s*\{[\s\S]*?opacity:\s*1/.test(body)) {
      bad.push("A 档没有淡入（keyframes 里找不到 opacity 0 → 1）");
    }
    if (!/translateY\(\s*16px\s*\)/.test(body) || !/translateY\(\s*0\s*\)/.test(body)) {
      bad.push("A 档没有上浮（keyframes 里找不到 translateY 16px → 0）");
    }
    if (!/clip-path:\s*inset\(0\s+100%\s+0\s+0\)/.test(body) || !/clip-path:\s*inset\(0\s+0\s+0\s+0\)/.test(body)) {
      bad.push("B 档没有遮罩扫出（keyframes 里找不到 clip-path inset(0 100% 0 0) → inset(0 0 0 0)）");
    }
  }
  const rule = code.match(/\.fig-enter\s*\{([^}]*)\}/);
  if (!rule) {
    bad.push("没有 .fig-enter（引擎加了类也没有动画）");
  } else {
    const decl = rule[1];
    const m = decl.match(/animation:\s*cg-fig-enter\s+([\d.]+)s/);
    if (!m) {
      bad.push(`.fig-enter 没有引用 cg-fig-enter 动画：${decl.trim()}`);
    } else {
      const sec = Number(m[1]);
      if (!(sec >= 0.9 && sec <= 1.2)) bad.push(`入场动画时长 ${sec}s 不在 0.9–1.2s（§B 要求 0.9–1.1s）`);
      if (Math.abs(sec * 1000 - ENTRANCE_MS) > 1) {
        bad.push(`app.css 的 ${sec}s 与引擎 ENTRANCE_MS=${ENTRANCE_MS} 不一致（JS 会提前/滞后摘类）`);
      }
    }
    // 去掉括号里的参数（cubic-bezier 自带逗号）后再看还有没有逗号 ⇒ 一个元素挂了几条 animation
    const listed = (decl.split("animation:")[1] ?? "").replace(/\([^)]*\)/g, "");
    if (listed.includes(",")) {
      bad.push("一个元素挂了不止一条 animation ⇒ document.getAnimations() 会翻倍，对不上「同时运行的动画数 ≤ 8」");
    }
    if (!/cubic-bezier\(/.test(decl)) bad.push(".fig-enter 不是缓出曲线（收尾就不慢）");
  }
  if (!/\.fig-enter-wait\s*\{[^}]*opacity:\s*0/.test(code)) {
    bad.push("没有 .fig-enter-wait（滚轮翻页时没法先「等着」，会半路播完）");
  }
  if (/prefers-reduced-motion/.test(code)) {
    bad.push("出现了 prefers-reduced-motion（§B4：只提示、不禁用，样板那轮踩过这个坑）");
  }
  if (/animation:\s*none/.test(code)) bad.push("出现了 animation: none（会静默禁用动画）");
  return bad;
}

/* ---------------- C. 引擎：视口触发 / 滚动停止补播 / 并发上限 / 关掉就全可见 ---------------- */
function auditEngine(src: string): string[] {
  const bad: string[] = [];
  const cut = src.indexOf("export function useFigureEntrance");
  const tail = cut >= 0 ? src.slice(cut) : "";
  if (!/new IntersectionObserver\(/.test(src)) bad.push("没有用 IntersectionObserver（视口触发就没了）");
  if (!/threshold:\s*\[\s*0\s*,\s*IO_THRESHOLD\s*,\s*1\s*\]/.test(src)) {
    bad.push("IntersectionObserver 没有按 IO_THRESHOLD 触发");
  }
  if (!/querySelectorAll\(ENTRANCE_SELECTOR\)/.test(src)) bad.push("没有按 [data-fig-enter] 扫图元");
  if (!/new MutationObserver/.test(src)) bad.push("没有 MutationObserver（异步加载出来的图不会被接管）");
  if (!/window\.addEventListener\("scroll"/.test(src)) {
    bad.push("没有监听 scroll（滚轮翻页时没法「停下再补播」）");
  }
  if (!/setTimeout\(settle,\s*SETTLE_MS\)/.test(src)) bad.push("没有「滚动停止后补播」的定时器");
  if (!/if \(inFlight\.size >= MAX_CONCURRENT_ENTRANCE\) return;/.test(src)) {
    bad.push("占位等待没有受并发上限约束");
  }
  if (!/inFlight\.size < MAX_CONCURRENT_ENTRANCE/.test(src)) {
    bad.push("真正播放没有受并发上限约束（同屏几十张图会一起播）");
  }
  if (!/getItem\(FIGURE_ANIM_KEY\) !== "0"/.test(src)) {
    bad.push("开关不是「只有明确写了 0 才算关」（§B4：默认一定播）");
  }
  if (!tail) {
    bad.push("找不到 useFigureEntrance（引擎被改名/删掉了）");
    return bad;
  }
  if (!/el\.addEventListener\("animationend", done\)/.test(tail)) {
    bad.push("播完没有摘掉 .fig-enter（会留下残余 transform / clip-path）");
  }
  if (!/if \(!enabled\)/.test(tail) || !/ENTRANCE_CLASS, ENTRANCE_WAIT_CLASS\)/.test(tail)) {
    bad.push("开关关掉时没有把动画类摘干净（内容可能被 .fig-enter-wait 藏住）");
  }
  if (/prefersReducedMotion|matchMedia|reduced-motion/.test(tail)) {
    bad.push("引擎按系统「减少动画」做了禁用（§B4 明确不要）");
  }
  return bad;
}

/* ---------------- D. 接线：只有两处触发 + 图元标记 + 开关入口 + 弹窗重播 ---------------- */
function auditWiring(preview: string, md: string, ppt: string, modal: string): string[] {
  const bad: string[] = [];
  if (!/<img[^>]*data-fig-enter=""/.test(md)) {
    bad.push("MdWithFlow 的图（讲义正文内嵌概念图 / 配图）没挂 data-fig-enter");
  }
  if (!/SlideImagesBlock/.test(ppt) || !/data-fig-enter=""/.test(ppt)) {
    bad.push("PptDeckBlocks 的幻灯片缩略图没挂 data-fig-enter");
  }
  const marks = (preview.match(/data-fig-enter=""/g) ?? []).length;
  if (marks < 5) {
    bad.push(`PreviewStep 里只有 ${marks} 处图元挂了 data-fig-enter（图表 / 配图 / 内嵌流程图 / 库流程图 / 概念图 共 5 处）`);
  }
  if (!/useFigureEntrance\(animOn,\s*previewRoot\)/.test(preview)) {
    bad.push("PreviewStep 没有调用 useFigureEntrance(animOn, previewRoot)（引擎根本没启动）");
  }
  if (!/data-testid="preview-root"/.test(preview) || !/ref=\{setPreviewRoot\}/.test(preview)) {
    bad.push("根容器没有回调 ref / testid（观察范围限不住，别的步骤、分享页的图也会动）");
  }
  if (!/data-testid="anim-toggle"/.test(preview) || !/动画：\{animOn \? "开" : "关"\}/.test(preview)) {
    bad.push("浮动按钮区没有「动画：开/关」入口（与任务 74 的两个按钮同区）");
  }
  if (!/setAnimOn\(!animOn\)/.test(preview)) bad.push("「动画：开/关」按钮点了不切状态");
  if (!/animOn && prefersReducedMotion\(\)/.test(preview)) {
    bad.push("系统开了「减少动画」时没有提示（§B4 要求只提示、不禁用）");
  }
  if (!/className=\{[^}]*ENTRANCE_CLASS/.test(modal) || !/useFigureAnim/.test(modal)) {
    bad.push("FigureZoomModal 打开时没有播入场（任务 79 的第二处触发）");
  }
  if (!/key=\{playSeq\}/.test(modal)) bad.push("FigureZoomModal 没有 key={playSeq}（同一张图再开不会重播）");
  return bad;
}

const css = read("src/app.css");
const engineSrc = read("src/lib/useEnterOnce.ts");
const preview = read("src/components/steps/PreviewStep.tsx");
const md = read("src/components/MdWithFlow.tsx");
const ppt = read("src/components/PptDeckBlocks.tsx");
const modal = read("src/components/FigureZoomModal.tsx");

failures.push(...auditCss(css).map((m) => `app.css：${m}`));
failures.push(...auditEngine(engineSrc).map((m) => `useEnterOnce.ts：${m}`));
failures.push(...auditWiring(preview, md, ppt, modal).map((m) => `接线：${m}`));

/* ---------------- E. 反证据：摘掉任一关键接线 ⇒ 上面的审计必须变红 ---------------- */
const mutations: [string, [string, string, () => string[]]][] = [
  ["删掉 @keyframes cg-fig-enter",
    [css, css.replace("@keyframes cg-fig-enter {", "@keyframes cg-fig-enter-x {"), (m) => auditCss(m)]],
  ["把 .fig-enter 的动画声明删掉（animation: none）",
    [css, css.replace(/\.fig-enter \{[^}]*\}/, ".fig-enter { animation: none; }"), (m) => auditCss(m)]],
  ["把遮罩扫出（clip-path）从 keyframes 里删掉",
    [css, css.split("clip-path").join("opacity-x"), (m) => auditCss(m)]],
  ["把入场时长改成 0.3s",
    [css, css.replace("cg-fig-enter 1.05s", "cg-fig-enter 0.3s"), (m) => auditCss(m)]],
  ["用一个 @media 静默禁掉动画（样板那轮的坑）",
    [css, `${css}\n@media (prefers-reduced-motion: reduce) { .fig-enter { animation: none; } }\n`, (m) => auditCss(m)]],
  ["删掉 .fig-enter-wait（滚轮翻页会半路播完）",
    [css, css.replace(/\.fig-enter-wait \{[^}]*\}/, ""), (m) => auditCss(m)]],
  ["拆成两条 animation（document.getAnimations() 会翻倍）",
    [css, css.replace("animation: cg-fig-enter 1.05s cubic-bezier(0.16, 0.84, 0.24, 1) both;",
      "animation: cg-fig-enter 1.05s cubic-bezier(0.16, 0.84, 0.24, 1) both, cg-fig-wipe 1.05s linear both;"), (m) => auditCss(m)]],

  ["摘掉 IntersectionObserver",
    [engineSrc, engineSrc.replace("new IntersectionObserver(", "new XObserver("), (m) => auditEngine(m)]],
  ["摘掉滚动监听",
    [engineSrc, engineSrc.replace('window.addEventListener("scroll", onScroll, { passive: true });', ""), (m) => auditEngine(m)]],
  ["摘掉「滚动停止后补播」",
    [engineSrc, engineSrc.replace("setTimeout(settle, SETTLE_MS)", "void settle"), (m) => auditEngine(m)]],
  ["摘掉并发上限（占位等待那一处）",
    [engineSrc, engineSrc.replace("if (inFlight.size >= MAX_CONCURRENT_ENTRANCE) return;", ""), (m) => auditEngine(m)]],
  ["摘掉并发上限（真正播放那一处）",
    [engineSrc, engineSrc.replace("inFlight.size < MAX_CONCURRENT_ENTRANCE", "true"), (m) => auditEngine(m)]],
  ["播完不摘类（留下残余 transform / clip-path）",
    [engineSrc, engineSrc.replace('el.addEventListener("animationend", done);', ""), (m) => auditEngine(m)]],
  ["在引擎里跟随系统「减少动画」禁用",
    [engineSrc, engineSrc.replace("const apply = (el: Element) => {",
      'if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;\n    const apply = (el: Element) => {'), (m) => auditEngine(m)]],
  ["把开关口径改成「只有写了 1 才算开」",
    [engineSrc, engineSrc.replace('getItem(FIGURE_ANIM_KEY) !== "0"', 'getItem(FIGURE_ANIM_KEY) === "1"'), (m) => auditEngine(m)]],
  ["摘掉 [data-fig-enter] 扫描",
    [engineSrc, engineSrc.replace("querySelectorAll(ENTRANCE_SELECTOR)", "querySelectorAll(\"img\")"), (m) => auditEngine(m)]],

  ["摘掉 MdWithFlow 图元的 data-fig-enter",
    [md, md.replace(' data-fig-enter=""', ""), (m) => auditWiring(preview, m, ppt, modal)]],
  ["摘掉幻灯片缩略图的 data-fig-enter",
    [ppt, ppt.replace(' data-fig-enter=""', ""), (m) => auditWiring(preview, md, m, modal)]],
  ["摘掉 PreviewStep 的 useFigureEntrance 调用",
    [preview, preview.replace("useFigureEntrance(animOn, previewRoot);", ""), (m) => auditWiring(m, md, ppt, modal)]],
  ["摘掉根容器的 ref（观察范围限不住）",
    [preview, preview.replace(" ref={setPreviewRoot}", ""), (m) => auditWiring(m, md, ppt, modal)]],
  ["摘掉「动画：开/关」按钮",
    [preview, preview.replace('data-testid="anim-toggle"', ""), (m) => auditWiring(m, md, ppt, modal)]],
  ["摘掉弹窗里的入场类",
    [modal, modal.replace("className={animOn && open ? ENTRANCE_CLASS : undefined}", ""), (m) => auditWiring(preview, md, ppt, m)]],
  ["摘掉弹窗的 key={playSeq}（同图再开不重播）",
    [modal, modal.replace("key={playSeq}", ""), (m) => auditWiring(preview, md, ppt, m)]],
];
for (const [name, [base, mutated, run]] of mutations) {
  if (mutated === base) {
    failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
  } else if (run(mutated).length === 0) {
    failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  }
}

if (failures.length > 0) {
  console.error("前端「预览页图的入场动效」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端图的入场动效断言通过：进入视口播一次（IntersectionObserver 阈值 0.5、滚动停止后补播）、"
  + "A 淡入上浮 + B clip-path 遮罩扫出一并走一条 1.05s 缓出动画（收起很慢）、"
  + "同时在场 ≤8（超出的图不加类、内容照常可见）、"
  + "默认一定播且不跟随系统「减少动画」禁用（只提示 + 浮动按钮「动画：开/关」存 localStorage）、"
  + "只有第 4 步预览容器与放大弹窗两处触发（" + (preview.match(/data-fig-enter=""/g) ?? []).length
  + " 处图元标记 + 弹窗重播），" + mutations.length + " 条变异自检全红");
