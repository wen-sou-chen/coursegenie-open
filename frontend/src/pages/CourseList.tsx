import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Form, Input, InputNumber, Modal, Select, Space, Table, Typography } from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { Course, CourseScenario } from "../types";
import { k12Label } from "../lib/k12";
import AudienceField from "../components/AudienceField";
import ScenarioPicker from "../components/ScenarioPicker";

/** 删除失败时说人话：先告诉用户「现在能做什么」，原始报错只作兜底。 */
function deleteFailureMessage(err: unknown): string {
  const raw = String(err instanceof Error ? err.message : err);
  if (/Failed to fetch|NetworkError|fetch failed|连接/i.test(raw)) {
    return "网络连接失败：请确认后端还在运行，然后重试。";
  }
  if (/课程不存在|404/.test(raw)) {
    return "这门课可能已经被删除：请刷新列表后再看。";
  }
  if (/确认|400/.test(raw)) {
    return "课程名不一致：请输入完整课程名后再删除。";
  }
  if (/权限|401|403/.test(raw)) {
    return "没有删除权限：请重新登录或检查 API Key。";
  }
  if (/引用|409/.test(raw)) {
    return "这门课仍有其他数据引用，暂时不能删除；请先处理引用后再试。";
  }
  return `删除失败：${raw || "未知原因"}；请稍后重试，或查看后端日志。`;
}

export default function CourseList({ onOpen }: { onOpen: (id: string) => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Course | null>(null);
  const [confirmName, setConfirmName] = useState("");
  const [form] = Form.useForm();
  const { data: courses = [], isLoading } = useQuery({ queryKey: ["courses"], queryFn: api.listCourses });

  const create = useMutation({
    mutationFn: api.createCourse,
    onSuccess: (course) => {
      qc.invalidateQueries({ queryKey: ["courses"] });
      setOpen(false);
      form.resetFields();
      onOpen(course.id);
    },
    onError: (e) => message.error(String(e)),
  });

  const remove = useMutation({
    mutationFn: (target: Course) => api.deleteCourse(target.id, confirmName),
    onSuccess: (result, target) => {
      qc.invalidateQueries({ queryKey: ["courses"] });
      setDeleteTarget(null);
      setConfirmName("");
      message.success(`已删除《${target.title}》，清理 ${result.removed_files} 个文件`);
    },
    onError: (e) => message.error(deleteFailureMessage(e)),
  });

  /** 任务 51：表单里的 K-12 字段收进 `meta`（后端归一化后由 `Course.k12` 透出）。
   *  任务 85：使用场景同路收进 `meta.scenario`（默认不预选 ⇒ 不带就是「未指定」）。 */
  const onCreate = (values: Record<string, unknown>) => {
    const { stage, grade, textbook, volume, unit, class_hours, scenario, ...rest } = values;
    const k12 = Object.fromEntries(
      Object.entries({ stage, grade, textbook, volume, unit, class_hours })
        .filter(([, value]) => value !== undefined && value !== null && value !== ""),
    );
    const picked = scenario as CourseScenario | null | undefined;
    const meta: Record<string, unknown> = {};
    if (Object.keys(k12).length) meta.k12 = k12;
    if (picked && picked.selected?.length) meta.scenario = picked;
    create.mutate({ ...rest, meta } as Partial<Course>);
  };

  return (
    <Card title="课程项目" extra={<Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新建课程</Button>}>
      <Table<Course>
        rowKey="id"
        loading={isLoading}
        dataSource={courses}
        pagination={false}
        onRow={(r) => ({ onClick: () => onOpen(r.id), style: { cursor: "pointer" } })}
        columns={[
          { title: "标题", dataIndex: "title" },
          { title: "学科", dataIndex: "subject", width: 140 },
          { title: "受众", dataIndex: "audience", width: 180 },
          { title: "时长(分钟)", dataIndex: "duration_minutes", width: 120 },
          { title: "学段", width: 200, render: (_, r) => k12Label(r.k12) || "—" },
          { title: "状态", dataIndex: "status", width: 120 },
          {
            title: "操作",
            width: 120,
            render: (_, r) => (
              <Button
                danger
                icon={<DeleteOutlined />}
                onClick={(event) => {
                  event.stopPropagation();
                  setConfirmName("");
                  setDeleteTarget(r);
                }}
              >
                删除
              </Button>
            ),
          },
        ]}
      />
      <Modal title="新建课程" open={open} onOk={() => form.submit()} onCancel={() => setOpen(false)}>
        <Form form={form} layout="vertical" onFinish={onCreate}>
          <Form.Item name="title" label="课程标题" rules={[{ required: true }]}>
            <Input placeholder="如：光合作用入门" />
          </Form.Item>
          <Form.Item name="subject" label="学科类型"><Input placeholder="生物" /></Form.Item>
          <Form.Item name="audience" label="目标受众"><AudienceField /></Form.Item>
          <Form.Item name="duration_minutes" label="预计时长(分钟)" initialValue={60}>
            <InputNumber min={10} max={1440} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="stage" label="学段（K-12 备课用；留空=普通课程）">
            <Select allowClear placeholder="小学 / 初中 / 高中"
              options={[{ value: "小学" }, { value: "初中" }, { value: "高中" }]} />
          </Form.Item>
          <Form.Item name="grade" label="年级"><Input placeholder="如：二年级" /></Form.Item>
          <Form.Item name="textbook" label="教材版本"><Input placeholder="如：部编版" /></Form.Item>
          <Form.Item name="volume" label="册次">
            <Select allowClear options={[{ value: "上册" }, { value: "下册" }]} />
          </Form.Item>
          <Form.Item name="unit" label="单元"><Input placeholder="如：第一单元" /></Form.Item>
          <Form.Item name="class_hours" label="课时数">
            <InputNumber min={1} max={60} style={{ width: "100%" }} placeholder="如：3" />
          </Form.Item>
          <Form.Item
            name="scenario"
            label="使用场景（可多选；留空 = 未指定，按通用口径生成）"
            extra="场景只影响讲述口气与写作要求，不影响判定口径；留空与从前完全一致。"
          >
            <ScenarioPicker />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={deleteTarget ? `删除课程《${deleteTarget.title}》？` : "删除课程"}
        open={!!deleteTarget}
        okText="确认删除"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          disabled: !deleteTarget || confirmName !== deleteTarget.title,
          loading: remove.isPending,
        }}
        onOk={() => deleteTarget && remove.mutate(deleteTarget)}
        onCancel={() => {
          setDeleteTarget(null);
          setConfirmName("");
        }}
      >
        {deleteTarget && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Typography.Text>
              将一并删除：大纲与章节、讲义/教案/学案、课件（PPTX 与网页版）、配图、习题、笔记库、以及该课程在
              <Typography.Text code>storage</Typography.Text> 下的产物文件。
            </Typography.Text>
            <Typography.Text strong>删除后无法恢复。</Typography.Text>
            <Typography.Text>请输入课程全名以确认：</Typography.Text>
            <Input
              autoComplete="off"
              value={confirmName}
              onChange={(event) => setConfirmName(event.target.value)}
              placeholder={deleteTarget.title}
              status={confirmName && confirmName !== deleteTarget.title ? "error" : undefined}
            />
            {confirmName !== deleteTarget.title && (
              <Typography.Text type={confirmName ? "danger" : "secondary"} style={{ fontSize: 12 }}>
                请输入完整课程名
              </Typography.Text>
            )}
          </Space>
        )}
      </Modal>
    </Card>
  );
}
