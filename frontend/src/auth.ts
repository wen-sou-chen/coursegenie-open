/**
 * 任务 48u：对外演示的安全前置（A 方案）—— 前端登录 + 请求带 X-API-Key。
 *
 * 约定（照任务书 §B-A）：
 * - **有 key 才带**：本地单机 `AUTH_ENABLED=false` 时保持免登录可用；
 * - **公开分享侧绝不带**：`shareApi.ts` 不走这里（public 路由本来就免登录，
 *   带 key 反而把管理凭据送到公开页面上）；
 * - 任何接口回 401 ⇒ 清掉 key 并通知界面回登录页（key 撤了/换了不用手动清）；
 * - key 只进 localStorage，**不进 URL**（URL 里的 key 会写进访问日志和浏览器历史）。
 *
 * ⚠️ 不要用 `VITE_API_KEY` 这类打包进 JS 的变量当安全手段（任务书 §D-2）：
 *   任何人打开 devtools 都能拿到，那只防君子。要真防走登录（本文件）或演示模式。
 */
const STORAGE_KEY = "coursegenie.api_key";

let unauthorizedHandler: (() => void) | null = null;

export function getApiKey(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || null;
  } catch {
    return null;   // 隐私模式等 localStorage 不可用 ⇒ 退化成免登录
  }
}

export function setApiKey(key: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, key.trim());
  } catch {
    // 存不进去也不该让登录流程崩：本次会话仍可用
  }
}

export function clearApiKey(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 忽略
  }
}

/** 有 key 才带；无 key 返回空对象（直接展开进 headers）。 */
export function authHeader(): Record<string, string> {
  const key = getApiKey();
  return key ? { "X-API-Key": key } : {};
}

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

/** 401：key 失效或没带 ⇒ 清掉并让界面回到登录页。 */
export function handleUnauthorized(): void {
  clearApiKey();
  unauthorizedHandler?.();
}

/** 演示构建开关：`VITE_DEMO=1` 打包出来的前端只注册只读分享页（B 方案）。 */
export function isDemoBuild(): boolean {
  return import.meta.env?.VITE_DEMO === "1";
}
