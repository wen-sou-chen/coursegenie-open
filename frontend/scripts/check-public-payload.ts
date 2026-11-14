/**
 * 任务 89 前端断言：**公开侧载荷白名单投影**（0 LLM）。
 *
 * **为什么有这条**：DSH 走查第 7 步第一次去看公开侧 API 载荷，发现分享页把内部字段原样
 * 吐给客户/学生 —— 真库一门课 `cost_cny` 27 行 / `usage` 27 行 / `history` 27 行（每篇 5 个
 * 历史版本，里面带答案）/ `input_fingerprint` 27 行 / `*_ids` 各 27 行 / `qc` 27 行；
 * `?view=student` 只剥了正文 ⇒ 学员版载荷仍有 `**答案**` / `**解析**` / `✓`。
 *
 * 本脚本钉四组：
 * ① **源码级**：`share.py` 必须有一个**白名单**常量 + 一个**纯函数** `public_meta`，
 *    且 `public_content`（每行）与 `public_deck`（课件）都真的过它；
 * ② **白名单自证**：清单里**不许**混进内部键（`usage` / `cost` / `token` / `fingerprint` /
 *    `history` / `locked` / `structured_block` / `content_issues` / `qc` / `llm_used` …）
 *    —— 黑名单式写法（`meta.pop("usage")` / 一个个 `del`）在这里直接判红；
 * ③ **行为级（真 import、真调用）**：按文件路径 import `app.services.share`，喂一份「塞满
 *    内部键 + 允许键 + 一个 2027 年新键」的 meta ⇒ 返回值**恰好**只剩允许键，脏输入 ⇒ `{}`；
 * ④ **变异自检**：㈠ 把 `public_meta` 改回「原样返回」、㈡ 把 `history` 塞进白名单、
 *    ㈢ 让 `public_content` 不过投影 ⇒ 三条都必须变红（否则这条护栏是假的）。
 *
 * ⛔ 0 LLM：行为级只 import 纯函数模块（不启动应用、不连库、不读真库），子进程显式
 * `LLM_PROVIDER=mock` 且五件套指向临时目录（与 `verify.ps1` 同款，真机零写入）。
 * 运行：`node scripts/check-public-payload.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 `scripts/verify.ps1`（前端断言 28 → 29 条）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const REL_SHARE = "backend/app/services/share.py";
const REL_PUBLIC = "backend/app/api/routes/public.py";
const REL_CONTENT = "backend/app/api/routes/content.py";
const REL_COURSES = "backend/app/api/routes/courses.py";
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/** `def NAME` 到下一个顶层 `def` / 文件尾（与 check-student-view.ts 同款）。 */
function bodyOf(source: string, name: string): string {
  const start = source.indexOf(`def ${name}(`);
  if (start < 0) return "";
  const rest = source.slice(start + 1);
  const next = rest.indexOf("\ndef ");
  return next < 0 ? rest : rest.slice(0, next);
}

/** 白名单里**允许**的键（§A1 的内容属性 13 个 + 课件版面属性 5 个）。 */
const EXPECTED_KEYS = [
  "node_key", "sources", "caption", "role", "from_lecture", "generated_at",
  "minutes", "layout", "chart_type", "concept_id", "aliases", "chapters",
  "prompt_version", "scope", "chapter_node_id", "chapter_key", "engine", "slide_count",
];

/** 白名单里**绝不许可**出现的内部键 / 词干。 */
const FORBIDDEN_STEMS = [
  "usage", "cost", "token", "fingerprint", "history", "locked", "structured_block",
  "content_issues", "diagram_failures", "style_anchor_ref", "quiz_issues", "qc",
  "llm_used", "quizzes", "student_version", "vault", "card_anchor", "card_seq",
];
const FORBIDDEN_KEYS = ["quiz_ids", "diagram_ids", "chart_ids", "image_ids"];

/** 喂给纯函数的 meta：真库里真实存在的内部键（值取自任务 89 扫描原文）+ 允许键 + 一个新键。 */
const RICH_META: Record<string, unknown> = {
  // 内部键
  usage: { llm_calls: 3, total_tokens: 15449, cost_cny: 0.032546, models: ["deepseek/deepseek-chat"] },
  input_fingerprint: "1e0069315f7b55ad",
  input_fingerprint_at: "2026-09-17T06:44:02+00:00",
  history: [{ body: { markdown: "**答案**：酵母菌\n**解析**：无氧呼吸\n✓ 乙\n" } }],
  locked: true,
  locked_reason: "样板节定稿",
  quiz_ids: ["q1"], diagram_ids: ["d1"], chart_ids: ["c1"], image_ids: ["i1"],
  structured_block: { points: [] }, content_issues: ["表头缺单位"],
  diagram_failures: ["节点重叠"], style_anchor_ref: "1.1", quiz_issues: ["答案与选项长度不匹配"],
  qc: { score: 88 }, llm_used: true, quizzes: 3, student_version: true,
  card_anchor: "1.1", card_seq: 1,
  // 允许的内容属性（值原样保留，含「值不改动」的判据）
  node_key: "1.1",
  sources: [{ section: "核心讲解", quote: "发酵靠的是微生物", paragraph: 33 }],
  caption: "图 1 发酵曲线",
  role: "body",
  from_lecture: true,
  generated_at: "2026-09-17T06:44:02+00:00",
  minutes: 6,
  layout: "two-column",
  chart_type: "bar",
  concept_id: "c-1",
  aliases: ["发酵"],
  chapters: [1],
  prompt_version: "lecture-v14",
  // 2027 年才可能出现的内部键：白名单的判据就是它默认不外泄
  future_internal_key: "以后新长的统计",
};

/* ======================= ① 源码级：白名单 + 纯函数 + 接线 ======================= */

function auditSource(source: string, publicSource: string, internalSources: string[]): string[] {
  const bad: string[] = [];
  if (!/PUBLIC_META_KEYS\s*:\s*tuple\[str, \.\.\.\]\s*=\s*\(/.test(source)) {
    bad.push("share.py 缺 PUBLIC_META_KEYS 白名单常量（公开侧的键没有一个唯一出处）");
  }
  const whitelist = bodyOf(source, "public_meta");
  if (!whitelist) {
    bad.push("share.py 取不到 public_meta 的函数体（断言失效）");
  } else {
    if (!/for key in PUBLIC_META_KEYS if key in meta/.test(whitelist)) {
      bad.push("public_meta 不是「按白名单筛」的写法（必须逐个 key 过 PUBLIC_META_KEYS）");
    }
    for (const blacklist of [".pop(", "del meta", "meta.pop", "FORBIDDEN", "BLOCKED_KEYS"]) {
      if (whitelist.includes(blacklist)) {
        bad.push(`public_meta 里出现了黑名单式写法 ${blacklist}（规格要的是白名单）`);
      }
    }
    if (!/if not isinstance\(meta, dict\)/.test(whitelist)) {
      bad.push("public_meta 没有挡住非 dict 输入（脏数据会被原样透传）");
    }
  }
  const content = bodyOf(source, "public_content");
  if (!content) {
    bad.push("share.py 取不到 public_content 的函数体");
  } else if (!/public_meta\(|public_row\(/.test(content)) {
    bad.push("public_content 没有过投影（内容行的 meta 会原样出门）");
  }
  const row = bodyOf(source, "public_row");
  if (!row || !/public_meta\(/.test(row)) {
    bad.push("share.py 缺 public_row（内容行投影的唯一出口）");
  }
  const deck = bodyOf(source, "public_deck");
  if (!deck) {
    bad.push("share.py 取不到 public_deck 的函数体（§A3 要求课件也过一遍）");
  } else if (!/meta = public_meta\(item\.meta\)/.test(deck)) {
    bad.push("public_deck 没有过 public_meta（课件 meta 可能带 usage）");
  }
  // 公开侧四个接口都在同一组 public_* 函数上（路由不许自己拼载荷）
  for (const fn of ["public_overview", "public_plan", "public_content", "public_ppt"]) {
    if (!publicSource.includes(`share_service.${fn}(`)) {
      bad.push(`路由 /public 没有走 share_service.${fn}()（口径会分叉）`);
    }
  }
  // 内部侧：不许被投影函数碰到（只收窄公开侧）
  for (const [name, text] of internalSources.entries()) {
    if (/public_meta\(|PUBLIC_META_KEYS|public_row\(/.test(text)) {
      bad.push(`内部路由（第 ${name + 1} 个文件）里出现了公开侧投影（会把内部侧一起砍）`);
    }
  }
  return bad;
}

/** ② 白名单自证：清单里不许有内部键。 */
function auditWhitelist(keys: string[]): string[] {
  const bad: string[] = [];
  for (const key of keys) {
    if (FORBIDDEN_KEYS.includes(key)) {
      bad.push(`白名单里混进了内部键 ${key}`);
    }
    for (const stem of FORBIDDEN_STEMS) {
      if (key.includes(stem)) bad.push(`白名单里混进了内部键 ${key}（命中词干 ${stem}）`);
    }
  }
  const missing = EXPECTED_KEYS.filter((k) => !keys.includes(k));
  if (missing.length) bad.push(`白名单少了内容属性：${missing.join(" / ")}`);
  const extra = keys.filter((k) => !EXPECTED_KEYS.includes(k));
  if (extra.length) bad.push(`白名单多出未登记的键：${extra.join(" / ")}`);
  return bad;
}

/** 从源码里**真的解析出** PUBLIC_META_KEYS 的键（不许只看「清单里有没有这几个字」）。 */
function parseWhitelist(source: string): string[] {
  const start = source.indexOf("PUBLIC_META_KEYS");
  if (start < 0) return [];
  const open = source.indexOf("(", start);
  const close = source.indexOf(")", open);
  if (open < 0 || close < 0) return [];
  return [...source.slice(open + 1, close).matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

/* ======================= ③ 行为级：真 import、真调用 ======================= */

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
meta = json.loads(sys.argv[2])
mut_path = sys.argv[3] if len(sys.argv) > 3 else ""
sys.path.insert(0, os.path.join(repo, "backend"))
import app.services.share as share

out = {
    "keys": list(share.PUBLIC_META_KEYS),
    "out": share.public_meta(meta),
    "dirty": [share.public_meta(x) for x in (None, [], "", 3)],
    "sources_same_object": share.public_meta({"sources": meta["sources"]})["sources"] is meta["sources"],
}
if mut_path:
    src = open(mut_path, encoding="utf-8").read()
    mod = types.ModuleType("mutated_share")
    mod.__dict__["__file__"] = share.__file__
    exec(compile(src, mut_path, "exec"), mod.__dict__)
    out["mut"] = mod.public_meta(meta)
print(json.dumps(out, ensure_ascii=False))
`;

type Probe = {
  keys: string[];
  out: Record<string, unknown>;
  dirty: unknown[];
  sources_same_object: boolean;
  mut?: Record<string, unknown>;
};

function runProbe(python: string, tmpDir: string, mutatedSource?: string): Probe | string {
  const helper = join(tmpDir, "probe_share.py");
  writeFileSync(helper, PY_HELPER, "utf8");
  let mutPath = "";
  if (mutatedSource !== undefined) {
    mutPath = join(tmpDir, `mutated-share-${Math.random().toString(36).slice(2)}.py`);
    writeFileSync(mutPath, mutatedSource, "utf8");
  }
  const res = spawnSync(python, [helper, repo, JSON.stringify(RICH_META), mutPath], {
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

/** ④ 行为级判据：返回**恰好**是允许键（且值原样）。 */
function auditBehavior(probe: Probe, why: string): string[] {
  const bad: string[] = [];
  const expected = EXPECTED_KEYS.filter((k) => k in RICH_META);
  const got = Object.keys(probe.out ?? {}).sort();
  if (JSON.stringify(got) !== JSON.stringify([...expected].sort())) {
    bad.push(`${why}：public_meta 返回的键 = ${got.join(" / ")}（应为 ${expected.join(" / ")}）`);
  }
  for (const key of [...FORBIDDEN_KEYS, "usage", "history", "input_fingerprint",
    "input_fingerprint_at", "locked", "locked_reason", "structured_block",
    "content_issues", "diagram_failures", "style_anchor_ref", "quiz_issues", "qc",
    "llm_used", "quizzes", "student_version", "card_anchor", "card_seq",
    "future_internal_key"]) {
    if (key in (probe.out ?? {})) bad.push(`${why}：内部键 ${key} 混进了公开载荷`);
  }
  if (JSON.stringify(probe.out?.sources) !== JSON.stringify(RICH_META.sources)) {
    bad.push(`${why}：允许键的值被改动了（只许投影、不许改值）`);
  }
  if (!probe.sources_same_object) {
    bad.push(`${why}：允许键的值不是原对象（多了一层拷贝，容易顺带改内容）`);
  }
  for (const dirty of probe.dirty ?? []) {
    if (JSON.stringify(dirty) !== "{}") bad.push(`${why}：脏输入没有回 {}（实得 ${JSON.stringify(dirty)}）`);
  }
  return bad;
}

/* ======================= 主流程 ======================= */

const source = read(REL_SHARE);
failures.push(...auditSource(source, read(REL_PUBLIC), [read(REL_CONTENT), read(REL_COURSES)]));
const sourceKeys = parseWhitelist(source);
if (sourceKeys.length === 0) {
  failures.push("share.py 里解析不出 PUBLIC_META_KEYS 的键（白名单被挪走或改了形状）");
}
failures.push(...auditWhitelist(sourceKeys));

const python = findPython();
const tmpDir = mkdtempSync(join(tmpdir(), "cg89-payload-"));
let mutationChecked = 0;

try {
  if (!python) {
    failures.push("找不到可用的 Python（行为级断言不能静默跳过：需要 backend/.venv 或 PATH 里的 python3 / python）");
  } else {
    const probe = runProbe(python, tmpDir);
    if (typeof probe === "string") {
      failures.push(probe);
    } else {
      failures.push(...auditWhitelist(probe.keys));
      failures.push(...auditBehavior(probe, "真模块"));
    }

    /* ---- ④ 变异自检：三条都必须变红 ---- */
    // 每条变异声明「哪几层断言必须变红」——三层各管一段，互不代替：
    // source=纯函数写法与接线 / whitelist=清单内容 / behavior=真跑一次的返回键。
    type Mutation = {
      name: string;
      mutate: (src: string) => string;
      sourceRed: boolean;
      whitelistRed: boolean;
      leaked: string[];
    };
    const mutations: Mutation[] = [
      { name: "把 public_meta 改回「原样返回」",
        mutate: (src) => src.replace(
          "    return {key: meta[key] for key in PUBLIC_META_KEYS if key in meta}",
          "    return dict(meta)"),
        sourceRed: true, whitelistRed: false,
        leaked: ["usage", "history", "input_fingerprint", "future_internal_key"] },
      { name: "把 history 塞回白名单",
        mutate: (src) => src.replace('    "node_key", "sources",',
                                     '    "node_key", "sources", "history",'),
        sourceRed: false, whitelistRed: true,
        leaked: ["history"] },
      { name: "让 public_content 不过投影（meta 原样出门）",
        mutate: (src) => src.replace("    rows = [public_row(row) for row in rows]\n", ""),
        sourceRed: true, whitelistRed: false, leaked: [] },
    ];
    for (const { name, mutate, sourceRed, whitelistRed, leaked } of mutations) {
      mutationChecked += 1;
      const mutated = mutate(source);
      if (mutated === source) {
        failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
        continue;
      }
      if (sourceRed && auditSource(mutated, read(REL_PUBLIC), []).length === 0) {
        failures.push(`自检失败：变异「${name}」后**源码级**断言仍然通过`);
      }
      if (whitelistRed && auditWhitelist(parseWhitelist(mutated)).length === 0) {
        failures.push(`自检失败：变异「${name}」后**白名单**断言仍然通过`);
      }
      if (!python || leaked.length === 0) continue;
      const mutProbe = runProbe(python, tmpDir, mutated);
      if (typeof mutProbe === "string") {
        failures.push(`变异「${name}」：${mutProbe}`);
        continue;
      }
      if (leaked.filter((k) => k in (mutProbe.mut ?? {})).length === 0) {
        failures.push(`自检失败：变异「${name}」后**行为级**断言仍然通过（应泄漏 ${leaked.join(" / ")}）`);
      }
    }
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「公开侧载荷」断言失败（89）：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端公开侧载荷断言（89）通过："
  + "① 源码级：share.py 有 PUBLIC_META_KEYS 白名单 + 纯函数 public_meta（非 dict ⇒ {}），"
  + "public_content 每行都过 public_row、public_deck 也过 public_meta，四个公开接口同口径、内部路由不沾；"
  + `② 白名单自证：${EXPECTED_KEYS.length} 个键全是内容属性，没混进 usage/cost/token/fingerprint/history/locked/_ids/qc；`
  + "③ 行为级：真 import 真调用 —— 塞满内部键 + 一个 2027 年新键的 meta 只剩允许键（值原对象、未被改动），脏输入回 {}；"
  + `${mutationChecked} 条变异自检全红（原样返回 / history 回白名单 / public_content 不过投影）。`);
