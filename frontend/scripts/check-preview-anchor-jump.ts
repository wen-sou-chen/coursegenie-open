/**
 * 任务 114 前端断言：预览页的**跳转** —— 「点 → 展开目标面板 → 滚到展开后的位置」。
 *
 * 用户 2026-09-30 傍晚实测原话：「每一章节的课件，应该放在这个章节开头的部分，或者是前面的链接点的时候
 * 它能够跳出来；我去点那个课件的链接，**完全没有反应**，还是要在很后面去找。」
 *
 * 与既有断言的区别：这条**真跑**（把 `src/lib/panelJump.ts` 的函数拿过来执行），不是纯逻辑推导 ——
 * 桩里模拟「折叠态 / 展开态落点不同」，用它证明三件事：
 *   ① 目标默认折叠时，点击后**真的被展开**（activeKey 会包含它）；
 *   ② 顺序是「先展开 → 等一帧 → **再取落点**」——落点用的是**展开后**的位置（折叠态的 1200 拿不到分）；
 *   ③ 已展开时不重复展开；拿不到落点时不乱滚。
 *
 * 另有源码级接线审计（章卡顺序 / 受控折叠 / 锚点 / 标签可点），把「库里对了但页面没接上」这条堵住。
 *
 * 运行：`node scripts/check-preview-anchor-jump.ts`，失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { jumpToPanel, rafFrame } from "../src/lib/panelJump.ts";
import type { JumpOutcome, JumpPlan } from "../src/lib/panelJump.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");
const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };
const eq = (got: unknown, want: unknown, msg: string) =>
  check(JSON.stringify(got) === JSON.stringify(want), `${msg}（实际 ${JSON.stringify(got)}，应为 ${JSON.stringify(want)}）`);

/* ============================ ① 真的跑一遍：桩里「展开前后落点不同」 ============================ */

const FOLDED_TOP = 1200;   // 折叠态：面板看不见，落点停在这里（滚过去＝用户说的「点了没反应」）
const OPEN_TOP = 1680;     // 展开态：高度变了，落点跟着变（所以必须展开后再取一次）

interface Stub {
  calls: string[];
  open: { value: boolean };
  scrolled: number[];
  plan: JumpPlan;
}

function makeStub(opts: { open?: boolean; missing?: boolean } = {}): Stub {
  const calls: string[] = [];
  const scrolled: number[] = [];
  const open = { value: !!opts.open };
  const head = { top: 0, scrollIntoView: () => { scrolled.push(head.top); } };
  const plan: JumpPlan = {
    key: "deck-body-ch1",
    isOpen: (k) => { calls.push(`isOpen(${k})`); return open.value; },
    expand: (k) => { calls.push(`expand(${k})`); open.value = true; },
    nextFrame: async () => { calls.push(`nextFrame(open=${open.value})`); },
    headOf: (k) => {
      if (opts.missing) { calls.push(`headOf(${k})→null`); return null; }
      head.top = open.value ? OPEN_TOP : FOLDED_TOP;
      calls.push(`headOf(open=${open.value},top=${head.top})`);
      return head;
    },
    scrollTo: (el) => { calls.push("scrollTo"); el.scrollIntoView({ behavior: "smooth", block: "start" }); },
    remember: () => calls.push("remember"),
  };
  return { calls, open, scrolled, plan };
}

/** 与 jumpToPanel 同一套断言，但把「跳转时展开」那一步去掉（＝用户原来踩到的 bug）。 */
async function jumpWithoutExpand(plan: JumpPlan): Promise<JumpOutcome> {
  plan.remember?.();
  const opened = plan.isOpen(plan.key);
  await (plan.nextFrame ?? rafFrame)();
  const el = plan.headOf(plan.key);
  if (!el) return "target-missing";
  plan.scrollTo(el);
  return opened ? "already-open" : "expanded";
}

/** 跑一遍「点 → 展开 → 目标可见」的全部断言；返回失败条数（正反两次都用它）。 */
async function runJumpAssertions(run: (plan: JumpPlan) => Promise<JumpOutcome>): Promise<string[]> {
  const bad: string[] = [];
  const eq2 = (got: unknown, want: unknown, msg: string) => {
    if (JSON.stringify(got) !== JSON.stringify(want)) bad.push(`${msg}（实际 ${JSON.stringify(got)}，应为 ${JSON.stringify(want)}）`);
  };

  // ① 目标默认折叠 ⇒ 点一下必须**先展开**
  const a = makeStub();
  eq2(await run(a.plan), "expanded", "目标默认折叠时点跳转，没有走「先展开」这条路");
  eq2(a.open.value, true, "点了跳转，目标面板还是折叠的（activeKey 里没有它 ⇒ 用户看到的就是「没反应」）");
  // ② 顺序 + 落点：先展开 → 等一帧 → 取展开后的落点 → 再滚
  eq2(a.calls, [
    "remember",
    "isOpen(deck-body-ch1)",
    "expand(deck-body-ch1)",
    `nextFrame(open=true)`,
    `headOf(open=true,top=${OPEN_TOP})`,
    "scrollTo",
  ], "调用顺序不是「先展开 → 等一帧 → 取展开后的落点 → 再滚」");
  eq2(a.scrolled, [OPEN_TOP], "滚动用的不是**展开后**的位置（展开引起的高度变化会把落点顶偏）");
  eq2(a.scrolled.includes(FOLDED_TOP), false, "滚动落点取成了折叠态的位置");

  // ③ 目标本来就展开 ⇒ 不重复展开，但仍要滚到位
  const b = makeStub({ open: true });
  eq2(await run(b.plan), "already-open", "目标已展开时不该再展开一次");
  eq2(b.calls.some((c) => c.startsWith("expand")), false, "目标已展开却又展开了一次");
  eq2(b.scrolled, [OPEN_TOP], "目标已展开时没有滚到位");

  // ④ 拿不到落点（例如所在章卡还没挂载）⇒ 不乱滚
  const c = makeStub({ missing: true });
  eq2(await run(c.plan), "target-missing", "拿不到落点时应报 target-missing");
  eq2(c.scrolled, [], "拿不到落点时不该乱滚");

  return bad;
}

for (const msg of await runJumpAssertions(jumpToPanel)) failures.push(msg);

/* ============================ ② 反证据：去掉「跳转时展开」⇒ 上面这套必须红 ============================ */

const mutatedRed = await runJumpAssertions(jumpWithoutExpand);
check(mutatedRed.length > 0, "变异「去掉跳转时展开」居然还是绿的（这套断言盖不住用户踩到的那个 bug）");
check(mutatedRed.some((m) => m.includes("还是折叠的")), "变异后没有点名「目标还是折叠的」（activeKey 里没它）");
check(mutatedRed.some((m) => m.includes("滚动用的不是")), "变异后没有点名「落点取的是折叠态的位置」");
check(mutatedRed.some((m) => m.includes("调用顺序不是")), "变异后没有点名调用顺序（先展开 → 等一帧 → 取落点）");

/* ============================ ③ panelJump.ts：顺序必须是「展开 → 等一帧 → 取落点」 ============================ */

const jumpSrc = read("src/lib/panelJump.ts");
const jumpBody = jumpSrc.slice(jumpSrc.indexOf("export async function jumpToPanel"));
const iExpand = jumpBody.indexOf("plan.expand(");
const iFrame = jumpBody.indexOf("nextFrame");
const iHead = jumpBody.indexOf("plan.headOf(");
const iScroll = jumpBody.indexOf("plan.scrollTo(");
check(iExpand > 0 && iFrame > 0 && iHead > 0 && iScroll > 0, "panelJump.ts 里少了一步（展开 / 等一帧 / 取落点 / 滚动）");
check(iExpand < iFrame && iFrame < iHead && iHead < iScroll, "panelJump.ts 的顺序不是「先展开 → 等一帧 → 取落点 → 再滚」");
check(/if \(!opened\) plan\.expand\(plan\.key\)/.test(jumpBody), "panelJump.ts 没有「没展开就先展开」这一步");

/* ============================ ④ 组件接线：章卡顺序 / 受控折叠 / 锚点 / 标签可点 ============================ */

const preview = read("src/components/steps/PreviewStep.tsx");

function auditPreview(src: string): string[] {
  const bad: string[] = [];
  // 章卡内：课件在**最前面**（用户：「每一章节的课件，应该放在这个章节开头的部分」）
  const chapterItem = src.slice(src.indexOf("const chapterItem ="), src.indexOf("const unboundItem ="));
  const iDeck = chapterItem.indexOf("renderDeckRow(ch, true)");
  const iPoints = chapterItem.indexOf("pointCards(members, view)");
  if (iDeck < 0 || iPoints < 0) bad.push("章卡里找不到课件块或知识点卡");
  else if (iDeck > iPoints) bad.push("章卡里课件**不在最前面**（renderDeckRow 排在 pointCards 之后）");
  if (!/data-chapter-anchor=\{ch\.key\}/.test(chapterItem)) bad.push("章卡没有 data-chapter-anchor（跳转取不到本章的落点）");
  // 章内课件：受控折叠（跳转要能自己打开）+ 锚点
  if (!/data-deck-anchor=\{bodyKey\}/.test(src)) bad.push("章内课件没有 data-deck-anchor 锚点");
  if (!/activeKey=\{deckPanelActiveKeys\(bodyKey, deckOpen\)\}/.test(src)) {
    bad.push("章内课件折叠块不是受控的（跳转打不开它）");
  }
  if (!/const deckOpen = !!openDecks\[ch\.key\]/.test(src)) bad.push("章内课件的展开状态没有从 openDecks 来");
  // 章节头的「课件」标签：可点 → 展开 + 滚过去
  if (!/jumpToDeck\(ch\)/.test(src)) bad.push("章节头的课件标签没接 jumpToDeck（点了还是没反应）");
  if (!/课件（/.test(src)) bad.push("章节头没有「课件（N）」标签");
  // 章节头的「只看某类」标签：按章节视图走「筛选 + 展开 + 滚过去」
  const badgeJumps = (src.match(/view === "chapter" \? jumpToKind\(ch, kind\) : applyFilter\(kind\)/g) ?? []).length;
  if (badgeJumps !== 4) bad.push(`章节头的「只看」标签有 ${badgeJumps} 个接了 jumpToKind（应为 4 个：讲义/作业/流程图/数据图）`);
  // 每个面板都要有落点；概念图那个任务 76 锚点原样保留
  if (!/data-panel-anchor=\{panel\.key\}/.test(src)) bad.push("卡内面板没有 data-panel-anchor（跳转取不到落点）");
  if (!/data-diagram-anchor=\{diagrams\.length > 0 \? "1" : undefined\}/.test(src)) bad.push("任务 76 的概念图锚点被改掉了");
  // 章卡这一层受控（跳转要能展开某章；章卡折着时里面的面板根本没挂载）
  if (!/activeKey=\{openChapters\}/.test(src)) bad.push("章卡这一层不是受控展开（跳转到未展开的章会空手而归）");
  if (!/setOpenChapters\(chapterDefaultOpen\(\)\)/.test(src)) bad.push("章卡默认展开口径没了（视图/筛选一变应回到 chapterDefaultOpen()）");
  // 既有交互与属性一个不丢
  if (!/onZoom=\{openZoom\}/.test(src)) bad.push("点图放大丢了");
  if (!/onClick=\{\(\) => onOpenChapter\(member\.id\)\}/.test(src)) bad.push("「修改这一节」丢了");
  const marks = (src.match(/data-fig-enter=""/g) ?? []).length;
  if (marks !== 5) bad.push(`data-fig-enter 有 ${marks} 处（应为 5 处）`);
  if (!/本节暂无讲义/.test(src)) bad.push("空态文案丢了");
  return bad;
}

failures.push(...auditPreview(preview).map((m) => `PreviewStep.tsx：${m}`));
failures.push(...auditLib(read("src/lib/previewGrouping.ts")));

function auditLib(src: string): string[] {
  const bad: string[] = [];
  if (!/export function deckPanelActiveKeys\(key: string, open = false\)/.test(src)) {
    bad.push("deckPanelActiveKeys 不接受「跳转时打开」这个入参");
  }
  if (!/DECK_FOLDED_BY_DEFAULT && !open \? \[\] : \[key\]/.test(src)) bad.push("课件默认折叠的口径被改了");
  if (!/entries\.forEach\(\(text, i\)/.test(src)) bad.push("渲染树没有按「课件第一块」重排（验收证据会与页面不符）");
  return bad;
}

/* ============================ ⑤ rafFrame：无 DOM 环境要立刻回落（不能把门禁卡住） ============================ */

const t0 = Date.now();
await rafFrame();
check(Date.now() - t0 < 1000, "rafFrame 在没有 requestAnimationFrame 的环境里卡住了");

/* ============================ 输出 ============================ */

if (failures.length) {
  console.error("任务 114 断言失败：");
  for (const f of failures) console.error("  ✗ " + f);
  process.exit(1);
}
console.log("前端预览页「跳转能到」断言通过：点章节头的「课件（N）」标签 ⇒ **先展开（章卡 + 课件面板）→ 等一帧 → 按展开后的位置滚**（折叠态的落点 1200 拿不到分，用的是展开后的 1680）；已展开时不重复展开、拿不到落点时不乱滚；章卡内**课件排第一块**；章节头 4 个「只看」标签都走同一套跳转；变异「去掉跳转时展开」⇒ 这套断言点名报红。");
