/**
 * 任务 150 前端断言：讲师模式（导图导航 + 逐条揭示 + 展开 + 深浅 + 讲者视图）。
 *
 * 行为基准是 `AI样张与研究/讲师模式/讲师模式-包子第1章.html`。本脚本钉六组：
 * ① **后端路由**：`GET /api/ppt/{course_id}/outline`、`/{item_id}/presenter-data`、
 *    `/{item_id}/presenter` 三条都在，且分别复用 `presenter_mode` 的同一份数据/渲染，
 *    404 / 500 有明确原因，HTML 是 `text/html; charset=utf-8`；
 * ② **入口接线**：`PptDeckBlocks.tsx::DeckPreview` 里有「讲师模式」按钮，调
 *    `api.pptPresenterHtml(deck.id)`，先开新标签页再填 blob；失败有可见 `Alert`；
 * ③ **既有入口不动**：同一排仍保留「网页版预览」与「预览」，仍调 `api.pptDeckHtml`；
 * ④ **单文件模板五项**：逐条揭示 / 点开展开 / D 深浅 / M 导图 / P 讲者视图 + 深链参数；
 * ⑤ **结构页守卫**：标签与展开按钮都受 `!structural` 约束（练习/回顾页不许挂）；
 * ⑥ **变异自检**：删入口 / 改错接口 / 去掉结构页守卫 / 去掉 D / 去掉 M 徽章 / 去掉 blob
 *    六种「不该发生」的样子必须逐条判红。
 *
 * ⛔ 0 LLM：只读源码，不起后端、不碰真库 / 真 storage。
 * 运行：`node scripts/check-presenter-mode.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const REL_ROUTE = "backend/app/api/routes/ppt.py";
const REL_SERVICE = "backend/app/services/presenter_mode.py";
const REL_BLOCKS = "frontend/src/components/PptDeckBlocks.tsx";
const REL_API = "frontend/src/api.ts";
const REL_VERIFY = "scripts/verify.ps1";

const BUTTON_TEXT = "讲师模式";
const WEB_BUTTON_TEXT = "网页版预览（带动效、可放大）";
const NOTE_TEXT = "讲师模式：逐条揭示 + 点开看讲义原文 + 导图导航 + 讲者视图（离线单文件）";

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

function deckPreview(src: string): string {
  const at = src.indexOf("export function DeckPreview(");
  return at === -1 ? "" : src.slice(at);
}

/** 含 needle 的那个 `<Button …>…</Button>`（按钮文案与说明句都含「讲师模式」，必须只认按钮）。 */
function buttonOf(src: string, needle: string): string {
  for (const m of src.matchAll(/<Button[\s\S]*?<\/Button>/g)) {
    if (m[0].includes(needle)) return m[0];
  }
  return "";
}

function auditRoutes(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/api/routes/ppt.py"];
  if (!/@router\.get\("\/\{course_id\}\/outline", response_model=PptOutlineResponse\)/.test(source)) {
    bad.push("缺 GET /{course_id}/outline（或响应模型不是 PptOutlineResponse）");
  }
  if (!/@router\.get\("\/\{item_id\}\/presenter-data", response_model=PresenterDataResponse\)/.test(source)) {
    bad.push("缺 GET /{item_id}/presenter-data（或响应模型不是 PresenterDataResponse）");
  }
  if (!/@router\.get\("\/\{item_id\}\/presenter", response_class=HTMLResponse\)/.test(source)) {
    bad.push("缺 GET /{item_id}/presenter，或没有用 HTMLResponse 返回");
  }
  if (!source.includes("presenter_mode.outline_payload(")) bad.push("outline 没复用 presenter_mode.outline_payload");
  if (!source.includes("presenter_mode.build_data(")) bad.push("presenter-data 没复用 presenter_mode.build_data");
  if (!source.includes("presenter_mode.render_presenter(")) bad.push("presenter 没复用 presenter_mode.render_presenter");
  if (!source.includes('media_type="text/html; charset=utf-8"')) {
    bad.push("讲师模式 HTML 的 media_type 不是逐字 text/html; charset=utf-8");
  }
  if (!/raise HTTPException\(404/.test(source)) bad.push("没有 404 分支（课件/课程不存在）");
  if (!/raise HTTPException\(500/.test(source)) bad.push("没有 500 分支（渲染异常会静默空页）");
  if (!/logger\.exception\(/.test(source)) bad.push("异常没写日志（排查时看不到原因）");
  if (!/require_api_key/.test(source)) bad.push("路由没挂 require_api_key（与既有 PPT 路由不同鉴权）");
  return bad;
}

function auditEntry(source: string): string[] {
  const bad: string[] = [];
  const body = deckPreview(source);
  if (!body) return ["取不到 DeckPreview（PPT 面板与预览页共用积木）"];
  const entry = buttonOf(body, BUTTON_TEXT);
  if (!entry) bad.push("DeckPreview 里没有「讲师模式」按钮");
  if (!entry.includes("openPresenter")) bad.push("按钮没有接到 openPresenter");
  if (!body.includes("api.pptPresenterHtml(deck.id)")) bad.push("openPresenter 没有调 api.pptPresenterHtml(deck.id)");
  if (!body.includes('window.open("", "_blank")')) bad.push("没有先开新标签页（被浏览器拦成弹窗）");
  if (!body.includes("URL.createObjectURL(new Blob([html]")) bad.push("没有用 blob URL 打开单文件 HTML");
  if (!body.includes("setPresenterError")) bad.push("失败没有状态（用户点了没反应）");
  if (!/<Alert type="error"[\s\S]{0,200}presenterError/.test(body)) {
    bad.push("失败没有可见 Alert（presenterError）");
  }
  if (!body.includes(NOTE_TEXT)) bad.push("没有常显那句讲师模式能力说明");
  if (!body.includes(WEB_BUTTON_TEXT)) bad.push("既有「网页版预览」入口被删/改名");
  if (!body.includes("api.pptDeckHtml(deck.id)")) bad.push("既有网页版课件调用被删/改名");
  if (!body.includes("关闭预览")) bad.push("既有「预览」入口被删/改名");
  return bad;
}

function auditApi(source: string): string[] {
  if (!source) return ["取不到 frontend/src/api.ts"];
  if (!/pptPresenterHtml:\s*\(itemId: string\)\s*=>\s*requestText\(`\/ppt\/\$\{itemId\}\/presenter`\)/.test(source)) {
    return ["api.ts 的 pptPresenterHtml 没有指向 /ppt/{id}/presenter"];
  }
  if (!/pptDeckHtml:\s*\(itemId: string\)\s*=>\s*requestText\(`\/ppt\/\$\{itemId\}\/html`\)/.test(source)) {
    return ["既有 api.pptDeckHtml 被改动（网页版课件契约变了）"];
  }
  return [];
}

function auditTemplate(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/services/presenter_mode.py"];
  if (!/def render_presenter\(/.test(source)) bad.push("没有 render_presenter");
  if (!source.includes("const DECK = __DECK__;")) bad.push("模板没有注入 DECK 数据（不是单文件）");
  if (!source.includes("讲师模式")) bad.push("HTML 标题/文案里没有「讲师模式」");

  // 逐条揭示
  if (!/\["ArrowRight", "PageDown", " ", "Enter"\]\.includes\(k\)/.test(source)) {
    bad.push("没有 → / 空格 / PageDown 的逐条揭示键位");
  }
  if (!source.includes("revealed++") || !source.includes("if (!isStructural(s) && revealed < limitOf(s))")) {
    bad.push("逐条揭示没有「出完才翻页」的门槛");
  }

  // 展开
  if (!source.includes('querySelectorAll(".b")')) bad.push("要点行不可点（没有 .b 点击绑定）");
  if (!source.includes("function toggleRow(") || !source.includes("if (k === \"e\" || k === \"E\")")) {
    bad.push("没有点行/E 展开与收起");
  }
  for (const needle of ["完整句", "这一段还讲了", "it.source"]) {
    if (!source.includes(needle)) bad.push(`展开面板缺「${needle}」`);
  }
  if (!source.includes('if (k === "Escape") { $("mm").classList.remove("on"); openSet.clear(); render(); }')) {
    bad.push("Esc 没有全收");
  }

  // 深浅
  if (!source.includes('depth = depth === "full" ? "core" : "full"')) bad.push("D 没有切换完整/只讲必讲");
  if (!source.includes('else if (k === "d" || k === "D")')) bad.push("D 键没有绑定深浅切换");
  if (!source.includes('its.filter((it) => tierOf(it) === CORE).length')) bad.push("浅档没有按 core 过滤");
  if (!source.includes("const n = depth === \"core\"")) bad.push("逐条揭示没有随档位同步");

  // 导图
  if (!source.includes("function renderMap(")) bad.push("没有导图 renderMap");
  if (!source.includes("page_indexes")) bad.push("导图节点没有 page_indexes（跳不了页）");
  if (!source.includes("const badge = n.page_count > 1")) bad.push("多页节点没有「N 页」徽章");
  if (!source.includes("node.dataset.page")) bad.push("点节点没有跳页");
  if (!source.includes("done") || !source.includes("cur")) bad.push("导图没有当前高亮 / 已过变灰");
  if (!source.includes('if (k === "m" || k === "M") toggleMap();')) bad.push("M 没有打开导图");

  // 讲者视图
  if (!source.includes("function renderPresenter(")) bad.push("没有讲者视图 renderPresenter");
  for (const id of ["pNotes", "pClock", "pReveal", "pPace", "pNext"]) {
    if (!source.includes(`$("${id}")`)) bad.push(`讲者视图缺 ${id}`);
  }
  if (!source.includes("is_cuttable")) bad.push("讲者视图没有可砍页数");
  if (!source.includes('if (k === "p" || k === "P")')) bad.push("P 没有打开讲者视图");
  if (!source.includes('if (k === "t" || k === "T")')) bad.push("T 没有计时归零");

  // 深链
  if (!source.includes("new URLSearchParams(location.hash.slice(1))")) bad.push("没有解析深链");
  for (const key of ["\"p\"", "\"r\"", "\"open\"", "\"t\"", "\"presenter\"", "\"map\""]) {
    if (!source.includes(key)) bad.push(`深链缺参数 ${key}`);
  }
  if (!source.includes('m.get("t") === "core"') || !source.includes('m.get("presenter") === "1"') ||
      !source.includes('m.get("map") === "1"')) {
    bad.push("深链没有 t=core / presenter=1 / map=1");
  }

  // 结构页守卫
  if (!source.includes("const can = !structural && canExpand(it);")) {
    bad.push("结构页没有挡住展开按钮（练习/回顾页会挂展开）");
  }
  if (!source.includes("const tag = (!structural && tier !== CORE)")) {
    bad.push("结构页没有挡住档位标签");
  }

  // 单文件 / 离线
  if (/<script\s+src=/i.test(source) || /<link\b/i.test(source)) bad.push("模板引了外部 script/link，不是单文件");
  if (/https?:\/\//i.test(source)) bad.push("模板里有 http(s) 外部引用，离线打不开");
  return bad;
}

function auditVerify(source: string): string[] {
  if (!source.includes("check-presenter-mode.ts")) return ["scripts/verify.ps1 没接入 check-presenter-mode.ts"];
  return [];
}

const files = new Map<string, string>();
for (const rel of [REL_ROUTE, REL_SERVICE, REL_BLOCKS, REL_API, REL_VERIFY]) files.set(rel, read(rel));

const route = files.get(REL_ROUTE) ?? "";
const service = files.get(REL_SERVICE) ?? "";
const blocks = stripComments(files.get(REL_BLOCKS) ?? "");
const api = files.get(REL_API) ?? "";

const staticGroups: [string, string[]][] = [
  ["后端路由", auditRoutes(route)],
  ["入口接线", auditEntry(blocks)],
  ["api.ts", auditApi(api)],
  ["单文件模板", auditTemplate(service)],
  ["门禁接入", auditVerify(files.get(REL_VERIFY) ?? "")],
];
for (const [, bad] of staticGroups) failures.push(...bad);

/* 变异：六种「不该发生」的样子必须逐条判红（否则断言等于没写） */
const cases: [string, string[]][] = [
  ["删掉讲师模式按钮", auditEntry(blocks.replace(buttonOf(blocks, BUTTON_TEXT), ""))],
  ["接口指到旧地址", auditApi(api.replace("ppt/${itemId}/presenter", "ppt/${itemId}/presenter-old"))],
  ["去掉结构页守卫", auditTemplate(service.replace("const can = !structural && canExpand(it);", "const can = canExpand(it);"))],
  ["去掉 D 深浅", auditTemplate(service.replace('else if (k === "d" || k === "D")', "else if (false)"))],
  ["去掉多页徽章", auditTemplate(service.replace("const badge = n.page_count > 1", "const badge = false"))],
  ["不再先开标签页", auditEntry(blocks.replaceAll('window.open("", "_blank")', 'window.open("about:blank")'))],
];
for (const [label, flagged] of cases) {
  if (!flagged.length) failures.push(`变异「${label}」没有被检查器判红（断言失效）`);
}

if (failures.length) {
  console.error(`\n❌ 任务 150 讲师模式断言失败 ${failures.length} 条：`);
  for (const f of failures) console.error("   - " + f);
  process.exit(1);
}
console.log("✅ 任务 150 讲师模式断言全过（三条只读路由 + 共用入口 + 五项交互 + 结构页守卫；"
  + `${cases.length} 条变异全部判红）`);
