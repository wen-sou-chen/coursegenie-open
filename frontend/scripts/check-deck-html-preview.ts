/**
 * 任务 94 前端断言：**网页里可预览 HTML 课件（带入场动效）**（**0 LLM**）。
 *
 * 钉四组：
 * ① **源码级（后端接口）**：`app/api/routes/ppt.py` 里存在 `GET /ppt/{item_id}/html`、
 *    返回 `HTMLResponse` 且 `media_type` 逐字是 `text/html; charset=utf-8`、
 *    **复用** `deck_html.render_deck`（不许自己拼 HTML：源码里不许出现 CSS 骨架 / 内联样式），
 *    取图口径走 `delivery.figure_rows`、404（不存在 / 非 ppt）与 500（渲染异常带原因）都在；
 * ② **接线（共用积木）**：按钮文案与 `api.pptDeckHtml` 调用都在 `PptDeckBlocks.tsx` 的
 *    `DeckPreview` 里（PPT 面板与第 4 步预览页因此**同时**得到），面板文件里**不许**再各写一份；
 *    失败必须有可见提示（`Alert`）；前端**不许**把 API Key 拼进 URL（走 `authHeader`）；
 * ③ **行为级（真跑 Python + 真调接口）**：临时五件套里真建课 / 真建课件 / 真调接口，
 *    贴出：状态码、`content-type`、字节数、`@keyframes` 处数、`1.05s` 在不在、
 *    **与交付包 `课件/HTML/{deck}.html` 是否逐字节相同**、404/500 两种结果、0-LLM 护栏；
 * ④ **变异**：四种「不该发生」的样子（接口返回纯文本 / 按钮挪出共用积木 / 接口另写一套渲染 /
 *    失败不给可见提示）当变异喂给检查器 —— 必须逐条判红。
 *
 * ⛔ 0 LLM：子进程显式 `LLM_PROVIDER=mock`，五件套全部指向系统临时目录（不碰真库 / 真 vault）。
 * 运行：`node scripts/check-deck-html-preview.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1（前端断言 33 → 34 条）。
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

const REL_ROUTE = "backend/app/api/routes/ppt.py";
const REL_RENDER = "backend/app/services/deck_html.py";
const REL_DELIVERY = "backend/app/services/delivery.py";
const REL_BLOCKS = "frontend/src/components/PptDeckBlocks.tsx";
const REL_API = "frontend/src/api.ts";
const REL_PANEL = "frontend/src/components/PptPanel.tsx";
const REL_STEP = "frontend/src/components/steps/PreviewStep.tsx";

/** 按钮文案（界面上的唯一出处；任务 109 提级后改过一次，两处断言同步）。 */
const BUTTON_TEXT = "网页版预览（带动效、可放大）";

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ---------------- ① 源码级：后端接口 ---------------- */

/** 只取 `/html` 这一段路由（任务 150 新增了讲师模式 HTML 路由，全局匹配会互相兜底）。 */
function htmlRouteBlock(source: string): string {
  const at = source.indexOf('@router.get("/{item_id}/html"');
  if (at === -1) return "";
  const next = source.indexOf("\n@router.", at + 1);
  return source.slice(at, next === -1 ? source.length : next);
}

function auditRoute(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/api/routes/ppt.py"];
  const body = htmlRouteBlock(source);
  if (!body) {
    bad.push("ppt.py 没有 GET /{item_id}/html 路由");
    return bad;
  }
  if (!/response_class=HTMLResponse/.test(body) || !/HTMLResponse\(content=/.test(body)) {
    bad.push("接口没有用 HTMLResponse 返回（会变成 JSON/纯文本）");
  }
  if (!body.includes('media_type="text/html; charset=utf-8"')) {
    bad.push("media_type 不是逐字 text/html; charset=utf-8");
  }
  if (!/deck_html\.render_deck\(/.test(body)) {
    bad.push("接口没有复用 deck_html.render_deck（另写了一套渲染）");
  }
  if (!/delivery_mod\.figure_rows\(/.test(body)) {
    bad.push("取图口径没走 delivery.figure_rows（与出包会不一致）");
  }
  if (!/raise HTTPException\(404/.test(body)) bad.push("没有 404 分支（不存在 / 非 ppt）");
  if (!/except Exception/.test(body) || !/raise HTTPException\(500/.test(body)) {
    bad.push("渲染异常没有 500 + 原因（会静默返回空页）");
  }
  if (!/logger\.exception\(/.test(body)) bad.push("渲染异常没写日志");
  // 不许另写一套渲染：路由文件里不该出现 HTML 骨架 / 样式 / 动画参数
  for (const needle of ["@keyframes", "animation-duration", "<!DOCTYPE", "ANIM_SECONDS"]) {
    if (source.includes(needle)) bad.push(`ppt.py 里出现 ${needle}（像是自己拼 HTML，必须复用 deck_html）`);
  }
  if (!/require_api_key/.test(source)) bad.push("路由没挂 require_api_key（与既有 ppt 路由不同鉴权）");
  return bad;
}

function auditRenderSource(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/services/deck_html.py"];
  if (!/def render_deck\(/.test(source)) bad.push("deck_html.py 没有 render_deck（单份按需渲染）");
  if (!/def _render_row\(/.test(source)) bad.push("deck_html.py 没有 _render_row（唯一渲染路径）");
  if (!/return _render_row\(course, deck, list_nodes\(db, course\.id\), figure_index\(figures\)\)/.test(source)) {
    bad.push("render_deck 没有直接复用 _render_row（渲染路径分叉了）");
  }
  if (!/\[_render_row\(course, deck, nodes, index\)/.test(source)) {
    bad.push("build_files 没有复用 _render_row（出包与预览可能分叉）");
  }
  // 动画与版式口径（任务 88 已定稿）：**本任务一个字都不许动**
  for (const line of ["ANIM_SECONDS = 1.05", "ANIM_STEP_MS = 170", "ANIM_BASE_MS = 180",
                      "CANVAS_W, CANVAS_H = 1122, 793"]) {
    if (!source.includes(line)) bad.push(`deck_html.py 的动效/画布口径被改了：少了「${line}」`);
  }
  return bad;
}

function auditDeliverySource(source: string): string[] {
  const bad: string[] = [];
  if (!/def figure_rows\(items: list\[ContentItem\]\) -> list\[dict\]/.test(source)) {
    bad.push("delivery.py 没有 figure_rows（取图口径没有单一出处）");
  }
  if (!/figures=figure_rows\(items\)/.test(source)) {
    bad.push("出包没有用 figure_rows(items)（与预览接口的取图口径会分叉）");
  }
  return bad;
}

/* ---------------- ② 接线：共用积木 + 新标签页 ---------------- */

function auditWiring(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const blocks = files.get(REL_BLOCKS) ?? "";
  const api = files.get(REL_API) ?? "";
  const panel = files.get(REL_PANEL) ?? "";
  const step = files.get(REL_STEP) ?? "";

  if (!blocks.includes(BUTTON_TEXT)) {
    bad.push(`共用积木 PptDeckBlocks.tsx 里没有按钮「${BUTTON_TEXT}」`);
  }
  if (!/api\.pptDeckHtml\(deck\.id\)/.test(blocks)) {
    bad.push("共用积木没有调 api.pptDeckHtml(deck.id)");
  }
  if (!/export function DeckPreview\(/.test(blocks)) {
    bad.push("共用积木里没有 DeckPreview（按钮必须落在共用积木内）");
  }
  // 按钮必须在 **DeckPreview 组件体内部**（PPT 面板 / 第 4 步预览页都只是调用它）
  const deckPreviewAt = blocks.indexOf("export function DeckPreview(");
  const nextExportAt = blocks.indexOf("\nexport function ", deckPreviewAt + 1);
  const buttonAt = blocks.indexOf(BUTTON_TEXT);
  const deckPreviewEnd = nextExportAt === -1 ? blocks.length : nextExportAt;
  if (deckPreviewAt === -1 || buttonAt === -1
      || buttonAt < deckPreviewAt || buttonAt > deckPreviewEnd) {
    bad.push("按钮不在 DeckPreview 组件体里（挪出去后各面板就各写一套了）");
  }
  if (!/type="error"/.test(blocks) || !/htmlError/.test(blocks)) {
    bad.push("加载失败没有可见提示（Alert + htmlError）");
  }
  // 不许在面板里各写一份同名按钮
  for (const [name, src] of [[REL_PANEL, panel], [REL_STEP, step]] as [string, string][]) {
    if (src.includes(BUTTON_TEXT)) bad.push(`${name} 里又写了一份「${BUTTON_TEXT}」（应只在共用积木里）`);
    if (/pptDeckHtml/.test(src)) bad.push(`${name} 直接调了 pptDeckHtml（应走共用积木）`);
  }
  // API：新增方法 + 文本响应 + 鉴权头（Key 不进 URL）
  if (!/pptDeckHtml: \(itemId: string\) => requestText\(`\/ppt\/\$\{itemId\}\/html`\)/.test(api)) {
    bad.push("api.ts 没有 pptDeckHtml：走 /ppt/{itemId}/html 的文本响应");
  }
  if (!/async function requestText\(/.test(api) || !/authHeader\(\)/.test(api)) {
    bad.push("requestText 没带鉴权头（开着 AUTH_ENABLED 时会 401）");
  }
  if (/[?&](api_?key|key)=/.test(api + blocks)) {
    bad.push("把 API Key 拼进了 URL（本项目约定：key 只进 localStorage，不进 URL）");
  }
  if (!/window\.open\("", "_blank"\)/.test(blocks)) {
    bad.push("没有在新标签页打开（await 之后再开会被弹窗拦截，必须先开窗口）");
  }
  if (!/createObjectURL/.test(blocks)) {
    bad.push("没有走 blob URL（单文件 HTML 直接开新标签页会缺鉴权头）");
  }
  return bad;
}

/* ---------------- ③ 行为级：真跑 Python ---------------- */

const PY_PROBE = String.raw`
# -*- coding: utf-8 -*-
"""任务 94 行为探针：临时五件套里真建课 / 真调 GET /api/ppt/{id}/html（0 LLM）。"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="cg94_probe_"))
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
})
REPO = Path(os.environ["CG_REPO"])
sys.path.insert(0, str(REPO / "backend"))

from fastapi.testclient import TestClient            # noqa: E402
from app.main import app                             # noqa: E402
from app.db.base import Base                         # noqa: E402
from app.db.session import engine, SessionLocal      # noqa: E402
from app.models.course import ContentItem            # noqa: E402
from app.services import deck_html, delivery, llm    # noqa: E402
from tests.test_deck_html import _seed_deck, _seed_figure, SPOKEN, INTERNAL_NOTE   # noqa: E402
from tests.test_delivery import _new_course, _plan_approve, _add_section           # noqa: E402

Base.metadata.create_all(bind=engine)
out = {}
with TestClient(app) as client:
    course_id = _new_course(client, "94 网页版预览")
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "老面发酵")
    db = SessionLocal()
    try:
        from app.services.planner import list_nodes
        node = [n for n in list_nodes(db, course_id) if str(n.node_key) == "1.1"][0]
        node_id = node.id
    finally:
        db.close()
    deck_id = _seed_deck(course_id, title="第1章课件", scope="chapter", chapter_key="1",
                         slides=[("1 老面发酵", SPOKEN + "\n" + INTERNAL_NOTE, ["本章课件"]),
                                 ("老面发酵", "**发酵判断**：体积膨胀约 2 倍。", ["**体积**：膨胀约 2 倍"]),
                                 ("温度窗口", "**温度**：25~30C 最稳。", ["**温度**：25~30C"]),
                                 ("本章回顾", SPOKEN, ["要点回顾"])],
                         layouts=["cover", "point", "point", "review"])
    _seed_figure(course_id, node_id, "1.1", name="t94-a.svg", caption="老面发酵概念图")

    # ① 真调接口
    resp = client.get("/api/ppt/" + deck_id + "/html")
    out["status"] = resp.status_code
    out["content_type"] = resp.headers.get("content-type", "")
    out["bytes"] = len(resp.content)
    text = resp.content.decode("utf-8")
    out["keyframes"] = text.count("@keyframes")
    out["slides"] = text.count('class="slide"')
    squeezed = text.replace(" ", "")
    out["anim_1_05"] = "animation-duration:1.05s" in squeezed
    out["anim_step"] = ("180ms" in squeezed) and ("170ms" in squeezed)
    out["external_refs"] = text.count("http://") + text.count("https://")
    out["has_viewport"] = '<meta name="viewport" content="width=device-width, initial-scale=1">' in text
    out["has_print_a4"] = ("@media print" in text) and ("size:A4landscape" in squeezed)

    # ② 与交付包逐字节一致
    built = client.post("/api/delivery/course/" + course_id + "/build", json={"pdf": "off"})
    out["build_status"] = built.status_code
    packed = (delivery.deliveries_root() / built.json()["build_id"] / "课件" / "HTML" / "第1章课件.html")
    out["packed_bytes"] = packed.stat().st_size
    out["same_as_package"] = packed.read_bytes() == resp.content

    # ③ 404 两种 + 500 带原因 + 0 LLM
    out["status_404_unknown"] = client.get("/api/ppt/no-such-id/html").status_code
    db = SessionLocal()
    try:
        lecture = ContentItem(course_id=course_id, kind="lecture", title="讲义", body={}, meta={})
        db.add(lecture)
        db.commit()
        lecture_id = lecture.id
    finally:
        db.close()
    out["status_404_non_ppt"] = client.get("/api/ppt/" + lecture_id + "/html").status_code

    orig_render = deck_html.render_deck

    def _boom(*a, **k):
        raise RuntimeError("渲染炸了（探针注入）")

    deck_html.render_deck = _boom
    err = client.get("/api/ppt/" + deck_id + "/html")
    deck_html.render_deck = orig_render
    out["status_500"] = err.status_code
    out["reason_in_500"] = "渲染炸了（探针注入）" in err.text

    orig_client = llm.build_llm_client

    def _no_llm(*a, **k):
        raise AssertionError("预览不许调 LLM")

    llm.build_llm_client = _no_llm
    guarded = client.get("/api/ppt/" + deck_id + "/html")
    llm.build_llm_client = orig_client
    out["status_with_llm_guard"] = guarded.status_code

print("PROBE_JSON " + json.dumps(out, ensure_ascii=False))
`;

function runProbe(tmp: string, src: string): Record<string, unknown> {
  const py = join(tmp, "probe_t94.py");
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

function auditBehavior(out: Record<string, unknown>): string[] {
  const bad: string[] = [];
  if (out.status !== 200) bad.push(`接口没回 200（实得 ${String(out.status)}）`);
  const ct = String(out.content_type ?? "").toLowerCase();
  if (!ct.includes("text/html")) bad.push(`content-type 不是 text/html（实得 ${ct}）`);
  if (!ct.includes("charset=utf-8")) bad.push(`content-type 没有 charset=utf-8（实得 ${ct}）`);
  if (Number(out.bytes) < 2000) bad.push(`响应体太小（${String(out.bytes)} B），不像整份课件`);
  const frames = Number(out.keyframes);
  if (!(frames >= 3)) bad.push(`@keyframes 少于 3 处（实得 ${frames}）`);
  if (!out.anim_1_05) bad.push("没有 animation-duration:1.05s（慢动画口径丢了）");
  if (!out.anim_step) bad.push("错开节奏丢了（180ms / 170ms）");
  if (Number(out.external_refs) !== 0) bad.push(`HTML 里有外部引用 ${String(out.external_refs)} 处`);
  if (!out.has_viewport) bad.push("缺 viewport");
  if (!out.has_print_a4) bad.push("缺打印 A4 横版");
  if (out.build_status !== 200) bad.push(`交付包构建失败（${String(out.build_status)}）`);
  if (!out.same_as_package) {
    bad.push(`与交付包不一致（接口 ${String(out.bytes)} B vs 包内 ${String(out.packed_bytes)} B）`);
  }
  if (out.status_404_unknown !== 404) bad.push(`错 id 没有 404（${String(out.status_404_unknown)}）`);
  if (out.status_404_non_ppt !== 404) bad.push(`非 ppt 条目没有 404（${String(out.status_404_non_ppt)}）`);
  if (out.status_500 !== 500) bad.push(`渲染异常没有 500（${String(out.status_500)}）`);
  if (!out.reason_in_500) bad.push("500 没把原因带出来（静默空页）");
  if (out.status_with_llm_guard !== 200) bad.push("0-LLM 护栏下接口没回 200（说明它真的在调模型）");
  return bad;
}

/* ---------------- 走查 ---------------- */

const files = new Map<string, string>();
for (const rel of [REL_ROUTE, REL_RENDER, REL_DELIVERY, REL_BLOCKS, REL_API, REL_PANEL, REL_STEP]) {
  files.set(rel, read(rel));
}

failures.push(...auditRoute(files.get(REL_ROUTE) ?? ""));
failures.push(...auditRenderSource(files.get(REL_RENDER) ?? ""));
failures.push(...auditDeliverySource(files.get(REL_DELIVERY) ?? ""));
failures.push(...auditWiring(files));

const tmp = mkdtempSync(join(tmpdir(), "cg_deckprev94_"));
try {
  // 探针崩了也要把「源码级」的失败原样报出来（否则一条变异会盖掉全部诊断）
  let out: Record<string, unknown> | null = null;
  try {
    out = runProbe(tmp, PY_PROBE);
  } catch (e) {
    failures.push(`行为探针没跑通（接口可能已经坏了）：${String(e).split("\n")[0]}`);
  }
  if (out) failures.push(...auditBehavior(out));

  // ④ 变异：四种「不该发生」的样子必须被判红（否则断言等于没写）
  const route = files.get(REL_ROUTE) ?? "";
  const render = files.get(REL_RENDER) ?? "";
  const blocks = files.get(REL_BLOCKS) ?? "";
  const cases: [string, string[]][] = [
    ["接口返回纯文本（去掉 HTMLResponse）",
     auditRoute(route.replace(/HTMLResponse\(content=row\["text"\], media_type="text\/html; charset=utf-8"\)/,
                              'PlainTextResponse(content=row["text"])'))],
    ["接口另写一套渲染（不再调 deck_html.render_deck）",
     auditRoute(route.replace("deck_html.render_deck(", "self_render("))],
    ["单份渲染不再复用 _render_row（渲染路径分叉）",
     auditRenderSource(render.replace("return _render_row(course, deck, list_nodes(db, course.id), figure_index(figures))",
                                      "return _render_row_again(course, deck, figures)"))],
    ["按钮挪出共用积木（只留在 PPT 面板）",
     auditWiring(new Map(files)
       .set(REL_BLOCKS, blocks.split(BUTTON_TEXT).join("旧文案"))
       .set(REL_PANEL, (files.get(REL_PANEL) ?? "") + "\n" + BUTTON_TEXT + "\n"))],
  ];
  for (const [label, flagged] of cases) {
    if (!flagged.length) failures.push(`变异「${label}」没有被检查器判红（断言失效）`);
  }
  // 行为级变异：接口返回的「不是 HTML」必须被行为断言抓住
  const mutated = { ...(out ?? {}), status: 200, bytes: 9999, content_type: "application/json",
                    same_as_package: false, keyframes: 3, anim_1_05: true, anim_step: true,
                    external_refs: 0, has_viewport: true, has_print_a4: true, build_status: 200,
                    status_404_unknown: 404, status_404_non_ppt: 404, status_500: 500,
                    reason_in_500: true, status_with_llm_guard: 200 };
  if (!auditBehavior(mutated).length) failures.push("变异「接口返回 JSON」没有被行为断言判红（断言失效）");

  console.log(`网页版课件预览（任务 94）：HTTP ${String(out?.status)} · `
    + `${String(out?.content_type)} · ${String(out?.bytes)} B · @keyframes ${String(out?.keyframes)} 处 · `
    + `与交付包逐字节相同 ${String(out?.same_as_package)} · `
    + `404/404/500 = ${String(out?.status_404_unknown)}/${String(out?.status_404_non_ppt)}/${String(out?.status_500)} · `
    + `${cases.length + 1} 条变异全部判红`);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

if (failures.length) {
  console.error(`\n❌ 任务 94 网页版课件预览断言失败 ${failures.length} 条：`);
  for (const f of failures) console.error("   - " + f);
  process.exit(1);
}
console.log("✅ 任务 94 网页版课件预览断言全过（0 LLM：复用出包同一渲染路径，接口与交付包逐字节一致）");
