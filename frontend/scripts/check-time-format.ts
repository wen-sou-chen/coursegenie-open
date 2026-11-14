/**
 * 任务 83 前端断言：时间口径统一（任务 81 的 A2 遗留）。
 *
 * 起因（DSH 2026-09-23 复验 81）：任务 81 只做了一半 —— 质量报告那处时间对了，
 * **「版本」面板把库里的裸 UTC 当本地时间显示，比真实本地时间少 8 小时**（日期都会变）：
 * `course_versions.created_at` 是 `DateTime(timezone=True)` 配 `utcnow()`，但 SQLite 不存时区
 * ⇒ 读回来是 naive，`isoformat()` 出来 `2026-09-21T06:24:46.708384`（没有 `Z` / `+00:00`），
 * 而按 ES 规范 `new Date(...)` 把没有时区标记的串当**本地时间** ⇒ 真机显示 `06:24`，
 * 而这条快照的真实本地时间是 `14:24`。
 *
 * ⚠️ 更要紧的是**原来的断言会把「正确换算」判成失败**（参照实现 `new Date(iso).toLocaleString("sv-SE")`
 * 与实现共享同一个错误假设）⇒ 本脚本把四件事钉死：
 * ① 裸时间戳必须按 UTC 解释（`formatLocalTime` 的行为，含 UTC+8 的具体数字）；
 * ② 时间格式化**唯一出处**（`lib/datetime.ts`，全仓不再有自己写的 `function formatTime`）；
 * ③ 后端接口出口恒带时区（`versions.py` / `share.py` 不再吐裸 ISO 或裸 datetime）；
 * ④ `POST /plan/{id}/check` 有 INFO 日志（与 `material.py` 的同类写入口同款）。
 * 任务 84 增量：课件生成时间那两处（PPT 面板 / 第 4 步预览页共用的 `slideLabel`、大纲的「上次自检」）
 * 原先**各自裸切字符串** ⇒ 这里补两条 `2026-09-17T07:20:00(+00:00) ⇒ 本地 15:20` 的行为用例，
 * 并把这两个文件加进「唯一出处」清单；**裸切写法的源码级反面扫描**放在 `check-bare-time-slice.ts`。
 * 末尾带**行为级**变异自检：把 `datetime.ts` 的 UTC 归一化删掉、写成临时模块重新加载，
 * 行为断言必须变红（证明断言不是靠源码正则自说自话）。
 *
 * 运行：`node scripts/check-time-format.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { formatLocalTime } from "../src/lib/datetime.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

/** 前端源码树（相对仓库根的 posix 路径 → 内容），用来查「还有没有第二处实现」。 */
function walkSrc(dir: string, out: Map<string, string> = new Map()): Map<string, string> {
  for (const entry of readdirSync(join(repo, dir), { withFileTypes: true })) {
    const rel = `${dir}/${entry.name}`;
    if (entry.isDirectory()) walkSrc(rel, out);
    else if (/\.tsx?$/.test(entry.name)) out.set(rel, read(rel));
  }
  return out;
}

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ======================= ① 行为：裸时间戳按 UTC 解释（`formatLocalTime`） ======================= */
type Impl = (iso?: string | null, fallback?: string) => string;

/** 与实现**独立**的参照：先拿 UTC 定点（`Date.UTC`），再交给 Intl 按本地时区渲染。 */
const localOfUtc = (ms: number) =>
  new Date(ms).toLocaleString("sv-SE", { hour12: false }).slice(0, 16);
const UTC8 = new Date().getTimezoneOffset() === -480;
const SHAPE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/;

/** `utc8` 是「本机在 UTC+8 时该显示成什么」，取自真库真实记录（DSH 已逐位核过）。 */
const CASES: { iso: string; utcMs: number; utc8: string; why: string }[] = [
  { iso: "2026-09-21T06:24:46.708384", utcMs: Date.UTC(2026, 8, 21, 6, 24),
    utc8: "2026-09-21 14:24", why: "版本面板那条快照：裸 UTC 没按 UTC 解释（少 8 小时）" },
  { iso: "2026-09-23T07:46:24.527564", utcMs: Date.UTC(2026, 8, 23, 7, 46),
    utc8: "2026-09-23 15:46", why: "DSH 决定性实验那条：今早 07:46:24 建快照不许显示成昨天" },
  { iso: "2026-09-22T13:56:02+00:00", utcMs: Date.UTC(2026, 8, 22, 13, 56),
    utc8: "2026-09-22 21:56", why: "质量报告那条：带 +00:00 的没换算到浏览器本地时区" },
  // 任务 84 §B3：课件生成时间那两处（PPT 面板 / 第 4 步预览页）的数字，真机上就是少 8 小时。
  { iso: "2026-09-17T07:20:00+00:00", utcMs: Date.UTC(2026, 8, 17, 7, 20),
    utc8: "2026-09-17 15:20", why: "课件 deck.generated_at（带 +00:00）：裸切会显示成 07:20（与课件文件 mtime 15:24 差 8 小时）" },
  { iso: "2026-09-17T07:20:00", utcMs: Date.UTC(2026, 8, 17, 7, 20),
    utc8: "2026-09-17 15:20", why: "同一时刻的裸串（没有时区标记）也必须按 UTC 解释成 15:20" },
];

function auditBehavior(impl: Impl): string[] {
  const bad: string[] = [];
  for (const { iso, utcMs, utc8, why } of CASES) {
    const got = impl(iso);
    if (!SHAPE.test(got)) { bad.push(`impl("${iso}") 不是本地时间人类可读格式：${got}`); continue; }
    if (/[TZ]/.test(got) || /\+\d{2}:\d{2}$/.test(got)) bad.push(`impl("${iso}") 还带 T / Z / 时区：${got}`);
    if (/\.\d+/.test(got)) bad.push(`impl("${iso}") 还带微秒：${got}`);
    if (got !== localOfUtc(utcMs)) bad.push(`${why}：impl("${iso}") = ${got}，应为 ${localOfUtc(utcMs)}`);
    if (UTC8 && got !== utc8) bad.push(`UTC+8 机器上 impl("${iso}") 应为 ${utc8}，实际 ${got}`);
  }
  // 带非零偏移的串：按它自己的时区换算（不能一律当 UTC）
  const plus8 = impl("2026-09-22T21:56:02+08:00");
  if (plus8 !== localOfUtc(Date.UTC(2026, 8, 22, 13, 56))) {
    bad.push(`impl("2026-09-22T21:56:02+08:00") = ${plus8}，应等于 UTC 13:56 的本地渲染`);
  }
  // 三种既有输入的行为必须逐字不变
  if (impl("") !== "-") bad.push(`空串兜底不是「-」：${impl("")}`);
  if (impl(null) !== "-") bad.push(`null 兜底不是「-」：${impl(null)}`);
  if (impl(null, "永不过期") !== "永不过期") bad.push(`自定义兜底没生效：${impl(null, "永不过期")}`);
  if (impl("不是时间") !== "不是时间") bad.push(`解析失败时应原样返回：${impl("不是时间")}`);
  return bad;
}
failures.push(...auditBehavior(formatLocalTime));

/* ======================= ② 唯一出处：前端不再有自己写的 formatTime ======================= */
const srcFiles = walkSrc("frontend/src");

function auditSingleSource(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const offenders = [...files].filter(([, src]) => /function\s+formatTime\s*\(/.test(src)).map(([p]) => p);
  if (offenders.length > 0) {
    bad.push(`还有自己写的 formatTime（应全部删掉、只留 lib/datetime.ts 的 formatLocalTime）：${offenders.join("、")}`);
  }
  const dt = files.get("frontend/src/lib/datetime.ts") ?? "";
  if (!/export function formatLocalTime/.test(dt)) bad.push("lib/datetime.ts 没有导出 formatLocalTime（唯一出处）");
  if (!/BARE_LOCAL_SHAPE/.test(dt)) bad.push("lib/datetime.ts 没有「裸时间戳」判定（等于取消了 UTC 解释）");
  if (!/\$\{text\.replace\(" ", "T"\)\}Z/.test(dt)) {
    bad.push("lib/datetime.ts 没给裸时间戳补 `Z`（按 ES 规范会被当本地时间，界面上少 8 小时）");
  }
  for (const rel of [
    "frontend/src/components/VersionsPanel.tsx",
    "frontend/src/components/QualityPanel.tsx",
    "frontend/src/components/UsagePanel.tsx",
    "frontend/src/components/DeliveryPanel.tsx",
    "frontend/src/components/SharePanel.tsx",
    "frontend/src/pages/ShareView.tsx",
    // 任务 84：这两处原先各自裸切字符串（PPT 面板的 slideLabel / 大纲的「上次自检」）
    "frontend/src/components/PptDeckBlocks.tsx",
    "frontend/src/components/OutlinePanel.tsx",
  ]) {
    const src = files.get(rel) ?? "";
    if (!/from "\.\.\/(\.\.\/)?lib\/datetime"/.test(src)) {
      bad.push(`${rel} 没引用 lib/datetime（时间应只有一处实现）`);
    }
  }
  const usage = files.get("frontend/src/components/UsagePanel.tsx") ?? "";
  if (!/报告生成于 \{formatLocalTime\(data\.generated_at\)\}/.test(usage)) {
    bad.push("UsagePanel 的「报告生成于」没走 formatLocalTime（还是裸 ISO）");
  }
  if (/\{data\.generated_at\}/.test(usage)) bad.push("UsagePanel 里还有原样渲染的 {data.generated_at}");
  return bad;
}
failures.push(...auditSingleSource(srcFiles));

/* ======================= ③ 后端出口：ISO 恒带时区 ======================= */
const timefmtPy = read("backend/app/services/timefmt.py");
const sharePy = read("backend/app/services/share.py");
const versionsPy = read("backend/app/api/routes/versions.py");

/** 时间字段（来自库里的 naive datetime）在分享接口里的四个落点。 */
const SHARE_TIME_FIELDS = ["expires_at", "revoked_at", "created_at", "last_accessed_at"];

function auditBackendTime(t: string, share: string, versions: string): string[] {
  const bad: string[] = [];
  if (!/def as_aware\(/.test(t)) bad.push("timefmt.py 没有 as_aware（时间口径的唯一实现）");
  if (!/def as_utc_iso\(/.test(t)) bad.push("timefmt.py 没有 as_utc_iso（接口出口）");
  if (/def as_aware\(/.test(share)) bad.push("share.py 又自己实现了一份 as_aware（只允许 timefmt.py 一处）");
  if (!/from app\.services\.timefmt import as_aware, as_utc_iso/.test(share)) {
    bad.push("share.py 没从 timefmt 引入（唯一实现）");
  }
  for (const field of SHARE_TIME_FIELDS) {
    if (new RegExp(`"${field}": link\\.${field},`).test(share)) {
      bad.push(`share.py 的 ${field} 还是裸 datetime（前端会当本地时间，少 8 小时）`);
    }
    if (!new RegExp(`"${field}": as_utc_iso\\(`).test(share)) {
      bad.push(`share.py 的 ${field} 没走 as_utc_iso（接口出口必须带时区）`);
    }
  }
  if (/\.isoformat\(\)/.test(versions)) bad.push("versions.py 又直接 isoformat()（裸 UTC ⇒ 版本面板少 8 小时）");
  if (!/as_utc_iso\(v\.created_at\)/.test(versions)) bad.push("versions.py 的 created_at 没走 as_utc_iso");
  return bad;
}
failures.push(...auditBackendTime(timefmtPy, sharePy, versionsPy));

/* ======================= ④ 写入口的 INFO 日志（任务 81 新增的那条） ======================= */
const planPy = read("backend/app/api/routes/plan.py");

function auditPlanLog(py: string): string[] {
  const bad: string[] = [];
  if (!/^import logging$/m.test(py)) bad.push("plan.py 没 import logging");
  if (!/logger = logging\.getLogger\(__name__\)/.test(py)) bad.push("plan.py 没有模块级 logger");
  if (!/logger\.info\("大纲自检补跑入口被调用/.test(py)) {
    bad.push("POST /plan/{id}/check 没有 INFO 日志（任务 72 的同类写入口有两条）");
  }
  if (!/不调模型、不产生用量/.test(py)) bad.push("日志没说清这条入口「不调模型、不产生用量」");
  return bad;
}
failures.push(...auditPlanLog(planPy));

/* ======================= 变异自检（含行为级：重写临时模块再加载） ======================= */
const tmpDir = mkdtempSync(join(tmpdir(), "cg83-time-"));
let mutationChecked = 0;

async function behaviorOfSource(source: string): Promise<string[]> {
  const file = join(tmpDir, `mutated-${Math.random().toString(36).slice(2)}.ts`);
  writeFileSync(file, source, "utf8");
  const mod = await import(pathToFileURL(file).href) as { formatLocalTime: Impl };
  return auditBehavior(mod.formatLocalTime);
}

try {
  const datetimeTs = read("frontend/src/lib/datetime.ts");
  const noUtc = datetimeTs.replace('`${text.replace(" ", "T")}Z`', "text");
  if (noUtc === datetimeTs) {
    failures.push("自检失败：变异「去掉裸时间戳按 UTC 解释」没改到源码（断言可能已失效）");
  } else if ((await behaviorOfSource(noUtc)).length === 0) {
    failures.push("自检失败：把 datetime.ts 的 UTC 归一化删掉后，行为断言仍然通过");
  }

  // 另一种**看似能修**的写法：把「裸时间戳」的判定放宽到连 Z / ±hh:mm 一起包进来
  // （等于把带时区的串再补一个 Z）⇒ 带 +08:00 的串会解析失败 / 换算错。
  const wider = "const BARE_LOCAL_SHAPE = /^\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}(:\\d{2}(\\.\\d+)?)?(Z|[+-]\\d{2}:\\d{2})?$/;";
  const forceUtc = datetimeTs.replace(/const BARE_LOCAL_SHAPE = .*/, () => wider);
  if (forceUtc === datetimeTs) {
    failures.push("自检失败：变异「把带时区的串也当裸时间戳」没改到源码");
  } else if ((await behaviorOfSource(forceUtc)).length === 0) {
    failures.push("自检失败：把带 +08:00 的串也当裸时间戳后，行为断言仍然通过");
  }

  const mutations: [string, string, string, (src: string) => string[]][] = [
    ["把 versions.py 改回裸 isoformat()", versionsPy,
      versionsPy.replace("as_utc_iso(v.created_at)", "v.created_at.isoformat()"),
      (s) => auditBackendTime(timefmtPy, sharePy, s)],
    ["把 share.py 的 expires_at 改回裸 datetime", sharePy,
      sharePy.replace('"expires_at": as_utc_iso(link.expires_at),', '"expires_at": link.expires_at,'),
      (s) => auditBackendTime(timefmtPy, s, versionsPy)],
    ["把 share.py 里的 as_utc_iso 摘掉（只留裸 datetime）", sharePy,
      sharePy.replace(/as_utc_iso\(/g, "(").replace("import as_aware, as_utc_iso", "import as_aware"),
      (s) => auditBackendTime(timefmtPy, s, versionsPy)],
    ["删掉 POST /plan/{id}/check 的 INFO 日志", planPy,
      planPy.replace(/^ *logger\.info\("大纲自检补跑入口被调用[\s\S]*?\n/m, ""),
      auditPlanLog],
    ["让 DeliveryPanel 又自己写一份 formatTime", srcFiles.get("frontend/src/components/DeliveryPanel.tsx") ?? "",
      (srcFiles.get("frontend/src/components/DeliveryPanel.tsx") ?? "")
        .replace("function humanBytes", "function formatTime(iso?: string | null): string { return String(iso); }\n\nfunction humanBytes"),
      (s) => auditSingleSource(new Map(srcFiles).set("frontend/src/components/DeliveryPanel.tsx", s))],
  ];
  for (const [name, base, mutated, run] of mutations) {
    mutationChecked += 1;
    if (mutated === base) {
      failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
    } else if (run(mutated).length === 0) {
      failures.push(`自检失败：变异「${name}」后判定仍然通过`);
    }
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「时间口径统一」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端时间口径断言（83 + 84）通过："
  + "① 裸时间戳按 UTC 解释（2026-09-21T06:24:46.708384 ⇒ 本地 2026-09-21 14:24；任务 84 的课件时间 "
  + "2026-09-17T07:20:00(+00:00) ⇒ 15:20）、带 ±hh:mm 的按自身偏移、空值 / 解析失败行为不变；"
  + "② 时间格式化唯一出处（frontend/src/lib/datetime.ts，全仓自己写的 function formatTime = 0 处，8 个面板/页面都引用它，"
  + "含任务 84 的 PptDeckBlocks / OutlinePanel）；"
  + `③ 后端出口恒带时区（timefmt.py 唯一实现；share.py 的 ${SHARE_TIME_FIELDS.join(" / ")} 与 versions.py 的 created_at 都走 as_utc_iso）；`
  + "④ POST /plan/{id}/check 有 INFO 日志；"
  + `${mutationChecked + 2} 条变异自检全红（含 2 条行为级：重写临时模块后行为断言必须变红）；`
  + "裸切写法的源码级反面扫描见 check-bare-time-slice.ts");
