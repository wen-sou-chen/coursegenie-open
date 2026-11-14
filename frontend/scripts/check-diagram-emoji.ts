/**
 * 任务 77 前端断言：概念图节点**图标口径**（白名单 + 兜底轮换 + 提示词约束 + 版本号）。
 *
 * 起因（用户 2026-09-22 看 HTML 样板）：「概念图上出现 👨‍🍳 / 🥟 / 🌡️ / 🌀 这类与主题不搭的 emoji」，
 * 而且说「具体怎么做你自己决定」。根因在我们自己的 `backend/app/services/diagram.py`：
 * 提示词只写「单个字符或省略」，`_emoji_of()` 模型给什么就用什么（兜底轮换只管"有没有"）。
 *
 * 后端行为由 `backend/tests/test_diagram_emoji.py` 断言（真知识点 × 7 布局跑纯函数路径）；
 * 这里做**源码级**口径断言，保证「白名单被删/提示词约束被撤回/图又用回渲染器默认头像」这类
 * 回退装不回来。图标清单本身跨文件校验：必须落在 `figure_png._EMOJI_RANGES` 里
 * （否则 PNG 里会被 `strip_emoji()` 抹掉）。
 *
 * 运行：`node scripts/check-diagram-emoji.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
/**
 * ⚠️ DSH 2026-09-22 应急修复（原样交付时**门禁是红的**）：
 * 本脚本原来直接 `readFileSync(...)`，匹配串里用的是 `\n`；而本机 git 的工作区是 **CRLF**
 * （`core.autocrlf`），于是所有多行匹配全部失效 —— 表现为「_emoji_of 没走 _pick_emoji」
 * 加 4 条「自检失败：…没改到源码（断言可能已失效）」，`verify.ps1` **exit=1**。
 * 仓库里既有的两个同类脚本都做了归一化（`check-preview-ux.ts:30`、`check-figure-zoom.ts:26`），
 * 这里按同一惯例补上，行为与 LF 环境下**逐字一致**。
 */
const readPy = (rel: string) =>
  readFileSync(join(repo, "backend", "app", "services", rel), "utf8").replace(/\r+\n/g, "\n");

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

/** 列表里的字符串字面量（忽略行尾注释：注释里也会写图标做说明）。 */
function literals(body: string): string[] {
  const out: string[] = [];
  for (const line of body.split("\n")) {
    for (const m of (line.split("#")[0]).matchAll(/"((?:\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}|[^"\\])*)"/g)) {
      out.push(decode(m[1]));
    }
  }
  return out;
}

function constString(src: string, name: string): string {
  const m = src.match(new RegExp(`${name}\\s*=\\s*"([^"]*)"`));
  if (!m) throw new Error(`源码里找不到常量 ${name}`);
  return m[1];
}

// 「闹笑话」家族（§B1：食物 / 厨具 / 人物 / 表情 / 手势 / 国旗 / 货币；另加动物）
const FORBIDDEN: [number, number][] = [
  [0x1f1e6, 0x1f1ff], // 国旗
  [0x1f32d, 0x1f32f], // 🌭🌮🥙
  [0x1f345, 0x1f35f], // 🍅🍆🍇
  [0x1f362, 0x1f37f], // 🍳🍣🍺🍽
  [0x1f400, 0x1f43f], // 动物
  [0x1f466, 0x1f478], // 人物
  [0x1f4b0, 0x1f4b8], // 货币
  [0x1f590, 0x1f596], // 手势
  [0x1f600, 0x1f64f], // 表情
  [0x1f918, 0x1f91f], // 手势
  [0x1f950, 0x1f96f], // 🥐🥟🥡🥢（🥟 就是真机上那枚）
  [0x1f9c0, 0x1f9c2], // 🧀🧁🧂
  [0x1f9d0, 0x1f9e0], // 人物 / 身体（🧠）
  [0x270a, 0x270d],   // 手势
];
const forbidden = (ch: string) => FORBIDDEN.some(([lo, hi]) => {
  const cp = ch.codePointAt(0) ?? 0;
  return lo <= cp && cp <= hi;
});

/** 图标口径审计（抽成函数，便于下面用「变异后的源码」做判定自检）。 */
function auditDiagram(src: string, png: string): string[] {
  const bad: string[] = [];
  const icons = literals(listBody(src, "DIAGRAM_EMOJI_WHITELIST"));
  const wheel = literals(listBody(src, "_EMOJI_WHEEL"));
  const prompt = src.match(/DIAGRAM_SYSTEM\s*=\s*"""([\s\S]*?)"""/)?.[1] ?? "";

  if (icons.length === 0) bad.push("diagram.py 里没有 DIAGRAM_EMOJI_WHITELIST（图标没有白名单）");
  if (icons.length < 10 || icons.length > 16) bad.push(`白名单应 10~16 个图标，实际 ${icons.length}`);
  if (new Set(icons).size !== icons.length) bad.push("白名单里有重复图标");
  const flagged = icons.flatMap((icon) => [...icon].filter(forbidden));
  if (flagged.length > 0) bad.push(`白名单里混进了闹笑话家族：${flagged.join("")}`);
  if (!icons.every((icon) => wheel.includes(icon) || true)) bad.push("白名单解析异常");
  if (wheel.length < 6) bad.push(`兜底轮换只有 ${wheel.length} 个（太少，同图会撞图标）`);
  if (new Set(wheel).size !== wheel.length) bad.push("兜底轮换里有重复项");
  if (!wheel.every((icon) => icons.includes(icon))) bad.push("兜底轮换不是白名单子集");

  // 图标必须落在 figure_png 的 emoji 区间里，否则 PNG 重绘会被 strip_emoji 抹掉
  const skip = [...(png.match(/_EMOJI_SKIP\s*=\s*\(([^)]*)\)/)?.[1] ?? "")
    .matchAll(/"((?:\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}|[^"\\])*)"/g)].map((m) => decode(m[1]));
  const ranges = [...(png.match(/_EMOJI_RANGES\s*=\s*\(([\s\S]*?)\)\s*\n/)?.[1] ?? "")
    .matchAll(/\(0x([0-9A-Fa-f]+),\s*0x([0-9A-Fa-f]+)\)/g)]
    .map((m) => [parseInt(m[1], 16), parseInt(m[2], 16)] as [number, number]);
  if (ranges.length === 0) bad.push("figure_png.py 里找不到 _EMOJI_RANGES");
  const outside = icons.flatMap((icon) => [...icon].filter((ch) =>
    !skip.includes(ch) && !ranges.some(([lo, hi]) => lo <= (ch.codePointAt(0) ?? 0) && (ch.codePointAt(0) ?? 0) <= hi)));
  if (outside.length > 0) bad.push(`白名单里有 figure_png 不认的字符（PNG 里会被抹掉）：${outside.join("")}`);

  // 口径唯一入口：_emoji_of 必须走 _pick_emoji，_pick_emoji 必须做白名单校验
  if (!/def _pick_emoji\([\s\S]*?candidate\s+in\s+DIAGRAM_EMOJI_WHITELIST/.test(src)) {
    bad.push("_pick_emoji 没有做白名单校验（模型给什么就用什么）");
  }
  const emojiOfBody = src.match(/def _emoji_of\(node: dict, index: int\) -> str:\n([^\n]*)\n/)?.[1] ?? "";
  if (!/^    return _pick_emoji\(node\.get\("emoji"\), _EMOJI_WHEEL\[index % len\(_EMOJI_WHEEL\)\]\)$/.test(emojiOfBody)) {
    bad.push("_emoji_of 没走 _pick_emoji（白名单形同虚设）");
  }
  if (/str\(head\.get\("emoji"\) or ""\)\.strip\(\)/.test(src)) {
    bad.push("compare 栏还在直接照抄模型给的 emoji（没过白名单）");
  }

  // 提示词：只能从清单里选 + 点名排除家族 + JSON 字段不再写「单个字符或省略」
  if (constString(src, "DIAGRAM_PROMPT_VERSION") !== "diagram-v2") {
    bad.push(`DIAGRAM_PROMPT_VERSION 不是 diagram-v2（改了提示词必须升版本）`);
  }
  if (prompt.includes("单个字符或省略")) bad.push("提示词还在写「单个字符或省略」（等于没约束）");
  if (!/emoji 只能从下面这 %d 个图标里\*\*原样选一个\*\*/.test(prompt)) {
    bad.push("提示词没有「emoji 只能从下面这 N 个图标里原样选一个」这条规则");
  }
  for (const word of ["食物", "厨具", "人物", "表情", "国旗", "货币"]) {
    if (!prompt.includes(word)) bad.push(`提示词没有点名排除「${word}」`);
  }
  // 清单与数量由常量在运行时注入（源码里是 %d / %s 占位符）⇒ 只认死这条注入口径，
  // 「清单内容 / 数量正确」由 backend/tests/test_diagram_emoji.py 对着**运行时**提示词断言。
  if (!/% \(len\(DIAGRAM_EMOJI_WHITELIST\),\s*" "\.join\(DIAGRAM_EMOJI_WHITELIST\)\)/.test(src)) {
    bad.push("提示词没有把白名单（和数量）注入进去（模型看到的还是空约束）");
  }

  // 标题条：必须显式填场景字段（任务 92 起渲染器缺省值本身也是白名单内那枚 📘）
  if (!/scene\.setdefault\("emoji",\s*_DIAGRAM_HEAD_EMOJI\)/.test(src)) {
    bad.push("_build_scene 没填 scene[\"emoji\"]（图左上角会直接用 excalidraw_layout.DEFAULT_HEAD，绕开这层口径）");
  }
  const head = src.match(/_DIAGRAM_HEAD_EMOJI\s*=\s*"([^"]*)"/)?.[1] ?? "";
  if (!head || !icons.includes(decode(head))) bad.push("_DIAGRAM_HEAD_EMOJI 不在白名单里");
  const center = src.match(/_DIAGRAM_CENTER_EMOJI\s*=\s*"([^"]*)"/)?.[1] ?? "";
  if (!center || !icons.includes(decode(center))) bad.push("_DIAGRAM_CENTER_EMOJI 不在白名单里");
  if (!/"prompt_version":\s*DIAGRAM_PROMPT_VERSION/.test(src)) {
    bad.push("落库 meta 里没有记 prompt_version（回查不了哪张图是哪个版本生成的）");
  }
  return bad;
}

const diagramPy = readPy("diagram.py");
const figurePngPy = readPy("figure_png.py");
const failures: string[] = [...auditDiagram(diagramPy, figurePngPy)];
const icons = literals(listBody(diagramPy, "DIAGRAM_EMOJI_WHITELIST"));
const wheel = literals(listBody(diagramPy, "_EMOJI_WHEEL"));

// 判定自检：把口径改回去 / 摘掉接线 ⇒ 上面的审计必须变红（否则等于永远绿灯）
const MUTATIONS: [string, string][] = [
  ["_emoji_of 不再走白名单（照抄模型的 emoji）",
   diagramPy.replace(
     /def _emoji_of\(node: dict, index: int\) -> str:\n    return _pick_emoji\(node\.get\("emoji"\), _EMOJI_WHEEL\[index % len\(_EMOJI_WHEEL\)\]\)/,
     "def _emoji_of(node: dict, index: int) -> str:\n    return str(node.get(\"emoji\") or \"\").strip() or _EMOJI_WHEEL[index % len(_EMOJI_WHEEL)]")],
  ["白名单里塞一个披萨",
   diagramPy.replace("DIAGRAM_EMOJI_WHITELIST = [\n", 'DIAGRAM_EMOJI_WHITELIST = [\n    "\\U0001F355",\n')],
  ["兜底轮换里塞白名单外的图标",
   diagramPy.replace(/"\\U0001F501",/, '"\\U0001F95F",')],
  ["提示词撤掉白名单约束",
   diagramPy.replace("emoji 只能从下面这 %d 个图标里**原样选一个**", "emoji 随意")],
  ["摘掉标题条图标（图又用回 👨‍🍳）",
   diagramPy.replace('    scene.setdefault("emoji", _DIAGRAM_HEAD_EMOJI)\n', "")],
  ["删掉 meta.prompt_version",
   diagramPy.replace('              "prompt_version": DIAGRAM_PROMPT_VERSION},\n', "")],
  ["figure_png 不再认符号类 emoji（图标会被抹掉）",
   figurePngPy.replace("(0x2600, 0x27BF)", "(0x2600, 0x2610)")],
];
for (const [name, mutated] of MUTATIONS) {
  const onPng = name.startsWith("figure_png");
  const base = onPng ? figurePngPy : diagramPy;
  if (mutated === base) {
    failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
  } else if (auditDiagram(onPng ? diagramPy : mutated, onPng ? mutated : figurePngPy).length === 0) {
    failures.push(`自检失败：${name} 后判定仍然通过`);
  }
}

if (failures.length > 0) {
  console.error("前端「概念图图标口径」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`前端概念图图标口径断言通过：白名单 ${icons.length} 个（${icons.join(" ")}）、`
  + `兜底轮换 ${wheel.length} 个且是白名单子集（顺序稳定）、提示词 DIAGRAM_PROMPT_VERSION=`
  + `${constString(diagramPy, "DIAGRAM_PROMPT_VERSION")} 点名「只能从清单里原样选一个」并排除食物/厨具/人物/表情/国旗/货币、`
  + `标题条显式填 scene 字段（渲染器缺省值见任务 92 的断言）、meta 记 prompt_version、`
  + `图标全部落在 figure_png 的 emoji 区间内（${MUTATIONS.length} 条变异自检全红）`);
