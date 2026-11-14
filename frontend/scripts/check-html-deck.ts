/**
 * 任务 88 前端断言：**交付包里的单文件 HTML 课件**（**0 LLM**，纯本地渲染）。
 *
 * 钉四组：
 * ① **源码级（渲染层）**：`deck_html.py` 存在 + 视觉口径常量（画布 1122×793 = A4 横版、
 *    眉标 27 / 主句 54 / 全页最小 14、动效 1.05s / 步进 170ms）+ 目录名 `HTML` +
 *    ⛔ 0-LLM 护栏（不许出现 `complete_json` / `build_llm_client` / `openai`）+
 *    单文件口径（CSS/JS 内联、图 `data:image/svg+xml;base64,`、**不许**出现 `http(s)://`）；
 * ② **接线**：`delivery.py` 真调 `deck_html.build_files` 并把每个文件 `files.append(...)`
 *    （⇒ 进 manifest）、计数写进 `counts.html_decks`、出包页「本次将包含」那行并列写明
 *    `课件/HTML/（网页版，可投影 / 打印 / 手机看）`、交付说明与验收单都写到网页版；
 * ③ **前端**：出包页渲染的是后端给的条目（`dataSource={preview.items}`）⇒ 后端 label 到屏幕；
 * ④ **行为级（真跑 Python）**：真 import `render_html` 渲一份合成课件，用**本脚本自带的检查器**
 *    验（无外链 / viewport / @media print + A4 横版 / @keyframes / 图内联 / 页数 / 页码 /
 *    备注拆分 / `**粗体**` 渲染）；再把三种「不该发生」的样子（外链 CDN、去掉横版规则、
 *    文件不进 manifest）当变异喂给检查器 —— 必须逐条判红。
 *
 * ⛔ 0 LLM：子进程显式 `LLM_PROVIDER=mock`，一切路径指向系统临时目录（不碰真库 / 真 vault）。
 * 运行：`node scripts/check-html-deck.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1（前端断言 30 → 31 条）。
 */
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
/** Python 侧只去掉「整行 # 注释」（不要拿块注释规则扫 Python）。 */
const stripPyComments = (s: string) => s.replace(/^[ \t]*#[^\n]*$/gm, "");

const REL_RENDER = "backend/app/services/deck_html.py";
const REL_DELIVERY = "backend/app/services/delivery.py";
const REL_PANEL = "frontend/src/components/DeliveryPanel.tsx";

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

/* ---------------- ① 源码级：渲染层与常量 ---------------- */

function auditRenderSource(source: string): string[] {
  const bad: string[] = [];
  if (!source) return ["取不到 backend/app/services/deck_html.py"];
  // 视觉口径逐行钉死（画布 = A4 横版 297×210mm @96dpi；样板 v8 的字号与动效节奏）
  const constants: string[] = [
    'DIR_NAME = "HTML"',
    "CANVAS_W, CANVAS_H = 1122, 793",
    "PAD_X, PAD_TOP, PAD_BOTTOM = 112, 60, 36",
    "FS_EYEBROW, FS_HEADLINE, FS_MIN = 27, 54, 14",
    "ANIM_SECONDS = 1.05",
    "ANIM_STEP_MS = 170",
    "ANIM_BASE_MS = 180",
    "MAX_PAGE_FIGURES = 2",
  ];
  for (const line of constants) {
    if (!source.includes(line)) bad.push(`deck_html.py 少了口径常量「${line}」（被改了）`);
  }
  // 单文件口径：CSS/JS 内联 + 图 base64
  if (!/data:image\/svg\+xml;base64,/.test(source)) bad.push("图没有走 base64 内联（会带到外链）");
  if (!/@page\s*\{\s*size:\s*A4\s+landscape;\s*margin:\s*0\s*\}/.test(source)) {
    bad.push("打印规则不是 A4 横版（@page{size:A4 landscape;margin:0}）");
  }
  if (!/@keyframes rise/.test(source)) bad.push("缺 @keyframes 入场动效");
  if (!/<meta name="viewport" content="width=device-width, initial-scale=1">/.test(source)) {
    bad.push("骨架里没有逐字写死的 viewport meta");
  }
  if (!/prefers-reduced-motion/.test(source)) bad.push("没有「只提示、不跟随系统禁用动画」的处理");
  for (const needle of ["complete_json", "build_llm_client", "openai", "LLMClient"]) {
    if (source.includes(needle)) bad.push(`deck_html.py 里出现了 ${needle}（HTML 课件必须 0 LLM）`);
  }
  // 渲染层自己不许拼外链（只扫代码，不扫模块 docstring —— 那里正解释「SVG 的 xmlns 是 http」）
  const code = source.replace(/^"""[\s\S]*?"""/, "");
  for (const bad_word of ["https://", "http://", "//cdn"]) {
    if (code.includes(bad_word)) bad.push(`deck_html.py 的代码里出现了 ${bad_word}`);
  }
  return bad;
}

/* ---------------- ② 接线：交付包 ---------------- */

function auditWiring(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const delivery = stripPyComments(files.get(REL_DELIVERY) ?? "");
  // 任务 94：取图口径收成唯一一处 `figure_rows(items)`（任务 94 §B2 要求接口与包内逐字节一致）
  if (!/html_rows = deck_html\.build_files\(db, course, figures=figure_rows\(items\)\)/.test(delivery)) {
    bad.push("delivery.py 没有调 deck_html.build_files（HTML 课件没接线）");
  }
  if (!/for row in html_rows:\s*\n\s+files\.append\(_write_text\(\s*\n?\s*out_dir, f"\{DECK_DIR\}\/\{deck_html\.DIR_NAME\}\/\{row\['name'\]\}", row\["text"\]\)\)/.test(delivery)) {
    bad.push("HTML 文件没有逐个进 files 清单（客户侧核验会报「包内多出未声明文件」）");
  }
  if (!/plan\["counts"\]\["html_decks"\] = len\(html_rows\)/.test(delivery)) {
    bad.push("counts.html_decks 没写（说明与 manifest 的数字会缺一项）");
  }
  if (!/（整课 \+ 各章 pptx）\+ \{DECK_DIR\}\/\{deck_html\.DIR_NAME\}\//.test(delivery)
      || !/（网页版，可投影 \/ 打印 \/ 手机看）/.test(delivery)) {
    bad.push("出包页「本次将包含」的课件那行没有并列写明 课件/HTML/（网页版…）");
  }
  if (!/`课件\/HTML\/`/.test(delivery)) bad.push("交付说明里没有 `课件/HTML/` 那一行");
  if (!/另附网页版 HTML/.test(delivery)) bad.push("验收单的交付清单没有写明网页版 HTML");
  return bad;
}

/* ---------------- ③ 前端：条目来自后端 ---------------- */

function auditFrontend(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const panel = files.get(REL_PANEL) ?? "";
  if (!/dataSource=\{preview\.items\}/.test(panel)) {
    bad.push("出包页没有把后端给的条目（preview.items）渲染出来 ⇒ 后端写的「课件/HTML/」到不了屏幕");
  }
  return bad;
}

/* ---------------- ④ 行为级：真跑 Python + 本脚本自带检查器 ---------------- */

type RenderOut = { html: string; slides: number; images: number; notes: string };

function runPython(tmp: string): RenderOut {
  const python = existsSync(join(repo, "backend/.venv/Scripts/python.exe"))
    ? join(repo, "backend/.venv/Scripts/python.exe")
    : (process.platform === "win32" ? "python" : "python3");
  const svg = join(tmp, "fig.svg");
  writeFileSync(svg, '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">'
    + '<rect width="200" height="80" fill="#fffbf2"/></svg>', "utf8");
  const script = `
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, r"${join(repo, "backend")}")
from app.services import deck_html as d

course = SimpleNamespace(title="合成课：发酵原理")
deck = SimpleNamespace(title="第1章 发酵原理 · 课件")
blocks = [
    {"index": 1, "layout": "cover", "title": "1 发酵原理", "bullets": ["受众：初中生"],
     "spoken": "开场先讲这门课要解决什么。", "internal": "", "figures": []},
    {"index": 2, "layout": "point", "title": "老面发酵", "bullets": ["**体积**：膨胀约 2 倍", "蜂窝细密"],
     "spoken": "先看体积，再看蜂窝。",
     "internal": "课件操作提示（仅讲师 / 运营可见，勿投屏）：每章单独生成。",
     "figures": [{"caption": "老面发酵概念图", "kind": "diagram", "svg": Path(r"${svg}")}]},
    {"index": 3, "layout": "point", "title": "温度窗口", "bullets": ["25~30℃ 最稳"],
     "spoken": "温度决定菌群活性。", "internal": "",
     "figures": [{"caption": "温度对照", "kind": "chart_table",
                  "table": "| 温度 | 状态 |\\n|---|---|\\n| 25~30℃ | 正常 |"}]},
]
html = d.render_html(course, deck, blocks)
out = {"html": html, "slides": html.count('class="slide"'),
       "images": html.count("data:image/svg+xml;base64,"), "notes": ""}
print(json.dumps(out, ensure_ascii=False))
`;
  const file = join(tmp, "render-88.py");
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
  return JSON.parse(line) as RenderOut;
}

/** 单文件 HTML 的独立检查器（**不复用被测代码**；与后端 pytest 的 _audit_html 同一套判据）。 */
function auditHtml(text: string, expectSlides = 3): string[] {
  const bad: string[] = [];
  const ext = text.match(/https?:\/\/[^\s"'<>]+/g) ?? [];
  if (ext.length) bad.push(`有外部依赖（http(s):// ×${ext.length}，例：${ext[0]}）`);
  if (/<link[^>]+rel=["']stylesheet/.test(text)) bad.push("有外部样式表 <link rel=stylesheet>");
  if (/<script[^>]+src=/.test(text)) bad.push("有外部脚本 <script src>");
  if (!text.includes('<meta name="viewport" content="width=device-width, initial-scale=1">')) {
    bad.push("缺 viewport meta（逐字口径：width=device-width, initial-scale=1）");
  }
  if (!text.includes("@media print")) bad.push("缺 @media print（打印会带上进度条与按钮）");
  if (!/size:\s*A4\s+landscape/.test(text)) bad.push("缺打印横版规则 size: A4 landscape");
  if (!text.includes("@keyframes")) bad.push("缺 @keyframes 入场动效");
  if (!text.includes("animation-duration:1.05s")) bad.push("入场动效不是 1.05s（样板口径）");
  const slides = (text.match(/class="slide"/g) ?? []).length;
  if (slides !== expectSlides) bad.push(`页数不对：实得 ${slides}，应 ${expectSlides}`);
  return bad;
}

function auditBehavior(out: RenderOut): string[] {
  const bad: string[] = [];
  bad.push(...auditHtml(out.html, 3));
  if (out.images !== 1) bad.push(`内联图应 1 张（矢量源），实得 ${out.images}`);
  if (!out.html.includes("<strong>体积</strong>")) bad.push("**粗体** 没渲染成 <strong>");
  if (out.html.includes("**")) bad.push("行内标记 `**` 原样落进了 HTML");
  if (!out.html.includes("本页讲稿摘要")) bad.push("备注里没有「本页讲稿摘要」");
  if (!out.html.includes("讲师提示（勿投屏）")) bad.push("内部提示没有单独标注（会被投影出去）");
  if (!out.html.includes("1 / 3") || !out.html.includes("3 / 3")) bad.push("页脚缺页码");
  if (!out.html.includes("合成课：发酵原理")) bad.push("页脚缺课程名");
  if (!/<table>/.test(out.html) || !out.html.includes("温度对照")) {
    bad.push("数据卡（无矢量源）没有以表格随页呈现");
  }
  if (!out.html.includes('href="#p3"')) bad.push("目录浮层缺跳转链（#pN）");
  if (!out.html.includes("<title>第1章 发酵原理 · 课件 · HTML 课件</title>")) {
    bad.push("标题里没有 deck 标题");
  }
  return bad;
}

/* ---------------- 走查 ---------------- */

const files = new Map<string, string>();
for (const rel of [REL_RENDER, REL_DELIVERY, REL_PANEL]) files.set(rel, read(rel));

const renderSource = stripPyComments(files.get(REL_RENDER) ?? "");
const deliverySource = stripPyComments(files.get(REL_DELIVERY) ?? "");
failures.push(...auditRenderSource(renderSource));
failures.push(...auditWiring(files));
failures.push(...auditFrontend(files));

const tmp = mkdtempSync(join(tmpdir(), "cg_html88_"));
try {
  const out = runPython(tmp);
  failures.push(...auditBehavior(out));

  // ④ 变异：三种「不该发生」的样子必须被检查器判红（否则断言等于没写）
  const cases: [string, string][] = [
    ["外部 CDN 样式", out.html.replace("</head>", '<link rel="stylesheet" href="https://cdn.example.com/x.css"></head>')],
    ["外部脚本", out.html.replace("</head>", '<script src="app.js"></script></head>')],
    ["去掉打印横版", out.html.replace("size:A4 landscape", "size:A4")],
    ["去掉 @keyframes", out.html.replace(/@keyframes/g, "@kf")],
    ["去掉 viewport", out.html.replace(/<meta name="viewport"[^>]*>/, "")],
    ["少一页幻灯片", out.html.replace('class="slide"', 'class="slide-x"')],
  ];
  for (const [label, mutated] of cases) {
    const flagged = auditHtml(mutated, 3);
    if (!flagged.length) failures.push(`变异「${label}」没有被检查器判红（断言失效）`);
  }
  // ③ 接线变异：HTML 不进 files ⇒ 必须判红
  const noAppend = deliverySource.replace(
    /for row in html_rows:\s*\n\s+files\.append\(_write_text\(\s*\n?\s*out_dir, f"\{DECK_DIR\}\/\{deck_html\.DIR_NAME\}\/\{row\['name'\]\}", row\["text"\]\)\)/,
    "");
  const wiringMutated = auditWiring(new Map([[REL_DELIVERY, noAppend], [REL_RENDER, renderSource]]));
  if (!wiringMutated.some((m) => m.includes("files 清单"))) {
    failures.push("变异「HTML 不进 files 清单」没有被接线断言判红（断言失效）");
  }
  console.log(`HTML 课件（任务 88）：页数 ${out.slides} / 内联图 ${out.images} / `
    + `HTML ${(Buffer.byteLength(out.html, "utf8") / 1024).toFixed(1)} KB / 变异 ${cases.length + 1} 条全部判红`);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

if (failures.length) {
  console.error(`\n❌ 任务 88 HTML 课件断言失败 ${failures.length} 条：`);
  for (const f of failures) console.error("   - " + f);
  process.exit(1);
}
console.log("✅ 任务 88 HTML 课件断言全过（0 LLM：渲染层无模型调用、图全部内联）");