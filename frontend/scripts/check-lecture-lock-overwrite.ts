/**
 * 任务 91 前端断言：**单篇「重跑这一节」看锁 + 覆盖即摘徽标**（0 LLM）。
 *
 * **为什么有这条**：`POST /api/lecture/generate` 通篇没有 `_locked_of` 检查，已「审核通过」
 * 的那一节照样被重跑覆盖；而覆盖后 meta 是**合并写法** ⇒ `locked` / `locked_reason` 被原样
 * 保留 ⇒ **界面说「审核通过」、正文其实是没审过的那一版**（真库包子课 1.1 就是现成的例子）。
 * 批量路径本来就是对的（`skip_locked=True` 跳过已锁节点并列进 `locked_skipped`）。
 *
 * 本脚本钉四组：
 * ① **源码级（后端）**：请求体有 `allow_overwrite_locked` 且**默认 False**；路由在
 *    `_target_lecture` 之后、入队之前做锁检查并 **409**；服务在**正文被覆盖之后**清掉
 *    `locked` / `locked_reason`；**批量路径的 `skip_locked` 语义一个字没动**（路由里
 *    根本不许出现 `skip_locked`）；
 * ② **源码级（前端）**：`WorkbenchStep.tsx` 的两个「重新生成」入口在锁定态下先弹确认
 *    （文案必须说清「会覆盖老师审过的那一版、并摘除徽标」）、确认后把
 *    `allowOverwriteLocked` 置真；主按钮在锁定态下**自己不许直接触发**（rc-trigger 会把
 *    子元素 onClick 与自己串起来，所以必须显式置 undefined）；覆盖成功后要**明说徽标已摘**；
 * ③ **行为级（真跑 Python，真 ASGI 请求）**：造课 → 出大纲 → 生成 1 篇 → 手改正文 +
 *    「审核通过」加锁（模拟真库包子课 1.1）⇒ ㈠批量 force=true：`locked_skipped` 有它、
 *    正文 sha1 不变、锁与原因都在；㈡单篇默认：**409**、正文/锁/历史都不动（不带 item_id 也 409）；
 *    ㈢单篇 `allow_overwrite_locked=true`：正文换新版、`locked=False`、`locked_reason` 清掉、
 *    旧版进 history；㈣解锁后的单篇照旧能覆盖；㈤同族「按质检建议重生成」也不许留徽标；
 * ④ **变异自检**：㈠去掉路由拦截、㈡去掉服务的摘徽标、㈢请求体默认改回 True、
 *    ㈣主按钮去掉锁定守卫、㈤确认文案不说徽标 ⇒ 源码级各自变红；另加一条**行为级变异**：
 *    去掉摘徽标后真跑一次 ⇒ 必须重现「正文换了、locked 还在」的老毛病。
 *
 * ⛔ 0 LLM：探针全程 `LLM_PROVIDER=mock`，五个环境变量（DATABASE_URL / STORAGE_DIR /
 * OBSIDIAN_VAULT_ROOT / PPT_WORK_DIR / EXPORT_DIR）全指临时目录；探针内部还会**自检**
 * 这五个路径必须在临时目录下，否则直接拒跑（真库/真 vault 只做只读指纹）。
 * 运行：`node scripts/check-lecture-lock-overwrite.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 `scripts/verify.ps1`（前端断言 32 → 33 条）。
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(join(here, "..", ".."));
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
const stripComments = (s: string) =>
  s.replace(/\{?\/\*[\s\S]*?\*\/\}?/g, "").replace(/^\s*#.*$/gm, "").replace(/^\s*\/\/.*$/gm, "");

const REL_SVC = "backend/app/services/lecture.py";
const REL_ROUTE = "backend/app/api/routes/lecture.py";
const REL_SCHEMA = "backend/app/schemas/lecture.py";
const REL_TSX = "frontend/src/components/steps/WorkbenchStep.tsx";
const REL_API = "frontend/src/api.ts";
const REL_PROBE = "backend/tests/_task91_lock_probe.py";

const ROUTE_GATE = '    if blocked and not payload.allow_overwrite_locked:\n'
  + '        raise HTTPException(409, blocked)\n';
const SVC_CLEAR = '    if _locked_of(item):\n'
  + '        meta = dict(item.meta or {})\n'
  + '        meta["locked"] = False\n'
  + '        meta.pop("locked_reason", None)\n'
  + '        item.meta = meta\n';
const SCHEMA_DEFAULT = "        default=False,\n";
const TSX_PRIMARY_GUARD = "                  onClick={lectureLocked\n"
  + "                    ? undefined\n"
  + "                    : () => genLecture.mutate({ tone: \"正式\", itemId: lecture?.id })}\n";
const TSX_TITLE_LOCKED = "这一节已审核通过并锁定：重新生成会覆盖老师审过的那一版，并摘除「审核通过」徽标（新版需重新审核）。";

const failures: string[] = [];

function expect(cond: boolean, msg: string): void {
  if (!cond) failures.push(msg);
}

/* ---------------- ① 后端源码级 ---------------- */

function auditBackend(svc: string, route: string, schema: string): string[] {
  const bad: string[] = [];
  const svcC = stripComments(svc);
  const routeC = stripComments(route);

  if (!/allow_overwrite_locked: bool = Field\(/.test(schema)) {
    bad.push(`${REL_SCHEMA}：请求体没有 allow_overwrite_locked（单篇覆盖就没有显式开关）`);
  } else if (!/allow_overwrite_locked: bool = Field\(\s*default=False/.test(schema)) {
    bad.push(`${REL_SCHEMA}：allow_overwrite_locked 的默认值不是 False（默认必须是「不覆盖」）`);
  }

  if (!/blocked = locked_overwrite_blocked\(current\)/.test(routeC)) {
    bad.push(`${REL_ROUTE}：单篇 /generate 没有算「已锁定」拦截（缺 locked_overwrite_blocked(current)）`);
  }
  if (!/raise HTTPException\(409, blocked\)/.test(routeC)) {
    bad.push(`${REL_ROUTE}：已锁定的单篇重跑没有返回 409`);
  }
  const gateAt = routeC.indexOf("raise HTTPException(409, blocked)");
  const queueAt = routeC.indexOf('has_active_job(db, course.id, "lecture")');
  if (gateAt >= 0 && queueAt >= 0 && gateAt > queueAt) {
    bad.push(`${REL_ROUTE}：锁检查排在入队之后（必须先拦、再排队）`);
  }
  if (/skip_locked/.test(routeC)) {
    bad.push(`${REL_ROUTE}：路由里出现了 skip_locked —— 批量语义本来就不用传，不许动它`);
  }
  if (!/if skip_locked and _locked_of\(current\):/.test(svcC)) {
    bad.push(`${REL_SVC}：批量路径的 skip_locked 语义被动过了（_generate_one 里少了锁跳过）`);
  }

  if (!/def locked_overwrite_blocked\(/.test(svcC)) {
    bad.push(`${REL_SVC}：缺 locked_overwrite_blocked（拦截话术的唯一出处）`);
  }
  const clearAt = svcC.indexOf('meta["locked"] = False');
  const bodyAt = svcC.indexOf("item.body = body");
  if (clearAt < 0 || !/meta\.pop\("locked_reason", None\)/.test(svcC)) {
    bad.push(`${REL_SVC}：覆盖后没有清掉 locked / locked_reason（徽标会继续贴在没审过的那一版上）`);
  } else if (bodyAt < 0 || clearAt < bodyAt) {
    bad.push(`${REL_SVC}：摘徽标不在「正文已覆盖」之后（要贴住覆盖这件事本身）`);
  }
  if (!/LOCKED_OVERWRITE_NOTE = /.test(svcC)) {
    bad.push(`${REL_SVC}：缺 LOCKED_OVERWRITE_NOTE（覆盖已锁节后的实话实说）`);
  }
  return bad;
}

/* ---------------- ② 前端源码级 ---------------- */

function auditFrontend(tsx: string, api: string): string[] {
  const bad: string[] = [];
  const tsxC = stripComments(tsx);

  if (!/allow_overwrite_locked\?: boolean;/.test(api)) {
    bad.push(`${REL_API}：generateLecture 的参数里没有 allow_overwrite_locked`);
  }
  if (!/function overwriteConfirmTitle\(locked: boolean\): string/.test(tsxC)) {
    bad.push(`${REL_TSX}：缺 overwriteConfirmTitle（锁定态与普通态两套确认文案的唯一出处）`);
  }
  if (!tsxC.includes(TSX_TITLE_LOCKED)) {
    bad.push(`${REL_TSX}：锁定态的确认文案没说清「会覆盖老师审过的那一版、并摘除徽标」`);
  }
  const passThrough = tsxC.split("allowOverwriteLocked: lectureLocked").length - 1;
  if (passThrough < 2) {
    bad.push(`${REL_TSX}：两个「重新生成」入口（卡片上的按钮 + 正文大按钮）没有都把确认结果发出去`);
  }
  if (!/allow_overwrite_locked: d\.allowOverwriteLocked/.test(tsxC)) {
    bad.push(`${REL_TSX}：请求体没有把 allowOverwriteLocked 映射成 allow_overwrite_locked`);
  }
  if (!tsxC.includes(TSX_PRIMARY_GUARD)) {
    bad.push(`${REL_TSX}：锁定态下主按钮自己还会直接触发重跑（rc-trigger 会串起子元素 onClick，必须显式置 undefined）`);
  }
  if (!/disabled=\{!lectureLocked\}/.test(tsxC)) {
    bad.push(`${REL_TSX}：主按钮的 Popconfirm 在未锁定时没有被禁用（未锁定的交互会变）`);
  }
  if (!/if \(vars\.allowOverwriteLocked\) \{/.test(tsxC)) {
    bad.push(`${REL_TSX}：覆盖已锁节后没有额外提示（不能让老师以为还是审过的那一版）`);
  }
  if (!tsxC.includes("「审核通过」徽标已摘除，需重新审核")) {
    bad.push(`${REL_TSX}：覆盖已锁节后的提示没说「徽标已摘、需重新审核」`);
  }
  return bad;
}

/* ---------------- ③ 行为级（真跑 Python 探针） ---------------- */

type Case = Record<string, unknown>;
type Probe = {
  error?: string; env?: Record<string, string>; tmp?: string;
  batch_force?: Case; single_blocked?: Case; single_blocked_no_item_id?: Case;
  single_overwrite?: Case; unlocked_single_rerun?: Case; qc_regenerate?: Case;
  real_db_unchanged?: boolean; real_vault_unchanged?: boolean;
  real_before?: Record<string, unknown>; real_after?: Record<string, unknown>;
};

function pythonExe(): string | null {
  const venv = join(repo, "backend", ".venv", "Scripts", "python.exe");
  if (existsSync(venv)) return venv;
  for (const name of ["python3", "python"]) {
    const res = spawnSync(name, ["-V"], { encoding: "utf8" });
    if (res.status === 0) return name;
  }
  return null;
}

function runProbe(python: string, tmpDir: string, mutation?: string): Probe | string {
  const env: Record<string, string> = {
    ...process.env as Record<string, string>,
    DATABASE_URL: `sqlite:///${tmpDir.replace(/\\/g, "/")}/probe.db`,
    STORAGE_DIR: join(tmpDir, "storage"),
    OBSIDIAN_VAULT_ROOT: join(tmpDir, "vault"),
    PPT_WORK_DIR: join(tmpDir, "storage", "ppt"),
    EXPORT_DIR: join(tmpDir, "storage", "exports"),
    LLM_PROVIDER: "mock",
    IMAGE_PROVIDER: "mock",
    PYTHONIOENCODING: "utf-8",
    CG91_TMP: tmpDir,
  };
  for (const sub of ["storage", "vault", "storage/ppt", "storage/exports"]) {
    mkdirSync(join(tmpDir, sub), { recursive: true });
  }
  const args = [join(repo, REL_PROBE), repo];
  if (mutation) {
    const file = join(tmpDir, "mut-lecture.py");
    writeFileSync(file, mutation, "utf8");
    args.push("--lecture-mut", file);
  }
  const res = spawnSync(python, args, {
    encoding: "utf8", cwd: join(repo, "backend"), env, timeout: 600000,
  });
  const text = String(res.stdout ?? "").trim();
  const last = text.split("\n").filter(Boolean).pop() ?? "";
  if (res.status !== 0 && !last.startsWith("{")) {
    return `python 探针失败（exit=${res.status}）：${(res.stderr ?? "").trim().slice(-400) || "(无 stderr)"}`;
  }
  try {
    const parsed = JSON.parse(last) as Probe;
    if (parsed.error) return `python 探针拒跑：${parsed.error}（env=${JSON.stringify(parsed.env)}）`;
    return parsed;
  } catch {
    return `python 探针输出不是 JSON：${last.slice(0, 200)}`;
  }
}

function auditProbe(p: Probe, why: string): string[] {
  const bad: string[] = [];
  const at = (name: string) => `${why}.${name}`;

  // 五件套必须全在临时目录 + mock
  const env = p.env ?? {};
  for (const key of ["database_url", "storage_dir", "vault_root", "ppt_work_dir", "export_dir"]) {
    const value = String(env[key] ?? "").replace(/^sqlite:\/\/\//, "");
    if (!value) bad.push(`${at("env")}：${key} 读回来是空的`);
  }
  if (String(env.llm_provider) !== "mock") bad.push(`${at("env")}：LLM_PROVIDER 不是 mock（不许调真模型）`);
  if (!/vault$/i.test(String(env.vault_root ?? ""))) {
    bad.push(`${at("env")}：OBSIDIAN_VAULT_ROOT 没指到临时目录（${String(env.vault_root)}）`);
  }

  const batch = p.batch_force ?? {};
  if (batch.http !== 200 || batch.job !== "success") bad.push(`${at("batch_force")}：批量没跑成功`);
  if (!(Array.isArray(batch.locked_skipped) && (batch.locked_skipped as string[]).includes("1"))) {
    bad.push(`${at("batch_force")}：已锁节点没进 locked_skipped（${JSON.stringify(batch.locked_skipped)}）`);
  }
  if (batch.sha1_unchanged !== true) bad.push(`${at("batch_force")}：批量把已锁那一节的正文改了`);
  if (batch.locked !== true || batch.locked_reason !== "审核通过") {
    bad.push(`${at("batch_force")}：批量把已锁那一节的锁/原因动掉了`);
  }

  const blocked = p.single_blocked ?? {};
  if (blocked.http !== 409) bad.push(`${at("single_blocked")}：单篇重跑已锁节没有返回 409（实际 ${blocked.http}）`);
  if (!/已锁定/.test(String(blocked.detail ?? "")) || !/解锁/.test(String(blocked.detail ?? ""))) {
    bad.push(`${at("single_blocked")}：409 的话术没说清「已锁定 / 先解锁」（${String(blocked.detail)}）`);
  }
  if (blocked.sha1_unchanged !== true) bad.push(`${at("single_blocked")}：被拦下来了正文却还是变了`);
  if (blocked.locked !== true || blocked.locked_reason !== "审核通过") {
    bad.push(`${at("single_blocked")}：被拦下来时锁/原因被动了`);
  }
  if (blocked.history !== 0) bad.push(`${at("single_blocked")}：被拦下来却多了一条历史版本`);
  if ((p.single_blocked_no_item_id ?? {}).http !== 409) {
    bad.push(`${at("single_blocked_no_item_id")}：不带 item_id 的单篇重跑没有被拦（节点上那篇已锁）`);
  }

  const over = p.single_overwrite ?? {};
  if (over.http !== 200 || over.job !== "success") bad.push(`${at("single_overwrite")}：显式覆盖没跑成功`);
  if (over.unlocked !== true) bad.push(`${at("single_overwrite")}：覆盖已锁节后没有把「已解锁」如实带出来`);
  if (!/徽标已摘除/.test(String(over.unlock_message ?? ""))) {
    bad.push(`${at("single_overwrite")}：覆盖后的提示没提徽标已摘（${String(over.unlock_message)}）`);
  }
  if (over.sha1_changed !== true) bad.push(`${at("single_overwrite")}：显式覆盖后正文没变（覆盖没生效）`);
  if (over.locked !== false) bad.push(`${at("single_overwrite")}：覆盖后 locked 还是 true ⇒ 徽标在撒谎`);
  if (over.reason_key_present !== false) bad.push(`${at("single_overwrite")}：覆盖后 locked_reason 还留着`);
  if (over.history_has_reviewed !== true) bad.push(`${at("single_overwrite")}：旧版（老师审过的那一版）没进历史`);

  const free = p.unlocked_single_rerun ?? {};
  if (free.http !== 200 || free.job !== "success") bad.push(`${at("unlocked_single_rerun")}：未锁的单篇不能重跑了（回归）`);
  if (free.unlocked !== false || free.unlock_message !== "") {
    bad.push(`${at("unlocked_single_rerun")}：没锁的节也报了「已解锁」`);
  }
  if (free.sha1_changed !== true) bad.push(`${at("unlocked_single_rerun")}：未锁的单篇重跑没覆盖`);

  const qc = p.qc_regenerate ?? {};
  if (qc.http !== 200 || qc.job !== "success") bad.push(`${at("qc_regenerate")}：按质检建议重生成没跑成功`);
  if (qc.locked !== false) bad.push(`${at("qc_regenerate")}：同族的质检重生成把徽标留着了（正文已换）`);

  if (p.real_db_unchanged !== true) bad.push(`${why}：真库指纹变了（${JSON.stringify(p.real_before?.db)} -> ${JSON.stringify(p.real_after?.db)}）`);
  if (p.real_vault_unchanged !== true) {
    bad.push(`${why}：真 vault 课程区指纹变了（${JSON.stringify(p.real_before?.vault_courses)} -> ${JSON.stringify(p.real_after?.vault_courses)}）`);
  }
  return bad;
}

/* ---------------- 主流程 ---------------- */

const svc = read(REL_SVC);
const route = read(REL_ROUTE);
const schema = read(REL_SCHEMA);
const tsx = read(REL_TSX);
const api = read(REL_API);

failures.push(...auditBackend(svc, route, schema));
failures.push(...auditFrontend(tsx, api));

const python = pythonExe();
const tmpRoot = mkdtempSync(join(tmpdir(), "cg91-lock-"));
let mutations = 0;

try {
  if (!python) {
    failures.push("找不到可用的 Python（行为级断言不能静默跳过：需要 backend/.venv 或 PATH 里的 python3 / python）");
  } else {
    const probe = runProbe(python, join(tmpRoot, "clean"));
    if (typeof probe === "string") {
      failures.push(probe);
    } else {
      failures.push(...auditProbe(probe, "真模块"));
    }

    /* ---- ④ 源码级变异：五条都必须变红 ---- */
    const cases: { name: string; red: () => string[]; changed: boolean }[] = [
      {
        name: "去掉路由的 409 拦截（回到「照样覆盖」）",
        changed: route.includes(ROUTE_GATE),
        red: () => auditBackend(svc, route.replace(ROUTE_GATE, "    # 变异：不拦了\n"), schema),
      },
      {
        name: "去掉服务的「覆盖即摘徽标」",
        changed: svc.includes(SVC_CLEAR),
        red: () => auditBackend(svc.replace(SVC_CLEAR, "    # 变异：不摘徽标\n"), route, schema),
      },
      {
        name: "请求体默认值改回 True（默认就允许覆盖）",
        changed: schema.includes(SCHEMA_DEFAULT),
        red: () => auditBackend(svc, route,
          schema.replace("        default=False,\n", "        default=True,\n")),
      },
      {
        name: "主按钮去掉锁定守卫（锁定时自己也会直接重跑）",
        changed: tsx.includes(TSX_PRIMARY_GUARD),
        red: () => auditFrontend(
          tsx.replace(TSX_PRIMARY_GUARD,
            "                  onClick={() => genLecture.mutate({ tone: \"正式\", itemId: lecture?.id })}\n"),
          api),
      },
      {
        name: "锁定态确认文案不说「徽标」",
        changed: tsx.includes(TSX_TITLE_LOCKED),
        red: () => auditFrontend(tsx.replace(TSX_TITLE_LOCKED,
          "这一节已审核通过并锁定：重新生成会覆盖它（新版需重新审核）。"), api),
      },
    ];
    for (const item of cases) {
      mutations += 1;
      if (!item.changed) {
        failures.push(`自检失败：变异「${item.name}」没改到源码（断言可能已失效）`);
        continue;
      }
      if (item.red().length === 0) {
        failures.push(`自检失败：变异「${item.name}」后断言仍然通过`);
      }
    }

    /* ---- 行为级变异：去掉摘徽标 ⇒ 真跑一次必须重现「正文换了、locked 还在」 ---- */
    if (svc.includes(SVC_CLEAR)) {
      mutations += 1;
      const mutated = runProbe(python, join(tmpRoot, "mutated"),
        svc.replace(SVC_CLEAR, "    # 变异：不摘徽标\n"));
      if (typeof mutated === "string") {
        failures.push(`行为级变异：${mutated}`);
      } else if ((mutated.single_overwrite ?? {}).locked !== true) {
        failures.push("自检失败：去掉摘徽标后，行为级仍然看到 locked=False（变异没生效）");
      }
    } else {
      failures.push("自检失败：行为级变异没改到源码");
    }
  }
} finally {
  rmSync(tmpRoot, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「审核锁」断言失败（91）：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端审核锁断言（91）通过："
  + "① 后端：请求体 allow_overwrite_locked 默认 False、单篇 /generate 在入队前 409、"
  + "覆盖成功后清 locked/locked_reason（且清的动作贴在正文覆盖之后），批量路径的 skip_locked 语义一个字没动；"
  + "② 前端：两个「重新生成」入口在锁定态下先弹确认（文案写明会覆盖并摘徽标）、"
  + "主按钮锁定时不自己触发、覆盖后明说「徽标已摘、需重新审核」；"
  + "③ 行为级真跑：批量 force 仍 locked_skipped 且正文 sha1 不变、单篇默认 409（带/不带 item_id 都拦）、"
  + "显式覆盖后正文换新版 + locked False + locked_reason 清空 + 旧版进 history、未锁单篇照旧覆盖、"
  + "同族质检重生成也不留徽标、真库与真 vault 指纹不变；"
  + `${mutations} 条变异（5 源码级 + 1 行为级，行为级必须重现「正文换了、locked 还在」）全红。`);