/**
 * 任务 86 §B1/B2/B3 前端断言：**口径看得见**（0 LLM）。
 *
 * 为什么要有这条：用户 2026-09-23 的原话是「点了场景**没看到哪里有变化**」。根因不是场景没生效，
 * 而是**没有任何地方**记着「这一篇是按什么口径生成的」—— 内容条目 `meta` 里没这项、笔记库
 * frontmatter 里也没有、界面上更没有徽标。本脚本钉住这套「可见性」，共七组：
 *
 * ① **后端口径标记**：`scenario.py` 的 `run_meta` 未选场景**不写** `scenario_used`（老课行为不变）、
 *    `badge_of` 没有标记时**只能**回「通用(未标记)」（函数体里不许出现任何具体场景 key = 不许猜）；
 * ② **三处生成点都写标记**：讲义（`lecture.py`，带 `generated_at` + 版本）/ 课件（`ppt.py`）/
 *    大纲（`planner.py`，只记场景不硬造版本号）；
 * ③ **笔记库 frontmatter**：`kb.py` 的**三类页**（讲义 / 教案 / 学案）都带 `scenario` + `prompt_version`；
 * ④ **界面徽标**：`ScenarioBadge` 存在且挂到**三处**（第 3 步讲义卡 / 第 4 步预览页 / 课件卡），
 *    hover 有「口径 + 提示词版本 + 生成时间」；老内容 ⇒ 「通用(未标记)」（行为级）；
 * ⑤ **界面说清「会变 / 不会变」**：选择器下方那段 + `lms` 旁那句 + 第 3 步「改了要重新生成才生效」；
 * ⑥ **成本提示**：`lib/genCost.ts` 是**唯一出处**（别处不许再写 `0.033`），且两处按钮都带上它；
 * ⑦ **变异自检**：以上每条都真的能变红（含 2 条行为级 + 1 条「让未标记显示成具体场景」）。
 * ⑧ **86b 补的护栏**：`frontmatter_fields` **返回了什么键**也要钉住 —— 复验 86 时 DSH 的 M2 变异
 *    （把它的函数体整个换成 `return []`）当时三支断言全绿；现在这条断言 + 对应变异都在（行为级的
 *    真 import 另见 `check-scenario-frontmatter.ts`）。
 *
 * 运行：`node scripts/check-scenario-visibility.ts`（Node >= 22.18 自带类型擦除；86b 起同级还有
 * `check-scenario-frontmatter.ts` 做行为级真 import）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

/** 扫「写法」时先去掉注释（与 83/84/85 同款：注释里可以写旧写法）。 */
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

/** Python 侧只去掉「整行 # 注释」。
 *
 * ⚠️ **不要**拿上面那套 `/* ... *\/` 去扫 Python：`delivery.py` 的 docstring 里写着
 * ``讲义/*.html``，会被当成块注释开头，一路吃到下一个 `*\/` —— 实测会把整个文件吃掉
 * 78%（74273 → 21821 字节），所有判定静默失效。 */
const stripPyComments = (s: string) => s.replace(/^[ \t]*#[^\n]*$/gm, "");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

const REL_PY = "backend/app/services/scenario.py";
const REL_LECTURE = "backend/app/services/lecture.py";
const REL_PPT = "backend/app/services/ppt.py";
const REL_PLANNER = "backend/app/services/planner.py";
const REL_KB = "backend/app/services/kb.py";
const REL_DELIVERY = "backend/app/services/delivery.py";
const REL_TS = "frontend/src/lib/scenarios.ts";
const REL_BADGE = "frontend/src/components/ScenarioBadge.tsx";
const REL_PICKER = "frontend/src/components/ScenarioPicker.tsx";
const REL_WORKBENCH = "frontend/src/components/steps/WorkbenchStep.tsx";
const REL_PREVIEW = "frontend/src/components/steps/PreviewStep.tsx";
const REL_PPT_PANEL = "frontend/src/components/PptPanel.tsx";
const REL_GENCOST = "frontend/src/lib/genCost.ts";
const REL_ANCHOR = "frontend/src/components/StyleAnchorPanel.tsx";

/* ============================ ① 后端：口径标记的口径 ============================ */

/** `def NAME` 到下一个顶层 `def` / 文件尾（用于只扫某个函数的函数体）。 */
function bodyOf(source: string, name: string): string {
  const start = source.indexOf(`def ${name}(`);
  if (start < 0) return "";
  const rest = source.slice(start + 1);
  const next = rest.indexOf("\ndef ");
  return next < 0 ? rest : rest.slice(0, next);
}

/** 把某个函数的**整个函数体**换成 `return []`（复验 86 的 M2 变异；任务 86b 拿它当反证）。 */
function emptyFunctionBody(source: string, name: string): string {
  const body = bodyOf(source, name);
  if (!body) return source;
  const signature = body.slice(0, body.indexOf("\n"));
  return source.replace(body, `${signature}\n    return []\n`);
}

function auditPyMarkers(source: string): string[] {
  const bad: string[] = [];
  for (const needle of [
    'SCENARIO_USED_KEY = "scenario_used"',
    'PROMPT_VERSION_KEY = "prompt_version"',
    'UNMARKED_BADGE = "通用(未标记)"',
    'UNMARKED_LABEL = "通用"',
    'UNMARKED_VERSION = "未记录"',
  ]) {
    if (!source.includes(needle)) bad.push(`scenario.py 缺常量 ${needle}`);
  }
  for (const fn of ["run_meta", "used_meta", "badge_of", "prompt_version_of",
    "frontmatter_fields", "tally"]) {
    if (!source.includes(`def ${fn}(`)) bad.push(`scenario.py 缺函数 def ${fn}(`);
  }

  const runMeta = bodyOf(source, "run_meta");
  if (!runMeta) {
    bad.push("scenario.py 取不到 run_meta 的函数体（断言失效）");
  } else {
    // 未选场景 ⇒ **不写** scenario_used（老课生成口径与从前一字不差）
    if (!/if block:\s*\n\s+out\[SCENARIO_USED_KEY\] = block/.test(runMeta)) {
      bad.push("run_meta 没有写成「未选场景就不写 scenario_used」（缺 if block: 守卫）");
    }
    // 没有版本的产物（口播稿 / 大纲）不许硬造版本号
    if (!/if prompt_version:\s*\n\s+out\[PROMPT_VERSION_KEY\]/.test(runMeta)) {
      bad.push("run_meta 没有写成「没有提示词版本就不写 prompt_version」");
    }
  }

  const badgeOf = bodyOf(source, "badge_of");
  if (!badgeOf) {
    bad.push("scenario.py 取不到 badge_of 的函数体（断言失效）");
  } else {
    if (!/or UNMARKED_BADGE/.test(badgeOf)) {
      bad.push("badge_of 没有回落到 UNMARKED_BADGE（老内容会显示成空白徽标）");
    }
    // **不许猜**：badge_of 的函数体里一旦出现具体场景 key，就等于把老内容默认成某个场景
    const guessed = ["classroom", "live", "recorded", "self_study", "lms"]
      .filter((key) => badgeOf.includes(`"${key}"`));
    if (guessed.length) {
      bad.push(`badge_of 里出现了具体场景 key（把未标记的老内容猜成某个场景）：${guessed.join(",")}`);
    }
  }

  // 任务 86b：钉住 frontmatter_fields **到底返回了什么键**。
  // 只钉「函数存在」不够 —— 函数体清空成 `return []` 时，笔记 frontmatter 会静默丢掉两个字段。
  const frontmatter = bodyOf(source, "frontmatter_fields");
  if (!frontmatter) {
    bad.push("scenario.py 取不到 frontmatter_fields 的函数体（断言失效）");
  } else {
    for (const key of ['("scenario"', '("prompt_version"']) {
      if (!frontmatter.includes(key)) {
        bad.push(`frontmatter_fields 的函数体里没有 ${key}（返回键名没被钉住 ⇒ 笔记 frontmatter 会缺字段）`);
      }
    }
    for (const fallback of ["UNMARKED_LABEL", "UNMARKED_VERSION"]) {
      if (!frontmatter.includes(fallback)) {
        bad.push(`frontmatter_fields 没有回落到 ${fallback}（老内容没有口径时会写出空值）`);
      }
    }
  }
  return bad;
}

/** 三处生成点都要写标记（否则界面徽标永远是「通用(未标记)」）。 */
function auditGenerators(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const lecture = stripPyComments(files.get(REL_LECTURE) ?? "");
  if (!/scenario\.run_meta\(course, PROMPT_TEMPLATE_VERSION\)/.test(lecture)) {
    bad.push("lecture.py 生成讲义时没写当次口径（scenario.run_meta(course, PROMPT_TEMPLATE_VERSION)）");
  }
  if (!/item\.meta = \{/.test(lecture) || !/generated_at/.test(lecture)) {
    bad.push("lecture.py 没有把生成时间写进条目 meta（徽标 hover 的「生成时间」无从取）");
  }
  const ppt = stripPyComments(files.get(REL_PPT) ?? "");
  if (!/scenario\.run_meta\(course, PROMPT_TEMPLATE_VERSION\)/.test(ppt)) {
    bad.push("ppt.py 生成课件时没写当次口径（徽标会一直显示「通用(未标记)」）");
  }
  const planner = stripPyComments(files.get(REL_PLANNER) ?? "");
  if (!/scenario\.run_meta\(course\)/.test(planner)) {
    bad.push("planner.py 生成大纲时没写当次口径");
  }
  if (/scenario\.run_meta\(course,\s*PROMPT_TEMPLATE_VERSION\)/.test(planner)) {
    bad.push("planner.py 给大纲硬塞了讲义模板版本号（大纲没有自己的版本常量，不许硬造）");
  }
  return bad;
}

/** ③ 笔记库 frontmatter：讲义 / 教案 / 学案三类页都要带这两个字段。 */
function auditKbFrontmatter(source: string): string[] {
  const bad: string[] = [];
  const hits = (source.match(/scenario\.frontmatter_fields\(/g) ?? []).length;
  if (hits < 3) {
    bad.push(`kb.py 只有 ${hits} 处 frontmatter 带口径字段（讲义 / 教案 / 学案三类页都要带，应 >= 3）`);
  }
  // 教案与学案的 tag 是它们自己的（coursegenie 教案 / 学案），确认这两个 payload 也在列
  for (const tag of ["coursegenie 教案", "coursegenie 学案", "coursegenie 知识点"]) {
    const at = source.indexOf(tag);
    if (at < 0) { bad.push(`kb.py 找不到 ${tag} 的 frontmatter（断言失效）`); continue; }
    const window = source.slice(at, at + 900);
    if (!/scenario\.frontmatter_fields\(/.test(window)) {
      bad.push(`${tag} 的 frontmatter 没带 scenario / prompt_version（导出后看不出生成口径）`);
    }
  }
  return bad;
}

/** 交付说明里那行「本包各篇的口径」。 */
function auditVoiceTally(source: string): string[] {
  const bad: string[] = [];
  if (!/plan\["voice_tally"\] = scenario\.tally\(/.test(source)) {
    bad.push("delivery.py 没有把「本包各篇的口径」汇总写进 plan（交付说明无从渲染）");
  }
  if (!/plan\.get\("voice_tally"\)/.test(source)) {
    bad.push("delivery.py 的 _readme_lines 没有读 voice_tally（算了却没写进交付说明）");
  }
  return bad;
}

/* ============================ ② 界面：徽标 + 文案 + 成本 ============================ */

function auditUi(files: Map<string, string>): string[] {
  const bad: string[] = [];

  const badge = stripComments(files.get(REL_BADGE) ?? "");
  if (!/data-testid="scenario-badge"/.test(badge)) {
    bad.push("ScenarioBadge 缺 data-testid=\"scenario-badge\"（走查探针要抓它）");
  }
  for (const field of ["生成时的口径", "提示词版本", "生成时间"]) {
    if (!badge.includes(field)) bad.push(`ScenarioBadge 的 hover 少了「${field}」`);
  }
  if (!/scenarioBadgeOf\(/.test(badge)) bad.push("ScenarioBadge 没走 scenarioBadgeOf（老内容就不会回落成「通用(未标记)」）");

  // 徽标落点：第 3 步讲义卡 / 第 4 步预览页（讲义 + 课件）/ 课件卡
  const spots: [string, string, RegExp][] = [
    [REL_WORKBENCH, "第 3 步讲义卡", /<ScenarioBadge meta=\{lecture\.meta/],
    [REL_PREVIEW, "第 4 步预览页讲义卡", /<ScenarioBadge meta=\{lec\.meta/],
    [REL_PREVIEW, "第 4 步预览页课件卡", /<ScenarioBadge meta=\{deck\.meta\}/],
    [REL_PPT_PANEL, "课件卡片", /<ScenarioBadge meta=\{deck\.meta\}/],
  ];
  for (const [rel, why, probe] of spots) {
    if (!probe.test(stripComments(files.get(rel) ?? ""))) bad.push(`${why}没有挂口径徽标`);
  }

  const picker = stripComments(files.get(REL_PICKER) ?? "");
  if (!/data-testid="scenario-scope-note"/.test(picker)) bad.push("场景选择器下方缺「会变 / 不会变」说明块");
  if (!/data-testid="lms-note"/.test(picker)) bad.push("lms 选项旁缺那句话（它只管出包形态、不改内容）");
  for (const phrase of ["会影响", "不会影响", "必须重新生成才会变"]) {
    if (!picker.includes(phrase)) bad.push(`场景选择器说明里缺「${phrase}」`);
  }

  const workbench = stripComments(files.get(REL_WORKBENCH) ?? "");
  if (!/data-testid="scenario-regen-note"/.test(workbench)) {
    bad.push("第 3 步正文页缺那一行「改了使用场景后，需要重新生成才会生效」");
  }

  const gencost = stripComments(files.get(REL_GENCOST) ?? "");
  if (!/LECTURE_COST_YUAN/.test(gencost) || !/estimateCostText/.test(gencost)) {
    bad.push("genCost.ts 缺单价常量 / estimateCostText（成本文案的唯一出处）");
  }
  const anchor = stripComments(files.get(REL_ANCHOR) ?? "");
  if (!/estimateCostText/.test(anchor)) bad.push("StyleAnchorPanel 的重跑按钮没带预估花费");
  if ((anchor.match(/estimateCostText\(/g) ?? []).length < 3) {
    bad.push("StyleAnchorPanel 只在部分按钮上标了花费（两个按钮 + 弹窗都要标）");
  }
  const wb = stripComments(files.get(REL_WORKBENCH) ?? "");
  if (!/LECTURE_COST_ONE/.test(wb)) bad.push("第 3 步生成按钮没标单篇花费（LECTURE_COST_ONE）");

  // 任务 86 血泪教训：批量按钮的文案是**模板字符串**，里面必须写 `${costOf(...)}`。
  // 我第一版写成 `{costOf(batchArticles)}`（JSX 花括号）⇒ 反引号里是字面量，
  // 界面上真的印出了「按章重做全部（3 章{costOf(batchArticles)}）」——
  // 源码级计数看不出来，是 UI 走查抓到的。所以这里把两条文案逐字钉住。
  for (const label of ["按章重做全部", "按章生成剩余全部"]) {
    const ok = new RegExp(
      "`" + label + "（\\$\\{batchQueue\\.length\\} 章\\$\\{costOf\\(batchArticles\\)\\}）`",
    ).test(anchor);
    if (!ok) bad.push(`${label} 的文案没写对（模板字符串里必须写 \${costOf(batchArticles)}，不是 {costOf(...)}）`);
  }
  return bad;
}

/** 成本口径唯一出处：除了 genCost.ts，谁都不许再写 0.033。 */
function auditCostSingleSource(files: Map<string, string>): string[] {
  const bad: string[] = [];
  for (const [rel, src] of files) {
    if (rel === REL_GENCOST || !rel.endsWith(".ts") && !rel.endsWith(".tsx")) continue;
    if (/0\.033/.test(stripComments(src))) bad.push(`${rel} 自己写了一遍 0.033（成本口径必须只有 genCost.ts 一个出处）`);
  }
  return bad;
}

/* ============================ 行为级：前端读数 ============================ */

type Scenario = { primary: string; selected: string[]; updated_at?: string };
type Mod = {
  SCENARIO_UNMARKED_BADGE: string;
  scenarioBadge: (key?: string | null) => string;
  scenarioBadgeOf: (meta?: Record<string, unknown> | null) => string;
  usedScenarioOf: (meta?: Record<string, unknown> | null) => Scenario | null;
  promptVersionOf: (meta?: Record<string, unknown> | null) => string;
  generatedAtOf: (meta?: Record<string, unknown> | null) => string;
  sameScenario: (a?: Scenario | null, b?: Scenario | null) => boolean;
};

const UNMARKED = "通用(未标记)";

function auditBehavior(mod: Mod): string[] {
  const bad: string[] = [];
  if (mod.SCENARIO_UNMARKED_BADGE !== UNMARKED) {
    bad.push(`未标记的徽标文案被改了：${mod.SCENARIO_UNMARKED_BADGE}`);
  }
  // **老内容一律「通用(未标记)」**：不许猜、不许默认成某个场景
  for (const empty of [undefined, null, {}, { scenario_used: null }, { scenario_used: {} },
    { scenario_used: { selected: [] } }, { scenario_used: { selected: ["不认识"] } }]) {
    if (mod.scenarioBadgeOf(empty) !== UNMARKED) {
      bad.push(`没有口径标记的内容必须显示「${UNMARKED}」：${JSON.stringify(empty)} ⇒ ${mod.scenarioBadgeOf(empty)}`);
    }
  }
  // 有标记 ⇒ 主场景短标
  if (mod.scenarioBadgeOf({ scenario_used: { primary: "recorded", selected: ["recorded"] } }) !== "录课向") {
    bad.push("录课向的内容没显示成「录课向」");
  }
  if (mod.scenarioBadgeOf({ scenario_used: { primary: "classroom", selected: ["classroom", "live"] } }) !== "课堂向") {
    bad.push("徽标必须取**主场景**的短标（多选时不许取别的）");
  }
  if (mod.scenarioBadge("nope") !== "") bad.push("未知场景 key 的短标必须是空串（调用方据此回落）");
  // 脏数据自洽修正：primary ∉ selected ⇒ 退回 selected[0]
  const messy = mod.usedScenarioOf({
    scenario_used: { primary: "lms", selected: ["recorded", "nope", "recorded"] },
  }) as Scenario;
  if (!messy || messy.primary !== "recorded" || messy.selected.join(",") !== "recorded") {
    bad.push(`usedScenarioOf 没有自洽修正（去重 / 只留合法 key / primary ∈ selected）：${JSON.stringify(messy)}`);
  }
  if (mod.promptVersionOf({}) !== "" || mod.promptVersionOf({ prompt_version: "lecture-v14" }) !== "lecture-v14") {
    bad.push("promptVersionOf 读数不对（缺项要回空串）");
  }
  if (mod.generatedAtOf({}) !== "" || mod.generatedAtOf({ generated_at: "2026-09-23T09:46:21+00:00" }) !== "2026-09-23T09:46:21+00:00") {
    bad.push("generatedAtOf 读数不对（缺项要回空串）");
  }
  const r: Scenario = { primary: "recorded", selected: ["recorded"] };
  if (!mod.sameScenario(null, null)) bad.push("两边都没选场景时也应算「口径一致」");
  if (!mod.sameScenario(r, { ...r })) bad.push("同场景应算「口径一致」");
  if (mod.sameScenario(null, r) || mod.sameScenario(r, { primary: "classroom", selected: ["classroom"] })) {
    bad.push("口径不同却算成「一致」（会让用户以为不用重跑）");
  }
  return bad;
}

/* ============================ 走查 ============================ */

const files = new Map<string, string>();
for (const rel of [REL_PY, REL_LECTURE, REL_PPT, REL_PLANNER, REL_KB, REL_DELIVERY,
  REL_TS, REL_BADGE, REL_PICKER, REL_WORKBENCH, REL_PREVIEW, REL_PPT_PANEL,
  REL_GENCOST, REL_ANCHOR]) {
  files.set(rel, read(rel));
}

const pySource = files.get(REL_PY) ?? "";

failures.push(...auditPyMarkers(pySource));
failures.push(...auditGenerators(files));
failures.push(...auditKbFrontmatter(files.get(REL_KB) ?? ""));
failures.push(...auditVoiceTally(files.get(REL_DELIVERY) ?? ""));
failures.push(...auditUi(files));
failures.push(...auditCostSingleSource(files));

const real = await import(pathToFileURL(join(repo, REL_TS)).href) as Mod;
failures.push(...auditBehavior(real));

/* ============================ ⑦ 变异自检 ============================ */

const tmpDir = mkdtempSync(join(tmpdir(), "cg86-visibility-"));
let mutationChecked = 0;

async function behaviorOfSource(source: string): Promise<string[]> {
  const file = join(tmpDir, `mutated-${Math.random().toString(36).slice(2)}.ts`);
  writeFileSync(file, source, "utf8");
  const mod = await import(pathToFileURL(file).href) as Mod;
  return auditBehavior(mod);
}

try {
  const behaviorMutations: [string, string, (src: string) => string][] = [
    ["让「未标记」的老内容显示成具体场景（classroom）",
      files.get(REL_TS) ?? "",
      (src) => src.replace(
        '(used ? scenarioBadge(used.primary) : "") || SCENARIO_UNMARKED_BADGE;',
        '(used ? scenarioBadge(used.primary) : "") || "课堂向";')],
    ["让徽标取任一场景而不是主场景",
      files.get(REL_TS) ?? "",
      (src) => src.replace(
        'return (used ? scenarioBadge(used.primary) : "")',
        'return (used ? scenarioBadge(used.selected[used.selected.length - 1]) : "")')],
  ];
  for (const [name, base, mutate] of behaviorMutations) {
    mutationChecked += 1;
    const mutated = mutate(base);
    if (mutated === base) {
      failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
    } else if ((await behaviorOfSource(mutated)).length === 0) {
      failures.push(`自检失败：变异「${name}」后行为断言仍然通过`);
    }
  }

  const withFile = (rel: string, src: string) => new Map(files).set(rel, src);

  const sourceMutations: [string, string][] = [
    ["删掉讲义页 frontmatter 的 scenario / prompt_version 字段",
      auditKbFrontmatter(
        (files.get(REL_KB) ?? "").replace(
          '                       *scenario.frontmatter_fields(lecture.meta if lecture is not None else {})]),',
          "",
        ),
      ).join("")],
    ["让 run_meta 无论选没选场景都写 scenario_used",
      auditPyMarkers(
        (files.get(REL_PY) ?? "").replace("    if block:\n        out[SCENARIO_USED_KEY] = block",
          "    out[SCENARIO_USED_KEY] = block or {}"),
      ).join("")],
    ["让 badge_of 缺省猜成 classroom",
      auditPyMarkers(
        (files.get(REL_PY) ?? "").replace(
          'return badge(used.get("primary") if used else "") or UNMARKED_BADGE',
          'return badge(used.get("primary") if used else "") or badge("classroom")'),
      ).join("")],
    ["把 frontmatter_fields 的函数体清空成 return []（复验 86 时这个变异曾经全绿）",
      auditPyMarkers(emptyFunctionBody(files.get(REL_PY) ?? "", "frontmatter_fields")).join("")],
    ["给 planner 硬塞讲义模板版本号",
      auditGenerators(withFile(REL_PLANNER,
        (files.get(REL_PLANNER) ?? "").replace("scenario.run_meta(course)",
          "scenario.run_meta(course, PROMPT_TEMPLATE_VERSION)"))).join("")],
    ["摘掉课件卡上的徽标",
      auditUi(withFile(REL_PPT_PANEL,
        (files.get(REL_PPT_PANEL) ?? "").replace("<ScenarioBadge meta={deck.meta} />", ""))).join("")],
    ["摘掉第 3 步那行「必须重新生成才会变」",
      auditUi(withFile(REL_WORKBENCH,
        (files.get(REL_WORKBENCH) ?? "").replace('data-testid="scenario-regen-note"', ""))).join("")],
    ["把批量按钮的模板插值写回 JSX 花括号（界面上会印出 {costOf(...)}）",
      auditUi(withFile(REL_ANCHOR,
        (files.get(REL_ANCHOR) ?? "").replace("${costOf(batchArticles)}", "{costOf(batchArticles)}"))).join("")],
    ["摘掉选择器下方的「会变 / 不会变」说明",
      auditUi(withFile(REL_PICKER,
        (files.get(REL_PICKER) ?? "").replace('data-testid="scenario-scope-note"', ""))).join("")],
    ["在别的文件里再写一遍 0.033",
      auditCostSingleSource(withFile(REL_PREVIEW,
        `${files.get(REL_PREVIEW) ?? ""}\nconst LECTURE_PRICE = 0.033;\n`)).join("")],
    ["让交付说明不写「本包各篇的口径」",
      auditVoiceTally(
        (files.get(REL_DELIVERY) ?? "").replace('plan["voice_tally"] = scenario.tally(', 'plan["voice_tally_x"] = scenario.tally('),
      ).join("")],
    ["让 kb.py 只给讲义页写口径（学案页漏掉）",
      auditKbFrontmatter(
        (files.get(REL_KB) ?? "").replace(
          '("tags", "coursegenie 学案"),\n                                     *scenario.frontmatter_fields(lecture.meta)]), body])',
          '("tags", "coursegenie 学案")]), body])',
        ),
      ).join("")],
  ];
  for (const [name, red] of sourceMutations) {
    mutationChecked += 1;
    if (red.length === 0) failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「口径可见」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端口径可见断言（86）通过："
  + "① 后端 run_meta 未选场景不写 scenario_used、没有版本不写 prompt_version；badge_of 只回落「通用(未标记)」不猜场景；"
  + "② 三处生成点都写标记（讲义带 generated_at + 版本 / 课件 / 大纲不硬造版本号）；"
  + "③ 笔记库讲义 + 教案 + 学案三类页 frontmatter 都带 scenario / prompt_version；"
  + "③b frontmatter_fields 的返回键名被钉住（函数体清空成 return [] ⇒ 变红）；"
  + `④ 徽标挂到三处（第 3 步讲义卡 / 第 4 步预览页 / 课件卡）且 hover 含口径 + 版本 + 生成时间；`
  + "⑤ 选择器「会变 / 不会变」+ lms 标注 + 第 3 步「必须重新生成才会变」都在；"
  + `⑥ 成本口径唯一出处 genCost.ts（0.033 别处不再出现）且两处按钮都带上；`
  + `${mutationChecked} 条变异自检全红（含 2 条行为级：改写读数规则后重新加载模块）；`);
