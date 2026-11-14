import { useEffect, useState, type ReactNode } from "react";
import { App, Button, Card, Dropdown, Segmented, Space, Steps, Tabs, Tag, Tooltip, Typography } from "antd";
import { ArrowLeftOutlined, DownloadOutlined, SaveOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { triggerDownload } from "../downloadUtils";
import { manualSync, subscribeSync } from "../syncNotes";
import type { SyncState } from "../syncNotes";
import { k12Label } from "../lib/k12";
import { scenarioLine, scenarioOf } from "../lib/scenarios";
import { ScenarioEditor } from "../components/ScenarioPicker";
import OutlinePanel from "../components/OutlinePanel";
import ContentPanel from "../components/ContentPanel";
import KbPanel from "../components/KbPanel";
import PptPanel from "../components/PptPanel";
import QualityPanel from "../components/QualityPanel";
import UsagePanel from "../components/UsagePanel";
import VersionsPanel from "../components/VersionsPanel";
import AnalyticsPanel from "../components/AnalyticsPanel";
import SharePanel from "../components/SharePanel";
import DeliveryPanel from "../components/DeliveryPanel";
import ErrorBoundary from "../components/ui/ErrorBoundary";
import MaterialsStep from "../components/steps/MaterialsStep";
import PlanStep from "../components/steps/PlanStep";
import WorkbenchStep from "../components/steps/WorkbenchStep";
import PreviewStep from "../components/steps/PreviewStep";
import NoteStep from "../components/steps/NoteStep";

function formatWhen(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function CourseDetail({ courseId, onBack }: { courseId: string; onBack: () => void }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const [step, setStep] = useState(0);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [view, setView] = useState<"flow" | "tools">("flow");
  const [sync, setSync] = useState<SyncState>({ state: "idle" });
  const [saving, setSaving] = useState(false);

  const { data: course } = useQuery({ queryKey: ["course", courseId], queryFn: () => api.getCourse(courseId) });
  const { data: materials = [] } = useQuery({ queryKey: ["materials", courseId], queryFn: () => api.listMaterials(courseId) });
  const { data: plan } = useQuery({ queryKey: ["plan", courseId], queryFn: () => api.getPlan(courseId), retry: false });
  const { data: content = [] } = useQuery({ queryKey: ["content", courseId], queryFn: () => api.listContent(courseId) });
  const { data: kb } = useQuery({ queryKey: ["kb", courseId], queryFn: () => api.kbInfo(courseId), retry: false });

  useEffect(
    () =>
      subscribeSync((ev) => {
        if (ev.courseId === courseId) setSync(ev.state);
      }),
    [courseId],
  );

  const approved = course?.status === "approved";
  const hasOutline = (plan?.nodes?.length ?? 0) > 0;
  const hasContent = content.length > 0;
  // 7e：标题旁的同步状态读后端真实导出记录（exported_at=整课导出 / synced_at=增量同步）
  const kbMeta = (kb?.kb ?? {}) as { manifest?: unknown; exported_at?: string; synced_at?: string };
  const exported = Boolean(kbMeta.manifest);
  const lastExportAt = kbMeta.exported_at ?? kbMeta.synced_at;

  const stepStatus = (index: number): "wait" | "process" | "finish" | "error" => {
    if (step > index) return "finish";
    if (step === index) return "process";
    return "wait";
  };

  const handleSaveToNotes = async () => {
    setSaving(true);
    const r = await manualSync(courseId);
    setSaving(false);
    if (r.ok) message.success(r.msg);
    else message.error(r.msg);
  };

  const handleExportZip = async (scope: "all" | "nodes", nodeIds: string[]) => {
    const key = "export-zip";
    message.loading({ content: scope === "all" ? "正在打包整课笔记 ZIP…" : "正在打包所选知识点 ZIP…", key, duration: 0 });
    try {
      const res = await api.exportNotesZip({ course_id: courseId, scope, node_ids: nodeIds });
      message.success({ content: "打包完成，开始下载…", key, duration: 2 });
      triggerDownload(res.url, res.file);
    } catch (e) {
      message.error({ content: String(e), key, duration: 4 });
    }
  };

  const stepItems = [
    // 标题里不再写「①…⑤」：antd 的 Steps 自己会画序号，两处都写就成了「4④ 课程预览」（任务 68）
    { title: "原始资料", description: materials.length ? `${materials.length} 份已就绪` : "上传/粘贴备课素材", status: materials.length ? "finish" : stepStatus(0) },
    { title: "大纲通过", description: approved ? "已锁定" : hasOutline ? "已生成待通过" : "生成并确认大纲", status: approved ? "finish" : stepStatus(1) },
    { title: "章节制作", description: hasContent ? "讲义/正文/数据图" : "逐节做讲义与数据图", status: hasContent ? "finish" : stepStatus(2) },
    { title: "课程预览", description: "整课覆盖检查", status: stepStatus(3) },
    { title: "存入笔记", description: exported ? "已生成过笔记" : "导出 Obsidian", status: exported ? "finish" : stepStatus(4) },
  ] as const;

  // 任务 48x：任何一个面板崩溃都只坏一格，不再整页白掉
  // （此前 UsagePanel 抛 TypeError → React 卸载整棵树 ⇒ body 字数 0）。
  const panel = (name: string, node: ReactNode) => (
    <ErrorBoundary name={name}>{node}</ErrorBoundary>
  );

  return (
    <div>
      <Space style={{ justifyContent: "space-between", width: "100%", marginBottom: 12 }} wrap>
        <Space wrap>
          <a onClick={onBack} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <ArrowLeftOutlined /> 返回课程列表
          </a>
          <Typography.Title level={4} style={{ margin: 0 }}>{course?.title ?? "课程工作台"}</Typography.Title>
          {approved ? <Tag color="green">大纲已通过</Tag> : <Tag color="orange">大纲待通过</Tag>}
          {k12Label(course?.k12) ? <Tag color="blue">K-12 · {k12Label(course?.k12)}</Tag> : null}
          {/* 任务 85 §B3：一直显示这次按什么场景生成（未选 ⇒ 「未指定场景（按通用口径生成）」） */}
          <Tooltip title="点这里可以改：场景只影响讲述口气与写作要求，不影响判定口径">
            <Tag color={scenarioOf(course) ? "geekblue" : "default"} data-testid="course-scenario-tag">
              {scenarioLine(course)}
            </Tag>
          </Tooltip>
        </Space>
        <Space wrap>
          <Dropdown.Button
            type="primary"
            icon={<DownloadOutlined />}
            loading={saving}
            onClick={() => handleExportZip("all", [])}
            menu={{
              items: [
                {
                  key: "node",
                  label: selectedNodeId ? "导出当前知识点 ZIP" : "导出当前知识点 ZIP（先在第 3 步选中知识点）",
                  disabled: !selectedNodeId,
                  onClick: () => selectedNodeId && handleExportZip("nodes", [selectedNodeId]),
                },
              ],
            }}
          >
            导出 ZIP
          </Dropdown.Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSaveToNotes}>
            保存到 Obsidian 笔记
          </Button>
          <Tag
            color={sync.state === "synced" || (sync.state === "idle" && exported) ? "green" : sync.state === "error" ? "red" : sync.state === "syncing" ? "processing" : "default"}
          >
            {sync.state === "syncing" ? "同步中…"
              : sync.state === "synced" ? `上次同步 ${sync.time}`
              : sync.state === "error" ? "同步失败"
              : exported ? (formatWhen(lastExportAt) ? `已导出 ${formatWhen(lastExportAt)}` : "已导出（时间未知）")
              : "尚未导出"}
          </Tag>
          <Segmented
            value={view}
            onChange={(v) => setView(v as "flow" | "tools")}
            options={[
              { value: "flow", label: "分步制作（推荐）" },
              { value: "tools", label: "高级工具" },
            ]}
          />
        </Space>
      </Space>

      {view === "flow" ? (
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Card size="small" data-testid="scenario-editor-card">
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              <Typography.Text strong>使用场景（选中即生效，可随时改；留空 = 未指定）</Typography.Text>
              <ScenarioEditor courseId={courseId} value={scenarioOf(course)} />
            </Space>
          </Card>
          <Steps
            current={step}
            onChange={setStep}
            responsive
            items={stepItems.map((s) => ({ title: s.title, description: s.description, status: s.status as "wait" | "process" | "finish" | "error" }))}
          />
          {step === 0 && <MaterialsStep courseId={courseId} onNext={() => setStep(1)} />}
          {step === 1 && <PlanStep courseId={courseId} onNext={() => setStep(2)} />}
          {step === 2 && (
            <WorkbenchStep
              courseId={courseId}
              selectedNodeId={selectedNodeId}
              onSelectNode={setSelectedNodeId}
              onNext={() => setStep(3)}
            />
          )}
          {step === 3 && (
            <PreviewStep
              courseId={courseId}
              onOpenChapter={(id) => { setSelectedNodeId(id); setStep(2); }}
              onNext={() => setStep(4)}
            />
          )}
          {step === 4 && <NoteStep courseId={courseId} />}
        </Space>
      ) : (
        <Tabs
          items={[
            { key: "kb", label: "素材与笔记", children: panel("素材与笔记", <KbPanel courseId={courseId} />) },
            { key: "outline", label: "大纲规划", children: panel("大纲规划", <OutlinePanel courseId={courseId} />) },
            { key: "content", label: "内容生成", children: panel("内容生成", <ContentPanel courseId={courseId} />) },
            { key: "ppt", label: "PPT", children: panel("PPT", <PptPanel courseId={courseId} />) },
            { key: "quality", label: "质量报告", children: panel("质量报告", <QualityPanel courseId={courseId} />) },
            { key: "usage", label: "用量与成本", children: panel("用量与成本", <UsagePanel courseId={courseId} />) },
            { key: "share", label: "分享", children: panel("分享", <SharePanel courseId={courseId} />) },
            { key: "delivery", label: "交付包", children: panel("交付包", <DeliveryPanel courseId={courseId} />) },
            { key: "versions", label: "版本", children: panel("版本", <VersionsPanel courseId={courseId} />) },
            { key: "analytics", label: "数据分析", children: panel("数据分析", <AnalyticsPanel courseId={courseId} />) },
          ]}
        />
      )}
    </div>
  );
}
