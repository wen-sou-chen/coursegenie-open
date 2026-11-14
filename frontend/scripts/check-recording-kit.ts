/**
 * 任务 87（修订版）前端断言：**录课包与平台投放的出料**（**0 LLM**）。
 *
 * 钉四组：
 * ① **源码级（渲染层）**：`recording_kit.py` 的存在、五个硬常量（4.5 字/秒 / 360 秒闸门 /
 *    单行 42 / 每条 ≤2 行 / 20 字符每秒）、场景门（只在含 `recorded` 时出）、互动点门
 *    （含 `classroom` / `live`）、⛔ 0-LLM 护栏（不许出现 `complete_json` / `build_llm_client`）；
 * ② **接线**：`delivery.py` 只在 `kit_enabled` 时写 `录课包/`、每个文件都进 `files`（⇒ 进 manifest）；
 *    `publish.py` 的 SCORM `<item>` 按**课时**切（`zip(bundle["rows"], bundle["pages"])`）+ 写盘前自检；
 * ③ **前端**：出包页逐条显示「按课程场景会多出哪些料」（`data-testid="delivery-extras"`）+
 *    含 `lms` 时的 SCORM 入口与自检回显（`data-testid="scorm-entry"` / `scorm-self-check`）；
 * ④ **行为级（真跑 Python）**：真 import `recording_kit`，验秒数 / 内部提示拆分 / 断行 / SRT / 逐字稿，
 *    并把「字/秒放宽到 30」当变异 —— 同一条 SRT 的阅读速度必须被本脚本的检查器判红。
 *
 * ⛔ 0 LLM：子进程显式 `LLM_PROVIDER=mock`，且一切路径指向系统临时目录（不碰真库 / 真 vault）。
 * 运行：`node scripts/check-recording-kit.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1（前端断言 29 → 30 条）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
/** Python 侧只去掉「整行 # 注释」（不要拿块注释规则扫 Python，见 check-student-view.ts 的警告）。 */
const stripPyComments = (s: string) => s.replace(/^[ \t]*#[^\n]*$/gm, "");

const REL_KIT = "backend/app/services/recording_kit.py";
const REL_DELIVERY = "backend/app/services/delivery.py";
const REL_PUBLISH = "backend/app/services/publish.py";
const REL_DELIVERY_API = "frontend/src/deliveryApi.ts";
const REL_PANEL = "frontend/src/components/DeliveryPanel.tsx";

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ---------------- ① 源码级：渲染层与常量 ---------------- */

function auditKitSource(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/services/recording_kit.py"];
  const constants: [string, string][] = [
    ["CHARS_PER_SECOND", "4.5"],
    ["SECTION_LIMIT_SECONDS", "360"],
    ["MAX_LINE_CHARS", "42"],
    ["MAX_CUE_LINES", "2"],
    ["CPS_LIMIT", "20"],
  ];
  for (const [name, value] of constants) {
    if (!new RegExp(`^${name} = ${value.replace(".", "\\.")}(?![\\d.])`, "m").test(source)) {
      bad.push(`recording_kit.py 的 ${name} 不是 ${value}（规格硬常量被改了）`);
    }
  }
  if (!/def kit_enabled\(course\)[\s\S]{0,200}return "recorded" in _selected\(course\)/.test(source)) {
    bad.push("kit_enabled 没有把「含 recorded 才出」写死");
  }
  if (!/any\(key in _selected\(course\) for key in \("classroom", "live"\)\)/.test(source)) {
    bad.push("wants_interactions 没有把「含 classroom / live 才出互动点」写死");
  }
  if (!source.includes("超过 6 分钟，建议在此处切分")) bad.push("6 分钟闸门的提示语丢了");
  for (const needle of ["complete_json", "build_llm_client", "openai", "LLMClient"]) {
    if (source.includes(needle)) bad.push(`recording_kit.py 里出现了 ${needle}（录课包必须 0 LLM）`);
  }
  return bad;
}

/* ---------------- ② 接线：交付包与 SCORM ---------------- */

function auditWiring(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const delivery = stripPyComments(files.get(REL_DELIVERY) ?? "");
  if (!/kit_files = recording_kit\.build_files\(db, course\) if kit_entries else \[\]/.test(delivery)) {
    bad.push("delivery.py 没有「有原料才渲染」的守卫（可能无条件多出 录课包/）");
  }
  if (!/if recording_kit\.kit_enabled\(course\) and not kit_files:/.test(delivery)) {
    bad.push("delivery.py 缺「场景要出却没内容 ⇒ 显式报缺项」的兜底");
  }
  if (!/for row in kit_files:\s*\n\s+files\.append\(_write_text\(out_dir, row\["path"\], row\["text"\]\)\)/.test(delivery)) {
    bad.push("录课包的文件没有逐个进 files 清单（客户侧核验会报「包内多出未声明文件」）");
  }
  const publish = stripPyComments(files.get(REL_PUBLISH) ?? "");
  if (!/for number, \(row, page\) in enumerate\(zip\(bundle\["rows"\], bundle\["pages"\]\), start=1\)/.test(publish)) {
    bad.push("SCORM 的 <item> 不是按课时切（必须逐课时，不能整课一个）");
  }
  if (!/adlcp:datafromlms>node_key=/.test(publish)) bad.push("SCORM 的 <item> 少了知识点 key（adlcp:datafromlms）");
  if (!/<adlcp:maxtimeallowed>/.test(publish)) bad.push("SCORM 的 <item> 少了预计时长（adlcp:maxtimeallowed）");
  if (!/report = scorm_selfcheck\(manifest, names, sizes\)/.test(publish)
      || !/if not report\["ok"\]:\s*\n\s+raise ValueError/.test(publish)) {
    bad.push("SCORM 导出前没有自检（或自检不通过仍然落盘）");
  }
  return bad;
}

/* ---------------- ③ 前端 ---------------- */

function auditFrontend(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const panel = stripComments(files.get(REL_PANEL) ?? "");
  if (!/data-testid="delivery-extras"/.test(panel)) bad.push("出包页没有「按课程场景会多出哪些料」这一块");
  if (!/data-testid="scorm-entry"/.test(panel)) bad.push("出包页缺 SCORM 导出入口");
  if (!/data-testid="scorm-self-check"/.test(panel)) bad.push("出包页没有回显 SCORM 自检结果");
  for (const text of ["将包含", "本次不出", "将按课时打 SCORM 1.2 包"]) {
    if (!panel.includes(text)) bad.push(`出包页缺文案「${text}」`);
  }
  const api = stripComments(files.get(REL_DELIVERY_API) ?? "");
  if (!/exportScorm: \(courseId: string\)/.test(api)) bad.push("deliveryApi 缺 exportScorm");
  if (!/export interface DeliveryExtraItem/.test(api)) bad.push("deliveryApi 缺 DeliveryExtraItem 类型");
  if (!/format: "scorm"/.test(api)) bad.push("exportScorm 没有把 format: scorm 发出去");
  return bad;
}

/* ---------------- ④ 行为级：真跑 Python ---------------- */

type KitOut = {
  cps_limit: number; chars_per_second: number; section_limit: number;
  max_line: number; max_cue_lines: number;
  seconds_of_9: number; count_chars: number; spoken: string; internal: string;
  wrap_ok: boolean; kit_enabled_recorded: boolean; kit_enabled_classroom: boolean;
  interactions_classroom: boolean; srt: string; script: string; note: string;
};

/** 用真 Python 跑一次渲染层（不连库、不读真库；路径全部指向临时目录）。 */
function runPython(cps: number, tmp: string): KitOut {
  const python = existsSync(join(repo, "backend/.venv/Scripts/python.exe"))
    ? join(repo, "backend/.venv/Scripts/python.exe")
    : (process.platform === "win32" ? "python" : "python3");
  const script = `
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, r"${join(repo, "backend")}")
from app.services import recording_kit as kit

kit.CHARS_PER_SECOND = ${cps}
NOTE = ("这一页我们看发酵原理与温度窗口，先看体积再看蜂窝，最后闻酸香；"
        "三步都过才进下一步。") * 3
out = {
    "cps_limit": kit.CPS_LIMIT,
    "chars_per_second": kit.CHARS_PER_SECOND,
    "section_limit": kit.SECTION_LIMIT_SECONDS,
    "max_line": kit.MAX_LINE_CHARS,
    "max_cue_lines": kit.MAX_CUE_LINES,
    "seconds_of_9": kit.seconds_of("字数刚好九个字"),
    "count_chars": kit.count_chars("a b\\nc"),
    "wrap_ok": max(len(line) for line in kit.wrap_lines("啊" * 200)) <= kit.MAX_LINE_CHARS,
    "kit_enabled_recorded": kit.kit_enabled({"scenario": {"primary": "recorded", "selected": ["recorded"]}}),
    "kit_enabled_classroom": kit.kit_enabled({"scenario": {"primary": "classroom", "selected": ["classroom"]}}),
    "interactions_classroom": kit.wants_interactions(
        {"scenario": {"primary": "classroom", "selected": ["classroom"]}}),
    "note": NOTE,
}
spoken, internal = kit.split_notes("第一段口播稿。" + chr(10) + kit.INTERNAL_MARKS[0] + "内部操作提示")
out["spoken"], out["internal"] = spoken, internal
deck = SimpleNamespace(title="合成课件")
pages = [
    {"index": 1, "title": "封面", "spoken": NOTE, "internal": "", "chars": kit.count_chars(NOTE),
     "seconds": kit.seconds_of(NOTE), "bullets": ["要点一"], "layout": "cover"},
    {"index": 2, "title": "知识点页", "spoken": "再看温度窗口。", "internal": "（仅讲师可见）勿念",
     "chars": kit.count_chars("再看温度窗口。"), "seconds": kit.seconds_of("再看温度窗口。"),
     "bullets": ["要点二"], "layout": "point"},
]
out["srt"] = kit.srt_text(deck, pages)
out["script"] = kit.script_markdown(deck, pages)
print(json.dumps(out, ensure_ascii=False))
`;
  const file = join(tmp, `kit-${cps}.py`);
  writeFileSync(file, script, "utf8");
  const result = spawnSync(python, [file], {
    cwd: repo,
    encoding: "utf8",
    env: {
      ...process.env,
      LLM_PROVIDER: "mock",
      IMAGE_PROVIDER: "mock",
      DATABASE_URL: `sqlite:///${join(tmp, "check.db").replace(/\\/g, "/")}`,
      STORAGE_DIR: join(tmp, "storage"),
      OBSIDIAN_VAULT_ROOT: join(tmp, "vault"),
      PPT_WORK_DIR: join(tmp, "storage", "ppt"),
      EXPORT_DIR: join(tmp, "storage", "exports"),
      PYTHONIOENCODING: "utf-8",
    },
  });
  if (result.status !== 0) {
    throw new Error(`Python 行为级跑不通（exit=${result.status}）：${(result.stderr ?? "").slice(-400)}`);
  }
  const line = (result.stdout ?? "").trim().split("\n").pop() ?? "";
  return JSON.parse(line) as KitOut;
}

/** SRT 规格检查器（与后端 pytest 同一套判据；这里独立实现，避免「拿被测代码验被测代码」）。 */
function auditSrt(text: string, cps = 20): string[] {
  const bad: string[] = [];
  let previousEnd = -1;
  for (const block of text.trim().split(/\n\s*\n/)) {
    if (!block.trim()) continue;
    const lines = block.split("\n").filter((line) => line.trim());
    const match = /^(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)$/.exec(lines[1] ?? "");
    if (!match || lines.length < 3) { bad.push(`时间轴格式不合法：${(lines[1] ?? block).slice(0, 40)}`); continue; }
    const value = match.slice(1).map(Number);
    const start = value[0] * 3600 + value[1] * 60 + value[2] + value[3] / 1000;
    const end = value[4] * 3600 + value[5] * 60 + value[6] + value[7] / 1000;
    const body = lines.slice(2);
    const chars = body.reduce((sum, line) => sum + line.length, 0);
    const duration = Math.max(end - start, 0.001);
    if (end <= start) bad.push("cue 时长 ≤0");
    if (start < previousEnd - 1e-6) bad.push(`cue 与上一条重叠：${start} < ${previousEnd}`);
    if (chars / duration > cps) bad.push(`阅读速度 ${(chars / duration).toFixed(1)} 字符/秒 > ${cps}`);
    if (Math.max(...body.map((line) => line.length)) > 42) bad.push("单行超过 42 字符");
    if (body.length > 2) bad.push(`一条字幕 ${body.length} 行 > 2`);
    previousEnd = end;
  }
  return bad;
}

function auditKitBehavior(out: KitOut): string[] {
  const bad: string[] = [];
  if (out.cps_limit !== 20 || out.max_line !== 42 || out.max_cue_lines !== 2) {
    bad.push(`字幕硬约束被改了：${JSON.stringify([out.cps_limit, out.max_line, out.max_cue_lines])}`);
  }
  if (out.seconds_of_9 !== 2) bad.push(`9 个字 ÷ 4.5 应向上取整为 2 秒，实得 ${out.seconds_of_9}`);
  if (out.count_chars !== 3) bad.push(`字数口径（去空白）不对：${out.count_chars}`);
  if (out.spoken !== "第一段口播稿。") bad.push(`口播稿拆分不对：${out.spoken}`);
  if (!out.internal.includes("勿投屏")) bad.push("内部提示没有被拆出来（会被念进视频）");
  if (!out.wrap_ok) bad.push("断行超过了 42 字符");
  if (!out.kit_enabled_recorded || out.kit_enabled_classroom) bad.push("场景门不对（只有 recorded 才出录课包）");
  if (!out.interactions_classroom) bad.push("互动点的场景门不对（classroom / live 才出）");
  if (!out.script.includes(out.note)) bad.push("逐字稿没有逐字保留原备注（被改写了）");
  if (!/【第 1 页 · ≈\d+ 秒】/.test(out.script)) bad.push("逐字稿缺「【第 N 页 · ≈S 秒】」页标记");
  if (!out.script.includes("下一页我们看「知识点页」。")) bad.push("逐字稿缺页间固定连接句");
  if (!out.script.includes("> 讲师提示（勿投屏，不计时长）：")) bad.push("内部提示没有单独标注");
  if (out.srt.includes("勿念")) bad.push("内部提示进了字幕（不该念）");
  bad.push(...auditSrt(out.srt));
  return bad;
}

/* ---------------- 走查 ---------------- */

const files = new Map<string, string>();
for (const rel of [REL_KIT, REL_DELIVERY, REL_PUBLISH, REL_DELIVERY_API, REL_PANEL]) {
  files.set(rel, read(rel));
}

failures.push(...auditKitSource(stripPyComments(files.get(REL_KIT) ?? "")));
failures.push(...auditWiring(files));
failures.push(...auditFrontend(files));

const tmpDir = mkdtempSync(join(tmpdir(), "cg87-kit-"));
let mutationChecked = 0;
try {
  const normal = runPython(4.5, tmpDir);
  failures.push(...auditKitBehavior(normal));

  /* 变异自检：每一条都必须让对应的判定变红 */
  const sourceMutations: [string, string[]][] = [
    ["把录课包的场景门去掉（无条件生成）",
      auditKitSource(stripPyComments(
        (files.get(REL_KIT) ?? "").replace('return "recorded" in _selected(course)', "return True")))],
    ["把 6 分钟闸门放宽（360 → 3600）",
      auditKitSource(stripPyComments(
        (files.get(REL_KIT) ?? "").replace("SECTION_LIMIT_SECONDS = 360", "SECTION_LIMIT_SECONDS = 3600")))],
    ["让逐字稿改成调模型",
      auditKitSource(`from app.services.llm import complete_json\n${files.get(REL_KIT) ?? ""}`)],
    ["把 SCORM 的 item 切分改成整课一个",
      auditWiring(new Map(files).set(REL_PUBLISH, (files.get(REL_PUBLISH) ?? "").replace(
        'for number, (row, page) in enumerate(zip(bundle["rows"], bundle["pages"]), start=1)',
        'for number, page in enumerate(bundle["pages"], start=1)')))],
    ["把 SCORM 的导出前自检去掉",
      auditWiring(new Map(files).set(REL_PUBLISH, (files.get(REL_PUBLISH) ?? "").replace(
        'report = scorm_selfcheck(manifest, names, sizes)', "report = {'ok': True}")))],
    ["摘掉出包页的「会多出哪些料」",
      auditFrontend(new Map(files).set(REL_PANEL,
        (files.get(REL_PANEL) ?? "").replace(/data-testid="delivery-extras"/g, "")))],
    ["摘掉出包页的 SCORM 入口",
      auditFrontend(new Map(files).set(REL_PANEL,
        (files.get(REL_PANEL) ?? "").replace(/data-testid="scorm-entry"/g, "")))],
  ];
  for (const [name, red] of sourceMutations) {
    mutationChecked += 1;
    if (red.length === 0) failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  }

  // 行为级变异：把「字/秒」放宽到 30 ⇒ 同一条 SRT 的阅读速度 > 20 ⇒ 检查器必须报红
  mutationChecked += 1;
  const widened = runPython(30, tmpDir);
  if (widened.srt === normal.srt) {
    failures.push("自检失败：变异「字/秒放宽到 30」没有改变输出（变异没生效）");
  } else if (!auditSrt(widened.srt).some((row) => row.includes("阅读速度"))) {
    failures.push("自检失败：变异「字/秒放宽到 30」后阅读速度断言仍然通过");
  }
} catch (error) {
  failures.push(`行为级检查异常：${error instanceof Error ? error.message : String(error)}`);
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「录课包」断言失败：");
  for (const row of failures) console.error(`  - ${row}`);
  process.exit(1);
}
console.log("前端录课包断言（87）通过："
  + "① 渲染层常量（4.5 字/秒 / 360 秒闸门 / 单行 42 / ≤2 行 / 20 字符每秒）与两个场景门（recorded / classroom|live）都在 + ⛔ 0-LLM 源码级护栏；"
  + "② 录课包只在场景含 recorded 时写、逐文件进 manifest；SCORM 按课时切 item + 写前自检；"
  + "③ 出包页逐条显示「会多出哪些料」+ 含 lms 时的 SCORM 入口与自检回显；"
  + "④ 行为级真跑 Python：秒数 / 内部提示拆分 / 断行 / SRT / 逐字稿逐字保留；"
  + `${mutationChecked} 条变异自检全红（含「字/秒放宽到 30」「item 切成整课一个」）。`);
