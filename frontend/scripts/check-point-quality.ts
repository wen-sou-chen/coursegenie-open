/**
 * 任务 75 前端断言：素材要点抽取的「质量」与「呈现」收敛在界面上真的接上了。
 *
 * 起因（DSH 2026-09-21 复验任务 70 时实测，真素材 AI 课 6 份 12,878 字）：机制全对，但抽出来的
 * 要点本身有脏数据 —— 60 条里 26 条带 `**` / `[[wikilink]]` / 反引号并原样进建议文案；10 条「案例」
 * 抽的是 `### 案例 N：X` 小标题（分类名）；未覆盖 53 条直接铺开，还给出
 * 「建议新增知识点《用户身份》」「《任务目标等）》」这种不可用的建议。
 *
 * 后端行为由 `backend/tests/test_point_quality.py` 断言（去装饰 / 限长 / 碎片过滤 / 案例类取消 /
 * filtered_out_count / 老口径不回退）；这里做**源码级**接线断言，防止「后端修好了、界面还是老样子」。
 *
 * 运行：`node scripts/check-point-quality.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const failures: string[] = [];

function read(rel: string): string {
  return readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");
}

const planner = readFileSync(join(repo, "backend", "app", "services", "planner.py"), "utf8");
const outline = read("src/components/OutlinePanel.tsx");
const types = read("src/types.ts");

/** ① 抽点质量：去装饰 + 限长 24 + 尾部括号不成对丢弃（任务 75 §B1）。 */
const MAX_LEN = /^POINT_MAX_LEN = 24$/m;
const MIN_LEN = /^POINT_DETAIL_MIN_LEN = 4$/m;
const MARK = /_RE_POINT_MARK = re\.compile\(r"\[\*`\]\+"\)/;
const WIKILINK = /_RE_WIKILINK_PLAIN = re\.compile/;
const REF_NOTE = /_RE_REF_NOTE = re\.compile/;
const REF_HINT = /_RE_REF_HINT = re\.compile\(r"→\|参见\|结合"\)/;
const BALANCED = /def _balanced_brackets\(text: str\) -> bool:/;
const BALANCED_REJECT = /if not _balanced_brackets\(text\):\n\s+return ""/;
const REJECTS = /if len\(text\) < 2 or len\(text\) > POINT_MAX_LEN:\n\s+return ""/;
for (const [name, pattern, why] of [
  ["长度上限 24（POINT_MAX_LEN）", MAX_LEN, "要点可以长到 40 字，仍会把整句当要点"],
  ["明细最小长度 4（POINT_DETAIL_MIN_LEN）", MIN_LEN, "碎片（「用户身份」这类）会进建议清单"],
  ["_RE_POINT_MARK（去 `**` / 反引号）", MARK, "装饰会原样进建议文案"],
  ["_RE_WIKILINK_PLAIN（[[…]] 只留可见文字）", WIKILINK, "wikilink 会原样进建议文案"],
  ["_RE_REF_NOTE / _RE_REF_HINT（去行内尾注）", REF_NOTE, "`（→ 参见 [[PE_xxx]]）` 会被当要点"],
  ["行内尾注特征判定", REF_HINT, "看不出括号注释是不是交叉引用"],
  ["_balanced_brackets（不成对括号丢弃）", BALANCED, "`任务目标等）` 这类碎片会被当要点"],
  ["超长 / 过短直接丢弃的分支", REJECTS, "限长只写在注释里、没有真的拦住"],
] as [string, RegExp, string][]) {
  if (!pattern.test(planner)) failures.push(`planner.py 缺少${name}（${why}）`);
}

/** ② 案例类取消（任务 75 §B2①）：`### 案例 N：X` 小标题不再抽成分类名。 */
if (/_RE_CASE_SENT|_RE_CASE_LEAD/.test(planner)) {
  failures.push("planner.py 里案例类正则还在（小标题仍会被抽成「事实性治理 / 教育场景」）");
}
const sentenceBody = planner.slice(planner.indexOf("def _sentence_point_rows("),
                                     planner.indexOf("def _balanced_brackets("));
if (/"case"/.test(sentenceBody) || /accept\(case\./.test(sentenceBody)) {
  failures.push("_sentence_point_rows 仍然在抽案例类（分类名会进未覆盖清单）");
}

/** ③ 未覆盖明细收敛：filtered_out_count 如实返回 + 建议文案模板统一（任务 75 §B3）。 */
for (const field of ["filtered_out_count", "filtered_out_samples", "material_uncovered_shown_count"]) {
  if (!new RegExp(`"${field}":`).test(planner)) {
    failures.push(`planner.py 没有返回 ${field}（过滤掉的条目静默消失）`);
  }
}
const NEW_JOIN = /return f"建议并入第 \{best_key\} 章「\{best_title\}」"/;
const NEW_ADD = /return f"建议新增知识点《\{point\}》"/;
if (!NEW_JOIN.test(planner)) failures.push("建议文案没有统一成「建议并入第 N 章「章名」」");
if (!NEW_ADD.test(planner)) failures.push("建议文案没有统一成「建议新增知识点《X》」");
if (/建议合并到第|与本章主题最接近|没有主题相近的章可以挂靠/.test(planner)) {
  failures.push("建议文案里仍留着旧的长尾巴（模板没统一）");
}
// 单独一条：未覆盖提示的首行也不许带 Markdown 装饰（修前是 `**没覆盖**：`，在卡片里原样显示）
const NOTICE_PLAIN = /个；没覆盖：\{shown\}/;
const NOTICE_MARKED = /\*\*/;
if (!NOTICE_PLAIN.test(planner)) {
  failures.push("未覆盖提示里没有「个；没覆盖：」这句（模板被改动了）");
}
const noticeBlock = planner.slice(planner.indexOf('if not coverage["points"]:'),
                                 planner.indexOf('"material_notice": material_notice'))
  // 注释里出现的 `**`（如「每条的**可操作建议**」）不算文案，先掐掉行内注释
  .split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");
if (NOTICE_MARKED.test(noticeBlock)) {
  failures.push("未覆盖提示里仍带 `**`（会原样显示在卡片首行）");
}

/** ④ 前端呈现：默认前 8 条 + 展开看全部 + 按类型分组 + 过滤条数如实显示。 */
const m = outline.match(/const UNCOVERED_DEFAULT_VISIBLE = (\d+);/);
if (!m) failures.push("OutlinePanel 没有 UNCOVERED_DEFAULT_VISIBLE（清单会一次全铺开）");
else if (Number(m[1]) !== 8) failures.push(`未覆盖清单默认条数不是 8（现在是 ${m[1]}）`);
const SLICE = /showAllUncovered \? rows : rows\.slice\(0, UNCOVERED_DEFAULT_VISIBLE\)/;
const EXPAND = /展开看全部（\$\{check\.material_uncovered_detail\.length\} 条）/;
const COLLAPSE = /"收起"/;
const TOGGLE = /setShowAllUncovered\(\(v\) => !v\)/;
const GROUP = /last\.kind === row\.kind/;
const FILTERED = /\(check\.filtered_out_count \?\? 0\) > 0/;
const LOW = /UNCOVERED_LOW_NOTICE/;
for (const [name, pattern, why] of [
  ["默认只渲染前 8 条", SLICE, "53 条会一次性铺满整页"],
  ["「展开看全部(N 条)」入口", EXPAND, "看不到被折叠的条目"],
  ["「收起」入口", COLLAPSE, "展开了收不回去"],
  ["展开态切换", TOGGLE, "按钮点了没反应"],
  ["按类型分组", GROUP, "清单仍然是一锅粥"],
  ["「另有 N 条句子碎片已过滤」", FILTERED, "过滤掉的条目说不清、像丢数据"],
  ["覆盖率低时先给结论", LOW, "一上来就甩几十条"],
] as [string, RegExp, string][]) {
  if (!pattern.test(outline)) failures.push(`OutlinePanel 没有${name}（${why}）`);
}

/** ⑤ types.ts：后端新字段前端读得到。 */
for (const field of ["filtered_out_count", "filtered_out_samples", "material_uncovered_shown_count"]) {
  if (!new RegExp(`^\\s*${field}\\?:`, "m").test(types)) {
    failures.push(`types.ts 里没有声明 ${field}（后端返回了、前端读不到）`);
  }
}

/** ⑥ 后端测试真的存在（去装饰 / 限长 / 案例取消 / filtered_out_count）。 */
const testPath = join(repo, "backend", "tests", "test_point_quality.py");
if (!existsSync(testPath)) {
  failures.push("backend/tests/test_point_quality.py 不存在（要点质量没有回归测试）");
} else {
  const testSrc = readFileSync(testPath, "utf8");
  for (const needle of ["test_points_are_plain_labels_without_markdown_decoration",
                        "test_points_are_short_labels_not_sentences",
                        "test_case_class_is_not_extracted_from_markdown_headings",
                        "test_uncovered_detail_reports_filtered_out_count",
                        "filtered_out_count"]) {
    if (!testSrc.includes(needle)) failures.push(`test_point_quality.py 缺少 ${needle} 相关断言`);
  }
}

// 判定自检：把关键接线抹掉后必须变红 —— 否则这些断言等于永远绿灯
if (MAX_LEN.test(planner.replace("POINT_MAX_LEN = 24", "POINT_MAX_LEN = 40"))) {
  failures.push("自检失败：把限长改回 40 后判定仍然通过");
}
if (MARK.test(planner.replace('_RE_POINT_MARK = re.compile(r"[*`]+")', "_RE_POINT_MARK = re.compile('nope')"))) {
  failures.push("自检失败：抹掉去装饰正则后判定仍然通过");
}
if (BALANCED_REJECT.test(planner.replace(/if not _balanced_brackets\(text\):\n\s+return ""/, "pass"))) {
  failures.push("自检失败：抹掉不成对括号判定后判定仍然通过");
}
if (BALANCED.test(planner.replace("def _balanced_brackets(text: str) -> bool:", "def _nope(text: str) -> bool:"))) {
  failures.push("自检失败：抹掉 _balanced_brackets 定义后判定仍然通过");
}
if (!/"case"/.test(sentenceBody + '        accept(case.group("term"), "case")')) {
  failures.push("自检失败：案例类判定的检测逻辑是空的（永远绿灯）");
}
if (SLICE.test(outline.replace(/rows\.slice\(0, UNCOVERED_DEFAULT_VISIBLE\)/g, "rows"))) {
  failures.push("自检失败：抹掉「默认前 8 条」后判定仍然通过");
}
if (FILTERED.test(outline.replace(/\(check\.filtered_out_count \?\? 0\) > 0/g, "false"))) {
  failures.push("自检失败：抹掉过滤条数提示后判定仍然通过");
}
if (GROUP.test(outline.replace(/last\.kind === row\.kind/g, "true"))) {
  failures.push("自检失败：抹掉按类型分组后判定仍然通过");
}
if (NEW_JOIN.test(planner.replace(/建议并入第 \{best_key\} 章「\{best_title\}」/g, "建议合并到第"))) {
  failures.push("自检失败：把建议文案改回旧模板后判定仍然通过");
}
if (NOTICE_PLAIN.test(planner.replace("个；没覆盖：{shown}", "个；**没覆盖**：{shown}"))) {
  failures.push("自检失败：把个；没覆盖： 改回 `**没覆盖**` 后判定仍然通过");
}

if (failures.length > 0) {
  console.error("前端「素材要点质量与呈现」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端要点质量断言通过：planner 去装饰 / 限长 24 / 不成对括号丢弃 / 案例类取消 / "
  + "filtered_out_count 如实返回 / 建议模板统一；OutlinePanel 默认只渲染前 8 条 + 展开看全部 + "
  + "按类型分组 + 过滤条数可见 + 覆盖率低先给结论；types.ts 三个新字段都有声明");