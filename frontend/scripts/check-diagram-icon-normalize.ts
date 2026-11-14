/**
 * 任务 92 前端断言：**存量**概念图图标归一 + 重渲染（0 LLM）。
 *
 * 起因（DSH 2026-09-24 只读实测）：交付包 `图表/svg/*.svg` 里 122/139 张图的左上角标题条
 * 顶着 👨‍🍳（节点图标里还混着 📊 / 🔄 / 🟠…）。根因是 `excalidraw_layout.DEFAULT_HEAD` 曾经
 * 就是 👨‍🍳，任务 77 只收敛了「生成时」的口径 —— 存量场景把这枚 emoji 写死在 elements 里。
 *
 * 这里做五类断言：① 渲染器缺省值必须是白名单内那枚（改回 👨‍🍳 ⇒ 红）；
 * ② 迁移脚本既要处理标题条那一枚、又要复用 `diagram.py` 那唯一一套白名单判定
 *（自己另写一份清单 ⇒ 红）；③ 默认 dry-run（不加 `--apply` 就写库 ⇒ 红）；
 * ④ 真跑 Python：临时库里造一张存量图 ⇒ dry-run 不动库、--apply 改库并重渲染 SVG、
 *    再跑一次 0 改动（幂等）；
 * ⑤（任务 100）归一必须同时覆盖 Excalidraw 的**编辑镜像** `rawText` / `originalText`
 *（只改 `text` ⇒ 客户拿 `.excalidraw` 原件回编辑器改标题时 👨‍🍳 会「回来」）。
 *    后端逐条断言见 `backend/tests/test_diagram_icon_normalize.py`。
 *
 * 运行：`node scripts/check-diagram-icon-normalize.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const failures: string[] = [];

/** 读源码并把 CRLF 归一成 LF（本机工作区是 CRLF，多行匹配不归一就全失效）。 */
const readPy = (rel: string) =>
  readFileSync(join(repo, "backend", "app", "services", rel), "utf8").replace(/\r+\n/g, "\n");
const readRepoFile = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

/** 把 Python 源码里的 `"\U0001F4D8"` / `"\u2705"` 字面量还原成字符。 */
function decode(lit: string): string {
  return lit.replace(/\\U([0-9A-Fa-f]{8})|\\u([0-9A-Fa-f]{4})/g,
    (_, big: string, small: string) => String.fromCodePoint(parseInt(big ?? small, 16)));
}

/** 取 `NAME = [...]` 的列表主体（拿不到就抛，避免静默跳过）。 */
function listBody(src: string, name: string): string {
  const m = src.match(new RegExp(`${name}\\s*=\\s*\\[([\\s\\S]*?)\\]`));
  if (!m) throw new Error(`源码里找不到 ${name} 列表`);
  return m[1];
}

/** 列表里的字符串字面量（忽略行尾注释）。 */
function literals(body: string): string[] {
  const out: string[] = [];
  for (const line of body.split("\n")) {
    for (const m of (line.split("#")[0]).matchAll(/"((?:\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}|[^"\\])*)"/g)) {
      out.push(decode(m[1]));
    }
  }
  return out;
}

const SCRIPT_REL = "scripts/normalize_diagram_icons.py";
/** 渲染器**旧**缺省值（任务 92 之前的 👨‍🍳）—— 存量场景里写死的就是它。 */
const LEGACY_HEAD = "\u{1F468}\u200D\u{1F373}";

/** 源码级审计（抽成函数：下面用「变异后的源码」做判定自检）。 */
function audit(layoutSrc: string, diagramSrc: string, pngSrc: string, scriptSrc: string): string[] {
  const bad: string[] = [];
  const icons = literals(listBody(diagramSrc, "DIAGRAM_EMOJI_WHITELIST"));
  const head = decode(diagramSrc.match(/_DIAGRAM_HEAD_EMOJI\s*=\s*"([^"]*)"/)?.[1] ?? "");
  const defaultHead = decode(layoutSrc.match(/DEFAULT_HEAD\s*=\s*"([^"]*)"/)?.[1] ?? "");

  // ① 渲染器缺省值：白名单内、且不是那枚厨师
  if (!defaultHead) bad.push("excalidraw_layout.py 里找不到 DEFAULT_HEAD");
  if (defaultHead && !icons.includes(defaultHead)) {
    bad.push(`DEFAULT_HEAD 不在白名单里（缺 emoji 字段的场景会渲染出白名单外图标）：${defaultHead}`);
  }
  if (defaultHead === LEGACY_HEAD || defaultHead.includes("\u{1F373}")) {
    bad.push("DEFAULT_HEAD 又变回了 👨‍🍳（存量场景会长回厨师）");
  }
  if (defaultHead !== head || !head) bad.push("DEFAULT_HEAD 与 diagram._DIAGRAM_HEAD_EMOJI 不一致");

  // ② 归一函数：标题条要单独处理、节点要复用唯一一套白名单判定
  const normalizeBody = diagramSrc.match(/def normalize_scene_icons\([\s\S]*?\n(?=def )/)?.[0] ?? "";
  if (!normalizeBody) {
    bad.push("diagram.py 里没有 normalize_scene_icons（存量图标没有归一入口）");
  } else {
    if (!/if index == head:/.test(normalizeBody) || !/_DIAGRAM_HEAD_EMOJI/.test(normalizeBody)) {
      bad.push("normalize_scene_icons 不处理标题条那一枚（只改节点 ⇒ 标题条永远还是 👨‍🍳）");
    }
    if (!/_emoji_of\(/.test(normalizeBody)) {
      bad.push("normalize_scene_icons 没走 _emoji_of（等于另写一套白名单判定）");
    }
    if (!/scene_icon_text\(/.test(normalizeBody)) {
      bad.push("normalize_scene_icons 没按 scene_icon_text 认图标（圈号这类正文会被误改）");
    }
    // ⑤（任务 100）镜像字段：同一个字符串在 Excalidraw 里有 text + rawText/originalText 三份
    if (!/_ICON_MIRROR_FIELDS/.test(normalizeBody) || !/el\[field\] = new/.test(normalizeBody)) {
      bad.push("normalize_scene_icons 没把编辑镜像写回（只改 text ⇒ 客户在 Excalidraw 里改标题时 👨‍🍳 会回来）");
    }
  }
  if (!/_ICON_MIRROR_FIELDS\s*=\s*\(\s*"rawText"\s*,\s*"originalText"\s*\)/.test(diagramSrc)) {
    bad.push("diagram.py 里没有 _ICON_MIRROR_FIELDS = (\"rawText\", \"originalText\")（镜像字段没有唯一出处）");
  }
  if (!/def normalize_item_icons\(/.test(diagramSrc)) bad.push("diagram.py 里没有 normalize_item_icons");
  if (!/def rerender_svg_preview\(/.test(diagramSrc)) bad.push("diagram.py 里没有 rerender_svg_preview");
  if (!/def is_icon_text\(/.test(pngSrc)) bad.push("figure_png.py 里没有 is_icon_text（图标判定没有唯一出处）");

  // ③ 迁移脚本：默认 dry-run、--apply 才写、复用上面的函数、不另写清单
  if (!scriptSrc) bad.push(`${SCRIPT_REL} 不存在（存量图标没有迁移入口）`);
  else {
    if (!/--apply/.test(scriptSrc) || !/action="store_true"/.test(scriptSrc)) {
      bad.push("迁移脚本没有 --apply 开关");
    }
    if (!/if args\.apply:\s*\n\s+db\.commit\(\)/.test(scriptSrc)) {
      bad.push("迁移脚本的写库没有挂在 --apply 上（dry-run 会写库）");
    }
    if (!/--course/.test(scriptSrc)) bad.push("迁移脚本不支持 --course");
    for (const need of ["normalize_item_icons", "normalize_scene_icons", "DIAGRAM_EMOJI_WHITELIST"]) {
      if (!scriptSrc.includes(need)) bad.push(`迁移脚本没有复用 diagram.py 的 ${need}`);
    }
    const own = scriptSrc.split("\n").filter((line) =>
      /^(DIAGRAM_EMOJI_WHITELIST|_EMOJI_WHEEL|WHITELIST)\s*=\s*\[/.test(line.trim()));
    if (own.length > 0) bad.push("迁移脚本自己另写了一份图标清单（判定必须只有一处出处）");
  }
  return bad;
}

/* ---------------- ④ 行为级：真跑迁移脚本（临时五件套） ---------------- */

type RunOut = {
  dry_changed: number; dry_db_same: boolean; dry_svg_same: boolean;
  apply_changed: number; apply_svg_clean: boolean; apply_svg_rewritten: boolean;
  second_changed: number; final_outside: number;
  mirror_outside: number; mirror_unaligned: number; excalidraw_has_legacy: boolean;
};

function runPython(tmp: string): RunOut {
  const python = existsSync(join(repo, "backend/.venv/Scripts/python.exe"))
    ? join(repo, "backend/.venv/Scripts/python.exe")
    : (process.platform === "win32" ? "python" : "python3");
  const script = `
import hashlib, json, os, subprocess, sys
from pathlib import Path

repo = Path(r"${repo}")
backend = repo / "backend"
sys.path.insert(0, str(backend))
from app.db.base import Base
from app.db.session import engine, SessionLocal
from app.models.course import ContentItem, Course
from app.services import figure_png
from app.services.diagram import (
    DIAGRAM_EMOJI_WHITELIST, _build_scene, _write_svg, build_excalidraw_json,
    diagram_to_svg, excalidraw_text_of, render, scene_icon_text,
)
from app.services.excalidraw_layout import DEFAULT_HEAD

LEGACY = "\\U0001F468\\u200d\\U0001F373"
CIRCLED = "\\u2460"
WHITELIST = {i.replace("\\ufe0f", "") for i in DIAGRAM_EMOJI_WHITELIST}
SCRIPT = repo / "scripts" / "normalize_diagram_icons.py"
ENV = {**os.environ}

Base.metadata.create_all(bind=engine)
db = SessionLocal()
course = Course(title="图标归一行为检查", subject="检查", audience="中学生", duration_minutes=45)
db.add(course)
db.commit()
data = {"layout": "radial", "caption": "存量场景", "center": "中心一句话",
        "nodes": [{"title": "要点一", "subs": []}, {"title": "要点二", "subs": []}], "edges": []}
scene = _build_scene("radial", data, "存量场景")
scene["emoji"] = LEGACY
scene["center"]["emoji"] = "\\U0001F4CA"
for card, hostile in zip(scene["cards"], ("\\U0001F504", "\\U0001F9E0")):
    card["emoji"] = hostile
elements = render("radial", scene)
elements.append({"id": "circled", "type": "text", "x": 40, "y": 900, "width": 40, "height": 40,
                 "text": CIRCLED, "fontSize": 30, "textAlign": "center", "strokeColor": "#374151"})
svg_rel = _write_svg(diagram_to_svg(elements))
item = ContentItem(course_id=course.id, kind="diagram", title="存量概念图",
                   body={"layout": "radial", "caption": "存量场景",
                         "excalidraw_json": build_excalidraw_json(elements), "svg_preview": svg_rel},
                   meta={"node_key": "1.1"})
db.add(item)
db.commit()
svg_path = Path(os.environ["STORAGE_DIR"]) / svg_rel

def db_hash():
    # 每次开新会话读：子进程写的改动不会被本次会话的 identity map 缓存挡住
    fresh = SessionLocal()
    try:
        rows = [(r.id, r.body) for r in fresh.query(ContentItem).order_by(ContentItem.id).all()]
        return hashlib.sha1(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    finally:
        fresh.close()

def svg_digest():
    return hashlib.sha1(svg_path.read_bytes()).hexdigest()

def run(*extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--course", course.id, *extra],
                          cwd=str(backend), env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")

def changed_of(text):
    for line in text.splitlines():
        if "张：改动" in line:
            return int(line.split("改动 ")[1].split(" 张")[0])
    return -1

before_db, before_svg = db_hash(), svg_digest()
dry = run()
dry_db, dry_svg = db_hash(), svg_digest()
apply = run("--apply")
applied_svg = svg_path.read_text(encoding="utf-8")
after_db = db_hash()
second = run("--apply")
db.refresh(item)
elements_now = (item.body or {}).get("excalidraw_json", {}).get("elements") or []
outside = [i for i in (scene_icon_text(e) for e in elements_now)
           if i and i.replace("\\ufe0f", "") not in WHITELIST]
circled = [e for e in elements_now if e.get("type") == "text" and e.get("text") == CIRCLED]
def aligned(el):
    return el.get("text") == el.get("rawText") == el.get("originalText")

icons_now = [e for e in elements_now if scene_icon_text(e) is not None]
mirror_outside = [f for e in icons_now for f in ("text", "rawText", "originalText")
                  if str(e.get(f) or "").replace("\\ufe0f", "") not in WHITELIST]
mirror_unaligned = [e.get("text") for e in icons_now if not aligned(e)]
exported = excalidraw_text_of(item)
print(json.dumps({
    "dry_changed": changed_of(dry.stdout), "dry_db_same": dry_db == before_db,
    "dry_svg_same": dry_svg == before_svg, "dry_exit": dry.returncode,
    "apply_changed": changed_of(apply.stdout), "apply_exit": apply.returncode,
    "apply_svg_rewritten": svg_digest() != before_svg, "apply_svg_clean": LEGACY not in applied_svg,
    "db_changed": after_db != before_db, "second_changed": changed_of(second.stdout),
    "second_exit": second.returncode, "final_outside": len(outside), "circled_kept": len(circled),
    "mirror_outside": len(mirror_outside), "mirror_unaligned": len(mirror_unaligned),
    "excalidraw_has_legacy": LEGACY in exported,
    "head_now": DEFAULT_HEAD,
}, ensure_ascii=False))
db.close()
`;
  const file = join(tmp, "check92.py");
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
      PYTHONUTF8: "1",
    },
  });
  if (result.status !== 0) {
    throw new Error(`行为级跑不通（exit=${result.status}）：${(result.stderr ?? "").slice(-600)}`);
  }
  return JSON.parse((result.stdout ?? "").trim().split("\n").pop() ?? "") as RunOut;
}

/* ---------------- 主流程 ---------------- */

const layoutSrc = readPy("excalidraw_layout.py");
const diagramSrc = readPy("diagram.py");
const pngSrc = readPy("figure_png.py");
const scriptSrc = existsSync(join(repo, SCRIPT_REL)) ? readRepoFile(SCRIPT_REL) : "";

failures.push(...audit(layoutSrc, diagramSrc, pngSrc, scriptSrc));

const tmp = mkdtempSync(join(tmpdir(), "cg_icon92_"));
let out: RunOut;
try {
  out = runPython(tmp);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
if (out.dry_exit !== 0 || out.dry_changed !== 1) {
  failures.push(`dry-run 没报「改动 1 张」（changed=${out.dry_changed}, exit=${out.dry_exit}）`);
}
if (!out.dry_db_same || !out.dry_svg_same) failures.push("dry-run 动了库或 SVG 缓存（默认必须只报告）");
if (out.apply_exit !== 0 || out.apply_changed !== 1 || !out.db_changed) {
  failures.push(`--apply 没改动库（changed=${out.apply_changed}, exit=${out.apply_exit}）`);
}
if (!out.apply_svg_rewritten) failures.push("--apply 没有重渲染 SVG 缓存");
if (!out.apply_svg_clean) failures.push("重渲染后的 SVG 里还有 👨‍🍳");
if (out.second_exit !== 0 || out.second_changed !== 0) {
  failures.push(`第二次 --apply 不是 0 改动（幂等失败：${out.second_changed}）`);
}
if (out.final_outside !== 0) failures.push(`归一后仍有白名单外图标 ${out.final_outside} 个`);
if (out.circled_kept !== 1) failures.push(`圈号 ① 被当成图标改了（应当保留 ${out.circled_kept} 枚）`);
if (out.excalidraw_has_legacy || out.mirror_outside !== 0 || out.mirror_unaligned !== 0) {
  failures.push(`导出的 .excalidraw（发给客户的可编辑原件）里还有 👨‍🍳：`
    + `命中=${out.excalidraw_has_legacy}、镜像字段白名单外 ${out.mirror_outside} 处、`
    + `三份文本没对齐 ${out.mirror_unaligned} 处（编辑镜像 rawText/originalText 没归一）`);
}

// 变异自检：五种「不该发生」的样子必须被判红（否则断言等于没写）
const MUTATIONS: [string, string, string, string][] = [
  ["DEFAULT_HEAD 改回 👨‍🍳",
   layoutSrc.replace(/DEFAULT_HEAD = "[^"]*"/, 'DEFAULT_HEAD = "\\U0001F468\\u200d\\U0001F373"'),
   diagramSrc, scriptSrc],
  ["迁移不处理标题条（只改节点）",
   layoutSrc,
   diagramSrc.replace(/if index == head:\n            label, new = "标题条", _DIAGRAM_HEAD_EMOJI\n/,
     'if False:\n            label, new = "标题条", _DIAGRAM_HEAD_EMOJI\n'),
   scriptSrc],
  ["dry-run 直接写库（写库不挂在 --apply 上）",
   layoutSrc, diagramSrc,
   scriptSrc.replace(/if args\.apply:\n            db\.commit\(\)/, "db.commit()")],
  ["迁移脚本另写一份图标清单",
   layoutSrc, diagramSrc,
   `DIAGRAM_EMOJI_WHITELIST = ["\\U0001F4D8"]\n` + scriptSrc],
  ["归一没写回编辑镜像（只改 text，rawText/originalText 留旧值）",
   layoutSrc,
   diagramSrc.replace('        for field in _ICON_MIRROR_FIELDS:\n'
     + '            was = el.get(field)\n'
     + '            if isinstance(was, str) and was != new:\n'
     + '                el[field] = new\n'
     + '                notes.append(f"{label}.{field} {was} → {new}")\n', ""),
   scriptSrc],
];
for (const [label, l, d, s] of MUTATIONS) {
  if (l === layoutSrc && d === diagramSrc && s === scriptSrc) {
    failures.push(`变异「${label}」没改到源码（断言可能已失效）`);
    continue;
  }
  if (audit(l, d, pngSrc, s).length === 0) {
    failures.push(`变异「${label}」没有被判红（断言失效）`);
  }
}

if (failures.length > 0) {
  console.error("❌ 任务 92「存量概念图图标归一」断言失败：");
  for (const f of failures) console.error(`   - ${f}`);
  process.exit(1);
}
const iconCount = literals(listBody(diagramSrc, "DIAGRAM_EMOJI_WHITELIST")).length;
console.log(`✅ 任务 92 存量概念图图标归一断言全过：DEFAULT_HEAD 已从 👨‍🍳 换成白名单内图标、`
  + `迁移复用同一套判定（白名单 ${iconCount} 个）、默认 dry-run 不动库、--apply 改库并重渲染 SVG、`
  + `第二次 0 改动（幂等）、圈号 ① 保留、编辑镜像 rawText/originalText 同归一（任务 100）、`
  + `变异 ${MUTATIONS.length} 条全判红；`
  + `行为级实测：dry-run 改动 ${out.dry_changed} 张且库/SVG 不变、--apply 改动 ${out.apply_changed} 张、`
  + `归一后白名单外图标 ${out.final_outside} 个、导出的 .excalidraw 里 👨‍🍳 ${out.excalidraw_has_legacy ? "仍在" : "= 0 份"}、`
  + `镜像字段白名单外 ${out.mirror_outside} 处 / 三份文本没对齐 ${out.mirror_unaligned} 处`);
