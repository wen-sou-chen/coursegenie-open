/**
 * 任务 72 前端断言：素材行的**三态文案**互不相同 + 老数据回填走显式入口。
 *
 * 起因（DSH 复验 69 时发现，2026-09-21）：
 * ① 界面对素材 6 打的是「已清理 3 行对话外壳 / 页眉」，可**库里的原文其实没清**
 *    （`meta.shell_stripped=False`，实测正文里 `生成详细版` / `好的 ✅` 都还在）——
 *    真正发生的是「出库（注入 / 导出）时会剥这 3 行」。判据必须是 `shell_stripped`，
 *    不能只看 `cleaned_lines`（老数据的它是现算出来的）。
 * ② 打开素材页的那次 `GET /api/material/{course_id}` 会调 `backfill_material_meta()`
 *    并 `db.commit()` ⇒ 破「GET 不改状态」（实测真库 mtime 2026-09-21 13:29:04 被写变）。
 *
 * 后端行为由 `backend/tests/test_material_injection.py` 断言；这里做**源码级**接线断言，
 * 保证「后端改对了、界面还在说反话」这种组合装不回来。
 *
 * 运行：`node scripts/check-material-meta-copy.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) =>
  readFileSync(join(here, "..", rel), "utf8").replace(/\r+\n/g, "\n");

const types = read("src/types.ts");
const api = read("src/api.ts");
const materials = read("src/components/steps/MaterialsStep.tsx");
const routePy = readFileSync(join(repo, "backend", "app", "api", "routes", "material.py"), "utf8");
const servicePy = readFileSync(join(repo, "backend", "app", "services", "material.py"), "utf8");
const kbPy = readFileSync(join(repo, "backend", "app", "services", "kb.py"), "utf8");

/** 三态文案（界面）—— 同一个词不许出现在两种状态里。 */
const STATE_UPLOAD = /检测到\s*\{m\.detected_lines\}\s*行[^<]*保持原样/;              // ① 上传原件
const STATE_EXPORT = /出库时会剥离\s*\{m\.cleaned_lines\}\s*行[^<]*库内保留原文/;      // ② 老数据没落库
const STATE_CLEANED = /已清理\s*\{m\.cleaned_lines\}\s*行[^<]*库内正文已更新/;          // ③ 库内真改过
const GATE_EXPORT = /!m\.shell_stripped\s*&&\s*\(m\.cleaned_lines \?\? 0\) > 0/;
const GATE_CLEANED = /m\.shell_stripped\s*&&\s*\(m\.cleaned_lines \?\? 0\) > 0/;
const CLEAN_BUTTON = /clean\.mutate\(m\.id\)/g;

/** 素材行的三态断言（抽成函数，便于下面用「变异后的源码」做判定自检）。 */
function auditMaterials(src: string): string[] {
  const bad: string[] = [];
  const upload = src.match(STATE_UPLOAD)?.[0] ?? "";
  const exported = src.match(STATE_EXPORT)?.[0] ?? "";
  const cleaned = src.match(STATE_CLEANED)?.[0] ?? "";
  if (!upload) bad.push("MaterialsStep 没有「检测到 N 行…（按口径保持原样）」文案");
  if (!exported) bad.push("MaterialsStep 没有「出库时会剥离 N 行（库内保留原文）」文案");
  if (!cleaned) bad.push("MaterialsStep 没有「已清理 N 行（库内正文已更新）」文案");
  // 「别用同一个词」：未清的两态不许再出现「已清理」
  if (/已清理/.test(upload)) bad.push("上传原件那态用了「已清理」（应只说「检测到…保持原样」）");
  if (/已清理/.test(exported)) bad.push("未落库那态用了「已清理」（库里原文没动，会误导）");
  if (!GATE_EXPORT.test(src)) bad.push("「出库时会剥离」没有挂在 !shell_stripped 上（判据错）");
  if (!GATE_CLEANED.test(src)) bad.push("「已清理（库内正文已更新）」没有挂在 shell_stripped 上");
  // 未落库那一态必须留着「一键清理」按钮（点它才落库）；上传原件那态也要有
  const buttons = (src.match(CLEAN_BUTTON) ?? []).length;
  if (buttons < 2) bad.push(`「一键清理」入口只剩 ${buttons} 处（未落库那态点不了）`);
  return bad;
}

const failures: string[] = [...auditMaterials(materials)];

/** ④ 类型：`shell_stripped` 必须在 types.ts 里声明（否则前端读不到、三态之一永远不亮）。 */
if (!/^\s*shell_stripped\??:/m.test(types)) {
  failures.push("types.ts 里没有声明 shell_stripped（后端返回了、前端读不到）");
}

/** ⑤ 显式入口：补齐素材信息要打后端的新接口，且提示语里带上份数。 */
if (!/backfillMaterialMeta:[\s\S]{0,240}?\/material\/\$\{courseId\}\/backfill/.test(api)) {
  failures.push("api.ts 没有 backfillMaterialMeta（补齐素材信息点了没反应）");
}
if (!/已补齐 \$\{res\.backfilled\} 份素材的页数\/来源信息/.test(materials)) {
  failures.push("MaterialsStep 没有提示「已补齐 N 份素材的页数/来源信息」");
}

/** ⑥ 后端：GET 不许再写库（抽成函数，便于判定自检）。 */
function auditGetIsReadonly(src: string): string[] {
  const bad: string[] = [];
  const fn = src.match(/@router\.get\("\/\{course_id\}"\)[\s\S]*?(?=\n@router\.)/)?.[0] ?? "";
  if (!fn) bad.push("routes/material.py 里找不到 GET /{course_id} 的处理函数");
  else if (/material_service\.backfill_material_meta\(/.test(fn)) {
    bad.push("GET /api/material/{course_id} 里还在调 backfill_material_meta（GET 会写库）");
  } else if (/db\.commit\(\)/.test(fn)) {
    bad.push("GET /api/material/{course_id} 的**代码**里有 db.commit()（GET 不许改状态）");
  }
  return bad;
}
failures.push(...auditGetIsReadonly(routePy));

if (!/@router\.post\("\/\{course_id\}\/backfill"\)/.test(routePy)) {
  failures.push("缺少显式回填入口 POST /material/{course_id}/backfill");
}
if (!/logger\.info\([\s\S]{0,160}补齐素材信息/.test(routePy)) {
  failures.push("显式回填入口没有写 INFO 日志（改过库要能回查）");
}
if (/def clean_material[\s\S]*?AI 补充与粘贴件在入库时已清理过/.test(servicePy)) {
  failures.push("clean_material 仍在无条件拒绝「必须干净」的素材（老数据点一键清理会报 400）");
}
/** ⑦ 交付页（素材清单.md）用同一套三态说法。 */
if (!/已清理 \{cleaned\['lines'\]\} 行对话外壳 \/ 页眉（库内正文已更新）/.test(kbPy)) {
  failures.push("kb.py 素材页没写「已清理 N 行（库内正文已更新）」");
}
if (!/出库时会剥离 \{cleaned\['lines'\]\} 行对话外壳 \/ 页眉（库内保留原文/.test(kbPy)) {
  failures.push("kb.py 素材页没写「出库时会剥离 N 行（库内保留原文）」");
}

// 判定自检：把口径改回修前 / 把接线摘掉 ⇒ 上面的断言必须变红（否则等于永远绿灯）
const MUTATIONS: [string, string][] = [
  ["把未落库那态改回「已清理」",
   materials.replace("出库时会剥离 {m.cleaned_lines} 行对话外壳 / 页眉（库内保留原文）",
                      "已清理 {m.cleaned_lines} 行对话外壳 / 页眉")],
  ["把上传原件那态改回「已清理」",
   materials.replace("检测到 {m.detected_lines} 行疑似页眉页脚 / 对话外壳（按口径保持原样）",
                      "已清理 {m.detected_lines} 行对话外壳 / 页眉")],
  ["把状态三的判据从 shell_stripped 改回 cleaned_lines",
   materials.replace("m.shell_stripped && (m.cleaned_lines ?? 0) > 0 ?", "m.cleaned_lines ?")],
  ["把状态二的判据改成只看 must_clean",
   materials.replace("!m.shell_stripped && (m.cleaned_lines ?? 0) > 0 ?", "m.cleaned_lines ?")],
  ["摘掉未落库那态的「一键清理」",
   materials.replace(/<Popconfirm\s+title=\{`把剥离结果落库？[\s\S]*?<\/Popconfirm>/, "")],
];
for (const [name, mutated] of MUTATIONS) {
  if (mutated === materials) {
    failures.push(`自检失败：变异「${name}」没改到源码（断言可能已失效）`);
  } else if (auditMaterials(mutated).length === 0) {
    failures.push(`自检失败：${name} 后判定仍然通过`);
  }
}
const routeWithBackfillInGet = routePy.replace(
  /(def list_materials\(course_id: str, db: Session = Depends\(get_db\)\):[\s\S]*?rows = material_service\.list_materials\(db, course_id\))/,
  "$1\n    material_service.backfill_material_meta(db, rows)",
);
if (routeWithBackfillInGet === routePy) {
  failures.push("自检失败：变异「把回填塞回 GET」没改到源码");
} else if (auditGetIsReadonly(routeWithBackfillInGet).length === 0) {
  failures.push("自检失败：把回填塞回 GET 后判定仍然通过");
}

if (failures.length > 0) {
  console.error("前端「素材三态文案 + 显式回填入口」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端素材三态断言通过：上传原件「检测到 N 行…保持原样」/ 未落库「出库时会剥离 N 行（库内保留原文）」"
  + "+一键清理 / 已落库「已清理 N 行（库内正文已更新）」三种说法互不相同且判据是 shell_stripped；"
  + "补齐素材信息走显式入口 POST /material/{id}/backfill（GET 只读、入口写 INFO 日志），交付页同口径");
