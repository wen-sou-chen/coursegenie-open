/**
 * 任务 101 前端断言：风格库「角色组」不再写死领域角色 + 可按 `course.meta["roles"]` 配置。
 *
 * 起因（DSH 2026-09-26 实测）：`style_library.py:119 _role_items()` 硬编码了
 * `师傅 👨‍🍳 / 学员 🧑‍🎓 / 酵母菌 🦠`，而 `library_text()` 不接课程 ⇒ **两门课导出的是同一套风格库**
 * ⇒ 客户打开「AI 幻觉治理实战」的图表库会看到一张「角色组 · 酵母菌」和顶着 👨‍🍳 的「师傅」。
 *
 * 这里做三类断言：
 * ① 源码级：`DEFAULT_ROLES` = 中性三件套（讲解者/学员/主题）、源码里不再有 👨‍🍳 / 🦠 / 师傅 / 酵母菌、
 *    `_roles_of` 真的做「1–6 条 + label/emoji 非空」校验；
 * ② 接线：`build_library` / `library_text` / `kb.export_course` / 预览接口四条链都把课程传下去
 *    （且没有留下无参调用 —— 那等于配置口子没接上）；
 * ③ 行为级：真 import `app.services.style_library` 跑一遍 —— 缺省中性、配了照它出、
 *    其它三组（14 条）逐字节不变、非法配置回落且不抛。
 * 另有 6 条**变异**自检：把上面每条改回「不该发生」的样子，断言必须判红。
 *
 * 运行：`node scripts/check-style-library-roles.ts`（Node >= 22.18 自带类型擦除）
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

const readRepoFile = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

const LIB_REL = "backend/app/services/style_library.py";
const KB_REL = "backend/app/services/kb.py";
const ROUTE_REL = "backend/app/api/routes/kb.py";

/** 那三张「领域专属角色」卡（任务 101 之前的硬编码：师傅 👨‍🍳 / 学员 🧑‍🎓 / 酵母菌 🦠）。 */
const CHEF = "\u{1F468}\u200D\u{1F373}";
const YEAST = "\u{1F9A0}";
const NEUTRAL = ["讲解者", "学员", "主题"];
const CONFIGURED = ["师傅", "学员", "酵母菌"];

/** 取 `DEFAULT_ROLES: tuple[...] = ( ... )` 里的中文标签（emoji 行带反斜杠，天然被跳过）。 */
function defaultRoleLabels(src: string): string[] {
  const body = src.match(/DEFAULT_ROLES[^=]*=\s*\(([\s\S]*?)\n\)/)?.[1] ?? "";
  const labels: string[] = [];
  for (const line of body.split("\n")) {
    const m = (line.split("#")[0]).match(/"([^"\\]*)"\s*,/);
    if (m) labels.push(m[1]);
  }
  return labels;
}

/** 源码级审计（抽成函数：下面用「变异后的源码」做判定自检）。 */
function audit(libSrc: string, kbSrc: string, routeSrc: string): string[] {
  const bad: string[] = [];

  // ① 不再有领域专属角色的字面量（原文 / 转义两种写法都查）
  const legacy: [string, string][] = [
    [CHEF, "👨‍🍳"],
    ["\\U0001F468", "👨（👨‍🍳 的码位写法）"],
    ["\\U0001F373", "🍳（👨‍🍳 的码位写法）"],
    [YEAST, "🦠（酵母菌）"],
    ["师傅", "「师傅」"],
    ["酵母菌", "「酵母菌」"],
  ];
  for (const [needle, label] of legacy) {
    if (libSrc.includes(needle)) bad.push(`style_library.py 里又出现了 ${label}（角色组不该写死领域角色）`);
  }

  // ② 缺省三件套 = 中性
  const labels = defaultRoleLabels(libSrc);
  if (labels.join("/") !== NEUTRAL.join("/")) {
    bad.push(`DEFAULT_ROLES 不是中性三件套（实得 ${labels.join("/") || "解析不到"}）`);
  }

  // ③ `_roles_of` 真的校验「1–6 条 + label/emoji 非空」
  const rolesBody = libSrc.match(/def _roles_of\([\s\S]*?\n(?=def )/)?.[0] ?? "";
  if (!rolesBody) bad.push("style_library.py 里没有 _roles_of（没有唯一一处读 course.meta['roles'] 的地方）");
  else {
    if (!/get\("roles"\)/.test(rolesBody)) bad.push("_roles_of 没读 course.meta[\"roles\"]");
    if (!/if len\(raw\) > ROLE_LIMIT:/.test(rolesBody)) bad.push("_roles_of 没挡住「超过 6 条」");
    if (!/\.strip\(\)/.test(rolesBody) || !/if not label or not emoji:/.test(rolesBody)) {
      bad.push("_roles_of 没挡住「label/emoji 为空或全是空格」");
    }
    const fallbacks = rolesBody.match(/return list\(DEFAULT_ROLES\)/g)?.length ?? 0;
    if (fallbacks < 3 || !/return roles, ""/.test(rolesBody)) {
      bad.push(`_roles_of 非法时不回落 DEFAULT_ROLES（回落分支 ${fallbacks} 处；会抛异常或导出空角色组）`);
    }
  }

  // ④ 角色组必须来自 `_roles_of(course)`，不是写死的常量
  const buildBody = libSrc.match(/def build_library\([\s\S]*?\n(?=def )/)?.[0] ?? "";
  if (!/def build_library\([^)]*course: Course \| None = None/.test(libSrc)) {
    bad.push("build_library 不接 course（配置口子接不上）");
  }
  if (!/_roles_of\(course\)/.test(buildBody)) bad.push("build_library 没把 course 交给 _roles_of");
  if (!/role_items\(colors, roles\)/.test(buildBody)) bad.push("build_library 没把 roles 传给 _role_items");
  if (!/def library_text\([^)]*course: Course \| None = None/.test(libSrc)) {
    bad.push("library_text 不接 course（导出那条链读不到课程配置）");
  }

  // ⑤ 接线：出包 / 写盘 / 预览三处都要把课程传下去
  if (!/library_text\(course=course\)/.test(kbSrc)) {
    bad.push("kb.export_course 没把 course 传给 library_text（出包那份还是默认角色组）");
  }
  if (!/build_library\(course=course\)/.test(libSrc)) {
    bad.push("write_style_library 没把 course 传给 build_library（写盘那份还是默认角色组）");
  }
  if (!/build_library\(course=course\)/.test(routeSrc)) {
    bad.push("预览接口没把 course 传给 build_library（网页预览与导出会不一致）");
  }

  // ⑥ 不许留下无参调用（那等于配置口子形同虚设）
  for (const [src, rel] of [[libSrc, LIB_REL], [kbSrc, KB_REL], [routeSrc, ROUTE_REL]] as const) {
    for (const fn of ["build_library", "library_text"]) {
      if (new RegExp(`${fn}\\(\\s*\\)`).test(src)) bad.push(`${rel} 里还剩无参的 ${fn}() 调用`);
    }
  }
  return bad;
}

/* ---------------- 行为级：真 import 跑一遍 ---------------- */

type RunOut = {
  items: number; default_roles: string[]; configured_roles: string[]; invalid_roles: string[][];
  default_has_chef: boolean; default_has_yeast: boolean; configured_has_chef: boolean;
  tail_same: boolean; groups: string[];
};

function runPython(tmp: string): RunOut {
  const python = existsSync(join(repo, "backend/.venv/Scripts/python.exe"))
    ? join(repo, "backend/.venv/Scripts/python.exe")
    : (process.platform === "win32" ? "python" : "python3");
  const script = `
import json, sys
from pathlib import Path
from types import SimpleNamespace

repo = Path(r"${repo}")
sys.path.insert(0, str(repo / "backend"))
from app.services import style_library

CHEF = "\\U0001F468\\u200d\\U0001F373"
YEAST = "\\U0001F9A0"
CONF = [{"label": "师傅", "emoji": CHEF},
        {"label": "学员", "emoji": "\\U0001F9D1\\u200d\\U0001F393"},
        {"label": "酵母菌", "emoji": YEAST}]


def probe(meta):
    lib = style_library.build_library(course=SimpleNamespace(meta=meta, id="probe"))
    items = lib["libraryItems"]
    return ([i["name"] for i in items], json.dumps(lib, ensure_ascii=False),
            json.dumps(items[3:], ensure_ascii=False, indent=2))


dnames, dtext, dtail = probe({})
cnames, ctext, ctail = probe({"roles": CONF})
invalid = []
for meta in [{"roles": []},
             {"roles": [{"label": f"x{i}", "emoji": "\\U0001F3AF"} for i in range(7)]},
             {"roles": [{"emoji": "\\U0001F3AF"}]},
             {"roles": [{"label": "师傅"}]},
             {"roles": [{"label": "  ", "emoji": "  "}]}]:
    try:
        invalid.append(probe(meta)[0][:3])
    except Exception as exc:                       # noqa: BLE001
        invalid.append(["RAISED " + type(exc).__name__])
print(json.dumps({
    "items": len(dnames), "default_roles": dnames[:3], "configured_roles": cnames[:3],
    "invalid_roles": invalid, "default_has_chef": CHEF in dtext,
    "default_has_yeast": YEAST in dtext, "configured_has_chef": CHEF in ctext,
    "tail_same": dtail == ctail,
    "groups": sorted({n.split(" ", 1)[0] for n in dnames if " " in n}),
}, ensure_ascii=False))
`;
  const file = join(tmp, "check101.py");
  writeFileSync(file, script, "utf8");
  const result = spawnSync(python, [file], {
    cwd: repo, encoding: "utf8",
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
  });
  if (result.status !== 0) {
    throw new Error(`行为级跑不通（exit=${result.status}）：${(result.stderr ?? "").slice(-600)}`);
  }
  return JSON.parse((result.stdout ?? "").trim().split("\n").pop() ?? "") as RunOut;
}

/* ---------------- 主流程 ---------------- */

const libSrc = readRepoFile(LIB_REL);
const kbSrc = readRepoFile(KB_REL);
const routeSrc = readRepoFile(ROUTE_REL);

failures.push(...audit(libSrc, kbSrc, routeSrc));

const tmp = mkdtempSync(join(tmpdir(), "cg_style101_"));
let out: RunOut;
try {
  out = runPython(tmp);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

if (out.items !== 17) failures.push(`风格库条目数应为 17，实得 ${out.items}`);
if (out.groups.join("/") !== ["卡片组", "色卡组", "角色组", "连线组"].join("/")) {
  failures.push(`四组变了：${out.groups.join("/")}`);
}
if (out.default_roles.join("/") !== NEUTRAL.map((l) => `角色组 · ${l}`).join("/")) {
  failures.push(`缺省角色组不是中性三件套：${out.default_roles.join("/")}`);
}
if (out.default_has_chef || out.default_has_yeast) {
  failures.push("缺省导出的风格库里仍有 👨‍🍳 / 🦠（领域专属角色没清掉）");
}
if (out.configured_roles.join("/") !== CONFIGURED.map((l) => `角色组 · ${l}`).join("/")) {
  failures.push(`配了 course.meta.roles 之后角色组没照它出：${out.configured_roles.join("/")}`);
}
if (!out.configured_has_chef) failures.push("配了 roles 却出不来 👨‍🍳（配置口子读不到 course.meta）");
if (!out.tail_same) failures.push("配了 roles 之后「卡片组/连线组/色卡组」14 条被改了（不许动其它三组）");
if (out.invalid_roles.length !== 5) failures.push(`非法配置用例数不对：${out.invalid_roles.length}`);
for (const roles of out.invalid_roles) {
  if (roles.join("/") !== `角色组 · ${NEUTRAL[0]}/角色组 · ${NEUTRAL[1]}/角色组 · ${NEUTRAL[2]}`) {
    failures.push(`非法配置没回落中性默认：${roles.join("/")}`);
  }
}

// 变异自检：六种「不该发生」的样子必须被判红（否则断言等于没写）
const MUTATIONS: [string, string, string, string][] = [
  ["DEFAULT_ROLES 改回 师傅/学员/酵母菌",
   libSrc.replace(/DEFAULT_ROLES[^=]*=\s*\([\s\S]*?\n\)/,
     'DEFAULT_ROLES: tuple[tuple[str, str], ...] = (\n'
     + '    ("师傅", "\\U0001F468\\u200D\\U0001F373"),\n'
     + '    ("学员", "\\U0001F9D1\\u200D\\U0001F393"),\n'
     + '    ("酵母菌", "\\U0001F9A0"),\n)'),
   kbSrc, routeSrc],
  ["build_library 不再读课程（永远用默认）",
   libSrc.replace("    roles, role_problem = _roles_of(course)", "    roles, role_problem = _roles_of(None)"),
   kbSrc, routeSrc],
  ["roles 没传给 _role_items（读到了配置也不用）",
   libSrc.replace("_role_items(colors, roles)", "_role_items(colors)"),
   kbSrc, routeSrc],
  ["_roles_of 去掉「最多 6 条」校验",
   libSrc.replace("    if len(raw) > ROLE_LIMIT:\n"
     + '        return list(DEFAULT_ROLES), f"最多 {ROLE_LIMIT} 条，实得 {len(raw)} 条"\n', ""),
   kbSrc, routeSrc],
  ["出包那条链不传课程（library_text()）",
   libSrc, kbSrc.replace("library_text(course=course)", "library_text()"), routeSrc],
  ["预览接口不传课程（build_library()）",
   libSrc, kbSrc, routeSrc.replace("build_library(course=course)", "build_library()")],
];
for (const [label, l, k, r] of MUTATIONS) {
  if (l === libSrc && k === kbSrc && r === routeSrc) {
    failures.push(`变异「${label}」没改到源码（断言可能已失效）`);
    continue;
  }
  if (audit(l, k, r).length === 0) failures.push(`变异「${label}」没有被判红（断言失效）`);
}

if (failures.length > 0) {
  console.error("❌ 任务 101「风格库角色组中性化 + 可按课程配置」断言失败：");
  for (const f of failures) console.error(`   - ${f}`);
  process.exit(1);
}
console.log("✅ 任务 101 风格库角色组断言全过：缺省 = 讲解者/学员/主题（源码里已无 👨‍🍳/🦠/师傅/酵母菌），"
  + `按 course.meta.roles 配置后角色组照它出且其它 14 条逐字节不变（tail_same=${out.tail_same}）、`
  + `非法配置 ${out.invalid_roles.length} 组全回落中性默认且不抛；`
  + `四组 = ${out.groups.join(" / ")}、条目 ${out.items} 条；变异 ${MUTATIONS.length} 条全判红`);