/** 任务 48u（B 方案）：只读演示模式下的兜底页。
 *
 * 演示模式（后端 `DEMO_READONLY=true` 或前端 `VITE_DEMO=1` 构建）只注册
 * `/share/{token}` 分享页；工作台路由不注册，直接访问就落到这里（返回只读页，
 * 不返回 404 —— 免得访客以为是坏链）。
 */
import { Button, Result, Typography } from "antd";

export default function DemoNotice() {
  const shareHint = window.location.origin + "/share/<token>";
  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <Result
        status="info"
        title="只读演示模式"
        subTitle="这台服务只开放「分享页」给学生/客户看，工作台（生成、导出、交付包）已关闭。"
        extra={
          <Typography.Paragraph type="secondary" style={{ maxWidth: 520, margin: "0 auto" }}>
            怎么看课程：请用分享链接访问 <Typography.Text code>{shareHint}</Typography.Text>
            。<br />
            需要工作台？在单机上关掉演示模式（<Typography.Text code>DEMO_READONLY=false</Typography.Text>）
            重新启动后端即可。
            <div style={{ marginTop: 12 }}>
              <Button href="/api/health" target="_blank" rel="noreferrer">看看服务状态</Button>
            </div>
          </Typography.Paragraph>
        }
      />
    </div>
  );
}
