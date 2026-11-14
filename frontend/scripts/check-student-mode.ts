/**
 * 任务 151 前端断言：学生版（地图 → 微单元 → 即时自测 → 复习卡）+ 答案泄漏守卫。
 *
 * 行为基准是 `AI样张与研究/学生版/学生版-包子制作入门.html`。本脚本钉六组：
 * ① **后端路由**：`GET /api/ppt/{course_id}/student`、`/{course_id}/student-html`
 *    都在，分别复用 `student_mode.build_course_data/render_student_html`；
 *    既有网页版 / 讲师模式路由仍在，404 / 500 有明确原因；
 * ② **入口接线**：`PptPanel.tsx::CoursePptTools` 里有「学生版」按钮，调
 *    `api.pptStudentHtml(courseId)`，先开新标签页再填 blob；失败有可见 `Alert`；
 * ③ **单文件模板五项**：地图 / 微单元 / 三种自测 / 复习卡 / localStorage 进度 + 深链；
 * ④ **答案默认隐藏**：整份数据 base64 内嵌，答案字段是 `answer_b64/correct_b64/explain_b64`，
 *    初始 HTML 不出现 `**答案**`；作答后才在 JS 里 `atob` 解码；
 * ⑤ **数据口径**：`####` 只归上一小节；来源括号按嵌套配平删除；一句话最多两句不切半；
 * ⑥ **变异自检**：删入口 / 改错接口 / `####` 开新小节 / 答案改明文 / 去掉进度 /
 *    去掉来源剥除六种「不该发生」的样子必须逐条判红。
 *
 * ⛔ 0 LLM：只读源码，不起后端、不碰真库 / 真 storage。
 * 运行：`node scripts/check-student-mode.ts`（Node >= 22.18 自带类型擦除）
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
const REL_SERVICE = "backend/app/services/student_mode.py";
const REL_PANEL = "frontend/src/components/PptPanel.tsx";
const REL_API = "frontend/src/api.ts";
const REL_VERIFY = "scripts/verify.ps1";

const BUTTON_TEXT = "学生版";
const NOTE_TEXT = "学生版：地图 → 微单元（含即时自测）→ 复习卡（离线单文件）";

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

function courseTools(src: string): string {
  const start = src.indexOf("function CoursePptTools(");
  if (start === -1) return "";
  const end = src.indexOf("export default function PptPanel(", start);
  return end === -1 ? src.slice(start) : src.slice(start, end);
}

/** 含 needle 的那个 `<Button …>…</Button>`（说明句也含「学生版」，必须只认按钮）。 */
function buttonOf(src: string, needle: string): string {
  for (const m of src.matchAll(/<Button[\s\S]*?<\/Button>/g)) {
    if (m[0].includes(needle)) return m[0];
  }
  return "";
}

function auditRoutes(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/api/routes/ppt.py"];
  if (!/@router\.get\("\/\{course_id\}\/student", response_model=StudentDataResponse\)/.test(source)) {
    bad.push("缺 GET /{course_id}/student（或响应模型不是 StudentDataResponse）");
  }
  if (!/@router\.get\("\/\{course_id\}\/student-html", response_class=HTMLResponse\)/.test(source)) {
    bad.push("缺 GET /{course_id}/student-html，或没有用 HTMLResponse 返回");
  }
  if (!source.includes("student_mode.build_course_data(")) bad.push("student 没复用 student_mode.build_course_data");
  if (!source.includes("student_mode.render_student_html(")) bad.push("student-html 没复用 student_mode.render_student_html");
  if (!source.includes('media_type="text/html; charset=utf-8"')) {
    bad.push("学生版 HTML 的 media_type 不是逐字 text/html; charset=utf-8");
  }
  if (!/raise HTTPException\(404/.test(source)) bad.push("没有 404 分支（课程 / 讲义不存在）");
  if (!/raise HTTPException\(500/.test(source)) bad.push("没有 500 分支（渲染异常会静默空页）");
  if (!/logger\.exception\(/.test(source)) bad.push("异常没写日志（排查时看不到原因）");
  if (!/require_api_key/.test(source)) bad.push("路由没挂 require_api_key（与既有 PPT 路由不同鉴权）");
  for (const route of [
    '@router.get("/{item_id}/html", response_class=HTMLResponse)',
    '@router.get("/{course_id}/outline", response_model=PptOutlineResponse)',
    '@router.get("/{item_id}/presenter-data", response_model=PresenterDataResponse)',
    '@router.get("/{item_id}/presenter", response_class=HTMLResponse)',
  ]) {
    if (!source.includes(route)) bad.push(`既有路由被改动：${route}`);
  }
  return bad;
}

function auditEntry(source: string): string[] {
  const bad: string[] = [];
  const body = courseTools(source);
  if (!body) return ["取不到 PptPanel.tsx::CoursePptTools"];
  const entry = buttonOf(body, BUTTON_TEXT);
  if (!entry) bad.push("CoursePptTools 里没有「学生版」按钮");
  if (!entry.includes("openStudent")) bad.push("按钮没有接到 openStudent");
  if (!body.includes("api.pptStudentHtml(courseId)")) bad.push("openStudent 没有调 api.pptStudentHtml(courseId)");
  if (!body.includes('window.open("", "_blank")')) bad.push("没有先开新标签页（await 后会被弹窗拦截）");
  if (!body.includes("new Blob([html]") || !body.includes("text/html;charset=utf-8")) {
    bad.push("没有用 blob 打开单文件 HTML");
  }
  if (!body.includes("tab.location.href = url")) bad.push("没有把 blob URL 填进新标签页");
  if (!body.includes("URL.revokeObjectURL")) bad.push("blob URL 没释放");
  if (!/<Alert type="error"[\s\S]{0,300}studentError/.test(body)) {
    bad.push("失败没有可见 Alert（studentError）");
  }
  if (!body.includes(NOTE_TEXT)) bad.push("没有常显那句学生版能力说明");
  return bad;
}

function auditApi(source: string): string[] {
  if (!/pptStudentHtml:\s*\(courseId: string\)\s*=>\s*requestText\(`\/ppt\/\$\{courseId\}\/student-html`\)/.test(source)) {
    return ["api.ts 的 pptStudentHtml 没有指向 /ppt/{courseId}/student-html"];
  }
  return [];
}

function auditTemplate(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/services/student_mode.py"];
  for (const fn of ["def build_course_data(", "def render_student_html(", "def split_sections(",
                    "def first_sentence(", "def strip_sources(", "def _matching_close("]) {
    if (!source.includes(fn)) bad.push(`缺 ${fn}`);
  }

  // 地图 / 微单元 / 自测 / 复习卡 / 进度
  for (const needle of ["function renderMap(", "function openUnit(", "function renderCards(",
                        "function pick(", "function judgeFill(", "function revealShort(",
                        "function flip(", "先搞清楚", "怎么用", "别踩坑",
                        '"choice"', '"fill"', '"short"', "localStorage", "cg-student-",
                        "state.done", "state.cards", "URLSearchParams", 'params.get("u")',
                        'params.get("cards") === "1"']) {
    if (!source.includes(needle)) bad.push(`学生版模板缺 ${needle}`);
  }

  // 答案默认隐藏：整份数据 base64，答案只以 *_b64 存在
  for (const needle of ["const DATA_B64", "atob", "TextDecoder", "function b64(",
                        '"answer_b64": _b64(answer_text)',
                        '"correct_b64": _b64(str(correct))',
                        '"explain_b64": _b64(explanation)',
                        '"options": [{"text": item} for item in options]']) {
    if (!source.includes(needle)) bad.push(`答案编码口径缺 ${needle}`);
  }
  if (source.includes('"answer": answer_text') || source.includes('"explanation": explanation')) {
    bad.push("答案/解析仍以明文字段返回（不是 *_b64）");
  }
  if (source.includes("**答案**") || source.includes("**解析**")) {
    bad.push("服务源码出现 `**答案**` / `**解析**` 字面量（初始 HTML 会泄漏）");
  }
  if (!source.includes("base64.b64encode(raw).decode(\"ascii\")")) {
    bad.push("整份数据没有 base64 内嵌（初始 HTML 会出现正文/答案原文）");
  }
  const openUnit = source.slice(source.indexOf("function openUnit("), source.indexOf("function pick("));
  if (openUnit.includes("b64(") || openUnit.includes("answer_b64") || openUnit.includes("explain_b64")) {
    bad.push("openUnit 初始渲染就解码答案（答案会默认展开）");
  }
  if (!source.includes('<div class="ans" id="ans"></div>')) {
    bad.push("自测答案容器初始不是空的");
  }

  // 数据口径：#### 归上一小节、来源嵌套配平、两句不切半
  if (!source.includes("out.setdefault(current, []).append(match4.group(1).strip())")) {
    bad.push("#### 没有归到上一小节");
  }
  if (source.includes("current = match4.group(1)")) {
    bad.push("#### 被当成新小节（核心讲解会被切碎）");
  }
  if (!source.includes("depth") || !source.includes("_RE_SOURCE_START")) {
    bad.push("来源括号没有按嵌套配平删除");
  }
  if (source.includes("t[:limit]") || source.includes("value[:limit]")) {
    bad.push("一句话仍在硬切半句");
  }
  if (!source.includes("if index >= 2") || !source.includes("return out.strip()")) {
    bad.push("一句话没有最多两句 / 不切半的边界");
  }

  // 单文件 / 手机宽度
  if (/<script\s+src=/i.test(source) || /<link\b/i.test(source)) bad.push("模板引了外部 script/link，不是单文件");
  if (/https?:\/\//i.test(source)) bad.push("模板里有 http(s) 外部引用，离线打不开");
  if (!source.includes("@media (max-width:420px)")) bad.push("没有 ≤420px 手机宽度断点");
  if (!source.includes("overflow-x:hidden")) bad.push("没有横向溢出兜底");
  return bad;
}

function auditVerify(source: string): string[] {
  if (!source.includes("check-student-mode.ts")) return ["scripts/verify.ps1 没接入 check-student-mode.ts"];
  return [];
}

const files = new Map<string, string>();
for (const rel of [REL_ROUTE, REL_SERVICE, REL_PANEL, REL_API, REL_VERIFY]) files.set(rel, read(rel));

const route = files.get(REL_ROUTE) ?? "";
const service = files.get(REL_SERVICE) ?? "";
const panel = stripComments(files.get(REL_PANEL) ?? "");
const api = files.get(REL_API) ?? "";

const staticGroups: [string, string[]][] = [
  ["后端路由", auditRoutes(route)],
  ["入口接线", auditEntry(panel)],
  ["api.ts", auditApi(api)],
  ["单文件模板", auditTemplate(service)],
  ["门禁接入", auditVerify(files.get(REL_VERIFY) ?? "")],
];
for (const [, bad] of staticGroups) failures.push(...bad);

/* 变异：六种「不该发生」的样子必须逐条判红（否则断言等于没写） */
const cases: [string, string[]][] = [
  ["删掉学生版按钮", auditEntry(panel.replace(buttonOf(courseTools(panel), BUTTON_TEXT), ""))],
  ["接口指到旧地址", auditApi(api.replace("${courseId}/student-html", "${courseId}/student-old"))],
  ["#### 又切成新小节", auditTemplate(service.replace(
    "out.setdefault(current, []).append(match4.group(1).strip())",
    "current = match4.group(1).strip()"))],
  ["答案改回明文", auditTemplate(service.replace(
    '"answer_b64": _b64(answer_text)', '"answer": answer_text'))],
  ["答案默认展开", auditTemplate(service.replace(
    '<div class="ans" id="ans"></div>',
    '<div class="ans on" id="ans">${md(b64(quiz.answer_b64))}</div>'))],
  ["去掉 localStorage 进度", auditTemplate(service.replaceAll("localStorage", "sessionStorage"))],
  ["去掉来源剥除", auditTemplate(service.replace("def strip_sources(", "def strip_sources_disabled("))],
];
for (const [label, flagged] of cases) {
  if (!flagged.length) failures.push(`变异「${label}」没有被检查器判红（断言失效）`);
}

if (failures.length) {
  console.error(`\n❌ 任务 151 学生版断言失败 ${failures.length} 条：`);
  for (const f of failures) console.error("   - " + f);
  process.exit(1);
}
console.log("✅ 任务 151 学生版断言全过（两条只读路由 + 共用入口 + 五项交互 + 答案默认隐藏 + 数据口径；"
  + `${cases.length} 条变异全部判红）`);
