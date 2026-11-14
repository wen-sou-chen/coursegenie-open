import { authHeader, getApiKey, handleUnauthorized } from "./auth";

/** 触发浏览器下载：同源 URL 直接 <a download> 点击即可。 */
export function triggerDownload(url: string, filename?: string): void {
  const a = document.createElement("a");
  a.href = url;
  if (filename) a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/**
 * 任务 48u：带 X-API-Key 的下载。
 *
 * 交付包 zip 走 `/api/delivery/{id}/download`（要鉴权），而 `<a href>` 这类浏览器
 * 直接下载**不带自定义 header** ⇒ 开了 AUTH_ENABLED 会 401。这里先 fetch 再落盘。
 * 没 key（单机默认）时行为与 `triggerDownload` 完全一致。
 */
export async function triggerAuthedDownload(url: string, filename?: string): Promise<void> {
  if (!getApiKey()) {
    triggerDownload(url, filename);
    return;
  }
  const resp = await fetch(url, { headers: authHeader() });
  if (resp.status === 401) {
    handleUnauthorized();
    throw new Error("登录已失效，请重新登录后再下载");
  }
  if (!resp.ok) throw new Error(`下载失败（HTTP ${resp.status}）`);
  const href = URL.createObjectURL(await resp.blob());
  triggerDownload(href, filename);
  window.setTimeout(() => URL.revokeObjectURL(href), 60000);
}
