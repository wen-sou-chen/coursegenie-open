/**
 * 任务 48t 前端口径断言：antd 的 message 必须走 <App> 的 context 实例 + favicon 必须在。
 *
 * 起因（DSH 第一次跑自动 UI 冒烟抓到）：① `frontend/index.html` 没有任何
 * `<link rel="icon">`、`frontend/public/` 也不存在 ⇒ `GET /favicon.ico` 404、控制台常驻
 * 一条 `Failed to load resource: 404`，标签页没图标（录屏做宣传很显眼）；
 * ② `main.tsx` 只有 `ConfigProvider`、没有 antd 的 `<App>` ⇒ 面板里的静态
 * `message.success(...)` 会报 `Static function can not consume context like dynamic theme`，
 * 提示条也不跟随动态主题。
 *
 * 这里不启 DOM，只做源码级形状断言（与 check-delivery-mode.ts 同一套路），
 * 防「新面板又用静态 message」「哪天把 <App> 拆掉」「图标文件被删」这类静默回归。
 *
 * 口径说明（2026-09-19 实测校准）：「迁移」= 文件不再从 antd 静态引入 `message`，改由
 * `App.useApp()` 取实例（调用点写法仍是 `message.xxx(...)`，故只能看 import 绑定，
 * 不能数调用点）。本轮 10 个面板迁移后，全仓调用点总数 218 一处未少，但静态绑定
 * 只剩 16 个文件 / 111 处 —— 这两个数就是技术债上限，只许减不许增。
 *
 * 运行：`node scripts/check-antd-context.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const fe = join(here, "..");
const read = (rel: string) => readFileSync(join(fe, rel), "utf8").replace(/\r+\n/g, "\n");
const src = (rel: string) => read(join("src", rel));

/**
 * 清理源码：`keepStrings=false` 时连字符串内容一并抹掉（数 `message.*` 调用用）；
 * `keepStrings=true` 只去注释（认 `import ... from "antd"` 用，字符串不能抹）。
 */
const scrub = (text: string, keepStrings: boolean): string => {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    const d = text[i + 1] ?? "";
    if (c === "/" && d === "*") {
      const end = text.indexOf("*/", i + 2);
      i = end === -1 ? text.length : end + 2;
      out += " ";
      continue;
    }
    if (c === "/" && d === "/") {
      const end = text.indexOf("\n", i);
      i = end === -1 ? text.length : end;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      const start = i;
      i += 1;
      while (i < text.length) {
        if (text[i] === "\\") { i += 2; continue; }
        if (text[i] === c || text[i] === "\n") { i += 1; break; }
        i += 1;
      }
      out += keepStrings ? text.slice(start, i) : ' "" ';
      continue;
    }
    out += c;
    i += 1;
  }
  return out;
};

const strip = (text: string): string => scrub(text, false);

/** 静态 message 调用（`message="..."` 这种 JSX 属性不算）。 */
const staticCalls = (text: string): number =>
  (strip(text).match(/(?<![\w.])message\.(success|error|warning|info|loading|open)\(/g) ?? []).length;

/** 是否从 antd 静态引入 `message`（迁移过的文件必须为 false）。 */
const importsStaticMessage = (text: string): boolean =>
  [...scrub(text, true).matchAll(/import\s*\{([^}]*)\}\s*from\s*"antd"/gs)].some((m) =>
    m[1].split(",").some((part) => part.trim().split(/\s+as\s+/)[0] === "message"),
  );

/** 任务 48t 本轮迁移到 App.useApp() 的面板（其余存量见技术债上限）。 */
const MIGRATED = [
  "pages/CourseDetail.tsx",
  "components/QcBadge.tsx",
  "components/ConceptPanel.tsx",
  "components/PptPanel.tsx",
  "components/DeliveryPanel.tsx",
  "components/steps/PlanStep.tsx",
  "components/steps/NoteStep.tsx",
  "components/steps/MaterialsStep.tsx",
  "components/steps/WorkbenchStep.tsx",
  "components/steps/PreviewStep.tsx",
  // 任务 81：「立即自检」的提示条要用 context 实例（顺带把本页从技术债里划掉）
  "components/OutlinePanel.tsx",
];

/** 技术债上限（2026-09-19 迁移后实测：16 个文件、111 处静态调用；只许减不许增）。 */
const DEBT_FILES_MAX = 16;
const DEBT_CALLS_MAX = 111;

const failures: string[] = [];

// ------------------------------------------------------------------ main.tsx 的 <App>
const main = src("main.tsx");
if (!/import \{[^}]*\bApp\b[^}]*\} from "antd"/.test(main)) {
  failures.push('main.tsx 没从 antd 引入 App（App.useApp() 会拿不到 context）');
}
if (!main.includes("<AntdApp>") || !main.includes("</AntdApp>")) {
  failures.push("main.tsx 没有把应用包进 antd 的 <App>");
} else if (main.indexOf("<AntdApp>") > main.indexOf("<App />")) {
  failures.push("antd 的 <App> 不在最外层（工作台拿不到它的 context）");
} else {
  const order = ["<AntdApp>", "<QueryClientProvider", "<App />", "</QueryClientProvider>", "</AntdApp>"];
  const idx = order.map((t) => main.indexOf(t));
  if (idx.some((i) => i === -1)) {
    failures.push("main.tsx 的 JSX 标签不全（48t 改动时曾改丢 <QueryClientProvider>，这里防回归）");
  } else if (!idx.every((v, i) => i === 0 || v > idx[i - 1])) {
    failures.push("main.tsx 的 JSX 嵌套顺序不对：应为 <AntdApp><QueryClientProvider><App /></QueryClientProvider></AntdApp>");
  }
}

// ------------------------------------------------------------------ 迁移过的面板
for (const rel of MIGRATED) {
  const t = src(rel);
  if (importsStaticMessage(t)) failures.push(`${rel} 又从 antd 静态引入了 message（改用 App.useApp()）`);
  if (!/const \{ message \} = App\.useApp\(\)/.test(t)) {
    failures.push(`${rel} 没有 const { message } = App.useApp()`);
  }
}

// ------------------------------------------------------------------ 技术债不许增长
const walk = (dir: string, out: string[] = []): string[] => {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.tsx?$/.test(name)) out.push(p);
  }
  return out;
};
let debtFiles = 0;
let debtCalls = 0;
for (const p of walk(join(fe, "src"))) {
  const rel = p.slice(join(fe, "src").length + 1).replace(/\\/g, "/");
  if (MIGRATED.includes(rel)) continue;
  const text = readFileSync(p, "utf8");
  if (!importsStaticMessage(text)) continue;
  debtFiles += 1;
  debtCalls += staticCalls(text);
}
if (debtFiles > DEBT_FILES_MAX || debtCalls > DEBT_CALLS_MAX) {
  failures.push(
    `静态 message 技术债涨了（文件 ${debtFiles}/${DEBT_FILES_MAX}、调用 ${debtCalls}/${DEBT_CALLS_MAX}）` +
      "：新代码请用 App.useApp()，不要再写静态 message.*",
  );
}

// ------------------------------------------------------------------ favicon
const indexHtml = read("index.html");
if (!/rel="icon"/.test(indexHtml) || !indexHtml.includes("/favicon.svg")) {
  failures.push('index.html 缺 <link rel="icon" href="/favicon.svg">');
}
if (!indexHtml.includes("/favicon.ico")) failures.push("index.html 没有声明 /favicon.ico 兜底");
for (const rel of ["public/favicon.svg", "public/favicon.ico"]) {
  const p = join(fe, rel);
  if (!existsSync(p)) { failures.push(`缺图标文件 ${rel}`); continue; }
  if (statSync(p).size < 100) failures.push(`${rel} 太小，像是空文件`);
}
const ico = existsSync(join(fe, "public/favicon.ico")) ? readFileSync(join(fe, "public/favicon.ico")) : null;
if (ico && !(ico[0] === 0 && ico[1] === 0 && ico[2] === 1 && ico[3] === 0)) {
  failures.push("public/favicon.ico 不是合法的 ICO（头 4 字节应为 00 00 01 00）");
}
const svgText = existsSync(join(fe, "public/favicon.svg")) ? read("public/favicon.svg") : "";
if (!svgText.includes("<svg") || !svgText.includes('viewBox="0 0 32 32"')) failures.push("public/favicon.svg 形状不对");

// ------------------------------------------------------------------ 两条 antd 控制台警告
const workbench = src("components/steps/WorkbenchStep.tsx");
if (!/\n\s*forceRender\b/.test(strip(workbench))) {
  failures.push("WorkbenchStep 的编辑节点弹窗没有 forceRender（useForm 未连接警告会回来）");
}
const noteStep = src("components/steps/NoteStep.tsx");
if (strip(noteStep).includes("addonBefore")) {
  failures.push("NoteStep 又用了已废弃的 Input addonBefore（改用 Space.Compact）");
}
if (!strip(noteStep).includes("Space.Compact")) failures.push("NoteStep 没有用 Space.Compact 承载目录图标");

// OutlinePanel 的预填只能写「还没有大纲」时才挂载的 <Form form={form}>
// （2026-09-19 实测根因：已有大纲时该表单不渲染，两个 useEffect 又直接写值 ⇒ 报 useForm 未连接；
// 反向证明：撕掉守卫立刻回到 1 条警告，恢复后 0 条）。
const outlinePath = join(fe, "src", "components", "OutlinePanel.tsx");
const outline = existsSync(outlinePath) ? src("components/OutlinePanel.tsx") : "";
if (!strip(outline).includes("if (hasOutline) return;")) {
  failures.push("OutlinePanel 的预填 useEffect 没有 hasOutline 守卫（表单未挂载时写值会报 useForm 未连接）");
}

if (failures.length) {
  console.error("[FAIL] 任务 48t 口径断言未通过：");
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(
  `[PASS] 任务 48t 口径断言：antd <App> 已接线 + ${MIGRATED.length} 个面板改用 App.useApp()` +
    `（静态 message 技术债 ${debtFiles} 文件 / ${debtCalls} 处，上限 ${DEBT_FILES_MAX}/${DEBT_CALLS_MAX}）` +
    ` + favicon 齐 + 两条警告修复就位`,
);
