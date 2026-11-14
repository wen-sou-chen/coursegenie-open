import { useEffect, useState } from "react";
import { Layout, Menu, Spin, Typography } from "antd";
import { BookOutlined, DashboardOutlined, LogoutOutlined } from "@ant-design/icons";
import CourseList from "./pages/CourseList";
import CourseDetail from "./pages/CourseDetail";
import ShareView from "./pages/ShareView";
import LoginPage from "./pages/LoginPage";
import DemoNotice from "./pages/DemoNotice";
import { api } from "./api";
import { clearApiKey, getApiKey, isDemoBuild, setUnauthorizedHandler } from "./auth";

const { Header, Content } = Layout;

/** /share/{token} 只读分享页：现有 SPA 没有路由库，按 location.pathname 判断即可。 */
function readShareToken(): string | null {
  const match = window.location.pathname.match(/^\/share\/([^/]+)\/?$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

/**
 * 任务 48u：入口闸门（四档）。
 * - checking：启动先问一次 `/api/health`（这个接口不要 key），再决定进哪一档；
 * - demo：只读演示模式（后端 DEMO_READONLY=true 或 VITE_DEMO=1 构建）⇒ 只给分享页；
 * - login：后端 AUTH_ENABLED=true 且本地没 key ⇒ 先去登录页换 key；
 * - ready：单机默认档位，行为与改造前一致。
 * 分享页 `/share/{token}` 不受闸门影响（免登录）。其余路径在 demo 档渲染只读提示页。
 */
type Gate = "checking" | "demo" | "login" | "ready";

export default function App() {
  const [courseId, setCourseId] = useState<string | null>(null);
  const [authOn, setAuthOn] = useState(false);
  const [gate, setGate] = useState<Gate>(() => (isDemoBuild() ? "demo" : "checking"));
  const shareToken = readShareToken();

  // key 失效（401）⇒ 清 key 回登录页；分享页的请求不带 key，不会被带进来
  useEffect(() => {
    setUnauthorizedHandler(() => setGate((prev) => (prev === "demo" ? prev : "login")));
    return () => setUnauthorizedHandler(null);
  }, []);

  // 健康检查只用来选档位：后端没起来时照旧进工作台（错误由各面板自己提示）
  useEffect(() => {
    if (shareToken || isDemoBuild()) return;
    let alive = true;
    api
      .health()
      .then((h) => {
        if (!alive) return;
        setAuthOn(Boolean(h.auth_enabled));
        if (h.demo_readonly) setGate("demo");
        else setGate(h.auth_enabled && !getApiKey() ? "login" : "ready");
      })
      .catch(() => {
        if (alive) setGate("ready");
      });
    return () => {
      alive = false;
    };
  }, [shareToken]);

  // 分享页免登录：它只走 /api/public/*，带管理 key 反而是把凭据送到公开页
  if (shareToken) {
    return <ShareView token={shareToken} />;
  }

  if (gate === "demo") {
    return <DemoNotice />;
  }

  if (gate === "checking") {
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          gap: 12,
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Spin size="large" />
        <Typography.Text type="secondary">正在检查服务状态…</Typography.Text>
      </div>
    );
  }

  if (gate === "login") {
    return <LoginPage onDone={() => setGate("ready")} />;
  }

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <Typography.Title level={4} style={{ color: "#fff", margin: 0 }}>
          CourseGenie
        </Typography.Title>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[courseId ? "detail" : "list"]}
          items={[
            { key: "list", icon: <DashboardOutlined />, label: "课程列表", onClick: () => setCourseId(null) },
            ...(courseId ? [{ key: "detail", icon: <BookOutlined />, label: "课程工作台" }] : []),
            ...(authOn
              ? [
                  {
                    key: "logout",
                    icon: <LogoutOutlined />,
                    label: "退出登录",
                    onClick: () => {
                      clearApiKey();
                      setGate("login");
                    },
                  },
                ]
              : []),
          ]}
          style={{ flex: 1, minWidth: 300 }}
        />
      </Header>
      <Content style={{ padding: 24 }}>
        <div style={{ maxWidth: 1200, margin: "0 auto" }}>
          {courseId ? (
            <CourseDetail courseId={courseId} onBack={() => setCourseId(null)} />
          ) : (
            <CourseList onOpen={(id) => setCourseId(id)} />
          )}
        </div>
      </Content>
    </Layout>
  );
}
