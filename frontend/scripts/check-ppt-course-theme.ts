/**
 * 任务 145 前端断言：主题跟随课程（`course.meta.ppt_theme`）。
 *
 * 为什么要有这条：主题的**唯一出处是后端**（`app/services/ppt_theme_setting.py` 的
 * 「显式传 > 课程设置 > 后端配置 > 内置默认」），而界面上有三处很容易被写坏：
 * ① 下拉默认值 —— 必须是**本课当前主题**，不是第一个、也不是全局默认（老代码写死
 *    `useState("clean")`，等于每门课都当清简教学）；
 * ② 来源文案 —— 两侧各一份表（后端 `SOURCE_LABELS` / 前端 `THEME_SOURCE_LABELS`），
 *    写歪了界面就会出现「本课设置」和「本课的主题」两种说法；
 * ③ 改主题的后果 —— **只改外观、已生成的课件不会变**，必须重新生成才生效（不提示 = 用户以为坏了）。
 *
 * 本脚本钉五件事：
 * ① **两侧表逐字一致**：来源表 + `APPLY_NOTE` + `FALLBACK_THEMES`（首屏兜底）与后端五套主题；
 * ② **后端口径**（源码级）：`generate_deck` 默认值是空串（不许再有 `theme07`）、优先级唯一出处、
 *    写入校验（非法值 `ValueError`/4xx）与 `POST /theme` 的日志一句话；
 * ③ **界面接线**：下拉默认 = `courseThemeId(themeData)`、选项标「本课当前」、改了即存
 *    （`api.setPptTheme`）、提示文案在位、后端没答复时不预选第一项；
 * ④ **行为级**：真 import `lib/pptTheme.ts` 跑断言 + **变异自检**（改坏一处必须判红）；
 * ⑤ **真调接口**（临时五件套 + 0 LLM）：真建课 / 真设置主题 / 真出课件，验证四档优先级
 *    与「清了设置回到配置」在真接口上成立。
 *
 * 运行：`node scripts/check-ppt-course-theme.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

const REL_LIB = "frontend/src/lib/pptTheme.ts";
const REL_PANEL = "frontend/src/components/PptPanel.tsx";
const REL_API = "frontend/src/api.ts";
const REL_SETTING = "backend/app/services/ppt_theme_setting.py";
const REL_PPT = "backend/app/services/ppt.py";
const REL_ROUTE = "backend/app/api/routes/ppt.py";
const REL_THEME = "backend/app/services/ppt_theme.py";
const REL_VERIFY = "scripts/verify.ps1";

const files = new Map<string, string>();
for (const rel of [REL_LIB, REL_PANEL, REL_API, REL_SETTING, REL_PPT, REL_ROUTE, REL_THEME,
  REL_VERIFY]) {
  files.set(rel, read(rel));
}
const libSource = files.get(REL_LIB) ?? "";
const panelSource = files.get(REL_PANEL) ?? "";
const apiSource = files.get(REL_API) ?? "";
const settingSource = files.get(REL_SETTING) ?? "";
const pptSource = files.get(REL_PPT) ?? "";
const routeSource = files.get(REL_ROUTE) ?? "";
const themeSource = files.get(REL_THEME) ?? "";

/* ======================= ① 两侧表逐字一致 ======================= */

type Row = [string, string];

/** 从一段源码里取 `键: "值",`（前端）/ `"键": "值",`（后端）的表。 */
function rowsOf(source: string, keyPattern: string): Row[] {
  const out: Row[] = [];
  for (const m of source.matchAll(new RegExp(`^\\s*${keyPattern}\\s*:\\s*"([^"]*)",\\s*$`, "gm"))) {
    out.push([m[1], m[2]]);
  }
  return out;
}

/** 截取一个「块」：从 marker 起到第一个结束符（前后端两种表都是这个形状）。 */
function blockOf(source: string, marker: string, end = "};"): string {
  const start = source.indexOf(marker);
  if (start < 0) return "";
  const stop = source.indexOf(end, start);
  return stop < 0 ? "" : source.slice(start, stop);
}

function auditSourceTables(pyTable: Row[], tsTable: Row[], applyPy: string, applyTs: string,
  fallback: Row[], backendThemes: Row[]): string[] {
  const bad: string[] = [];
  if (JSON.stringify(pyTable) !== JSON.stringify(tsTable)) {
    bad.push(`来源表两侧不一致（后端 ${JSON.stringify(pyTable)}；前端 ${JSON.stringify(tsTable)}）`);
  }
  if (pyTable.map((r) => r[0]).join(",") !== "param,course,config,default") {
    bad.push(`来源 key 集合 / 顺序不对：${pyTable.map((r) => r[0]).join(",")}`);
  }
  if (pyTable.some((r) => !r[1])) bad.push("来源表有空的显示名（界面会出现空白括注）");
  if (applyPy !== applyTs || !applyPy) {
    bad.push(`APPLY_NOTE 两侧不一致 / 为空（后端「${applyPy}」；前端「${applyTs}」）`);
  }
  if (!applyPy.includes("只改外观") || !applyPy.includes("重新生成")) {
    bad.push(`改主题的后果必须说清「只改外观 + 要重新生成」：${applyPy}`);
  }
  if (JSON.stringify(fallback) !== JSON.stringify(backendThemes)) {
    bad.push(`首屏兜底表与后端五套主题不一致（兜底 ${JSON.stringify(fallback)}；`
      + `后端 ${JSON.stringify(backendThemes)}）`);
  }
  if (fallback.length !== 5) bad.push(`首屏兜底表应有 5 套，实际 ${fallback.length} 套`);
  return bad;
}

const pyLabelTable = rowsOf(blockOf(settingSource, "SOURCE_LABELS: dict[str, str] = {", "\n}"),
  '"([a-z_]+)"');
const tsLabelTable = rowsOf(blockOf(libSource, "THEME_SOURCE_LABELS: Record<string, string> = {"),
  "([a-zA-Z_]+)");
const pyApplyNote = (settingSource.match(/^APPLY_NOTE = "([^"]*)"/m) ?? [])[1] ?? "";
const tsApplyNote = (libSource.match(/^export const THEME_APPLY_NOTE = "([^"]*)";/m) ?? [])[1] ?? "";
const fallbackRows: Row[] = [...blockOf(panelSource, "const FALLBACK_THEMES = [", "];")
  .matchAll(/\{\s*id:\s*"([^"]+)",\s*name:\s*"([^"]+)"/g)].map((m) => [m[1], m[2]] as Row);
const themeBlock = blockOf(themeSource, "THEMES: dict[str, dict] = {", "\n}");
const pyThemeIds = [...themeBlock.matchAll(/^    THEME_([A-Z]+): \{$/gm)].map((m) => m[1].toLowerCase());
const pyThemeNames = [...themeBlock.matchAll(/^        "name": "([^"]+)",$/gm)].map((m) => m[1]);
const pyThemeRows: Row[] = pyThemeIds.map((id, i) => [id, pyThemeNames[i] ?? ""] as Row);

/* ======================= ② 后端口径（源码级） ======================= */

function auditBackend(setting: string, ppt: string, route: string): string[] {
  const bad: string[] = [];
  const priority = "调用方显式传的 theme（非空字符串） > 课程 meta.ppt_theme > 后端配置 PPT_THEME > 内置默认";
  if (!setting.includes(priority)) {
    bad.push("ppt_theme_setting.py 的优先级口径不是唯一那一条（四档顺序被改了？）");
  }
  if (!/^META_KEY = "ppt_theme"$/m.test(setting)) bad.push("课程级设置的 key 不是 course.meta.ppt_theme");
  if (!/^def resolve\(/m.test(setting) || !/^def write\(/m.test(setting)) {
    bad.push("ppt_theme_setting.py 必须同时有 resolve（读优先级）与 write（写校验）");
  }
  const resolveBody = setting.slice(setting.indexOf("def resolve("), setting.indexOf("def write("));
  const order = [resolveBody.indexOf("if raw_param:"), resolveBody.indexOf("raw_setting(course)"),
    resolveBody.indexOf("resolve_theme(config)")];
  if (order[0] < 0 || order[1] < 0 || !(order[0] < order[1])) {
    bad.push("resolve 里「显式传」必须排在「课程设置」之前（顺序写死）");
  }
  if (!/def normalize_key/.test(setting) || !/theme_keys|resolve_theme/.test(setting)) {
    bad.push("写入必须走统一校验（normalize_key 基于 ppt_theme 的唯一出处）");
  }
  const writeBody = setting.slice(setting.indexOf("def write("));
  if (!/raise ValueError\(/.test(writeBody) || !writeBody.includes("未写库")) {
    bad.push("非法值必须 raise ValueError 且说清「未写库」（不许静默回落）");
  }
  const withoutDashi = ppt.replace(/def _dashi_theme\([\s\S]*?\n\n\n/, "");
  if (/theme07/.test(withoutDashi)) {
    bad.push("ppt.py 里还有 theme07 当默认主题（dashi 的 themeNN 映射不算）");
  }
  if (!/theme: str = "", engine: str \| None = None,/.test(ppt)) {
    bad.push("generate_deck 的 theme 默认值必须是空串（= 走课程设置，不是写死某一套）");
  }
  if (ppt.includes('theme: str = "theme07"')) bad.push('generate_deck 的默认值又被写回 "theme07"');
  if (!/def generate_all_chapter_decks\(/.test(ppt) || !/theme: str = "",/.test(ppt)) {
    bad.push("批量补齐也必须默认空串（与 generate_deck 同一条优先级）");
  }
  if (!route.includes('@router.post("/theme")')) bad.push("缺少设置本课主题的接口 POST /ppt/theme");
  if (!/raise HTTPException\(400, str\(exc\)\) from exc/.test(route)) {
    bad.push("POST /ppt/theme 的非法值必须是 400（不是静默回落）");
  }
  if (!route.includes("logger.info(\"课程主题已更新")) bad.push("改主题必须写 logger.info");
  if (!route.includes("ppt_theme_setting.APPLY_NOTE")) {
    bad.push("日志 / 返回文案必须复用 APPLY_NOTE（唯一出处）");
  }
  const themesAt = route.indexOf('@router.get("/themes")');
  const dynamicAt = route.indexOf('@router.get("/{course_id}")');
  if (themesAt < 0 || dynamicAt < 0 || themesAt > dynamicAt) {
    bad.push("GET /ppt/themes 必须声明在 /{course_id} **之前**（否则被当课程 id 吃掉）");
  }
  if (!/course_id: str = "", db: Session = Depends\(get_db\)/.test(route)) {
    bad.push("GET /ppt/themes 必须接受可选 course_id（前端要靠它拿「本课当前主题」）");
  }
  return bad;
}

/* ======================= ③ 界面接线 ======================= */

function auditWiring(panel: string, api: string, verify: string): string[] {
  const bad: string[] = [];
  const body = stripComments(panel);
  if (!body.includes("api.pptThemes(courseId)")) {
    bad.push("下拉的主题列表必须按课程取（api.pptThemes(courseId)），否则拿不到「本课当前主题」");
  }
  if (!body.includes("courseThemeId(themeData)")) {
    bad.push("必须用 courseThemeId(themeData) 拿本课当前主题（不许拿第一个主题顶替）");
  }
  if (!/const theme = themePicked \?\? courseTheme;/.test(body)) {
    bad.push("下拉的 value 必须是「本课当前主题」（用户改过才用改过的值）");
  }
  if (/useState\("clean"\)/.test(body)) bad.push('下拉默认值又被写死成 useState("clean")');
  if (!body.includes("themeSelectOptions(themeOptions, courseTheme)")) {
    bad.push("选项必须标出「本课当前」那一项（themeSelectOptions）");
  }
  if (!body.includes("currentThemeLine(themeData)")) {
    bad.push("必须显示「当前：深蓝商务（本课设置）」（currentThemeLine）");
  }
  if (!body.includes("api.setPptTheme(courseId, value)")) {
    bad.push("改了主题必须落库（api.setPptTheme）——只改本地 state 等于没记住");
  }
  if (!body.includes("{THEME_APPLY_NOTE}")) {
    bad.push("必须把「只改外观、要重新生成才生效」印在界面上（THEME_APPLY_NOTE）");
  }
  if (!body.includes("ppt-theme-warning")) {
    bad.push("课程里存的是脏值时必须在界面上给出原因（不许静默）");
  }
  if (!body.includes("THEME_LOADING_LINE")) {
    bad.push("后端没答复时要用占位文案（不预选第一项）");
  }
  if (!panel.includes("兜底")) {
    bad.push("FALLBACK_THEMES 必须注明「只是首屏兜底」");
  }
  if (!api.includes("pptThemes: (courseId?: string)")) {
    bad.push("api.pptThemes 必须接受可选 courseId");
  }
  if (!api.includes('"/ppt/theme"')) bad.push("api 里缺 setPptTheme → POST /ppt/theme");
  if (!verify.includes("check-ppt-course-theme.ts")) {
    bad.push("本条断言没有接进 scripts/verify.ps1（等于没跑）");
  }
  return bad;
}

/* ======================= ④ 行为级 + 变异自检 ======================= */

type Lib = {
  THEME_SOURCE_LABELS: Record<string, string>;
  THEME_APPLY_NOTE: string;
  THEME_LOADING_LINE: string;
  themeSourceLabel: (s?: string | null) => string;
  courseThemeId: (d?: unknown) => string;
  currentThemeLine: (d?: unknown) => string;
  themeSelectOptions: (o: unknown, current?: string | null) => { value: string; label: string }[];
};

const OPTIONS = [
  { id: "clean", name: "清简教学" },
  { id: "professional", name: "专业培训" },
  { id: "navy", name: "深蓝商务" },
];

function auditBehavior(mod: Lib): string[] {
  const bad: string[] = [];
  const line = (course: unknown) => mod.currentThemeLine({ course } as never);
  if (line({ id: "navy", name: "深蓝商务", source: "course" }) !== "当前：深蓝商务（本课设置）") {
    bad.push(`本课设置那一档的显示行不对：${line({ id: "navy", name: "深蓝商务", source: "course" })}`);
  }
  if (line({ id: "clean", name: "清简教学", source: "config" }) !== "当前：清简教学（后端配置）") {
    bad.push("后端配置那一档的显示行不对");
  }
  if (line({ id: "clean", name: "清简教学", source: "default" }) !== "当前：清简教学（内置默认）") {
    bad.push("内置默认那一档的显示行不对");
  }
  if (line(null) !== "" || mod.currentThemeLine({}) !== "") {
    bad.push("后端没答复 / 没有课程时不许印半截话（应为空串）");
  }
  if (mod.themeSourceLabel("course") !== "本课设置") bad.push("themeSourceLabel 取不到「本课设置」");
  if (mod.themeSourceLabel("不认识的来源") !== "") bad.push("未知来源必须返回空串（不猜）");
  if (mod.courseThemeId({ course: { id: "navy" } }) !== "navy") bad.push("courseThemeId 没取到本课主题");
  if (mod.courseThemeId({ items: OPTIONS }) !== "") {
    bad.push("后端没答复时 courseThemeId 必须为空串（不许拿第一个主题顶替）");
  }
  if (mod.courseThemeId(undefined) !== "") bad.push("courseThemeId(undefined) 必须为空串");
  const marked = mod.themeSelectOptions(OPTIONS, "navy");
  if (marked.length !== OPTIONS.length) bad.push("themeSelectOptions 丢了选项");
  if (marked[2]?.label !== "深蓝商务（本课当前）") bad.push(`「本课当前」没标对：${marked[2]?.label}`);
  if (marked[0]?.label !== "清简教学" || marked[1]?.label !== "专业培训") {
    bad.push("只许标「本课当前」那一项，别的选项不许带后缀");
  }
  if (mod.themeSelectOptions(OPTIONS, "")?.some((o) => o.label.includes("本课当前"))) {
    bad.push("没有本课主题时不许标任何一项");
  }
  if (!mod.THEME_APPLY_NOTE.includes("重新生成") || !mod.THEME_APPLY_NOTE.includes("只改外观")) {
    bad.push(`改主题的提示文案不对：${mod.THEME_APPLY_NOTE}`);
  }
  if (!mod.THEME_LOADING_LINE || OPTIONS.some((o) => o.name === mod.THEME_LOADING_LINE)) {
    bad.push("读取中的占位文案不能是某一套主题名");
  }
  return bad;
}

/* ======================= ⑤ 真调接口（临时五件套 + 0 LLM） ======================= */

const PY_PROBE = String.raw`
# -*- coding: utf-8 -*-
"""任务 145 行为探针：临时五件套里真建课 / 真设主题 / 真出课件（0 LLM）。"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="cg145_probe_"))
for sub in ("db", "storage", "vault", "ppt", "export"):
    (TMP / sub).mkdir(parents=True, exist_ok=True)
os.environ.update({
    "DATABASE_URL": "sqlite:///" + (TMP / "db" / "p.sqlite").as_posix(),
    "STORAGE_DIR": str(TMP / "storage"),
    "OBSIDIAN_VAULT_ROOT": str(TMP / "vault"),
    "PPT_WORK_DIR": str(TMP / "storage" / "ppt"),
    "EXPORT_DIR": str(TMP / "export"),
    "LLM_PROVIDER": "mock",
    "IMAGE_PROVIDER": "mock",
    "AUTH_ENABLED": "false",
    "PPT_SLIDE_IMAGES": "false",
    "DASHI_PPT_FORCE_FALLBACK": "true",
    "PPT_THEME": "clean",
})
REPO = Path(os.environ["CG_REPO"])
sys.path.insert(0, str(REPO / "backend"))

from fastapi.testclient import TestClient            # noqa: E402
from app.main import app                             # noqa: E402
from app.db.base import Base                         # noqa: E402
from app.db.session import engine                    # noqa: E402
from app.services import llm                         # noqa: E402
from app.services.ppt_theme import resolve_theme      # noqa: E402
from tests.test_ppt_v4 import (_add_diagram, _add_point, _chapter, _course_deck, _gen,
                               _plan_and_approve)     # noqa: E402


def colours(path, page_index):
    """某一页出现的全部颜色（文字 run 色 + 形状实色填充）。"""
    from pptx import Presentation

    out = []
    for shape in Presentation(str(path)).slides[page_index].shapes:
        if getattr(shape, "has_text_frame", False):
            for para in shape.text_frame.paragraphs:
                for run in (para.runs or []):
                    try:
                        out.append(str(run.font.color.rgb))
                    except Exception:                      # noqa: BLE001
                        pass
        try:
            if str(shape.fill.type).startswith("SOLID"):
                out.append(str(shape.fill.fore_color.rgb))
        except Exception:                                  # noqa: BLE001
            pass
    return out


Base.metadata.create_all(bind=engine)
out = {}
with TestClient(app) as client:
    course_id = client.post("/api/course/", json={"title": "145 主题跟随课程"}).json()["id"]
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-145")

    # ① 设本课主题（中文名 ⇒ 存规范 id）
    set_resp = client.post("/api/ppt/theme", json={"course_id": course_id, "theme": "深蓝商务"})
    out["set_status"] = set_resp.status_code
    out["set_body"] = set_resp.json()
    out["meta_after_set"] = client.get("/api/course/" + course_id).json()["meta"].get("ppt_theme")

    # ② 非法值：400 + 合法值清单 + 不改库
    bad = client.post("/api/ppt/theme", json={"course_id": course_id, "theme": "瞎写"})
    out["bad_status"] = bad.status_code
    out["bad_detail"] = bad.json().get("detail", "")
    out["meta_after_bad"] = client.get("/api/course/" + course_id).json()["meta"].get("ppt_theme")

    # ③ /ppt/themes 带「本课当前主题 + 来源」
    with_course = client.get("/api/ppt/themes?course_id=" + course_id).json()
    out["course_block"] = with_course.get("course")
    out["active_id"] = (with_course.get("active") or {}).get("id")
    out["no_param_course"] = client.get("/api/ppt/themes").json().get("course")

    # ④ 生成：不传 theme ⇒ 本课设置（navy）
    _gen(client, course_id, scope="course")
    navy_deck = _course_deck(client, course_id)
    out["navy_theme"] = navy_deck["theme"]
    out["navy_source"] = navy_deck.get("theme_source")
    navy_cover = resolve_theme("navy")[0]["palette"]["cover_title"]
    clean_accent = resolve_theme("clean")[0]["palette"]["cover_title"]
    navy_path = navy_deck["body"]["pptx"]
    out["navy_cover_has_navy_colour"] = navy_cover in colours(navy_path, 0)
    out["navy_has_no_clean_accent"] = clean_accent not in (colours(navy_path, 0)
                                                          + colours(navy_path, 1))

    # ⑤ 显式传主题赢过课程设置
    _gen(client, course_id, scope="course", theme="clean")
    clean_deck = _course_deck(client, course_id)
    out["clean_theme"] = clean_deck["theme"]
    out["clean_source"] = clean_deck.get("theme_source")
    clean_path = clean_deck["body"]["pptx"]
    out["clean_has_clean_accent"] = clean_accent in (colours(clean_path, 0)
                                                     + colours(clean_path, 1))

    # ⑥ 清空本课设置 ⇒ 回到后端配置
    cleared = client.post("/api/ppt/theme", json={"course_id": course_id, "theme": ""})
    out["clear_status"] = cleared.status_code
    out["clear_body"] = cleared.json()
    out["meta_after_clear"] = client.get("/api/course/" + course_id).json()["meta"].get("ppt_theme")
    out["course_after_clear"] = client.get("/api/ppt/themes?course_id=" + course_id).json()["course"]

    # ⑦ 0 LLM 护栏：生成课件本来就不该调模型
    original = llm.build_llm_client

    def _no_llm(*args, **kwargs):
        raise AssertionError("课件生成不许调 LLM")

    llm.build_llm_client = _no_llm
    try:
        _gen(client, course_id, scope="course")
        out["llm_guard_status"] = 200
    except Exception as exc:                               # noqa: BLE001
        out["llm_guard_status"] = str(exc)[:80]
    finally:
        llm.build_llm_client = original

print("PROBE_JSON " + json.dumps(out, ensure_ascii=False))
`;

function runProbe(tmp: string, src: string): Record<string, unknown> {
  const py = join(tmp, "probe_t145.py");
  writeFileSync(py, src.replace(/\r+\n/g, "\n"), "utf8");
  const python = join(repo, "backend", ".venv", "Scripts", "python.exe");
  const res = spawnSync(python, [py], {
    cwd: join(repo, "backend"),
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", LLM_PROVIDER: "mock",
           IMAGE_PROVIDER: "mock", CG_REPO: repo },
  });
  if (res.status !== 0) {
    throw new Error(`探针退出码 ${res.status}\n${res.stdout ?? ""}\n${res.stderr ?? ""}`);
  }
  const line = (res.stdout ?? "").split("\n").reverse().find((l) => l.startsWith("PROBE_JSON "));
  if (!line) throw new Error(`探针没有输出结果行：\n${res.stdout}\n${res.stderr}`);
  return JSON.parse(line.slice("PROBE_JSON ".length)) as Record<string, unknown>;
}

function auditProbe(out: Record<string, unknown>): string[] {
  const bad: string[] = [];
  const setBody = (out.set_body ?? {}) as Record<string, unknown>;
  if (out.set_status !== 200) bad.push(`设置本课主题没回 200（实得 ${String(out.set_status)}）`);
  if (setBody.theme !== "navy") bad.push(`中文名没归一化成规范 id：${String(setBody.theme)}`);
  if (setBody.setting !== "navy") bad.push(`库里存的不是规范 id：${String(setBody.setting)}`);
  if (setBody.source !== "course" || setBody.source_label !== "本课设置") {
    bad.push(`设置后的来源不对：${String(setBody.source)} / ${String(setBody.source_label)}`);
  }
  if (!String(setBody.message ?? "").includes(String(setBody.note ?? "x"))) {
    bad.push("返回文案没带上「只改外观、要重新生成」那一句");
  }
  if (out.meta_after_set !== "navy") bad.push(`主题没落库：${String(out.meta_after_set)}`);
  if (out.bad_status !== 400) bad.push(`非法主题没回 400（实得 ${String(out.bad_status)}）`);
  const detail = String(out.bad_detail ?? "");
  if (!detail.includes("不是合法主题") || !detail.includes("navy（深蓝商务）")) {
    bad.push(`400 的文案没说清合法值：${detail.slice(0, 120)}`);
  }
  if (out.meta_after_bad !== "navy") bad.push("非法值居然改了库");
  const block = (out.course_block ?? {}) as Record<string, unknown>;
  if (block.id !== "navy" || block.source !== "course" || block.setting !== "navy") {
    bad.push(`/ppt/themes 的本课块不对：${JSON.stringify(block)}`);
  }
  if (block.source_label !== "本课设置") bad.push("本课来源的显示名应是「本课设置」");
  if (!Array.isArray(block.colors) || (block.colors as string[]).length !== 6) {
    bad.push("本课块必须带 6 色数据色板");
  }
  if (out.no_param_course !== null) bad.push("不带 course_id 时 course 必须是 null（老前端不受影响）");
  if (out.active_id !== "clean") bad.push(`active 仍是后端配置那套（此处应为 clean）：${String(out.active_id)}`);
  if (out.navy_theme !== "navy" || out.navy_source !== "course") {
    bad.push(`不传 theme 时没走课程设置：${String(out.navy_theme)} / ${String(out.navy_source)}`);
  }
  if (!out.navy_cover_has_navy_colour || !out.navy_has_no_clean_accent) {
    bad.push("课程设 navy 出的课件不是 navy 配色（封面 / 内页色对不上）");
  }
  if (out.clean_theme !== "clean" || out.clean_source !== "param") {
    bad.push(`显式传主题没赢过课程设置：${String(out.clean_theme)} / ${String(out.clean_source)}`);
  }
  if (!out.clean_has_clean_accent) bad.push("显式传 clean 出的课件不是 clean 配色");
  if (out.clear_status !== 200 || out.meta_after_clear !== null) {
    bad.push(`清空本课设置没生效：${String(out.clear_status)} / ${String(out.meta_after_clear)}`);
  }
  const after = (out.course_after_clear ?? {}) as Record<string, unknown>;
  if (after.source !== "config" || after.setting !== null) {
    bad.push(`清空后应回到后端配置：${JSON.stringify(after)}`);
  }
  if (out.llm_guard_status !== 200) bad.push("0-LLM 护栏下生成课件失败（说明真的在调模型）");
  return bad;
}

/* ======================= 走查 ======================= */

failures.push(...auditSourceTables(pyLabelTable, tsLabelTable, pyApplyNote, tsApplyNote,
  fallbackRows, pyThemeRows));
failures.push(...auditBackend(settingSource, pptSource, routeSource));
failures.push(...auditWiring(panelSource, apiSource, files.get(REL_VERIFY) ?? ""));

const tmp = mkdtempSync(join(tmpdir(), "cg145_"));
try {
  // ④ 行为级：真 import lib/pptTheme.ts（Node 类型擦除）
  const libUrl = pathToFileURL(join(repo, REL_LIB)).href;
  const lib = await import(libUrl) as unknown as Lib;
  failures.push(...auditBehavior(lib));
  check(THEME_TABLE_OK(), "来源表两侧逐字一致（见 auditSourceTables 的报错）");

  // 变异自检：改坏一处必须判红（否则上面的断言等于没写）
  const mutatedLib = libSource
    .replace('course: "本课设置",', 'course: "本课的主题",')
    .replace("label: item.id === current ? `${item.name}（本课当前）` : item.name,",
             "label: item.name,");
  const mutatedPath = join(tmp, "pptTheme.ts");
  writeFileSync(mutatedPath, mutatedLib, "utf8");
  const mutated = await import(pathToFileURL(mutatedPath).href) as unknown as Lib;
  const mutations: [string, string[]][] = [
    ["来源表写歪（本课设置 → 本课的主题）",
     auditSourceTables(pyLabelTable, rowsOf(blockOf(mutatedLib,
       "THEME_SOURCE_LABELS: Record<string, string> = {"), "([a-zA-Z_]+)"),
     pyApplyNote, tsApplyNote, fallbackRows, pyThemeRows)],
    ["下拉不再标「本课当前」（标成第一项）", auditBehavior(mutated)],
    ["界面不按课程取主题（api.pptThemes() + 写死 clean）",
     auditWiring(panelSource.replace("api.pptThemes(courseId)", "api.pptThemes()")
       .replace("const theme = themePicked ?? courseTheme;", 'const theme = "clean";'),
     apiSource, files.get(REL_VERIFY) ?? "")],
    ["generate_deck 的默认值改回 theme07",
     auditBackend(settingSource,
       pptSource.replace('theme: str = "", engine: str | None = None,',
                         'theme: str = "theme07", engine: str | None = None,'),
       routeSource)],
    ["写入不再校验（非法值静默回落）",
     auditBackend(settingSource.replace("raise ValueError(", "raise NotImplementedError("),
       pptSource, routeSource)],
  ];
  for (const [label, flagged] of mutations) {
    if (!flagged.length) failures.push(`变异「${label}」没有被检查器判红（断言失效）`);
  }

  let out: Record<string, unknown> | null = null;
  try {
    out = runProbe(tmp, PY_PROBE);
  } catch (e) {
    failures.push(`行为探针没跑通（接口可能已经坏了）：${String(e).split("\n")[0]}`);
  }
  if (out) {
    failures.push(...auditProbe(out));
    const mutatedOut = { ...out, navy_theme: "clean", navy_source: "config",
                         meta_after_bad: "clean" };
    if (!auditProbe(mutatedOut).length) {
      failures.push("变异「优先级反过来」没有被行为断言判红（断言失效）");
    }
  }
  console.log(`主题跟随课程（任务 145）：来源表 ${pyLabelTable.length} 条逐字一致 · `
    + `优先级 ${mutations.length + 1} 条变异全部判红 · `
    + `真接口：设主题 ${String(out?.set_status)} / 非法 ${String(out?.bad_status)} / `
    + `不传 theme 出 ${String(out?.navy_theme)}（${String(out?.navy_source)}）/ `
    + `显式传 ${String(out?.clean_theme)}（${String(out?.clean_source)}）`);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

if (failures.length) {
  console.error(`\n❌ 任务 145 主题跟随课程断言失败 ${failures.length} 条：`);
  for (const f of failures) console.error("   - " + f);
  process.exit(1);
}
console.log("✅ 任务 145 主题跟随课程断言全过（课程设置记得住：显式传 > 本课 > 配置 > 内置默认）");

function THEME_TABLE_OK(): boolean {
  return JSON.stringify(pyLabelTable) === JSON.stringify(tsLabelTable)
    && pyApplyNote === tsApplyNote
    && JSON.stringify(fallbackRows) === JSON.stringify(pyThemeRows);
}
