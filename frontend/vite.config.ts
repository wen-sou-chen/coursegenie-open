import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 任务 48u：临时验收 / 对外 Demo 时可用 VITE_PROXY_TARGET 把代理指到别的后端
// （例如临时起在 8091 的「开了鉴权」后端）；不设时仍是本机 8000，单机用法一字不变。
const apiTarget = process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": apiTarget,
      "/exports": apiTarget,
      "/images": apiTarget,
      "/ppt-files": apiTarget,
    },
  },
});
