/**
 * 任务 85 前端断言：课程「使用场景」（五选多 + 主场景 + 场景进提示词）。
 *
 * 为什么要有这条：场景这件东西**两份表**（界面一份、提示词一份），而且它有一条很容易被写坏的
 * 口径 —— **未选择 = 空**（`meta.scenario` 缺失 / 空 ⇒ 一切与今天逐字一致，**缺省绝不许偷偷补
 * `classroom`**）。一旦有人「顺手给个默认值」，所有老课的行为都会变，而且没有任何测试会喊。
 *
 * 本脚本钉五件事：
 * ① **两侧表逐字一致**：`frontend/src/lib/scenarios.ts` 的 `SCENARIO_TABLE` 与
 *    `backend/app/services/scenario.py` 的 `SCENARIOS`（key 集合 + 显示名 + 一句话说明）；
 *    后端提示词侧的关键要求（6 分钟 / 1400–1800 字 / 不许前文指代 / 不许手势指代）必须还在；
 * ② **空值语义（行为级）**：`normalizeScenario` / `scenarioLine` / `scenarioPayload` 在
 *    未选 / 空 / 脏数据时都回到「未指定场景（按通用口径生成）」，**不补默认场景**；
 * ③ **界面接线**：建课表单有这一项且默认不预选、每项带 tooltip；课程详情页与第 2 步大纲页
 *    **复用同一个组件**；第 3 步与出包页各有那一行「本次按场景生成：…」；
 * ④ **后端护栏**（源码级）：`scenario_prompt_block` 未选返回空串、`scenario_meta` 函数体里
 *    不许出现 `classroom`（缺省补默认值的典型写法）、`PROMPT_TEMPLATE_VERSION` 已升到 v14；
 * ⑤ **变异自检**：上面每一条都真的能变红（含 2 条行为级：改写 `scenarios.ts` 后重新加载模块）。
 *
 * 运行：`node scripts/check-scenario.ts`（Node >= 22.18 自带类型擦除）
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

/** 扫「写法」时先去掉注释（与 83/84 两条断言同款：注释里可以写旧写法）。 */
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

const REL_TS = "frontend/src/lib/scenarios.ts";
const REL_PICKER = "frontend/src/components/ScenarioPicker.tsx";
const REL_LIST = "frontend/src/pages/CourseList.tsx";
const REL_DETAIL = "frontend/src/pages/CourseDetail.tsx";
const REL_PLAN = "frontend/src/components/steps/PlanStep.tsx";
const REL_WORKBENCH = "frontend/src/components/steps/WorkbenchStep.tsx";
const REL_DELIVERY = "frontend/src/components/DeliveryPanel.tsx";
const REL_PY = "backend/app/services/scenario.py";
const REL_LECTURE = "backend/app/services/lecture.py";

const tsSource = read(REL_TS);
const pySource = read(REL_PY);

/* ======================= ① 两侧表逐字一致 + 后端要求还在 ======================= */

/* 任务 86：场景表多了第 4 列（**短标**：徽标与交付说明汇总用）。两侧都要逐字一致 ——
   短标写歪了界面上就会出现两门课两个说法，所以并进同一条比对。 */
type Row = [string, string, string, string];

/** 前端 `["key", "显示名", '一句话说明']`（与后端 `("key", "显示名", '一句话说明')` 同款排版）。 */
function rowsOf(source: string, row: RegExp): Row[] {
  const out: Row[] = [];
  for (const match of source.matchAll(row)) out.push([match[1], match[2], match[3], match[4]]);
  return out;
}

const TS_ROW = /\[\s*"([a-z_]+)"\s*,\s*"([^"]*)"\s*,\s*'([^']*)'\s*,\s*"([^"]*)"\s*\]/g;
const PY_ROW = /\(\s*"([a-z_]+)"\s*,\s*"([^"]*)"\s*,\s*'([^']*)'\s*,\s*"([^"]*)"\s*\)/g;

function auditTables(front: string, back: string): string[] {
  const bad: string[] = [];
  const tsRows = rowsOf(front, TS_ROW);
  const pyRows = rowsOf(back, PY_ROW);
  if (tsRows.length !== 5) bad.push(`前端场景表应有 5 项，实际 ${tsRows.length} 项`);
  if (pyRows.length !== 5) bad.push(`后端场景表应有 5 项，实际 ${pyRows.length} 项`);
  if (JSON.stringify(tsRows) !== JSON.stringify(pyRows)) {
    bad.push(`两侧场景表不一致（前端：${JSON.stringify(tsRows)}；后端：${JSON.stringify(pyRows)}）`);
  }
  if (tsRows.map((row) => row[0]).join(",") !== "classroom,live,recorded,self_study,lms") {
    bad.push(`场景 key 集合 / 顺序不对：${tsRows.map((row) => row[0]).join(",")}`);
  }
  // 任务 86：短标必须每个 key 都有（空短标会让徽标变成一张空白标签），且两侧逐字一致 ——
  // 上面的 JSON 比对已经覆盖内容，这里再单独挡一次「某一侧空着」。
  const blankBadge = tsRows.filter((row) => !row[3]).map((row) => row[0]);
  if (blankBadge.length) bad.push(`这些场景缺短标（徽标会是空标签）：${blankBadge.join(",")}`);
  return bad;
}

function auditPyRequirements(source: string): string[] {
  const bad: string[] = [];
  const musts: [string, string][] = [
    ["上节课讲过", "self_study 必须点名禁止「上节课讲过」（验收 C3 的正则扫这句）"],
    ["刚才那个", "self_study 必须点名禁止「刚才那个」"],
    ["这个我们", "self_study 必须点名禁止「这个我们」"],
    ["1400–1800 字", "recorded 的 6 分钟口语量口径（1400–1800 字）"],
    ["看这张图右下角", "recorded 必须点名禁止手势 / 画面指代"],
    ["不许依赖前文指代", "self_study 的书面态要求"],
  ];
  for (const [needle, why] of musts) if (!source.includes(needle)) bad.push(`后端场景要求缺「${needle}」：${why}`);
  return bad;
}

function auditPyGuards(source: string, lectureSource: string): string[] {
  const bad: string[] = [];
  const body = source.slice(source.indexOf("def scenario_meta"));
  const metaBody = body.slice(0, body.indexOf("\ndef "));
  if (/classroom/.test(metaBody)) {
    bad.push("scenario_meta 函数体里出现了 classroom：缺省补默认场景是**明令禁止**的（§B1 空值语义）");
  }
  if (!/if not keys:\n\s+return ""/.test(source)) {
    bad.push("scenario_prompt_block 少了「未选场景（无口气要求）⇒ 返回空串」这条护栏");
  }
  if (!/PROMPT_TEMPLATE_VERSION = "lecture-v14"/.test(lectureSource)) {
    bad.push("lecture.py 的 PROMPT_TEMPLATE_VERSION 没升到 lecture-v14（场景改了却不重生成）");
  }
  return bad;
}

/* ======================= ② 空值语义 + 文案（行为级） ======================= */

type Scenario = { primary: string; selected: string[]; updated_at?: string };
type Mod = {
  SCENARIOS: readonly { key: string; label: string; hint: string }[];
  SCENARIO_KEYS: readonly string[];
  SCENARIO_UNSET: string;
  normalizeScenario: (raw?: unknown) => Scenario | null;
  scenarioLine: (course?: unknown) => string;
  scenarioPayload: (primary?: string, selected?: string[]) => Scenario;
};

const UNSET_LINE = "未指定场景（按通用口径生成）";

function auditBehavior(mod: Mod): string[] {
  const bad: string[] = [];
  const norm = mod.normalizeScenario;
  for (const empty of [undefined, null, {}, { selected: [] }, { selected: ["不认识"] },
    { primary: "", selected: [] }]) {
    if (norm(empty) !== null) bad.push(`空 / 脏场景必须归一化成 null（不许补默认场景）：${JSON.stringify(empty)}`);
  }
  if (mod.SCENARIO_UNSET !== UNSET_LINE) bad.push(`未选场景的文案被改了：${mod.SCENARIO_UNSET}`);
  if (mod.scenarioLine(null) !== UNSET_LINE) bad.push("没有课程时的显示行必须是「未指定场景（按通用口径生成）」");
  if (mod.scenarioLine({ meta: {} }) !== UNSET_LINE) bad.push("未选场景（老的课）的显示行必须是「未指定场景（按通用口径生成）」");
  if (mod.scenarioLine({ meta: { scenario: {} } }) !== UNSET_LINE) bad.push("meta.scenario 为空时也必须算未指定");
  const line = mod.scenarioLine({ scenario: { primary: "classroom", selected: ["classroom", "recorded"] } });
  if (line !== "本次按场景生成：线下课堂面授 + 录成视频课（主：线下课堂面授）") {
    bad.push(`已选场景的显示行不对：${line}`);
  }
  const messy = norm({ primary: "lms", selected: ["lms", "nope", "classroom", "lms"] }) as Scenario;
  if (JSON.stringify(messy?.selected) !== JSON.stringify(["classroom", "lms"])) {
    bad.push(`selected 必须去重 / 只留合法 key / 按固定顺序：${JSON.stringify(messy)}`);
  }
  const orphan = norm({ primary: "lms", selected: ["recorded"] }) as Scenario;
  if (!orphan || orphan.primary !== "recorded") bad.push("primary 不在 selected 里时必须自洽修正（退回 selected[0]）");
  const empty = mod.scenarioPayload("", []);
  if (!empty || empty.selected.length !== 0 || empty.primary !== "") {
    bad.push(`清空场景必须写出「空」而不是默认场景：${JSON.stringify(empty)}`);
  }
  const picked = mod.scenarioPayload("self_study", ["self_study", "recorded"]);
  if (!picked.selected.includes(picked.primary)) bad.push("primary 必须 ∈ selected");
  return bad;
}

/* ======================= ③ 界面接线（源码级，去注释后扫） ======================= */

function auditWiring(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const picker = stripComments(files.get(REL_PICKER) ?? "");
  if (!/Tooltip[\s\S]{0,80}title=\{item\.hint\}/.test(picker)) bad.push("ScenarioPicker 每项没挂 tooltip（title={item.hint}）");
  if (!/Checkbox\.Group/.test(picker)) bad.push("ScenarioPicker 不是五选多");
  if (!/<Select/.test(picker)) bad.push("ScenarioPicker 少了「指定主场景」");
  if (!/export function ScenarioEditor/.test(picker)) bad.push("ScenarioPicker 里没有落库版 ScenarioEditor（两处复用的那一份）");
  if (!/清空场景/.test(picker)) bad.push("ScenarioPicker 没有回到「未指定」的入口（清空场景）");

  const list = stripComments(files.get(REL_LIST) ?? "");
  if (!/name="scenario"/.test(list)) bad.push("建课表单没有使用场景这一项（Form.Item name=\"scenario\"）");
  if (!/meta\.scenario = picked/.test(list)) bad.push("建课表单没有把场景收进 meta.scenario");
  if (!/picked\.selected\?\.length/.test(list)) bad.push("建课表单会让空场景也写进 meta（默认不预选 = 不写）");

  for (const [rel, why] of [[REL_DETAIL, "课程详情页"], [REL_PLAN, "第 2 步大纲页"]] as const) {
    const src = stripComments(files.get(rel) ?? "");
    if (!/ScenarioEditor/.test(src)) bad.push(`${why}没有接场景组件（ScenarioEditor）`);
    if (!/scenarioOf\(course\)/.test(src)) bad.push(`${why}没有把课程当前的场景喂给组件`);
  }
  for (const [rel, why] of [[REL_WORKBENCH, "第 3 步章节制作"], [REL_DELIVERY, "出包页"]] as const) {
    const src = stripComments(files.get(rel) ?? "");
    if (!/scenarioLine\(course\)/.test(src)) bad.push(`${why}没有那一行「本次按场景生成：…」（§B3）`);
    if (!/data-testid="scenario-line"/.test(src)) bad.push(`${why}缺 data-testid="scenario-line"（探针要抓这行）`);
  }
  return bad;
}

/* ======================= 走查 ======================= */

const srcFiles = new Map<string, string>();
for (const rel of [REL_TS, REL_PICKER, REL_LIST, REL_DETAIL, REL_PLAN, REL_WORKBENCH, REL_DELIVERY]) {
  srcFiles.set(rel, read(rel));
}

failures.push(...auditTables(tsSource, pySource));
failures.push(...auditPyRequirements(pySource));
failures.push(...auditPyGuards(pySource, read(REL_LECTURE)));
failures.push(...auditWiring(srcFiles));

const real = await import(pathToFileURL(join(repo, REL_TS)).href) as Mod;
failures.push(...auditBehavior(real));

/* ======================= ⑤ 变异自检 ======================= */

const tmpDir = mkdtempSync(join(tmpdir(), "cg85-scenario-"));
let mutationChecked = 0;

async function behaviorOfSource(source: string): Promise<string[]> {
  const file = join(tmpDir, `mutated-${Math.random().toString(36).slice(2)}.ts`);
  writeFileSync(file, source, "utf8");
  const mod = await import(pathToFileURL(file).href) as Mod;
  return auditBehavior(mod);
}

try {
  const behaviorMutations: [string, string, (src: string) => string][] = [
    ["让「未选场景」补上默认场景 classroom",
      tsSource,
      (src) => src.replace(
        "  if (!selected.length) return null;",
        '  if (!selected.length) return { primary: "classroom", selected: ["classroom"] };')],
    ["让未选场景的显示行变成空串",
      tsSource,
      (src) => src.replace("  if (!meta) return SCENARIO_UNSET;", '  if (!meta) return "";')],
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

  const sourceMutations: [string, string][] = [
    ["改掉后端表里的一个显示名",
      auditTables(tsSource, pySource.replace('("live", "线上直播"', '("live", "网络直播"')).join("")],
    ["改掉前端表里的一句话说明",
      auditTables(tsSource.replace("只影响出包形态，不影响口气", "只影响出包"), pySource).join("")],
    ["把前端表里录课向的短标改掉",
      auditTables(tsSource.replace(', "录课向"]', ', "视频课向"]'), pySource).join("")],
    ["把某一侧短标留空",
      auditTables(tsSource.replace('"录课向"', '""'), pySource).join("")],
    ["删掉 self_study 的「不许依赖前文指代」要求",
      auditPyRequirements(pySource.replace("不许依赖前文指代", "")).join("")],
    ["把 PROMPT_TEMPLATE_VERSION 退回去",
      auditPyGuards(pySource, 'PROMPT_TEMPLATE_VERSION = "lecture-v13"').join("")],
    ["让 scenario_meta 缺省补 classroom",
      auditPyGuards(pySource.replace("    if not picked:\n        return {}", '    if not picked:\n        picked = ["classroom"]'), read(REL_LECTURE)).join("")],
    ["摘掉 ScenarioPicker 的 tooltip",
      auditWiring(new Map(srcFiles).set(REL_PICKER,
        (srcFiles.get(REL_PICKER) ?? "").replace("title={item.hint}", 'title="使用场景"'))).join("")],
    ["摘掉出包页那一行",
      auditWiring(new Map(srcFiles).set(REL_DELIVERY,
        (srcFiles.get(REL_DELIVERY) ?? "").replace(/scenarioLine\(course\)/g, '""'))).join("")],
  ];
  for (const [name, red] of sourceMutations) {
    mutationChecked += 1;
    if (red.length === 0) failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「使用场景」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端使用场景断言（85）通过："
  + "① 两侧场景表逐字一致（5 个 key · 显示名 · 一句话说明 · 短标，无空短标；后端关键要求 6 条仍在）；"
  + "② 空值语义（未选 / 空 / 脏数据 ⇒「未指定场景（按通用口径生成）」，不补默认场景；"
  + "已选 ⇒「本次按场景生成：A + B（主：A）」；primary ∈ selected）；"
  + `③ 界面接线（建课表单 + 课程详情页 + 第 2 步大纲页复用同一组件；第 3 步与出包页各一行）；`
  + `④ 后端护栏（prompt 未选返回空串 / scenario_meta 不出现 classroom / 版本 lecture-v14）；`
  + `${mutationChecked} 条变异自检全红（含 2 条行为级：重写 scenarios.ts 后重新加载模块）；`);
