/**
 * 任务 48u 前端口径断言：对外演示的安全前置不许静默回退。
 *
 * 起因（DSH 2026-09-17 查证）：后端早就支持 `AUTH_ENABLED` + `X-API-Key`，但
 * 前端**一个 key 都不带**、也没有登录页 ⇒ 开了鉴权整个网页就 401 用不了，
 * 不开鉴权又不能穿出去（谁拿到链接都能花你的 LLM 额度）。本轮补两条路：
 * A 方案登录带 key，B 方案只读演示模式。
 *
 * 这里不启 DOM，只做源码级形状断言（与 check-delivery-mode.ts 同一套路），
 * 防「改了 api.ts 忘了 deliveryApi.ts」「登录页把 key 又塞进 URL」
 * 「公开分享侧被顺手带上管理 key」这类静默回归。
 *
 * 运行：`node scripts/check-auth-header.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p: string) => readFileSync(p, "utf8").replace(/\r+\n/g, "\n");   // App.tsx 等文件历史上是 CRLF
const src = (rel: string) => read(join(here, "..", "src", rel));
const repoFile = (rel: string) => read(join(here, "..", "..", rel));

const failures: string[] = [];

// ---------------------------------------------------------------- A 方案：登录 + 带 key
const auth = src("auth.ts");
for (const fn of ["getApiKey", "setApiKey", "clearApiKey", "authHeader", "handleUnauthorized", "isDemoBuild"]) {
  if (!auth.includes(`export function ${fn}`)) failures.push(`auth.ts 缺 ${fn}()（登录态没地方存/没地方取）`);
}
if (!auth.includes('localStorage')) failures.push("auth.ts 的 key 不再存 localStorage");
if (/window\.location\.(search|href)\s*=|URLSearchParams/.test(auth)) {
  failures.push("auth.ts 把 key 写进了 URL（URL 会进访问日志与浏览器历史，绝不许）");
}

const api = src("api.ts");
if (!api.includes("...authHeader()")) failures.push("api.ts 的 request() 没带 X-API-Key（开了鉴权整个工作台 401）");
if (!/resp\.status === 401\)\s*handleUnauthorized\(\)/.test(api)) {
  failures.push("api.ts 没在 401 时清 key 回登录页（key 被撤后会一直白屏报错）");
}
if (!/health: \(\) => request<HealthPayload>\("\/health"\)/.test(api)) failures.push("api.ts 缺 health()（前端无法判断要不要登录/演示模式）");
if (!/login: \(data: \{ username: string; password: string \}\)/.test(api)) failures.push("api.ts 缺 login()（登录页换不到 key）");
if (!api.includes("auth_enabled") || !api.includes("demo_readonly")) failures.push("HealthPayload 没解析两个开关");

const deliveryApi = src("deliveryApi.ts");
if (!deliveryApi.includes("...authHeader()")) failures.push("deliveryApi.ts 没带 X-API-Key（交付包接口在鉴权下不可用）");
if (!deliveryApi.includes("handleUnauthorized()")) failures.push("deliveryApi.ts 没处理 401");

const downloads = src("downloadUtils.ts");
if (!downloads.includes("export async function triggerAuthedDownload")) failures.push("downloadUtils.ts 缺 triggerAuthedDownload()");
if (!/if \(!getApiKey\(\)\) \{\s*triggerDownload\(url, filename\);\s*return;\s*\}/.test(downloads)) {
  failures.push("triggerAuthedDownload 在「没有 key」时没有退回 triggerDownload（单机档位会变）");
}

const delivery = src("components/DeliveryPanel.tsx");
if (!delivery.includes("triggerAuthedDownload(deliveryApi.zipUrl(")) failures.push("交付包面板下载 zip 没走带 key 的下载");
if (/triggerDownload\(deliveryApi\.zipUrl\(/.test(delivery)) failures.push("交付包面板又回到 <a href> 下载（鉴权下 401）");

// ---------------------------------------------------------------- 公开分享侧绝不带 key
const shareApi = src("shareApi.ts");
if (/from "\.\/auth"/.test(shareApi) || /X-API-Key"\s*:/.test(shareApi.replace(/绝不允许带 X-API-Key/g, ""))) {
  failures.push("shareApi.ts 的公开侧带上了 X-API-Key（等于把管理凭据发给公开访客）");
}

// ---------------------------------------------------------------- B 方案：只读演示模式 + 闸门接线
const app = src("App.tsx");
if (!app.includes("isDemoBuild()")) failures.push("App.tsx 没接 VITE_DEMO 演示构建开关");
if (!app.includes("api\n      .health()") && !app.includes("api.health()")) failures.push("App.tsx 没查 /api/health");
if (!app.includes("h.demo_readonly")) failures.push("App.tsx 没按 demo_readonly 落只读提示页");
if (!app.includes("h.auth_enabled && !getApiKey()")) failures.push("App.tsx 没按 auth_enabled 决定要不要先登录");
if (!app.includes("<DemoNotice />")) failures.push("App.tsx 缺演示模式兜底页（工作台路由应落到只读提示）");
if (!app.includes("<LoginPage onDone=")) failures.push("App.tsx 缺登录页接线");
if (!app.includes("clearApiKey();")) failures.push("App.tsx 没有「退出登录」（清 key 回登录页）");
if (!app.includes("if (shareToken || isDemoBuild()) return;")) {
  failures.push("App.tsx 的健康检查没跳过分享页（分享页免登录会白等一次请求）");
}
const shareIdx = app.indexOf("if (shareToken) {");
const demoIdx = app.indexOf('if (gate === "demo")');
const loginIdx = app.indexOf('if (gate === "login")');
if (shareIdx < 0 || demoIdx < 0 || loginIdx < 0 || !(shareIdx < demoIdx && shareIdx < loginIdx)) {
  failures.push("App.tsx 的分享页分支不在闸门之前（演示/登录会挡住 /share/{token}）");
}

// ---------------------------------------------------------------- 不许把 VITE_API_KEY 当安全手段
const codeOnly = (text: string) =>
  text
    .split("\n")
    .filter((line) => !/^\s*(\*|\/\/|\/\*)/.test(line))
    .join("\n");   // 注释里写「不要用 VITE_API_KEY」不算违规
for (const [name, text] of Object.entries({ auth, api, deliveryApi, downloads, app })) {
  if (codeOnly(text).includes("VITE_API_KEY")) {
    failures.push(`${name} 用了 VITE_API_KEY（打包进 JS 的 key 只防君子，任务书 §D-2 明令禁止）`);
  }
}

// ---------------------------------------------------------------- 后端开关（B 方案兜底）
const config = repoFile(join("backend", "app", "core", "config.py"));
if (!config.includes("demo_readonly: bool = False")) failures.push("后端缺 demo_readonly 开关");
const main = repoFile(join("backend", "app", "main.py"));
if (!main.includes("demo_readonly_guard")) failures.push("后端缺 demo_readonly 中间件（反代配错就没人兜底）");
if (!main.includes('"demo_readonly": settings.demo_readonly')) failures.push("/api/health 没下发 demo_readonly（前端猜不到档位）");

if (failures.length) {
  console.error("[FAIL] 任务 48u 口径断言未通过：");
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log("[PASS] 任务 48u 口径断言：登录带 X-API-Key / 401 回登录页 / 分享页免登录 / 演示模式只留只读页");