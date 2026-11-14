/**
 * 任务 90 前端断言：**分享页「学员版」的空答案标签**（0 LLM）。
 *
 * **为什么有这条**：DSH 走查第 7 步做页面级复验时发现，切到「学员版」后**每一张随堂练习卡
 * 上都留着一个空的「答案：」标签**（临时五件套 + 临时前端实测：两门课各 6 处可见，值全是
 * 空字符串）。根因是两处口径不一致：
 *   ① 后端学员版投影把 `answer` / `explanation` **清成空串但保留键**（「键在但为空」的中间态）；
 *   ② 前端 `ShareView.tsx` 的「答案：」是**无条件渲染**的，而紧跟其后的「解析：」**有真值判断**
 *      —— 同一个组件里两行两套口径。
 *
 * 本脚本钉四组：
 * ① **源码级（三处同族渲染点）**：`ShareView.tsx` / `PreviewStep.tsx` / `ContentPanel.tsx`
 *    的「答案：」必须从「有没有值」推导（`hasAnswer`）—— **把守卫块摘掉后源码里不许再出现
 *    「答案：」**，也就是「无条件渲染」这种写法在这里直接判红；「解析：」必须同口径；
 *    题面与选项照常渲染（去答案不许连题一起去掉）；
 * ② **后端唯一出处**：`_QUIZ_HIDDEN_KEYS = ("answer", "explanation")` + `student_quiz_body()`
 *    按它去键 + `student_rows` 真的走它（与 `check-student-view.ts` 的组合断言互补）；
 * ③ **行为级（真 import、真调用）**：喂一道习题给 `audience.student_rows` ⇒ 学员版 body
 *    **没有** `answer` / `explanation` 键（不是空串）、题面/选项/题型/难度一字不动、处理幂等、
 *    教师版那一行不被就地改；
 * ④ **变异自检**：㈠ 摘掉分享页守卫、㈡ 摘掉预览页守卫、㈢ 摘掉内容面板守卫、
 *    ㈣ 后端改回「清成空串」、㈤ `_QUIZ_HIDDEN_KEYS` 少一个字段 ⇒ 各自必须变红。
 *
 * ⛔ 0 LLM：行为级只 import 纯函数模块（不启动应用、不连库、不读真库），子进程显式
 * `LLM_PROVIDER=mock` 且五件套指向临时目录（与 `verify.ps1` 同款，真机零写入）。
 * 运行：`node scripts/check-share-answer-labels.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 `scripts/verify.ps1`（前端断言 31 → 32 条）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
const stripComments = (s: string) =>
  s.replace(/\{?\/\*[\s\S]*?\*\/\}?/g, "").replace(/^\s*\/\/.*$/gm, "");

const REL_AUDIENCE = "backend/app/services/audience.py";
const REL_SHARE_VIEW = "frontend/src/pages/ShareView.tsx";
const REL_PREVIEW = "frontend/src/components/steps/PreviewStep.tsx";
const REL_PANEL = "frontend/src/components/ContentPanel.tsx";

/** 断言与变异共用的原文片段（同一段字符串只有一处出处，避免抄两遍后漂移）。 */
const PANEL_GUARDED =
  "                  {body.answer ? <p>答案：{body.answer}</p> : null}\n"
  + "                  {body.explanation ? <p>解析：{body.explanation}</p> : null}";
const PANEL_UNGUARDED =
  "                  <p>答案：{body.answer}</p>\n"
  + "                  <p>解析：{body.explanation}</p>";
const DROP_KEYS = "    return {k: v for k, v in body.items() if k not in _QUIZ_HIDDEN_KEYS}";
const EMPTY_STRINGS = '    return {**body, "answer": "", "explanation": ""}';
const HIDDEN_KEYS_DECL = '_QUIZ_HIDDEN_KEYS: tuple[str, ...] = ("answer", "explanation")';
const HIDDEN_KEYS_MUTATED = '_QUIZ_HIDDEN_KEYS: tuple[str, ...] = ("explanation",)';

const failures: string[] = [];

function bodyOf(source: string, name: string): string {
  const start = source.indexOf(`def ${name}(`);
  if (start < 0) return "";
  const rest = source.slice(start + 1);
  const next = rest.indexOf("\ndef ");
  return next < 0 ? rest : rest.slice(0, next);
}

/* ---------------- ① 前端三处渲染点：答案与解析同一口径 ---------------- */

/** 分享页 / 预览页的随堂练习卡：`hasAnswer` 守卫 + 题面与选项照常渲染。 */
function auditCardSite(src: string, rel: string): string[] {
  const bad: string[] = [];
  const code = stripComments(src);
  if (!/const hasAnswer = Boolean\(answerText\.trim\(\)\);/.test(code)) {
    bad.push(`${rel}：答案那行不是从「有没有值」推导的（缺 const hasAnswer = Boolean(answerText.trim())）`);
  }
  const guard = /\{hasAnswer \? \(([\s\S]*?)\) : null\}/.exec(code);
  if (!guard) {
    bad.push(`${rel}：找不到 {hasAnswer ? (…答案…) : null} 这种守卫写法（答案会掉进无条件渲染）`);
  } else if (!guard[1].includes("答案：")) {
    bad.push(`${rel}：守卫块里没有「答案：」（答案整段没了 ⇒ 教师版就没答案了；只许按真值渲染，不许删）`);
  }
  // 摘掉守卫块之后，源码里不许再出现「答案：」—— 出现即「无条件渲染」
  if (/答案：/.test(code.replace(/\{hasAnswer \? \([\s\S]*?\) : null\}/g, ""))) {
    bad.push(`${rel}：还有无条件渲染的「答案：」（学员版会露出空标签）`);
  }
  if (!/\{b\.explanation \?|\{b\.explanation &&/.test(code)) {
    bad.push(`${rel}：解析那行丢了真值判断（同一个卡片里两行必须同一口径）`);
  }
  if (!/\{String\.fromCharCode\(65 \+ i\)\}/.test(code)) {
    bad.push(`${rel}：选项列表没了（去答案不许把题面与选项也去掉）`);
  }
  return bad;
}

/** 内容面板的习题详情：两行都按真值渲染。 */
function auditPanelSite(src: string): string[] {
  const bad: string[] = [];
  const code = stripComments(src);
  const unguarded = code
    .replace(/\{body\.answer \? <p>答案：\{body\.answer\}<\/p> : null\}/g, "")
    .replace(/\{body\.explanation \? <p>解析：\{body\.explanation\}<\/p> : null\}/g, "");
  if (/答案：/.test(unguarded) || /解析：/.test(unguarded)) {
    bad.push("ContentPanel.tsx：习题详情里还有无条件渲染的「答案：/解析：」");
  }
  if (!/\{body\.answer \?/.test(code) || !/\{body\.explanation \?/.test(code)) {
    bad.push("ContentPanel.tsx：答案与解析两行不是按真值渲染");
  }
  return bad;
}

/* ---------------- ② 后端：去键的唯一出处 ---------------- */

function auditBackendSource(source: string): string[] {
  const bad: string[] = [];
  if (!/_QUIZ_HIDDEN_KEYS: tuple\[str, \.\.\.\] = \("answer", "explanation"\)/.test(source)) {
    bad.push('audience.py 的 _QUIZ_HIDDEN_KEYS 不是 ("answer", "explanation")（该隐藏的字段口径被改了）');
  }
  const fn = bodyOf(source, "student_quiz_body");
  if (!fn) {
    bad.push("audience.py 缺 student_quiz_body（学员版习题行投影的唯一出处）");
  } else if (!/not in _QUIZ_HIDDEN_KEYS/.test(fn)) {
    bad.push("student_quiz_body 没有按 _QUIZ_HIDDEN_KEYS 去掉键（答案字段可能原样漏出去）");
  }
  const rows = bodyOf(source, "student_rows");
  if (!/student_quiz_body\(body\)/.test(rows)) {
    bad.push("student_rows 的习题行没走 student_quiz_body（口径会分叉）");
  }
  if (/"answer": ""/.test(source) || /"explanation": ""/.test(source)) {
    bad.push('audience.py 里还有「清成空串」的写法（任务 90：要连键一起去掉，别留空壳）');
  }
  return bad;
}

/* ---------------- ③ 行为级：真 import、真调用 ---------------- */

function findPython(): string | null {
  const venv = join(repo, "backend", ".venv", "Scripts", "python.exe");
  if (existsSync(venv)) return venv;
  for (const name of ["python3", "python"]) {
    const res = spawnSync(name, ["-V"], { encoding: "utf8" });
    if (res.status === 0) return name;
  }
  return null;
}

const PY_HELPER = `
import json, os, sys, types
repo = sys.argv[1]
mut_path = sys.argv[2] if len(sys.argv) > 2 else ""
sys.path.insert(0, os.path.join(repo, "backend"))
import app.services.audience as audience

row = {"id": "q1", "kind": "quiz", "meta": {},
       "body": {"qtype": "choice", "difficulty": "easy", "question": "发酵的主角是谁？",
                "options": ["乳酸菌", "酵母菌"], "answer": "1", "explanation": "无氧呼吸。"}}
out = audience.student_rows([row])
again = audience.student_rows(out)
res = {
    "keys": sorted(out[0]["body"].keys()),
    "keys_plain": list(out[0]["body"].keys()),
    "idempotent": again == out,
    "teacher_untouched": row["body"]["answer"] == "1" and row["body"]["explanation"] == "无氧呼吸。",
    "hidden_keys": list(getattr(audience, "_QUIZ_HIDDEN_KEYS", [])),
    "has_fn": hasattr(audience, "student_quiz_body"),
}
if mut_path:
    src = open(mut_path, encoding="utf-8").read()
    mod = types.ModuleType("mutated_audience")
    mod.__dict__["__file__"] = audience.__file__
    exec(compile(src, mut_path, "exec"), mod.__dict__)
    res["mut_keys"] = sorted(mod.student_rows([row])[0]["body"].keys())
print(json.dumps(res, ensure_ascii=False))
`;

type Probe = {
  keys: string[];
  keys_plain: string[];
  idempotent: boolean;
  teacher_untouched: boolean;
  hidden_keys: string[];
  has_fn: boolean;
  mut_keys?: string[];
};

function runProbe(python: string, tmpDir: string, mutatedSource?: string): Probe | string {
  const helper = join(tmpDir, "probe_audience.py");
  writeFileSync(helper, PY_HELPER, "utf8");
  let mutPath = "";
  if (mutatedSource !== undefined) {
    mutPath = join(tmpDir, `mutated-audience-${Math.random().toString(36).slice(2)}.py`);
    writeFileSync(mutPath, mutatedSource, "utf8");
  }
  const res = spawnSync(python, [helper, repo, mutPath], {
    encoding: "utf8",
    cwd: join(repo, "backend"),
    env: {
      ...process.env,
      // 五件套 + mock：真机只读（与 verify.ps1 同一套口径）
      DATABASE_URL: `sqlite:///${tmpDir.replace(/\\/g, "/")}/probe.db`,
      STORAGE_DIR: join(tmpDir, "storage"),
      OBSIDIAN_VAULT_ROOT: join(tmpDir, "vault"),
      PPT_WORK_DIR: join(tmpDir, "storage", "ppt"),
      EXPORT_DIR: join(tmpDir, "storage", "exports"),
      LLM_PROVIDER: "mock",
      IMAGE_PROVIDER: "mock",
    },
  });
  if (res.status !== 0) {
    return `python 探针失败（exit=${res.status}）：${(res.stderr ?? "").trim() || "(无 stderr)"}`;
  }
  try {
    return JSON.parse(res.stdout) as Probe;
  } catch {
    return `python 探针输出不是 JSON：${String(res.stdout).trim().slice(0, 200)}`;
  }
}

/** ④ 行为级判据：答案/解析**键不存在**，其余字段一字不动。 */
function auditBehavior(probe: Probe, why: string): string[] {
  const bad: string[] = [];
  for (const key of ["answer", "explanation"]) {
    if (probe.keys.includes(key)) {
      bad.push(`${why}：学员版习题行还留着 ${key} 键（任务 90 要连键一起去掉，不是清成空串）`);
    }
  }
  const expected = ["difficulty", "options", "question", "qtype"];
  if ([...probe.keys].sort().join(" / ") !== [...expected].sort().join(" / ")) {
    bad.push(`${why}：学员版习题行的键 = ${probe.keys.join(" / ")}（应为 ${expected.join(" / ")}）`);
  }
  if (JSON.stringify(probe.hidden_keys) !== JSON.stringify(["answer", "explanation"])) {
    bad.push(`${why}：_QUIZ_HIDDEN_KEYS = ${probe.hidden_keys.join(" / ")}（应为 answer / explanation）`);
  }
  if (!probe.has_fn) bad.push(`${why}：audience 模块里没有 student_quiz_body（唯一出处丢了）`);
  if (!probe.idempotent) bad.push(`${why}：student_rows 不幂等`);
  if (!probe.teacher_untouched) bad.push(`${why}：教师版那一行的答案/解析被就地改了（不许动）`);
  return bad;
}

/* ---------------- 主流程 ---------------- */

const shareView = read(REL_SHARE_VIEW);
const preview = read(REL_PREVIEW);
const panel = read(REL_PANEL);
const audienceSrc = read(REL_AUDIENCE);

failures.push(...auditCardSite(shareView, "ShareView.tsx"));
failures.push(...auditCardSite(preview, "PreviewStep.tsx"));
failures.push(...auditPanelSite(panel));
failures.push(...auditBackendSource(audienceSrc));

const python = findPython();
const tmpDir = mkdtempSync(join(tmpdir(), "cg90-labels-"));
let mutationChecked = 0;

try {
  if (!python) {
    failures.push("找不到可用的 Python（行为级断言不能静默跳过：需要 backend/.venv 或 PATH 里的 python3 / python）");
  } else {
    const probe = runProbe(python, tmpDir);
    if (typeof probe === "string") {
      failures.push(probe);
    } else {
      failures.push(...auditBehavior(probe, "真模块"));
    }

    /* ---- ④ 变异自检：五条都必须变红 ---- */
    type Mutation = {
      name: string;
      base: string;
      mutate: (src: string) => string;
      red: (src: string) => string[];
    };
    const cardGuard = /\{hasAnswer \? \(\n\s+<Typography\.Paragraph style=\{\{ marginBottom: 4, fontSize: 13 \}\}>\n\s+答案：<Typography\.Text strong>\{answerText\}<\/Typography\.Text>\n\s+<\/Typography\.Paragraph>\n\s+\) : null\}/;
    const unguardedCard =
      "      <Typography.Paragraph style={{ marginBottom: 4, fontSize: 13 }}>\n"
      + "        答案：<Typography.Text strong>{answerText}</Typography.Text>\n"
      + "      </Typography.Paragraph>";
    const mutations: Mutation[] = [
      { name: "摘掉分享页「答案：」的守卫（回到无条件渲染）",
        base: shareView,
        mutate: (src) => src.replace(cardGuard, unguardedCard),
        red: (src) => auditCardSite(src, "ShareView.tsx") },
      { name: "摘掉预览页「答案：」的守卫",
        base: preview,
        mutate: (src) => src.replace(cardGuard, unguardedCard),
        red: (src) => auditCardSite(src, "PreviewStep.tsx") },
      { name: "摘掉内容面板「答案：/解析：」的守卫",
        base: panel,
        mutate: (src) => src.replace(PANEL_GUARDED, PANEL_UNGUARDED),
        red: (src) => auditPanelSite(src) },
      { name: "后端改回「清成空串」（键还在）",
        base: audienceSrc,
        mutate: (src) => src.replace(DROP_KEYS, EMPTY_STRINGS),
        red: (src) => auditBackendSource(src) },
      { name: "_QUIZ_HIDDEN_KEYS 少一个字段（answer 会漏出去）",
        base: audienceSrc,
        mutate: (src) => src.replace(HIDDEN_KEYS_DECL, HIDDEN_KEYS_MUTATED),
        red: (src) => auditBackendSource(src) },
    ];
    for (const { name, base, mutate, red } of mutations) {
      mutationChecked += 1;
      const mutated = mutate(base);
      if (mutated === base) {
        failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
        continue;
      }
      if (red(mutated).length === 0) {
        failures.push(`自检失败：变异「${name}」后断言仍然通过`);
      }
    }

    /* 行为级变异：后端改回清空串 ⇒ 真跑一次必须看到键又回来了 */
    const mutatedAudience = audienceSrc.replace(DROP_KEYS, EMPTY_STRINGS);
    if (mutatedAudience !== audienceSrc) {
      const mutProbe = runProbe(python, tmpDir, mutatedAudience);
      if (typeof mutProbe === "string") {
        failures.push(`行为级变异：${mutProbe}`);
      } else if (!["answer", "explanation"].some((k) => (mutProbe.mut_keys ?? []).includes(k))) {
        failures.push("自检失败：变异「后端改回清成空串」后**行为级**断言仍然通过（应答里该有 answer / explanation）");
      }
    } else {
      failures.push("自检失败：后端行为级变异没改到源码");
    }
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「空答案标签」断言失败（90）：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端空答案标签断言（90）通过："
  + "① 三处同族渲染点（分享页 / 预览页 / 内容面板）的「答案：」都从「有没有值」推导，"
  + "把守卫块摘掉后源码里再也找不到「答案：」（无条件渲染在这里判红），「解析：」同口径，题面与选项照常渲染；"
  + "② 后端去键的唯一出处：_QUIZ_HIDDEN_KEYS = (answer, explanation) + student_quiz_body() + student_rows 走它；"
  + "③ 行为级：真 import 真调用 —— 学员版习题行的键恰好是 题面/选项/题型/难度（answer 与 explanation 连键一起没有），"
  + "幂等、教师版那一行不被就地改；"
  + `${mutationChecked} 条源码级变异 + 1 条行为级变异全红。`);
