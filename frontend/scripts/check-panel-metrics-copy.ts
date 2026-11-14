/**
 * 任务 81 前端断言：高级工具面板的「数字与呈现」四处修正。
 *
 * 起因（DSH 2026-09-22 走查第 6 步「高级工具 10 面板」，10 个面板 0 崩溃 / 0 控制台错误 /
 * 0 个 4xx，但这四处「数字 / 说法不对」）：
 * ① 「数据分析 → 总字数」不是字数：`analytics.py:36` 用 `content.extract_words()`，
 *    而它是 `len(re.findall(r"[\u4e00-\u9fff\w]+", markdown))` —— **正则词块数**
 *    （中文连成一片只算 1 个）；真库 27 篇讲义实测 6,659，而 markdown 字符数 55,920。
 * ② 版本面板 `2026-09-21T06:24:46.708384`、质量报告 `生成于 2026-09-22T13:56:02+00:00`
 *    两处时间戳没本地化（交付包面板早已是「2026-09-20 15:39」这种人话）。
 * ③ 「大纲规划」空态写「在本页生成一次大纲后…」，与同屏「大纲已通过（锁定）」自相矛盾
 *    （真库 `course.meta.outline_check` 确实为空 —— 空态如实，错的是后面那句解释）。
 * ④ 「质量报告」面板 75/待改进 vs 交付包里的 `00-课程体检报告.md` 90/健康 没有一句解释。
 *
 * 这里跑**源码级**接线断言 + 变异自检，专门防「数字 / 说法」这类回退。
 * 运行：`node scripts/check-panel-metrics-copy.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { formatLocalTime } from "../src/lib/datetime.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
/** 扫「界面文案」时跳过注释（本文件的文档注释里引用了旧说法）。 */
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
/** 扫 Python 源码时还要去掉 `#` 行注释（本文件的注释里引用了旧写法）。 */
const stripPy = (s: string) => s.replace(/^[ \t]*#.*$/gm, "");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ======================= A1. 字数口径（后端 metric + 面板标签） ======================= */
const analyticsPy = read("backend/app/services/analytics.py");
const analyticsTsx = read("frontend/src/components/AnalyticsPanel.tsx");
const typesTs = read("frontend/src/types.ts");

function auditWordCount(py: string): string[] {
  const bad: string[] = [];
  if (/extract_words/.test(stripPy(py))) {
    bad.push("analytics.py 又在用 content.extract_words()（那是正则词块数，不是字数）");
  }
  if (!/def count_words\(/.test(py)) bad.push("analytics.py 没有 count_words()");
  if (!/"chars_total": chars_total/.test(py)) bad.push("接口没有返回 chars_total（口径没法对账）");
  if (!/words_total = sum\(count_words\(/.test(py)) {
    bad.push("words_total 不是用 count_words() 算的");
  }
  if (!/\[\\u4e00-\\u9fff\]/.test(py)) {
    bad.push("count_words 的汉字部分不是逐字算（正则里没有单独的汉字区间）");
  }
  return bad;
}
failures.push(...auditWordCount(analyticsPy));

function auditAnalyticsPanel(tsx: string): string[] {
  const bad: string[] = [];
  if (/title="总字数"/.test(tsx)) bad.push("面板标题又叫回「总字数」了（算法是汉字逐字 + 英文/数字词）");
  if (!/正文字数/.test(tsx)) bad.push("面板标题没写清是「正文字数」");
  if (!/value=\{stats\.words_total\}/.test(tsx)) bad.push("面板没读 words_total");
  if (!/stats\.chars_total/.test(tsx)) bad.push("面板没把 chars_total 显示出来（对账用）");
  if (!/Tooltip/.test(tsx)) bad.push("面板没有口径 Tooltip（汉字逐字 + 英文/数字词）");
  if (!/typeof stats\.chars_total === "number"/.test(tsx)) {
    bad.push("chars_total 没做存在性判断（后端没重启时整个面板会白屏）");
  }
  return bad;
}
failures.push(...auditAnalyticsPanel(analyticsTsx));
check(/chars_total: number/.test(typesTs), "types.ts 没有声明 chars_total");

/* ======================= A2. 时间本地化（唯一实现 + 两处落点） ======================= */
const datetimePath = join(here, "..", "src", "lib", "datetime.ts");
check(existsSync(datetimePath), "缺少 src/lib/datetime.ts（时间格式化的唯一出处）");

/**
 * 与实现**独立**的参照：Intl 的 sv-SE 就是 "YYYY-MM-DD HH:mm"（本地时区）。
 *
 * 任务 83 §B5：**必须显式把裸时间戳按 UTC 定点**再比 —— 原先这里是 `new Date(iso)`，
 * 与「裸串当本地时间」这个**错误假设**共享前提，于是把「按 UTC 正确换算」判成失败。
 * 本仓的裸时间戳全部来自后端 `utcnow()`（SQLite 不存时区）⇒ 参照侧也要按 UTC 补 `Z`。
 */
const expectLocal = (iso: string) =>
  new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(iso) ? iso : `${iso}Z`)
    .toLocaleString("sv-SE", { hour12: false }).slice(0, 16);
const SHAPE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/;
for (const iso of ["2026-09-22T13:56:02+00:00", "2026-09-21T06:24:46.708384",
                   "2026-09-20T15:39:00+00:00"]) {
  const got = formatLocalTime(iso);
  check(SHAPE.test(got), `formatLocalTime("${iso}") 不是本地时间人类可读格式：${got}`);
  check(!/[TZ]/.test(got) && !/\+\d{2}:\d{2}$/.test(got), `formatLocalTime("${iso}") 还带 T / Z / 时区：${got}`);
  check(!/\.\d+/.test(got), `formatLocalTime("${iso}") 还带微秒：${got}`);
  check(got === expectLocal(iso), `formatLocalTime("${iso}") = ${got}，与本地时区渲染 ${expectLocal(iso)} 不一致`);
}
check(formatLocalTime("") === "-" && formatLocalTime(null) === "-", "formatLocalTime 空值兜底不是「-」");
check(formatLocalTime("不是时间") === "不是时间", "formatLocalTime 解析失败时应原样返回，不能变成 NaN");

/* 任务 83 §B5：把「裸 UTC ⇒ 本地」的结果钉在两个具体数字上（数值取自真库那两条记录）。 */
const UTC8_CASES: [string, string][] = [
  ["2026-09-21T06:24:46.708384", "2026-09-21 14:24"],   // 版本面板那条快照：裸 UTC，真实本地是 14:24
  ["2026-09-22T13:56:02+00:00", "2026-09-22 21:56"],   // 质量报告那条：本来就带 +00:00，换算到 UTC+8
];
if (new Date().getTimezoneOffset() === -480) {
  for (const [iso, want] of UTC8_CASES) {
    const got = formatLocalTime(iso);
    check(got === want, `UTC+8 机器上 formatLocalTime("${iso}") 应为 ${want}，实际 ${got}`);
  }
}

const versionsTsx = read("frontend/src/components/VersionsPanel.tsx");
const qualityTsx = read("frontend/src/components/QualityPanel.tsx");

function auditVersions(tsx: string): string[] {
  const bad: string[] = [];
  if (!/from "\.\.\/lib\/datetime"/.test(tsx)) bad.push("VersionsPanel 没引用 datetime.formatLocalTime");
  if (!/render: \(at: string\) => formatLocalTime\(at\)/.test(tsx)) {
    bad.push("VersionsPanel 的「时间」列没走 formatLocalTime（还在原样渲染 ISO）");
  }
  return bad;
}
failures.push(...auditVersions(versionsTsx));

function auditQualityTime(tsx: string): string[] {
  const bad: string[] = [];
  if (!/from "\.\.\/lib\/datetime"/.test(tsx)) bad.push("QualityPanel 没引用 datetime.formatLocalTime");
  if (!/生成于 \{formatLocalTime\(data\.generated_at\)\}/.test(tsx)) {
    bad.push("质量报告的「生成于」没走 formatLocalTime（还在原样渲染 ISO）");
  }
  if (/\{data\.generated_at\}/.test(tsx)) bad.push("QualityPanel 里还有原样渲染的 {data.generated_at}");
  return bad;
}
failures.push(...auditQualityTime(qualityTsx));

/* ======================= A3. 大纲自检空态（分清两种情况 + 立即自检） ======================= */
const outlineTsx = read("frontend/src/components/OutlinePanel.tsx");
const planPy = read("backend/app/api/routes/plan.py");
const apiTs = read("frontend/src/api.ts");

function auditOutlineEmpty(tsx: string): string[] {
  const bad: string[] = [];
  if (/在本页生成一次大纲后/.test(stripComments(tsx))) {
    bad.push("空态又说回「在本页生成一次大纲后」（与「大纲已通过（锁定）」自相矛盾）");
  }
  if (!/"这份大纲还没跑过自检"/.test(tsx)) bad.push("空态没说清「这份大纲还没跑过自检」");
  if (!/立即自检\s*<\/Button>/.test(tsx)) bad.push("空态里没有「立即自检」按钮（点了走 POST /plan/{id}/check）");
  if (!/api\.runPlanCheck\(courseId\)/.test(tsx)) bad.push("「立即自检」没接 api.runPlanCheck");
  if (!/hasOutline \? \(/.test(tsx)) bad.push("空态没有按「有没有大纲」分成两种情况");
  if (!/还没有大纲，所以也没有自检结论/.test(tsx)) bad.push("缺「还没有大纲」那种情况的说法");
  if (!/不重新生成大纲、不调模型/.test(tsx)) bad.push("按钮旁没说清它不重新生成大纲 / 不调模型");
  return bad;
}
failures.push(...auditOutlineEmpty(outlineTsx));

function auditPlanCheckRoute(py: string): string[] {
  const bad: string[] = [];
  if (!/@router\.post\("\/\{course_id\}\/check"\)/.test(py)) bad.push("后端没有 POST /plan/{id}/check（「立即自检」没有落点）");
  if (!/record_outline_check\(db, course, outline_check\(/.test(py)) {
    bad.push("补跑自检没有走既有的 outline_check + record_outline_check");
  }
  if (!/if not nodes:/.test(py) || !/生成大纲/.test(py)) bad.push("没有大纲时没有明确 400 提示");
  return bad;
}
failures.push(...auditPlanCheckRoute(planPy));
check(/runPlanCheck: \(courseId: string\) => json<PlanCheckStored>/.test(apiTs),
  "api.ts 没有 runPlanCheck（POST /plan/{id}/check）");

/* ======================= A4. 质量报告 vs 出包快照的说明 ======================= */
const auditTsx = read("frontend/src/components/AuditPanel.tsx");

function auditSnapshotNote(tsx: string): string[] {
  const bad: string[] = [];
  if (!/现算/.test(tsx)) bad.push("没说明面板数字是「现算」的");
  if (!/出包时的快照/.test(tsx)) bad.push("没说明 00-课程体检报告.md 是「出包时的快照」");
  if (!/00-课程体检报告\.md/.test(tsx)) bad.push("说明里没点出文件名 00-课程体检报告.md");
  if (!/重新出包/.test(tsx)) bad.push("没说明「内容改过后要重新出包 / 重新写入笔记才会同步」");
  if (!/修复不一致（以网页为准）/.test(tsx)) bad.push("不一致 >0 时没有「点『修复不一致（以网页为准）』」那句");
  if (!/inconsistent > 0/.test(tsx)) bad.push("「修复不一致」那句没有挂在 inconsistent > 0 的条件上");
  return bad;
}
failures.push(...auditSnapshotNote(auditTsx));

/* ======================= 反证据：改回旧写法 ⇒ 必须变红 ======================= */
const mutations: [string, string, string, (src: string) => string[]][] = [
  ["把 A1 的算法改回 extract_words",
    analyticsPy,
    analyticsPy.replace("    words_total = sum(count_words(t.body.get(\"markdown\", \"\")) for t in texts)",
      "    words_total = sum(extract_words(t.body.get(\"markdown\", \"\")) for t in texts)"),
    auditWordCount],
  ["把「总字数」这个错标签放回面板",
    analyticsTsx,
    analyticsTsx.replace('title="正文字数（汉字 + 英文/数字词）"', 'title="总字数"'),
    auditAnalyticsPanel],
  ["把面板的 chars_total 显示删掉",
    analyticsTsx,
    analyticsTsx.replace(/\{typeof stats\.chars_total === "number" && \([\s\S]*?\)\}/, "—"),
    auditAnalyticsPanel],
  ["把 A3 空态文案改回旧句",
    outlineTsx,
    outlineTsx.replace('message="这份大纲还没跑过自检"',
      'message="还没有自检记录"').replace(
      /description=\{\s*<Space direction="vertical" size=\{6\}>[\s\S]*?<\/Space>\s*\}/,
      'description="在本页生成一次大纲后，这里会常驻显示「章节数是否兑现 + 素材要点覆盖清单 + 未覆盖建议」。"'),
    auditOutlineEmpty],
  ["把「立即自检」按钮摘掉",
    outlineTsx,
    outlineTsx.replace(/立即自检\s*<\/Button>/, "</Button>"),
    auditOutlineEmpty],
  ["把版本面板的时间列改回原样渲染 ISO",
    versionsTsx,
    versionsTsx.replace(/render: \(at: string\) => formatLocalTime\(at\) \}/, "}"),
    auditVersions],
  ["把质量报告的时间格式化摘掉",
    qualityTsx,
    qualityTsx.replace("生成于 {formatLocalTime(data.generated_at)}", "生成于 {data.generated_at}"),
    auditQualityTime],
  ["把 A4 的说明句删掉",
    auditTsx,
    auditTsx.replace(/本面板的数字（含总分与一致性）是现算的[\s\S]*?才会同步。/, "—")
      .replace(/一致性不一致 \{data\.consistency\.inconsistent\} 项[\s\S]*?自动导出一次笔记。/, "—"),
    auditSnapshotNote],
  ["把后端补跑自检的路由删掉",
    planPy,
    planPy.replace(/@router\.post\("\/\{course_id\}\/check"\)[\s\S]*?return \{\n        "course_id": course\.id,\n        "has_outline": True,\n        \*\*_stored_check_out\(stored_outline_check\(course\) or \{\}\),\n    \}\n/, ""),
    auditPlanCheckRoute],
];
for (const [name, base, mutated, run] of mutations) {
  if (mutated === base) {
    failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
  } else if (run(mutated).length === 0) {
    failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  }
}

if (failures.length > 0) {
  console.error("前端「面板数字与呈现」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端高级工具面板断言（81）通过："
  + `① 字数口径与标签一致（汉字逐字 + 英文/数字词，另有 chars_total 对账；不再用 extract_words 的词块数）；`
  + `② 版本面板 / 质量报告的时间统一走 ${"src/lib/datetime.ts"} 的 formatLocalTime（本地时间 YYYY-MM-DD HH:mm，无 T / 微秒 / +00:00）；`
  + "③ 大纲自检空态分清「没跑过自检」（带「立即自检」按钮，走 POST /plan/{id}/check，不重新生成大纲）与「还没有大纲」；"
  + `④ 质量报告写明「面板现算 vs 00-课程体检报告.md 出包快照」+ 不一致 >0 时的消除办法；`
  + `${mutations.length} 条变异自检全红`);
