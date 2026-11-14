/**
 * 任务 86 §B4 前端断言：**学员版（去答案）**（0 LLM）。
 *
 * 为什么要有这条：公开分享页原先与内部 `/content` 同构 ⇒ **里面躺着带答案的讲义**
 * （全课 78 处 `**答案**`）。老师把链接发给学生 = 答案外泄。修法必须落在两条硬口径上：
 *
 * ① **默认 = 现状（教师版）**：`normalize_view` / 路由默认 / 前端初值三处都必须是 `teacher`。
 *    任何一处「顺手默认去答案」，老师自己看到的讲义就少了一段且没有提示；
 * ② **只做确定性文本处理，不调模型**：学员版复用 `student_sheet.strip_leaks`（学案同一套规则），
 *    源码级护栏：`audience.py` / `student_sheet.py` 里**不许出现** `complete_json` / `build_llm_client`。
 *
 * 本脚本钉八组：后端开关与默认值 / 学员版去答案的规则来源 / 公开侧路由 / 交付包 `学员版讲义/`
 * （默认八件套结构不变）/ 前端切换与醒目标签 / 出包页勾选 / 交付 API 参数 / 分享 API 行为
 * （真实调用一次，断言 URL 带 `view=`），最后是变异自检（含「把去答案关掉」）。
 *
 * 运行：`node scripts/check-student-view.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

/** Python 侧只去掉「整行 # 注释」。
 *
 * ⚠️ **不要**拿上面那套 `/* ... *\/` 去扫 Python：`delivery.py` 的 docstring 里写着
 * ``讲义/*.html``，会被当成块注释开头，一路吃到下一个 `*\/` —— 实测会把整个文件吃掉
 * 78%（74273 → 21821 字节），所有判定静默失效。 */
const stripPyComments = (s: string) => s.replace(/^[ \t]*#[^\n]*$/gm, "");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

const REL_AUDIENCE = "backend/app/services/audience.py";
const REL_SHEET = "backend/app/services/student_sheet.py";
const REL_PUBLIC = "backend/app/api/routes/public.py";
const REL_SHARE = "backend/app/services/share.py";
const REL_DELIVERY = "backend/app/services/delivery.py";
const REL_DELIVERY_ROUTE = "backend/app/api/routes/delivery.py";
const REL_SHARE_API = "frontend/src/shareApi.ts";
const REL_SHARE_VIEW = "frontend/src/pages/ShareView.tsx";
const REL_DELIVERY_API = "frontend/src/deliveryApi.ts";
const REL_DELIVERY_PANEL = "frontend/src/components/DeliveryPanel.tsx";

function bodyOf(source: string, name: string): string {
  const start = source.indexOf(`def ${name}(`);
  if (start < 0) return "";
  const rest = source.slice(start + 1);
  const next = rest.indexOf("\ndef ");
  return next < 0 ? rest : rest.slice(0, next);
}

/* ---------------- ① 后端：开关 + 默认值 + 规则来源 + 0-LLM 护栏 ---------------- */

function auditAudience(source: string, sheetSource: string): string[] {
  const bad: string[] = [];
  if (!source.includes('AUDIENCE_TEACHER = "teacher"')) bad.push("audience.py 缺 AUDIENCE_TEACHER");
  if (!source.includes('AUDIENCE_STUDENT = "student"')) bad.push("audience.py 缺 AUDIENCE_STUDENT");
  if (!/def normalize_view\(/.test(source)) bad.push("audience.py 缺 normalize_view");

  // **默认 = 现状**：非法 / 空一律回 teacher，绝不默认去答案
  const norm = bodyOf(source, "normalize_view");
  if (!norm) {
    bad.push("audience.py 取不到 normalize_view 的函数体（断言失效）");
  } else {
    if (!/else AUDIENCE_TEACHER/.test(norm)) {
      bad.push("normalize_view 没有「其余一律 teacher」的回落（非法值可能变成去答案）");
    }
    if (/"student"\s*:/.test(norm) || /or AUDIENCE_STUDENT/.test(norm)) {
      bad.push("normalize_view 里出现了把空值当 student 的写法（默认必须 = 现状）");
    }
  }
  if (!/UNSET_VIEW = AUDIENCE_TEACHER/.test(source)) {
    bad.push("audience.py 缺 UNSET_VIEW = AUDIENCE_TEACHER（未指定的默认值没有显式声明）");
  }

  // 规则来源：复用学案那一套（学生填的学案与学生读的讲义，去答案口径必须一致）
  const student = bodyOf(source, "student_markdown");
  if (!student) {
    bad.push("audience.py 取不到 student_markdown（学员版正文的转换函数）");
  } else {
    if (!/student_sheet\.strip_leaks\(/.test(student)) {
      bad.push("student_markdown 没有复用 student_sheet.strip_leaks（等于又写了一套去答案规则）");
    }
    if (!/_RE_TICK_OPTION\.sub/.test(student)) {
      bad.push("student_markdown 没有把正确项的 ✓ 变回普通列表项");
    }
  }
  const rows = bodyOf(source, "student_rows");
  if (!rows) {
    bad.push("audience.py 取不到 student_rows（习题行的答案字段要清掉）");
  } else {
    // 任务 90（方案 A）：**连键一起去掉**。清成空串会留下「键在但为空」的中间态 ⇒
    // 分享页学员版每张随堂练习卡都露出一个空的「答案：」标签（实测每门课 6 处可见）。
    if (/"answer": ""/.test(rows) || /"explanation": ""/.test(rows)) {
      bad.push("student_rows 把答案清成了空串（任务 90：要连键一起去掉，别在载荷里留空壳）");
    }
    if (!/student_quiz_body\(body\)/.test(rows)) {
      bad.push("student_rows 的习题行没走 student_quiz_body（学员版习题行投影的唯一出处）");
    }
    if (!/kind == "lecture"/.test(rows)) {
      bad.push("student_rows 只处理了习题行、漏了讲义正文（或反之）");
    }
  }
  const quizBody = bodyOf(source, "student_quiz_body");
  if (!quizBody) {
    bad.push("audience.py 缺 student_quiz_body（学员版习题行投影的唯一出处）");
  } else if (!/not in _QUIZ_HIDDEN_KEYS/.test(quizBody)) {
    bad.push("student_quiz_body 不是按 _QUIZ_HIDDEN_KEYS 去掉键（答案字段可能原样漏出去）");
  }
  if (!/_QUIZ_HIDDEN_KEYS: tuple\[str, \.\.\.\] = \("answer", "explanation"\)/.test(source)) {
    bad.push("_QUIZ_HIDDEN_KEYS 不是 (\"answer\", \"explanation\")（要隐藏的字段口径被改了）");
  }

  // ⛔ 0 LLM：这两个文件里不许出现任何调模型的入口
  for (const [rel, src] of [[REL_AUDIENCE, source], [REL_SHEET, sheetSource]] as const) {
    for (const needle of ["complete_json", "build_llm_client", "openai", "LLMClient"]) {
      if (src.includes(needle)) bad.push(`${rel} 里出现了 ${needle}（学员版必须 0 LLM、只做确定性文本处理）`);
    }
  }

  // 学案那套规则要**公开**（学员版才能复用同一份，而不是各写一份）
  if (!/^def strip_leaks\(/m.test(sheetSource)) bad.push("student_sheet.strip_leaks 没有公开（学员版要复用它）");
  if (/def _strip_leaks\(/.test(sheetSource)) bad.push("student_sheet 里还留着私有的 _strip_leaks（两套规则会漂移）");
  for (const needle of ["评分要点", "答案", "解析"]) {
    if (!sheetSource.includes(needle)) bad.push(`student_sheet 的泄题行规则丢了「${needle}」`);
  }
  return bad;
}

/* ---------------- ② 公开侧 / 交付包 ---------------- */

function auditWiring(files: Map<string, string>): string[] {
  const bad: string[] = [];

  const pub = stripPyComments(files.get(REL_PUBLIC) ?? "");
  if (!/view: str = Query\(default=audience_service\.AUDIENCE_TEACHER/.test(pub)) {
    bad.push("公开侧 /content 没有 view 参数，或默认值不是 teacher（默认必须 = 现状）");
  }
  if (!/audience_view=view/.test(pub)) bad.push("公开侧 /content 没有把 view 传给 public_content");

  const share = stripPyComments(files.get(REL_SHARE) ?? "");
  if (!/audience_view: str = audience\.AUDIENCE_TEACHER/.test(share)) {
    bad.push("public_content 的 audience_view 默认值不是 teacher");
  }
  if (!/audience\.is_student\(audience_view\)/.test(share)) bad.push("public_content 没有按 view 分流");
  if (!/audience\.student_rows\(rows\)/.test(share)) bad.push("public_content 的学员版没有走 student_rows");

  const delivery = stripPyComments(files.get(REL_DELIVERY) ?? "");
  if (!/STUDENT_LECTURE_DIR = "学员版讲义"/.test(delivery)) bad.push("delivery.py 缺「学员版讲义」目录常量");
  if (!/LECTURE_DIR = "讲义"/.test(delivery)) bad.push("delivery.py 的默认「讲义」目录被动了（八件套结构不许变）");
  if (!/want_student = \(bool\(student_lectures\) if student_lectures is not None/.test(delivery)) {
    bad.push("delivery.py 的 want_student 没有「显式参数优先、否则按场景」的口径");
  }
  if (!/else "self_study" in \(scenario\.scenario_meta\(course\)\.get\("selected"\) or \[\]\)\)/.test(delivery)) {
    bad.push("delivery.py 没有按课程场景的 self_study 自动决定是否出学员版"
      + "（且必须走 scenario.scenario_meta —— 场景的唯一出处，不许自己再读一遍 meta）");
  }
  if (!/if not want_student:\s*\n\s+continue/.test(delivery)) {
    bad.push("delivery.py 的学员版讲义没有「不想出就跳过」的守卫（会无条件多出一个目录）");
  }
  if (!/audience\.student_markdown\(markdown\)/.test(delivery)) {
    bad.push("delivery.py 的学员版讲义正文没有走 audience.student_markdown");
  }

  const route = stripPyComments(files.get(REL_DELIVERY_ROUTE) ?? "");
  if (!/student_lectures: bool \| None = Field\(default=None/.test(route)) {
    bad.push("出包接口缺 student_lectures 参数（默认 None = 按场景自动）");
  }
  if (!/student_lectures=request\.student_lectures/.test(route)) {
    bad.push("出包接口没有把 student_lectures 透传给 build_delivery");
  }
  return bad;
}

/* ---------------- ③ 前端 ---------------- */

function auditFrontend(files: Map<string, string>): string[] {
  const bad: string[] = [];

  const api = stripComments(files.get(REL_SHARE_API) ?? "");
  if (!/export type AudienceView = "teacher" \| "student"/.test(api)) bad.push("shareApi 缺 AudienceView 类型");
  if (!/view: AudienceView = "teacher"/.test(api)) {
    bad.push("shareApi.content 的默认 view 不是 teacher（**默认必须 = 现状**）");
  }
  if (!/\?view=\$\{view\}/.test(api)) bad.push("shareApi.content 没有把 view 拼进 URL");

  const view = stripComments(files.get(REL_SHARE_VIEW) ?? "");
  if (!/data-testid="audience-switch"/.test(view)) bad.push("分享页缺「教师版 / 学员版」切换（data-testid=audience-switch）");
  if (!/data-testid="audience-current"/.test(view)) bad.push("分享页没有醒目地标出当前是哪个版本（data-testid=audience-current）");
  for (const text of ["当前：教师版（含答案与解析）", "当前：学员版（已隐藏答案与解析）"]) {
    if (!view.includes(text)) bad.push(`分享页缺那句「${text}」`);
  }
  if (!/useState<AudienceView>\("teacher"\)/.test(view)) {
    bad.push("分享页的视图初值不是 teacher（默认必须 = 现状）");
  }

  const panel = stripComments(files.get(REL_DELIVERY_PANEL) ?? "");
  if (!/学员版讲义/.test(panel)) bad.push("出包页没有「学员版讲义」这个勾选项");
  if (!/self_study/.test(panel)) bad.push("出包页没有按 self_study 给出默认（与后端同一条规则）");
  if (!/studentLectures/.test(panel)) bad.push("出包页没有把勾选结果传给构建请求");

  const dapi = stripComments(files.get(REL_DELIVERY_API) ?? "");
  if (!/student_lectures: studentLectures/.test(dapi)) bad.push("交付 API 没有把 student_lectures 发出去");
  return bad;
}

/* ---------------- ④ 行为级：真的调一次分享 API ---------------- */

type ShareApiMod = {
  publicShareApi: {
    content: (token: string, password?: string, view?: string) => Promise<unknown>;
  };
};

async function capturedCall(
  run: (api: ShareApiMod["publicShareApi"]) => Promise<unknown>,
  modulePath = join(repo, REL_SHARE_API),
): Promise<{ url: string; init?: RequestInit }> {
  const seen: { url: string; init?: RequestInit } = { url: "" };
  const original = globalThis.fetch;
  globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
    seen.url = String(url);
    seen.init = init;
    return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  try {
    const mod = await import(pathToFileURL(modulePath).href) as ShareApiMod;
    await run(mod.publicShareApi);
  } finally {
    globalThis.fetch = original;
  }
  return seen;
}

async function auditShareApiBehavior(modulePath = join(repo, REL_SHARE_API)): Promise<string[]> {
  const bad: string[] = [];
  const plain = await capturedCall((api) => api.content("TOK"), modulePath);
  if (!plain.url.includes("/public/TOK/content?view=teacher")) {
    bad.push(`默认调用必须带 view=teacher（默认 = 现状）：实际 ${plain.url}`);
  }
  const student = await capturedCall((api) => api.content("TOK", undefined, "student"), modulePath);
  if (!student.url.includes("/public/TOK/content?view=student")) {
    bad.push(`学员版调用必须带 view=student：实际 ${student.url}`);
  }
  const locked = await capturedCall((api) => api.content("TOK", "pw", "student"), modulePath);
  const headers = (locked.init?.headers ?? {}) as Record<string, string>;
  if (headers["X-Share-Password"] !== "pw") {
    bad.push("带密码的分享链接丢了 X-Share-Password 头（加了 view 之后回归）");
  }
  return bad;
}

/* ---------------- 走查 ---------------- */

const files = new Map<string, string>();
for (const rel of [REL_AUDIENCE, REL_SHEET, REL_PUBLIC, REL_SHARE, REL_DELIVERY,
  REL_DELIVERY_ROUTE, REL_SHARE_API, REL_SHARE_VIEW, REL_DELIVERY_API, REL_DELIVERY_PANEL]) {
  files.set(rel, read(rel));
}

failures.push(...auditAudience(files.get(REL_AUDIENCE) ?? "", files.get(REL_SHEET) ?? ""));
failures.push(...auditWiring(files));
failures.push(...auditFrontend(files));
failures.push(...await auditShareApiBehavior());

/* ---------------- 变异自检 ---------------- */

const tmpDir = mkdtempSync(join(tmpdir(), "cg86-student-"));
let mutationChecked = 0;

function mutate(rel: string, from: string | RegExp, to: string): Map<string, string> {
  const src = files.get(rel) ?? "";
  const next = src.replace(from, to);
  if (next === src) failures.push(`自检失败：变异没改到源码（${rel} 里找不到 ${JSON.stringify(from.slice(0, 60))}）`);
  return new Map(files).set(rel, next);
}

try {
  const sourceMutations: [string, string[]][] = [
    // 反证据 ㈠：把学员版的去答案逻辑关掉
    ["把学员版的去答案逻辑关掉（student_markdown 原样返回）",
      auditAudience(
        mutate(REL_AUDIENCE,
          "    return student_sheet.strip_leaks(_RE_TICK_OPTION.sub(r\"\\1- \", str(markdown or \"\")))",
          "    return str(markdown or \"\")",
        ).get(REL_AUDIENCE) ?? "",
        files.get(REL_SHEET) ?? "")],
    ["让 normalize_view 默认去答案（空 ⇒ student）",
      auditAudience(
        mutate(REL_AUDIENCE, "else AUDIENCE_TEACHER", "else AUDIENCE_STUDENT").get(REL_AUDIENCE) ?? "",
        files.get(REL_SHEET) ?? "")],
    ["让学员版自己写一套规则（不再复用学案的 strip_leaks）",
      auditAudience(
        mutate(REL_AUDIENCE, "student_sheet.strip_leaks(", "str(").get(REL_AUDIENCE) ?? "",
        files.get(REL_SHEET) ?? "")],
    // 任务 90：把「连键一起去掉」改回「清成空串」 ⇒ 分享页的「答案：」空标签会回来
    ["把学员版习题行的答案改回清成空串（键还在）",
      auditAudience(
        mutate(REL_AUDIENCE,
          "    return {k: v for k, v in body.items() if k not in _QUIZ_HIDDEN_KEYS}",
          "    return {**body, \"answer\": \"\", \"explanation\": \"\"}").get(REL_AUDIENCE) ?? "",
        files.get(REL_SHEET) ?? "")],
    ["让 student_sheet 把 strip_leaks 收回去（私有）",
      auditAudience(files.get(REL_AUDIENCE) ?? "",
        (files.get(REL_SHEET) ?? "").replace("def strip_leaks(", "def _strip_leaks("))],
    ["往 audience.py 里塞模型调用（complete_json）",
      auditAudience(
        `from app.services.llm import complete_json\n${files.get(REL_AUDIENCE) ?? ""}`,
        files.get(REL_SHEET) ?? "")],
    ["把公开侧 view 的默认值改成 student",
      auditWiring(mutate(REL_PUBLIC,
        "view: str = Query(default=audience_service.AUDIENCE_TEACHER",
        "view: str = Query(default=audience_service.AUDIENCE_STUDENT"))],
    ["让 public_content 不再分流（学员版也回教师版）",
      auditWiring(mutate(REL_SHARE, "if audience.is_student(audience_view):", "if False:"))],
    ["让学员版讲义无条件产出（去掉 want_student 守卫）",
      auditWiring(mutate(REL_DELIVERY,
        "        if not want_student:\n            continue\n", ""))],
    ["让交付接口丢掉 student_lectures 参数",
      auditWiring(mutate(REL_DELIVERY_ROUTE,
        "student_lectures=request.student_lectures", "student_lectures=None"))],
    ["把前端分享 API 的默认 view 改成 student",
      auditFrontend(mutate(REL_SHARE_API, 'view: AudienceView = "teacher"', 'view: AudienceView = "student"'))],
    ["摘掉分享页那个醒目标签",
      auditFrontend(mutate(REL_SHARE_VIEW, 'data-testid="audience-current"', ""))],
    ["摘掉出包页的「学员版讲义」勾选",
      auditFrontend(mutate(REL_DELIVERY_PANEL, /学员版讲义/g, "学员稿"))],
  ];
  for (const [name, red] of sourceMutations) {
    mutationChecked += 1;
    if (red.length === 0) failures.push(`自检失败：变异「${name}」后判定仍然通过`);
  }

  // 行为级变异：把 view 从 URL 里拿掉
  const mutatedApi = join(tmpDir, "shareApi-mutated.ts");
  writeFileSync(
    mutatedApi,
    (files.get(REL_SHARE_API) ?? "").replace("?view=${view}", ""),
    "utf8",
  );
  mutationChecked += 1;
  if ((await auditShareApiBehavior(mutatedApi)).length === 0) {
    failures.push("自检失败：变异「分享 API 不再带 view」后行为断言仍然通过");
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  console.error("前端「学员版」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端学员版断言（86）通过："
  + "① 后端开关默认 teacher（normalize_view / 路由 / 前端初值三处都 = 现状），UNSET_VIEW 显式声明；"
  + "② 去答案规则复用 student_sheet.strip_leaks（学案同一套）+ 正确项 ✓ 变回普通列表项"
  + "+ 习题行答案与解析**连键一起去掉**（任务 90，不留空串）；"
  + "③ ⛔ 0 LLM 源码级护栏（audience.py / student_sheet.py 里没有 complete_json / build_llm_client）；"
  + "④ 公开侧 /content?view= + 交付包「学员版讲义/」按 self_study 自动（默认八件套结构不变）；"
  + "⑤ 分享页切换 + 醒目标签 + 出包页勾选 + 交付 API 参数；"
  + `⑥ 行为级真的调用一次分享 API（默认 teacher / 学员版 student / 密码头不丢）；`
  + `${mutationChecked} 条变异自检全红（含「把去答案关掉」与「URL 不再带 view」）；`);
