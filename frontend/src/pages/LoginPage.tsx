/** 任务 48u（A 方案）：登录页 —— 只在后端 `AUTH_ENABLED=true` 时出现。
 *
 * 两条路：① 账号密码（`POST /api/auth/login` 换 api_key，可先注册）；
 * ② 直接贴 API Key（运营自己用 DEV_API_KEY 时最快）。
 * 拿到的 key 交给 `auth.ts` 存 localStorage，之后所有请求自动带 `X-API-Key`。
 */
import { useState } from "react";
import { Alert, Button, Card, Form, Input, Tabs, Typography } from "antd";
import { api } from "../api";
import { setApiKey } from "../auth";

type Props = { onDone: () => void };

export default function LoginPage({ onDone }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 成功不弹 toast：拿到 key 直接切进工作台（少一处 antd 静态 message 调用）
  const enter = (key: string) => {
    setApiKey(key);
    onDone();
  };

  const login = async (values: { username: string; password: string }) => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(values);
      enter(res.api_key);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#F5F7FA" }}>
      <Card style={{ width: 420 }}>
        <Typography.Title level={4} style={{ marginTop: 0 }}>CourseGenie · 登录</Typography.Title>
        <Typography.Paragraph type="secondary">
          这台服务开了鉴权（<Typography.Text code>AUTH_ENABLED=true</Typography.Text>）。
          只读分享页 <Typography.Text code>/share/&lt;token&gt;</Typography.Text> 不需要登录。
        </Typography.Paragraph>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
        <Tabs
          items={[
            {
              key: "password",
              label: "账号密码",
              children: (
                <Form layout="vertical" onFinish={login} disabled={busy}>
                  <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
                    <Input autoComplete="username" placeholder="至少 2 个字符" />
                  </Form.Item>
                  <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
                    <Input.Password autoComplete="current-password" placeholder="至少 6 个字符" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" loading={busy} block>登录</Button>
                </Form>
              ),
            },
            {
              key: "key",
              label: "用 API Key",
              children: (
                <Form layout="vertical" onFinish={(v: { apiKey: string }) => enter(v.apiKey)}>
                  <Form.Item name="apiKey" label="API Key" rules={[{ required: true, message: "请粘贴 API Key" }]}>
                    <Input.Password placeholder="DEV_API_KEY 或注册用户拿到的 key" />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" block>进入工作台</Button>
                </Form>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
