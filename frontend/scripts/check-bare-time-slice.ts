/**
 * 任务 84 前端断言：日期时间戳**不许裸切字符串**（源码级护栏）。
 *
 * 起因（DSH 2026-09-23 复验 83 时按「全面板日期扫描」挖出）：任务 83 把时间格式化收敛到唯一出处
 * `frontend/src/lib/datetime.ts`，但那条断言查的是「自己写的 `function formatTime` 定义处数 = 0」——
 * **同一个病换了张皮就溜过去了**：
 *
 *   const parts = [pages ? `${pages} 页` : "", at ? String(at).replace("T", " ").slice(0, 16) : ""];
 *
 * （`PptDeckBlocks.tsx` 的 `slideLabel`；`OutlinePanel.tsx` 的「上次自检」是同款写法。）它既不定义
 * `formatTime`、也不 import 任何格式化函数 ⇒ 真机 PPT 面板显示 `2026-09-17 07:20`，而课件文件
 * mtime 是 `2026-09-17 15:24`（少 8 小时）；更要紧的是**把它修好、或改得更错，门禁都 exit=0**。
 *
 * 本脚本钉三件事：
 * ① **反面扫描**：`frontend/src/**` 里 `\.replace\(\s*"T"\s*,\s*" "`、`\.slice\(\s*0\s*,\s*16\s*\)`
 *    与手工 UTC 后缀 `.replace("+00:00", " UTC")` 命中 **0 处**（先 `stripComments` 再扫：注释里可以写旧写法，
 *    但**夹在代码中间的注释也不能把违规藏过去**）；
 * ② **正面接线**：PPT 那一行摘要（`slideLabel`，覆盖 PPT 面板 2 处 + 第 4 步预览页 2 处调用点）
 *    与大纲「上次自检」都必须走 `formatLocalTime`（断言读的是**去掉注释后**的源码，
 *    免得有人用一句注释假装接了线）；
 * ③ **变异自检**：把 `slideLabel` 改回裸切 / 把裸切夹进注释 ⇒ 必须变红；只把旧写法留在注释里 ⇒ 不许误报。
 *
 * 运行：`node scripts/check-bare-time-slice.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

/** 前端源码树（相对仓库根的 posix 路径 → 内容），用来扫「还有没有裸切写法」。 */
function walkSrc(dir: string, out: Map<string, string> = new Map()): Map<string, string> {
  for (const entry of readdirSync(join(repo, dir), { withFileTypes: true })) {
    const rel = `${dir}/${entry.name}`;
    if (entry.isDirectory()) walkSrc(rel, out);
    else if (/\.tsx?$/.test(entry.name)) out.set(rel, read(rel));
  }
  return out;
}

/** 扫「写法」时先去掉注释（与任务 81 的 check-panel-metrics-copy.ts 同款）。 */
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const failures: string[] = [];

const REL_DECK = "frontend/src/components/PptDeckBlocks.tsx";
const REL_OUTLINE = "frontend/src/components/OutlinePanel.tsx";

/* ======================= ① 反面扫描：不许有裸切 / 手写时间串 ======================= */
const BARE_SLICE_PATTERNS: [string, RegExp][] = [
  ["把 ISO 的 T 直接换成空格（连时区一起丢）", /\.replace\(\s*"T"\s*,\s*" "/],
  ["按固定长度切成 16 个字符（直接当本地时间）", /\.slice\(\s*0\s*,\s*16\s*\)/],
  ["手工拼 UTC 后缀（全站口径是本地时间）", /\.replace\(\s*"\+00:00"\s*,\s*" UTC"\)/],
];

function auditBareSlice(files: Map<string, string>): string[] {
  const bad: string[] = [];
  for (const [rel, src] of files) {
    const code = stripComments(src);
    for (const [name, pattern] of BARE_SLICE_PATTERNS) {
      if (pattern.test(code)) {
        bad.push(`${rel} 还有「${name}」的写法（时间戳必须走 lib/datetime.ts 的 formatLocalTime）`);
      }
    }
  }
  return bad;
}

const srcFiles = walkSrc("frontend/src");
failures.push(...auditBareSlice(srcFiles));

/* ======================= ② 正面接线：这两处都走唯一出处 ======================= */
function auditWiring(files: Map<string, string>): string[] {
  const bad: string[] = [];
  const deck = stripComments(files.get(REL_DECK) ?? "");
  const outline = stripComments(files.get(REL_OUTLINE) ?? "");
  for (const [rel, code] of [[REL_DECK, deck], [REL_OUTLINE, outline]] as [string, string][]) {
    if (!/from "\.\.\/lib\/datetime"/.test(code)) {
      bad.push(`${rel} 没引用 lib/datetime（时间应只有一处实现）`);
    }
  }
  // 任务 84 §B1：页数那段保持原样，只有时间那一段换成唯一出处。
  if (!/export function slideLabel\(deck: PptDeck\): string \{[\s\S]{0,400}?formatLocalTime\(String\(at\)\)/.test(deck)) {
    bad.push("slideLabel 没有走 formatLocalTime（PPT 面板 / 第 4 步预览页的课件时间又会少 8 小时）");
  }
  // 任务 84 §B2：「上次自检」去掉手工 +00:00 → UTC，仍能看出是什么时候生成的。
  if (!/上次自检 \{formatLocalTime\(checkGeneratedAt\)\}/.test(outline)) {
    bad.push("大纲「上次自检」没走 formatLocalTime（全站只有它一处写 UTC，口径不统一）");
  }
  return bad;
}

failures.push(...auditWiring(srcFiles));

/* ======================= ③ 变异自检：反面扫描必须真的有牙齿 ======================= */
const deckSrc = srcFiles.get(REL_DECK) ?? "";
const outlineSrc = srcFiles.get(REL_OUTLINE) ?? "";
const GOOD_LINE =
  'const parts = [pages ? `${pages} 页` : "", at ? formatLocalTime(String(at)) : ""];';
const BAD_LINE =
  'const parts = [pages ? `${pages} 页` : "", at ? String(at).replace("T", " ").slice(0, 16) : ""];';
/** 想骗过反面扫描的写法：把注释**夹在调用中间**，让朴素的正则匹配不上。
 *  （注释刻意**不写**旧写法本身 —— 否则朴素的正则也能从注释里匹配到，就验不出 stripComments 的作用了。） */
const HIDDEN_LINE = [
  'const parts = [pages ? `${pages} 页` : "", at ? String(at).replace(',
  '  // 时间还是沿用旧写法（裸切），没走 formatLocalTime',
  '  "T", " ").slice(0, 16) : ""];',
].join("\n");
const OLD_LINE = '上次自检 {formatLocalTime(checkGeneratedAt)}';
const OLD_LINE_BACK = '上次自检 {checkGeneratedAt.replace("T", " ").replace("+00:00", " UTC")}';

/** [名称, 改哪个文件, 基线, 变异后, 判定函数, 期望（true = 必须报错）] */
const MUTATIONS: [string, string, string, string, (f: Map<string, string>) => string[], boolean][] = [
  ["把 slideLabel 改回裸切", REL_DECK, deckSrc, deckSrc.replace(GOOD_LINE, BAD_LINE), auditBareSlice, true],
  ["把裸切夹在注释中间（想骗过反面扫描）", REL_DECK, deckSrc,
    deckSrc.replace(GOOD_LINE, HIDDEN_LINE), auditBareSlice, true],
  ["只把旧写法留在注释里（不许误报）", REL_DECK, deckSrc,
    deckSrc.replace(GOOD_LINE, `${GOOD_LINE}\n// 旧写法 .replace("T", " ").slice(0, 16) 已删除`), auditBareSlice, false],
  ["把「上次自检」改回裸切", REL_OUTLINE, outlineSrc,
    outlineSrc.replace(OLD_LINE, OLD_LINE_BACK), auditWiring, true],
  ["让 slideLabel 只在注释里写着 formatLocalTime（代码还是裸切）", REL_DECK, deckSrc,
    deckSrc.replace(GOOD_LINE, `// 这里应该走 formatLocalTime(String(at))\n${BAD_LINE}`), auditWiring, true],
];

let mutationChecked = 0;
for (const [name, rel, base, mutated, run, wantHit] of MUTATIONS) {
  mutationChecked += 1;
  if (mutated === base) {
    failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
    continue;
  }
  const hits = run(new Map(srcFiles).set(rel, mutated));
  if (wantHit && hits.length === 0) failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  if (!wantHit && hits.length > 0) failures.push(`自检失败：变异「${name}」被误判（${hits[0]}）`);
}

if (failures.length > 0) {
  console.error("前端「时间戳不许裸切」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端裸切字符串断言（84）通过："
  + `① frontend/src/**（${srcFiles.size} 个 ts/tsx，先 stripComments）里「T 换空格 / slice(0, 16) / 手工 UTC 后缀」命中 0 处；`
  + "② 课件时间（PptDeckBlocks.slideLabel）+ 大纲「上次自检」都走 formatLocalTime，且这两处都引用了唯一出处；"
  + `③ ${mutationChecked} 条变异自检全红 / 无误报（含「把裸切夹进注释」⇒ 证明先 stripComments 再扫）`);
