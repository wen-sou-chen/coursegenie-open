import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "antd/dist/reset.css";
import "@xyflow/react/dist/style.css";
import "./app.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#4F46E5",
          colorSuccess: "#10B981",
          colorWarning: "#F59E0B",
          colorError: "#EF4444",
          borderRadius: 8,
          borderRadiusLG: 12,
          fontSize: 14,
          colorBgLayout: "#F5F7FA",
          colorTextBase: "#1F2329",
          colorTextSecondary: "#646A73",
        },
        components: {
          Card: {
            boxShadowTertiary: "0 1px 3px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.04)",
          },
          Button: {
            controlHeight: 36,
          },
        },
      }}
    >
      {/* 任务 48t：antd 的 <App> 提供 message/notification/modal 的 context 实例，
          面板用 App.useApp() 取 —— 静态 message.* 会报「Static function can not consume context」。 */}
      <AntdApp>
        <QueryClientProvider client={queryClient}>
          <App />
        </QueryClientProvider>
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
);
