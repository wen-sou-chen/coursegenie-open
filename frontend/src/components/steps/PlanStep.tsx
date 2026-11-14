import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Space, Tag, Typography } from "antd";
import { CheckOutlined, UndoOutlined } from "@ant-design/icons";
import { api } from "../../api";
import OutlinePanel from "../OutlinePanel";
import { ScenarioEditor } from "../ScenarioPicker";
import { scenarioOf } from "../../lib/scenarios";

export default function PlanStep({ courseId, onNext }: { courseId: string; onNext: () => void }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const { data: course } = useQuery({
    queryKey: ["course", courseId],
    queryFn: () => api.getCourse(courseId),
    retry: false,
  });
  const { data: plan } = useQuery({
    queryKey: ["plan", courseId],
    queryFn: () => api.getPlan(courseId),
    retry: false,
  });
  const approved = course?.status === "approved";
  const hasOutline = (plan?.nodes?.length ?? 0) > 0;

  const approve = useMutation({
    mutationFn: (v: boolean) => api.approvePlan(courseId, v),
    onSuccess: (r) => {
      message.success(r.status === "approved" ? "大纲已通过：后续数据图与内容都按这份大纲生成" : "已撤销通过：可继续修改大纲");
      qc.invalidateQueries({ queryKey: ["course", courseId] });
      qc.invalidateQueries({ queryKey: ["plan", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Card size="small">
        <Space wrap style={{ justifyContent: "space-between", width: "100%" }} align="center">
          <Space wrap>
            <Typography.Text strong>第 2 步 · 大纲与细纲</Typography.Text>
            {approved
              ? <Tag color="green">大纲已通过（锁定）</Tag>
              : hasOutline
                ? <Tag color="orange">已生成 · 待通过</Tag>
                : <Tag>未生成</Tag>}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {approved
                ? "锁定状态可直接进入下一步；如需改动大纲会先自动撤销通过。"
                : "逐章检查标题 / 学习目标 / 知识点（左侧点节点可修改），确认无误后点「大纲通过」。"}
            </Typography.Text>
          </Space>
          <Space>
            <Button
              type="primary"
              icon={<CheckOutlined />}
              disabled={!hasOutline || approved}
              loading={approve.isPending}
              onClick={() => approve.mutate(true)}
            >
              大纲通过
            </Button>
            <Button icon={<UndoOutlined />} disabled={!approved} onClick={() => approve.mutate(false)}>
              撤销通过
            </Button>
            <Button type="primary" ghost disabled={!approved} onClick={onNext}>
              下一步：章节制作
            </Button>
          </Space>
        </Space>
      </Card>
      {/* 任务 85：大纲页可直接改使用场景（与课程详情页共用同一个组件） */}
      <Card size="small" data-testid="scenario-editor-card">
        <Space direction="vertical" size={4} style={{ width: "100%" }}>
          <Typography.Text strong>使用场景（选中即生效，可随时改；留空 = 未指定）</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            场景只改讲述口气与写作要求（大纲 / 讲义 / 口播稿），不影响判定口径；改了场景后重生成即可生效。
          </Typography.Text>
          <ScenarioEditor courseId={courseId} value={scenarioOf(course)} />
        </Space>
      </Card>
      {hasOutline && !approved && (
        <Alert
          type="warning"
          showIcon
          message="大纲尚未通过"
          description="未通过前不能自动生成章节数据图与内容；把大纲改到满意后点上方「大纲通过」锁定。"
        />
      )}
      <OutlinePanel courseId={courseId} />
    </Space>
  );
}
