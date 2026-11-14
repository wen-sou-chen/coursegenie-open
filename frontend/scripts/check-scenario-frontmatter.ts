/**
 * 任务 86b §A：钉住 `scenario.frontmatter_fields` 的**返回键名**（**行为级真 import**，0 LLM）。
 *
 * **为什么有这条**：复验 86 时 DSH 做了一个比规格更强的变异 —— 把 `frontmatter_fields` 的函数体
 * **整个换成 `return []`** ⇒ **当时那三支断言仍然全绿**（源码级只钉了「函数存在」，没钉「返回什么键」）
 * ⇒ 笔记 frontmatter 会**静默丢掉** `scenario` / `prompt_version` 两个字段而没人发现。本脚本把这条
 * 缺口补硬：**真的跑一次 Python、按文件路径 import 这个模块、真调一次 `frontmatter_fields`**，
 * 断言返回值恰好是 `("scenario", …)` + `("prompt_version", …)` **两对、键名固定、值非空**。
 *
 * 与 `check-scenario-visibility.ts` ① 组的分工（互补，不是重复）：
 * - 那边是**源码级**（函数体里必须有 `("scenario"` / `("prompt_version"` + 回落常量）—— 管「写法没被换掉」；
 * - 这里是**行为级**（真 import、真调用）—— 管「真跑起来返回什么」，`return []` 这类变异逃不掉。
 * - `check-scenario-visibility.ts` 里对应的变异自检（清空函数体 ⇒ 变红）也留着。
 *
 * ⚠️ 工程取舍（写清理由）：行为级要真 import Python 模块 ⇒ 需要一个 Python（优先
 * `backend/.venv/Scripts/python.exe`，回落 `python3` / `python`）。`scenario.py` 是**零依赖纯函数模块**，
 * 所以这里用 `importlib` 按**文件路径**加载 —— **不启动应用、不连库、不读真库**（真机只读）。
 * **找不到 Python ⇒ 直接判失败**（不许静默跳过行为级，否则这条护栏就成了假的）。
 * ✅ 另有一层事实：pytest 里 `test_frontmatter_fields_labels_and_placeholders` 早就钉了同样的行为 ——
 * 也就是说 86 的缺口**只在前端这一组**；这条按 §A 把同一件事在前端也钉住，且用真 import 而非文本匹配。
 *
 * ⛔ 0 LLM：本脚本只 import 纯函数模块，且给子进程显式 `LLM_PROVIDER=mock`。
 * 运行：`node scripts/check-scenario-frontmatter.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 `scripts/verify.ps1`（前端断言 27 → 28 条）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const REL_PY = "backend/app/services/scenario.py";
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

const failures: string[] = [];

/** `def NAME` 到下一个顶层 `def` / 文件尾（与 check-scenario-visibility.ts 同款）。 */
function bodyOf(source: string, name: string): string {
  const start = source.indexOf(`def ${name}(`);
  if (start < 0) return "";
  const rest = source.slice(start + 1);
  const next = rest.indexOf("\ndef ");
  return next < 0 ? rest : rest.slice(0, next);
}

/** 把函数体整个换成 `return []`（复验 86 的 M2 变异）。 */
function emptyFunctionBody(source: string, name: string): string {
  const body = bodyOf(source, name);
  if (!body) return source;
  const signature = body.slice(0, body.indexOf("\n"));
  return source.replace(body, `${signature}\n    return []\n`);
}

/* ======================= ① 源码级：键名与回落常量都在函数体里 ======================= */

function auditSource(source: string): string[] {
  const bad: string[] = [];
  const body = bodyOf(source, "frontmatter_fields");
  if (!body) {
    bad.push("scenario.py 取不到 frontmatter_fields 的函数体（断言失效）");
    return bad;
  }
  for (const literal of ['("scenario"', '("prompt_version"']) {
    if (!body.includes(literal)) {
      bad.push(`frontmatter_fields 的函数体里没有 ${literal}（返回键名没被钉住）`);
    }
  }
  for (const fallback of ["UNMARKED_LABEL", "UNMARKED_VERSION"]) {
    if (!body.includes(fallback)) {
      bad.push(`frontmatter_fields 没有回落到 ${fallback}（老内容没有口径时会写出空值）`);
    }
  }
  return bad;
}

/* ======================= ② 行为级：真 import 一次，断言返回值 ======================= */

/** 两个字段的**固定键名与顺序**（frontmatter 的字段名就是契约）。 */
const FIELD_KEYS = ["scenario", "prompt_version"];

type Pair = [string, string];

/** 断言：恰好两对、键名与顺序固定、值都是非空字符串。 */
function auditPairs(rows: unknown, why: string): string[] {
  const bad: string[] = [];
  if (!Array.isArray(rows) || rows.length !== FIELD_KEYS.length) {
    return [`${why}：frontmatter_fields 必须返回**恰好两条**字段，实得 ${JSON.stringify(rows)}`];
  }
  const names = rows.map((pair) => (Array.isArray(pair) ? String(pair[0]) : "?"));
  if (names.join("|") !== FIELD_KEYS.join("|")) {
    bad.push(`${why}：返回键名必须是 ${FIELD_KEYS.join(" + ")}（顺序固定），实得 ${names.join(" + ")}`);
  }
  rows.forEach((pair, index) => {
    const value = Array.isArray(pair) ? pair[1] : undefined;
    if (typeof value !== "string" || value === "") {
      bad.push(`${why}：${FIELD_KEYS[index] ?? "?"} 的值必须是非空字符串，实得 ${JSON.stringify(value)}`);
    }
  });
  return bad;
}

/** 逐字比对期望值（真值也要钉住，不能只看"非空"）。 */
function auditValues(rows: unknown, expected: Pair[], why: string): string[] {
  const bad: string[] = [];
  const actual = Array.isArray(rows)
    ? rows.map((pair) => (Array.isArray(pair) ? [String(pair[0]), String(pair[1])] : pair))
    : rows;
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    bad.push(`${why}：期望 ${JSON.stringify(expected)}，实得 ${JSON.stringify(actual)}`);
  }
  return bad;
}

/** 子进程里那段 Python：按**文件路径**加载模块（零依赖纯函数模块，不启动应用、不连库）。 */
const PROBE = [
  "import importlib.util, json, sys",
  "spec = importlib.util.spec_from_file_location('cg_86b_frontmatter', sys.argv[1])",
  "mod = importlib.util.module_from_spec(spec)",
  "spec.loader.exec_module(mod)",
  "cases = json.loads(sys.argv[2])",
  "print(json.dumps([mod.frontmatter_fields(meta) for meta in cases], ensure_ascii=False))",
].join("\n");

/** 优先仓库自带的 venv（verify.ps1 也是这个顺序），回落 PATH 里的 python3 / python。 */
function findPython(): string | null {
  const venv = [join(repo, "backend", ".venv", "Scripts", "python.exe"),
    join(repo, "backend", ".venv", "bin", "python")].filter((exe) => existsSync(exe));
  for (const exe of [...venv, "python3", "python"]) {
    const probe = spawnSync(exe, ["-c", "print(1)"], { encoding: "utf8" });
    if (probe.status === 0) return exe;
  }
  return null;
}

/** 真跑一次 Python：返回每个用例的返回值（`string` ⇒ 跑不起来的原因）。 */
function callFrontmatterFields(exe: string, modulePath: string, cases: unknown[]): unknown[] | string {
  const res = spawnSync(exe, ["-c", PROBE, modulePath, JSON.stringify(cases)], {
    encoding: "utf8",
    // PYTHONDONTWRITEBYTECODE：只 import 不落 `__pycache__`（真机只读，脚本自己不产生任何写盘）。
    env: { ...process.env, LLM_PROVIDER: "mock", PYTHONIOENCODING: "utf-8",
      PYTHONDONTWRITEBYTECODE: "1" },
  });
  if (res.status !== 0) {
    return `python 退出码 ${res.status}：${(res.stderr ?? "").trim() || "(无 stderr)"}`;
  }
  try {
    return JSON.parse(res.stdout) as unknown[];
  } catch {
    return `python 输出不是 JSON：${String(res.stdout).trim()}`;
  }
}

/**
 * 三个用例（**0 LLM、纯函数**）：
 * ① 空 meta（老内容）⇒ 通用 / 未记录；
 * ② 录课向 + 提示词版本 ⇒ 录成视频课 / lecture-v14（**中文显示名**，不是界面短标）；
 * ③ 场景块**缺 `selected`**（规格 §A1 第二个例子的形状）⇒ 按「未标记」处理 = 通用。
 *    这条是**两边同口径**：前端 `normalizeScenario` 同样要求 `selected` 非空（缺 ⇒ null ⇒ 未标记），
 *    且生成侧 `run_meta` **永远同时写 primary + selected** ⇒ 真实数据不会走到这个形状。
 */
const CASES: [string, Record<string, unknown>, Pair[]][] = [
  ["空 meta（老内容）", {},
    [["scenario", "通用"], ["prompt_version", "未记录"]]],
  ["录课向 + 提示词版本",
    { scenario_used: { primary: "recorded", selected: ["recorded"] }, prompt_version: "lecture-v14" },
    [["scenario", "录成视频课"], ["prompt_version", "lecture-v14"]]],
  ["场景块缺 selected（规格 §A1 原文的形状）",
    { scenario_used: { primary: "recorded" }, prompt_version: "lecture-v14" },
    [["scenario", "通用"], ["prompt_version", "lecture-v14"]]],
];

const source = read(REL_PY);
failures.push(...auditSource(source));

const python = findPython();

/** 行为级：跑真模块（或变异后的临时模块）⇒ 返回失败项。 */
function auditBehaviorOfModule(modulePath: string, why: string): string[] {
  if (!python) return [`${why}：找不到可用的 Python，行为级断言跑不了`];
  const rows = callFrontmatterFields(python, modulePath, CASES.map(([, meta]) => meta));
  if (typeof rows === "string") return [`${why}：${rows}`];
  const bad: string[] = [];
  CASES.forEach(([label, , expected], index) => {
    const got = rows[index];
    bad.push(...auditPairs(got, `${why}/${label}`));
    bad.push(...auditValues(got, expected, `${why}/${label}`));
  });
  return bad;
}

if (!python) {
  failures.push("找不到可用的 Python（行为级断言不能静默跳过：需要 backend/.venv 或 PATH 里的 python3 / python）");
} else {
  failures.push(...auditBehaviorOfModule(join(repo, REL_PY), "真模块"));
}

/* ======================= ③ 变异自检：清空函数体 / 只留一个键 ⇒ 必须变红 ======================= */

const tmpDir = mkdtempSync(join(tmpdir(), "cg86b-frontmatter-"));
let mutationChecked = 0;

/** 源码级已经红了 ⇒ 函数体已经不是原形（例如反证据里把它整个清空）⇒ 依赖函数体锚点的变异无对象。 */
const sourceAlreadyBroken = auditSource(source).length > 0;

try {
  const mutations: [string, (src: string) => string][] = [
    ["把 frontmatter_fields 的函数体清空成 return []（复验 86 时这个变异曾经全绿）",
      (src) => emptyFunctionBody(src, "frontmatter_fields")],
    ["只返回 scenario、把 prompt_version 那一对删掉",
      (src) => src.replace('("prompt_version", prompt_version_of(meta) or UNMARKED_VERSION)', "")],
  ];
  for (const [name, mutate] of mutations) {
    mutationChecked += 1;
    const mutated = mutate(source);
    if (mutated === source) {
      if (sourceAlreadyBroken) {
        console.warn(`  · 变异「${name}」的锚点不在当前源码里（函数体已被改动）⇒ 这条自检跳过`);
      } else {
        failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
      }
      continue;
    }
    if (auditSource(mutated).length === 0) {
      failures.push(`自检失败：变异「${name}」后**源码级**断言仍然通过`);
    }
    const file = join(tmpDir, `mutated-${mutationChecked}.py`);
    writeFileSync(file, mutated, "utf8");
    if (auditBehaviorOfModule(file, `变异「${name}」`).length === 0) {
      failures.push(`自检失败：变异「${name}」后**行为级**断言仍然通过`);
    }
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「frontmatter 键名」断言失败（86b）：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端 frontmatter 键名断言（86b）通过："
  + "① 源码级：frontmatter_fields 的函数体里有 (\"scenario\" / (\"prompt_version\" + 回落 UNMARKED_LABEL / UNMARKED_VERSION；"
  + "② 行为级：真 import 该模块并调用 —— 恰好两对字段、键名与顺序固定、值非空，"
  + "空 meta ⇒ 通用 / 未记录，录课向 ⇒ 录成视频课 / lecture-v14（缺 selected 按未标记处理，与前端同口径）；"
  + `③ ${mutationChecked} 条变异自检全红（含「函数体清空成 return []」这条复验 86 时的逃逸变异）；`);
