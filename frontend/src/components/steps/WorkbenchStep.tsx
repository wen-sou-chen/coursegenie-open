import { useMemo, useState } from "react";
import SectionCard from "../ui/SectionCard";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Button, Collapse, Drawer, Empty, Form, Grid, Input, List, Modal, Popconfirm, Select,
  Space, Spin, Tag, Tooltip, Tree, Typography, Upload,
} from "antd";
import {
  CheckCircleOutlined, CodeOutlined, EditOutlined, ExperimentOutlined, FileAddOutlined,
  FileTextOutlined, LockOutlined, MenuUnfoldOutlined, PictureOutlined,
  PlusOutlined, QuestionCircleOutlined, ReadOutlined, ReloadOutlined, ShareAltOutlined, UploadOutlined,
} from "@ant-design/icons";
import { api } from "../../api";
import { requestAutoSync } from "../../syncNotes";
import { preservedReason } from "../../preserved";
import FlowDiagram from "../FlowDiagram";
import ChartSection from "./ChartSection";
import PptPanel from "../PptPanel";
import ConceptPanel from "../ConceptPanel";
import StyleAnchorPanel from "../StyleAnchorPanel";
import VaultStatusTag from "../VaultStatusTag";
import DiagramFailureTag from "../DiagramFailureTag";
import QcBadge from "../QcBadge";
import UsageBadge, { itemUsage } from "../UsageBadge";
import MdWithFlow from "../MdWithFlow";
import { jumpToText } from "../../lib/jumpToText";
import ScenarioBadge from "../ScenarioBadge";
import { LECTURE_COST_ONE } from "../../lib/genCost";
import { sameScenario, scenarioLine, scenarioOf, usedScenarioOf } from "../../lib/scenarios";
import {
  FIGURE_TERM, PLAIN_TEXT_BUTTON_LABEL, PLAIN_TEXT_BUTTON_TIP, PLAIN_TEXT_DONE,
} from "../../lib/figureTerms";

/** 任务 91：重跑确认框文案——已锁定的一节必须说清「会覆盖并摘徽标」。 */
function overwriteConfirmTitle(locked: boolean): string {
  return (locked
    ? `这一节已审核通过并锁定：重新生成会覆盖老师审过的那一版，并摘除「审核通过」徽标（新版需重新审核）。\n预估花费 ${LECTURE_COST_ONE}（1 篇）。确定覆盖？`
    : `重新生成会覆盖当前讲义全文（旧版自动进历史），确定继续？\n预估花费 ${LECTURE_COST_ONE}（1 篇）。`);
}

/** 讲义正文容器的 id（质检里「跳到正文」靠它定位）。 */
const LECTURE_BODY_ID = "cg-lecture-body";
import { useDiagramMaps } from "../../diagramMaps";
import { flowchartCount, mermaidCountOfRow } from "../../lib/contentCounts";
import type { ContentRow, PlanNode } from "../../types";
function imgSrc(row: ContentRow): string {
  const path = String(row.body.path ?? "");
  return path ? `/${path}` : "";
}
type ImgModalState = {
  prompt: string;
  size: string;
  itemId?: string;
  text_lang: string;
  overlay_text: string;
  overlay_pos: string;
  model: string;
};
const IMG_TEXT_LANG_OPTIONS = [
  { value: "none", label: "无文字（默认，推荐）" },
  { value: "zh", label: "中文（生成后本地叠加，保证不错字）" },
  { value: "en", label: "英文（生成后本地叠加）" },
];
const IMG_MODEL_OPTIONS = [
  { value: "", label: "默认（后端 IMAGE_MODEL 设置）" },
  { value: "google/gemini-3-pro-image", label: "高质量图文模型（更贵 · 中文更准）" },
];
type TreeNodeRec = { node: PlanNode; children: TreeNodeRec[] };
function treeOf(nodes: PlanNode[]): TreeNodeRec[] {
  const store = new Map<string, TreeNodeRec>();
  const roots: TreeNodeRec[] = [];
  for (const n of nodes) {
    const item: TreeNodeRec = { node: n, children: [] };
    store.set(n.key, item);
    const dot = n.key.lastIndexOf(".");
    const parentKey = dot > 0 ? n.key.slice(0, dot) : "";
    const parent = parentKey ? store.get(parentKey) : undefined;
    if (parent) parent.children.push(item);
    else roots.push(item);
  }
  return roots;
}
const KIND_LABEL: Record<string, string> = {
  lecture: "讲义",
  text: "正文",
  quiz: "习题",
  image: FIGURE_TERM.image,
  flowchart: "流程",
  chart: FIGURE_TERM.chart,
  diagram: FIGURE_TERM.diagram,
};
export default function WorkbenchStep({
  courseId, selectedNodeId, onSelectNode, onNext,
}: { courseId: string; selectedNodeId: string | null; onSelectNode: (id: string) => void; onNext: () => void }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editForm] = Form.useForm();
  const { data: course, isLoading: courseLoading } = useQuery({ queryKey: ["course", courseId], queryFn: () => api.getCourse(courseId), retry: false });
  const { data: plan, isLoading: planLoading } = useQuery({ queryKey: ["plan", courseId], queryFn: () => api.getPlan(courseId), retry: false });
  const { data: rows = [] } = useQuery({
    queryKey: ["content", courseId],
    queryFn: () => api.listContent(courseId),
  });
  const nodes = plan?.nodes ?? [];
  const approved = course?.status === "approved";
  const effectiveNode = nodes.find((n) => n.id === selectedNodeId) ?? nodes.find((n) => !n.key.includes(".")) ?? nodes[0];
  const [editingNode, setEditingNode] = useState<PlanNode | null>(null);
  const [textEdit, setTextEdit] = useState<ContentRow | null>(null);
  const [textDraft, setTextDraft] = useState("");
  const [quizEdit, setQuizEdit] = useState<ContentRow | null>(null);
  const [quizDraft, setQuizDraft] = useState({ question: "", options: "", answer: "", explanation: "" });
  const [flowView, setFlowView] = useState<ContentRow | null>(null);
  const [imgModal, setImgModal] = useState<ImgModalState | null>(null);
  const [imgPreview, setImgPreview] = useState<ContentRow | null>(null);
  const [flowModal, setFlowModal] = useState<{ description: string; kind: string } | null>(null);
  const [mermaidModal, setMermaidModal] = useState<{ title: string; mermaid: string } | null>(null);
  const [lectureEdit, setLectureEdit] = useState<ContentRow | null>(null);
  const [lectureDraft, setLectureDraft] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["content", courseId] });
  const descendants = useMemo(() => {
    if (!effectiveNode) return [];
    return nodes.filter((n) => n.id === effectiveNode.id || n.key.startsWith(`${effectiveNode.key}.`));
  }, [nodes, effectiveNode]);
  const nodeIds = effectiveNode ? descendants.map((n) => n.id) : [];
  const counts = useMemo(() => {
    const map = new Map<string, Record<string, number>>();
    for (const n of nodes) map.set(n.id, { lecture: 0, text: 0, quiz: 0, image: 0, flowchart: 0, chart: 0 });
    for (const r of rows) {
      if (!r.node_id) continue;
      const c = map.get(r.node_id);
      if (c) {
        const key = r.kind.startsWith("chart_") ? "chart" : r.kind;
        c[key] = (c[key] ?? 0) + 1;
        // 「流程图」还要数讲义/正文正文里的 ```mermaid 段（库里 kind='flowchart' 是 0 条，
        // 只按 kind 计数会让树上的「已制作」和章节速览永远漏掉流程图）——与预览页、顶部统计
        // 共用 lib/contentCounts.ts 同一个函数，口径一致。
        c.flowchart = (c.flowchart ?? 0) + mermaidCountOfRow(r);
      }
    }
    return map;
  }, [nodes, rows]);
  const allNodesOptions = useMemo(
    () => nodes.map((n) => ({ value: n.id, label: `${n.key} ${n.title}` })),
    [nodes],
  );
  const treeData = useMemo(() => {
    const build = (items: TreeNodeRec[]): Record<string, unknown>[] =>
      items.map((it) => {
        const n = it.node;
        const c = counts.get(n.id);
        const done = c && (c.text > 0 || c.image > 0 || c.flowchart > 0 || c.quiz > 0 || (c.chart ?? 0) > 0);
        return {
          key: n.id,
          title: (
            <Space size={4}>
              <Typography.Text style={{ fontSize: 13 }}>{n.key}</Typography.Text>
              <Typography.Text ellipsis style={{ maxWidth: 160 }}>{n.title}</Typography.Text>
              {done && <Tag color="green" style={{ fontSize: 11, marginInlineEnd: 0 }}>已制作</Tag>}
            </Space>
          ),
          children: build(it.children),
        };
      });
    return build(treeOf(nodes));
  }, [nodes, counts]);
  const outlineTree = (
    <div style={{ maxHeight: isMobile ? "72vh" : "62vh", overflow: "auto" }}>
      <Tree
        showLine
        defaultExpandAll
        selectedKeys={[effectiveNode?.id ?? ""]}
        onSelect={(keys) => { const k = keys[0]; if (k) onSelectNode(String(k)); }}
        treeData={treeData as never[]}
      />
    </div>
  );
  if (courseLoading || planLoading) {
    return <SectionCard loading style={{ minHeight: 260 }} />;
  }
  async function pollJob(jobId: string): Promise<boolean> {
    for (let i = 0; i < 24; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const s = await api.jobStatus(jobId);
        if (s.status === "success") return true;
        if (s.status === "failed") { message.error(s.error || "生成失败"); return false; }
      } catch { /* 轮询容错 */ }
    }
    message.warning("生成仍在进行，稍后可到「课程预览」查看结果。");
    return false;
  }
  const genTexts = useMutation({
    mutationFn: (ids: string[]) =>
      api.generateContent({ course_id: courseId, node_ids: ids, tone: "正式", include_quiz: false }),
    onSuccess: async ({ job_id }) => {
      message.info("讲解生成中…");
      const ok = await pollJob(job_id);
      if (ok) { message.success("讲解已生成"); refresh(); requestAutoSync(courseId); }
    },
    onError: (e) => message.error(String(e)),
  });
  const genWholeChapter = useMutation({
    mutationFn: (ids: string[]) =>
      api.generateContent({ course_id: courseId, node_ids: ids, tone: "正式", include_quiz: true }),
    onSuccess: async ({ job_id }) => {
      message.info("整章纯文本正文 + 习题生成中（不含图），大章节可能需要一会儿…");
      const ok = await pollJob(job_id);
      if (ok) { message.success(PLAIN_TEXT_DONE); refresh(); requestAutoSync(courseId); }
    },
    onError: (e) => message.error(String(e)),
  });
  const setAnchor = useMutation({
    mutationFn: (itemId: string) => api.setStyleAnchor({ course_id: courseId, source_item_id: itemId }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["style-anchor", courseId] });
      message.success(
        `已设为样板：${res.style_anchor.source_node_key}《${res.style_anchor.source_title}》` +
          `（讲法 ${res.style_anchor.tone} / 深度 ${res.style_anchor.depth}）；` +
          (res.locked ? "已自动加锁，按章批量不会覆盖这一节" : ""),
      );
    },
    onError: (e) => message.error(String(e)),
  });
  const lockLecture = useMutation({
    mutationFn: (itemId: string) => api.lockLecture(itemId, true, "审核通过"),
    onSuccess: () => { message.success("已审核加锁：按章批量会跳过这一节"); refresh(); },
    onError: (e) => message.error(String(e)),
  });
  const unlockRedo = useMutation({
    mutationFn: async (itemId: string) => {
      await api.lockLecture(itemId, false);
      const done = await api.generateLecture({
        course_id: courseId, node_id: effectiveNode.id, item_id: itemId, tone: "正式",
      });
      return done;
    },
    onSuccess: () => { message.success("已解锁并重做"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const genQuiz = useMutation({
    mutationFn: (nid: string) => api.generateQuiz({ course_id: courseId, node_id: nid, count: 3, difficulty: "medium" }),
    onSuccess: () => { message.success("本节习题已生成"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const imgGen = useMutation({
    mutationFn: (d: ImgModalState) =>
      api.generateImage({
        course_id: courseId,
        prompt: d.prompt,
        size: d.size,
        item_id: d.itemId,
        node_id: effectiveNode?.id ?? "",
        text_lang: d.text_lang,
        overlay_text: d.overlay_text,
        overlay_pos: d.overlay_pos,
        model: d.model,
      }),
    onSuccess: (row) => {
      const wanted = imgModal?.text_lang ?? "none";
      const got = String(row.body.text_lang ?? "none");
      if (wanted !== "none" && got !== wanted) {
        message.warning("后端未返回文字叠加标记：可能仍在运行旧版后端，请重启 uvicorn 后再试");
      } else {
        message.success(wanted === "none" ? "图片已生成，请预览确认" : "图片已生成，标题文字已本地叠加，请预览确认");
      }
      setImgPreview(row);
      refresh();
    },
    onError: (e) => message.error(String(e)),
  });
  const imgImport = useMutation({
    mutationFn: (f: File) => api.importImage(courseId, f, undefined, effectiveNode?.id ?? ""),
    onSuccess: () => { message.success("已导入本节位图配图"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const flowGen = useMutation({
    mutationFn: (d: { description: string; kind: string }) =>
      api.generateFlowchart({ course_id: courseId, description: d.description, kind: d.kind, node_id: effectiveNode?.id ?? "" }),
    onSuccess: () => { message.success("本节流程图已生成"); setFlowModal(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const flowImport = useMutation({
    mutationFn: (d: { title: string; mermaid: string }) =>
      api.importFlowchart({ course_id: courseId, title: d.title, mermaid: d.mermaid, node_id: effectiveNode?.id ?? "" }),
    onSuccess: () => { message.success("已导入本节流程图"); setMermaidModal(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const genLecture = useMutation({
    mutationFn: (d: { tone: string; itemId?: string; allowOverwriteLocked?: boolean }) =>
      api.generateLecture({
        course_id: courseId,
        node_id: effectiveNode?.id ?? "",
        tone: d.tone,
        item_id: d.itemId,
        allow_overwrite_locked: d.allowOverwriteLocked,
      }),
    onSuccess: (row, vars) => {
      message.success(row.meta?.history ? "完整讲义已重新生成（旧版已进历史）" : "完整讲义已生成（图文 + 流程 + 随堂练习）");
      if (vars.allowOverwriteLocked) {
        // 任务 91：覆盖了原本审核锁定的一节 ⇒ 徽标已摘，必须明说（不能让人以为还是审过的）
        message.warning("这一节原本已审核锁定：正文已换成新生成的这一版，「审核通过」徽标已摘除，需重新审核。");
      }
      refresh();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const saveLecture = useMutation({
    mutationFn: (d: { id: string; markdown: string }) => api.updateLecture(d.id, d.markdown),
    onSuccess: () => { message.success("讲义已保存"); setLectureEdit(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  // 任务 22：按质检建议重生成（把 meta.qc 的 warn/fail 注入提示词）
  const genLectureWithQc = useMutation({
    mutationFn: (id: string) => api.regenerateWithQc(id),
    onSuccess: (queued) => {
      message.success(`已按 ${queued.qc_issues.length} 条质检建议重生成讲义`);
      refresh();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const delLecture = useMutation({
    mutationFn: (id: string) => api.deleteLecture(id),
    onSuccess: () => { message.success("讲义已删除（含其概念图 / 数据图与随堂练习）"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const patchItem = useMutation({
    mutationFn: (d: { id: string; body: Record<string, unknown> }) => api.updateContent(d.id, { body: d.body }),
    onSuccess: () => { message.success("已保存"); setTextEdit(null); setQuizEdit(null); setFlowView(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const quizSave = useMutation({
    mutationFn: (d: { id: string; body: Record<string, unknown> }) => api.updateQuiz(d.id, d.body),
    onSuccess: () => { message.success("已保存习题"); setQuizEdit(null); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const delItem = useMutation({
    mutationFn: (id: string) => api.deleteContent(id),
    onSuccess: () => { message.success("已删除"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const delQuiz = useMutation({
    mutationFn: (id: string) => api.deleteQuiz(id),
    onSuccess: () => { message.success("已删除习题"); refresh(); requestAutoSync(courseId); },
    onError: (e) => message.error(String(e)),
  });
  const saveNode = useMutation({
    mutationFn: (d: { nodeId: string; data: Partial<PlanNode> }) => api.updatePlanNode(d.nodeId, d.data),
    onSuccess: () => {
      message.success("大纲节点已更新；如原已通过，将自动回到「待通过」状态，请重新确认大纲");
      setEditingNode(null);
      qc.invalidateQueries({ queryKey: ["plan", courseId] });
      qc.invalidateQueries({ queryKey: ["course", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });
  const openEditNode = (n: PlanNode) => {
    setEditingNode(n);
    editForm.setFieldsValue({
      title: n.title,
      objective: n.objective,
      teaching_strategy: n.teaching_strategy,
      assessment: n.assessment,
      prerequisites: (n.prerequisites ?? []).join(", "),
    });
  };
  const nodeRows = (kind: string): ContentRow[] =>
    effectiveNode ? rows.filter((r) => r.node_id === effectiveNode.id && r.kind === kind) : [];
  const texts = nodeRows("text");
  const quizzes = nodeRows("quiz");
  const images = nodeRows("image");
  const flows = nodeRows("flowchart");
  const lectures = nodeRows("lecture");
  const lecture = lectures[lectures.length - 1];
  // 任务 91：这一篇是否已「审核通过/人工编辑」加锁 ⇒ 重跑必须先弹确认
  const lectureLocked = Boolean((lecture?.meta as Record<string, unknown> | undefined)?.locked);
  const lectureMd = lecture ? String((lecture.body as { markdown?: string })?.markdown ?? "") : "";
  /** 讲义/正文里内嵌的 ```mermaid 段数（与预览页、顶部统计共用同一口径）。 */
  const embeddedFlows = flowchartCount([...lectures, ...texts]);
  const { diagramSrcs, chartSrcs, chartTables } = useDiagramMaps(rows);
  const openImgGen = () => {
    setImgModal({ prompt: "", size: "1024x1024", text_lang: "none", overlay_text: "", overlay_pos: "top", model: "" });
    setImgPreview(null);
  };
  const children = effectiveNode
    ? nodes.filter(
        (n) => n.key.startsWith(`${effectiveNode.key}.`) && n.key.split(".").length === effectiveNode.key.split(".").length + 1,
      )
    : [];
  if (!effectiveNode) {
    return (
      <SectionCard>
        <Empty description="还没有大纲：请先回到第 2 步生成并通过大纲，再到这里一节一节制作。" />
        <Button type="primary" onClick={onNext}>返回大纲步骤</Button>
      </SectionCard>
    );
  }
  const isChapter = !effectiveNode.key.includes(".");
  // 课件以「章」为单位:选章节点用自己,选知识点则归到它所属的章(取 key 首段),
  // 这样无论点哪一节,右侧都能看到「本章课件(PPT)」入口(用户反馈:只在章节点出现太隐蔽)。
  const chapterOf = (() => {
    if (isChapter) return effectiveNode;
    const rootKey = effectiveNode.key.split(".")[0];
    return nodes.find((n) => n.key === rootKey) ?? null;
  })();
  const activeChapter = isChapter
    ? effectiveNode
    : nodes.find((n) => n.key === effectiveNode.key.split(".")[0]) ?? null;
  // 章节是容器:所有内容都挂在子知识点上。章节视图只展示「本章小节速览」,
  // 不再渲染正文/习题/位图配图/流程图/数据图这一排必然为空的内容区(用户反馈:看着像全没做)。
  const showOwnContent = !isChapter || children.length === 0;
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {!approved && (
        <Alert
          type="warning"
          showIcon
          message="大纲尚未通过"
          description="先到第 2 步点「大纲通过」锁定大纲，才能在本页自动生成正文 / 概念图 / 数据图 / 位图配图 / 流程图 / 习题；已生成内容仍可修改与删除。"
        />
      )}
      <SectionCard size="small">
        <Space wrap style={{ justifyContent: "space-between", width: "100%" }} align="center">
          <Space wrap>
            <Typography.Text strong>第 3 步 · 章节制作工作台</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              左侧大纲树点哪节做哪节：该节正文 / 概念图 / 数据图 / 流程图 / 习题都在右侧，不会串到别的章节。
            </Typography.Text>
          </Space>
          <Button type="primary" ghost disabled={!approved} onClick={onNext}>下一步：课程预览</Button>
        </Space>
        {/* 任务 85 §B3：让用户知道这次按什么场景生成（未选 ⇒「未指定场景（按通用口径生成）」） */}
        <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="scenario-line">
          {scenarioLine(course)}
        </Typography.Text>
        {/* 任务 86 §B2：**改场景 ≠ 改内容** —— 已生成的内容不会自己换代（这是用户
            「点了场景没看到变化」的根因之一，必须写在正文页上，而不是藏在文档里）。 */}
        <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="scenario-regen-note">
          改了使用场景后，<strong>需要重新生成才会生效</strong>（已生成的内容不会自己变）。
        </Typography.Text>
      </SectionCard>
      <StyleAnchorPanel
        courseId={courseId}
        nodes={nodes}
        approved={approved}
        generatedNodeIds={rows.filter((r) => r.kind === "lecture" && r.node_id).map((r) => String(r.node_id))}
        activeChapter={activeChapter}
        onChanged={refresh}
        onSelectNode={onSelectNode}
      />
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
        {isMobile ? (
          <>
            <Button icon={<MenuUnfoldOutlined />} onClick={() => setDrawerOpen(true)}>
              课程大纲（按节制作）
            </Button>
            <Drawer title="课程大纲（按节制作）" open={drawerOpen} onClose={() => setDrawerOpen(false)} width={320}>
              {outlineTree}
            </Drawer>
          </>
        ) : (
          <SectionCard size="small" title="课程大纲（按节制作）" style={{ width: 300, flexShrink: 0 }}>
            {outlineTree}
          </SectionCard>
        )}
        <Space direction="vertical" style={{ flex: 1, minWidth: 0 }} size="middle">
          <SectionCard size="small">
            <Space wrap style={{ justifyContent: "space-between", width: "100%" }}>
              <Space wrap>
                <Typography.Title level={5} style={{ margin: 0 }}>
                  {effectiveNode.key} {effectiveNode.title}
                </Typography.Title>
                <Tag color={effectiveNode.key.includes(".") ? "purple" : "blue"}>
                  {effectiveNode.key.includes(".") ? "知识点/小节" : "章节"}
                </Tag>
                {isChapter && (
                  <Tooltip
                    title={
                      <div style={{ maxWidth: 460 }}>
                        {PLAIN_TEXT_BUTTON_TIP.map((line) => <div key={line}>{line}</div>)}
                      </div>
                    }
                  >
                    <span style={{ display: "inline-block" }}>
                      <Button
                        size="small"
                        icon={<FileAddOutlined />}
                        disabled={!approved || genWholeChapter.isPending}
                        loading={genWholeChapter.isPending}
                        onClick={() => genWholeChapter.mutate(nodeIds)}
                      >
                        {PLAIN_TEXT_BUTTON_LABEL}
                      </Button>
                    </span>
                  </Tooltip>
                )}
              </Space>
              <Button size="small" icon={<EditOutlined />} onClick={() => openEditNode(effectiveNode)}>编辑节点</Button>
            </Space>
            <div style={{ marginTop: 8 }}>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                <b>学习目标：</b>{effectiveNode.objective || "—"}
              </Typography.Text>
            </div>
            {children.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  本章含 {children.length} 个小节：{children.map((c) => `${c.key} ${c.title}`).join("、")}
                </Typography.Text>
              </div>
            )}
          </SectionCard>
          {/* 完整讲义（讲义优先：正文 + 概念图 + 流程图 + 随堂练习一篇到位） */}
          <SectionCard
            size="small"
            title={
              <Space wrap>
                <ReadOutlined />
                <Typography.Text strong>完整讲义</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  图文混排：正文、概念图、流程图、随堂练习一次生成，导出笔记按知识点成页
                </Typography.Text>
              </Space>
            }
            extra={
              lecture ? (
                <Space wrap>
                  {/* 任务 86 §B1：这一篇按什么口径生成的（老内容没有标记 ⇒ 通用(未标记)） */}
                  <ScenarioBadge meta={lecture.meta as Record<string, unknown> | undefined} />
                  <VaultStatusTag
                    vault={lecture.meta?.vault as Record<string, unknown> | undefined}
                    itemId={lecture.id}
                    kind="content"
                    onChanged={refresh}
                  />
                  <DiagramFailureTag
                    meta={lecture.meta as Record<string, unknown> | undefined}
                    itemId={lecture.id}
                    onChanged={refresh}
                  />
                  <QcBadge
                    meta={lecture.meta as Record<string, unknown> | undefined}
                    itemId={lecture.id}
                    regenerating={genLectureWithQc.isPending}
                    disabled={!approved}
                    onRegenerate={() => genLectureWithQc.mutate(lecture.id)}
                    onJumpToText={(text) => jumpToText(
                      document.getElementById(LECTURE_BODY_ID), text)}
                  />
                  <UsageBadge usage={itemUsage(lecture.meta as Record<string, unknown> | undefined)} />
                  {lecture.meta?.locked ? (
                    <>
                      <Tag icon={<LockOutlined />} color="gold">已审核锁定</Tag>
                      <Button
                        size="small"
                        loading={unlockRedo.isPending}
                        onClick={() => unlockRedo.mutate(lecture.id)}
                      >
                        解锁并重做
                      </Button>
                    </>
                  ) : (
                    <Popconfirm
                      title="标记为审核通过并加锁？之后「生成本章 / 按章生成剩余全部」都会跳过这一节，不会被覆盖。"
                      okText="审核通过"
                      onConfirm={() => lockLecture.mutate(lecture.id)}
                    >
                      <Button size="small" icon={<CheckCircleOutlined />} loading={lockLecture.isPending}>
                        审核通过（加锁）
                      </Button>
                    </Popconfirm>
                  )}
                  <Popconfirm
                    title="把这一篇固化为本课程样板（讲法/深度/图风格/术语/习题难度）？之后按章生成都会对齐它。"
                    okText="设为样板"
                    onConfirm={() => setAnchor.mutate(lecture.id)}
                  >
                    <Button size="small" icon={<ExperimentOutlined />} loading={setAnchor.isPending}>
                      以这篇为样板
                    </Button>
                  </Popconfirm>
                  <Button size="small" icon={<EditOutlined />} onClick={() => { setLectureEdit(lecture); setLectureDraft(lectureMd); }}>
                    编辑讲义
                  </Button>
                  <Popconfirm
                    title={overwriteConfirmTitle(lectureLocked)}
                    onConfirm={() => genLecture.mutate({
                      tone: "正式", itemId: lecture.id, allowOverwriteLocked: lectureLocked,
                    })}
                  >
                    <Button size="small" icon={<ReloadOutlined />} disabled={!approved} loading={genLecture.isPending}>
                      重新生成（{LECTURE_COST_ONE}）
                    </Button>
                  </Popconfirm>
                  <Popconfirm title="删除该讲义？其概念图 / 数据图与随堂练习会一并删除。" onConfirm={() => delLecture.mutate(lecture.id)}>
                    <Button size="small" danger>删除讲义</Button>
                  </Popconfirm>
                </Space>
              ) : null
            }
          >
            <Space direction="vertical" style={{ width: "100%" }} size="middle">
              <Popconfirm
                title={overwriteConfirmTitle(lectureLocked)}
                disabled={!lectureLocked}
                onConfirm={() => genLecture.mutate({
                  tone: "正式", itemId: lecture?.id, allowOverwriteLocked: lectureLocked,
                })}
              >
                <Button
                  type="primary"
                  size="large"
                  block
                  icon={<ReadOutlined />}
                  disabled={!approved || genLecture.isPending}
                  loading={genLecture.isPending}
                  onClick={lectureLocked
                    ? undefined
                    : () => genLecture.mutate({ tone: "正式", itemId: lecture?.id })}
                >
                  {lecture ? `重新生成完整讲义（${LECTURE_COST_ONE}）` : `生成完整讲义（${LECTURE_COST_ONE}）`}
                </Button>
              </Popconfirm>
              {/* 任务 86 §B3：口径已经一致就别为了场景再花一次钱（只提示，不拦着做别用途的重生成） */}
              {lecture && sameScenario(
                usedScenarioOf(lecture.meta as Record<string, unknown> | undefined),
                scenarioOf(course),
              ) ? (
                <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="scenario-same-note">
                  口径一致，无需重跑（这一篇的生成口径与课程当前设置相同）
                </Typography.Text>
              ) : null}
              {genLecture.isPending ? (
                <div style={{ textAlign: "center", padding: "24px 0" }}>
                  <Spin size="large" />
                  <div style={{ marginTop: 12, color: "#64748b" }}>
                    {lecture
                      ? "正在重新生成整篇讲义（正文 + 概念图 + 流程图 + 习题，约 10~60 秒）…"
                      : "正在生成整篇讲义（正文 + 概念图 + 流程图 + 习题，约 10~60 秒）…"}
                  </div>
                </div>
              ) : lecture ? (
                <div>
                  <Space wrap style={{ marginBottom: 8 }}>
                    {String((lecture.body as { tone?: string })?.tone ?? "") && (
                      <Tag color="blue">语气：{String((lecture.body as { tone?: string })?.tone ?? "")}</Tag>
                    )}
                    {(lecture.body as { keywords?: string[] })?.keywords?.map((k) => <Tag key={k}>{k}</Tag>)}
                    {String((lecture.body as { summary?: string })?.summary ?? "") && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {String((lecture.body as { summary?: string })?.summary ?? "")}
                      </Typography.Text>
                    )}
                  </Space>
                  {lectureMd ? (
                    <div id={LECTURE_BODY_ID} style={{ borderTop: "1px solid #f0f0f0", paddingTop: 12 }}>
                      <MdWithFlow
                        md={lectureMd}
                        diagramSrcs={diagramSrcs}
                        chartSrcs={chartSrcs}
                        chartTables={chartTables}
                      />
                    </div>
                  ) : (
                    <Empty description="讲义内容为空，点「重新生成完整讲义」或「编辑讲义」补全。" />
                  )}
                </div>
              ) : (
                <Empty
                  description={
                    isChapter
                      ? `章节本身不存放正文与图件：本章含 ${children.length} 个小节，请在下方「本章小节速览」里选一节制作（如 ${children[0]?.key ?? "1.1"}）。`
                      : "还没有完整讲义。点上方主按钮一次生成图文混排讲义；或展开「高级选项」按类型分步制作（无讲义时笔记系统会自动拼接正文 / 习题 / 概念图 / 流程图）。"
                  }
                />
              )}
              <Collapse
                size="small"
                items={[
                  {
                    key: "advanced",
                    label: (
                      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                        高级选项 · 分步生成正文 / 习题 / 位图配图 / 流程图
                      </Typography.Text>
                    ),
                    children: (
                      <Space wrap>
                        <Button
                          size="small"
                          icon={<FileTextOutlined />}
                          disabled={!approved || genTexts.isPending}
                          loading={genTexts.isPending}
                          onClick={() => genTexts.mutate([effectiveNode.id])}
                        >
                          生成正文讲解
                        </Button>
                        <Button
                          size="small"
                          icon={<QuestionCircleOutlined />}
                          disabled={!approved || genQuiz.isPending}
                          loading={genQuiz.isPending}
                          onClick={() => genQuiz.mutate(effectiveNode.id)}
                        >
                          生成习题（3 题）
                        </Button>
                        <Tooltip title="手工位图配图：AI 位图已按既定口径关闭（2026-09-08，讲义全矢量）；确需照片 / 拍照图时才用这里生成，交付主要靠概念图 / 数据图 / 流程图。">
                          <span style={{ display: "inline-block" }}>
                            <Button size="small" icon={<PictureOutlined />} disabled={!approved} onClick={openImgGen}>
                              生成位图配图
                            </Button>
                          </span>
                        </Tooltip>
                        <Button
                          size="small"
                          icon={<ShareAltOutlined />}
                          disabled={!approved}
                          onClick={() => setFlowModal({ description: "", kind: "flow" })}
                        >
                          生成流程图
                        </Button>
                        <Button size="small" icon={<CodeOutlined />} onClick={() => setMermaidModal({ title: "", mermaid: "" })}>
                          粘贴 Mermaid
                        </Button>
                      </Space>
                    ),
                  },
                ]}
              />
            </Space>
          </SectionCard>
          {isChapter && children.length > 0 && (
            <SectionCard size="small" title="本章小节速览（点节点跳转制作）">
              <List
                size="small"
                dataSource={children}
                renderItem={(c) => {
                  const cc = counts.get(c.id);
                  const parts = (Object.keys(KIND_LABEL) as (keyof typeof KIND_LABEL)[])
                    .filter((k) => (cc?.[k] ?? 0) > 0)
                    .map((k) => KIND_LABEL[k]);
                  return (
                    <List.Item actions={[<a key="go" onClick={() => onSelectNode(c.id)}>去制作</a>]}>
                      <Space>
                        <Typography.Text>{c.key} {c.title}</Typography.Text>
                        {parts.length ? <Tag color="green">{parts.join(" / ")}</Tag> : <Tag>未制作</Tag>}
                      </Space>
                    </List.Item>
                  );
                }}
              />
            </SectionCard>
          )}
          {chapterOf && (
            <PptPanel
              courseId={courseId}
              scope="chapter"
              chapterNodeId={chapterOf.id}
              disabled={!approved}
              title={`本章课件（PPT）· ${chapterOf.key} ${chapterOf.title}`}
            />
          )}
          {chapterOf && (
            <ConceptPanel
              courseId={courseId}
              chapterKey={chapterOf.key}
              disabled={!approved}
              title={`本章概念（概念库）· ${chapterOf.key} ${chapterOf.title}`}
            />
          )}
          {showOwnContent && (
            <>
          {/* 正文讲解：没有独立正文、又已有完整讲义时整卡隐藏（正文已含在讲义里，避免看着像「没做」） */}
          {(texts.length > 0 || !lecture) && (
          <SectionCard
            size="small"
            title={`正文讲解（${texts.length}）`}
          >
            {texts.length === 0 ? (
              <Empty description="本节还没有独立正文。生成入口在上方「高级选项」；如已有完整讲义，正文已含在讲义中。" />
            ) : (
              <Collapse
                items={texts.map((t) => ({
                  key: t.id,
                  label: (
                    <Space size={6} wrap>
                      <Typography.Text>{t.title}</Typography.Text>
                      <VaultStatusTag
                        vault={t.meta?.vault as Record<string, unknown> | undefined}
                        itemId={t.id}
                        kind="content"
                        onChanged={refresh}
                      />
                    </Space>
                  ),
                  extra: (
                    <Space size={4} onClick={(e) => e.stopPropagation()}>
                      <a onClick={() => { setTextEdit(t); setTextDraft(String(t.body.markdown ?? "")); }}>编辑</a>
                      <Popconfirm title="删除本节讲解？" onConfirm={() => delItem.mutate(t.id)}><a>删除</a></Popconfirm>
                    </Space>
                  ),
                  children: (
                    <div>
                      <Space size={4} style={{ marginBottom: 8 }} wrap>
                        {(t.body.keywords as string[] | undefined)?.map((k) => <Tag key={k}>{k}</Tag>)}
                      </Space>
                      <MdWithFlow
                        md={String(t.body.markdown ?? "")}
                        diagramSrcs={diagramSrcs}
                        chartSrcs={chartSrcs}
                        chartTables={chartTables}
                      />
                    </div>
                  ),
                }))}
              />
            )}
          </SectionCard>
          )}
          {/* 习题 */}
          <SectionCard
            size="small"
            title={`本节习题（${quizzes.length}）`}
            extra={
              <Space size={4}>
                {quizzes[0] && (
                  <VaultStatusTag
                    vault={quizzes[0].meta?.vault as Record<string, unknown> | undefined}
                    itemId={quizzes[0].id}
                    kind="quiz"
                    onChanged={refresh}
                  />
                )}
              </Space>
            }
          >
            {quizzes.length === 0 ? (
              <Empty description="还没有习题。出题入口在上方「高级选项」；如讲义已含随堂练习，习题会同步出现在这里。" />
            ) : (
              <List
                size="small"
                dataSource={quizzes}
                renderItem={(q) => {
                  const b = q.body as { question: string; options?: string[]; answer: string; difficulty: string; explanation?: string };
                  return (
                    <List.Item
                      actions={[
                        <a key="edit" onClick={() => {
                          setQuizEdit(q);
                          setQuizDraft({
                            question: b.question ?? "",
                            options: (b.options ?? []).join("\n"),
                            answer: b.answer ?? "",
                            explanation: b.explanation ?? "",
                          });
                        }}>编辑</a>,
                        <Popconfirm key="del" title="删除该习题？" onConfirm={() => delQuiz.mutate(q.id)}>
                          <a>删除</a>
                        </Popconfirm>,
                      ]}
                    >
                      <Space direction="vertical" size={0}>
                        <Typography.Text>{b.question}（{b.difficulty}）</Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>答案：{b.answer}</Typography.Text>
                      </Space>
                    </List.Item>
                  );
                }}
              />
            )}
          </SectionCard>
          {/* 位图配图 */}
          <SectionCard
            size="small"
            title={`本节位图配图（${images.length}）`}
            extra={
              <Space>
                <Upload accept="image/*" showUploadList={false} beforeUpload={(f) => { imgImport.mutate(f); return false; }}>
                  <Button size="small" icon={<UploadOutlined />}>上传我的图</Button>
                </Upload>
              </Space>
            }
          >
            {images.length === 0 ? (
              <Empty description={`还没有本节${FIGURE_TERM.image}。生成入口在上方「高级选项」；提示词可留空：自动按本节大纲目标构图，画面默认不出现文字。`} />
            ) : (
              <List
                size="small"
                grid={{ gutter: 12, column: 3 }}
                dataSource={images}
                renderItem={(im) => (
                  <List.Item>
                    <SectionCard
                      size="small"
                      cover={<img alt={im.title} src={imgSrc(im)} style={{ maxHeight: 160, objectFit: "cover" }} />}
                      actions={[
                        <a key="redo" onClick={() => {
                          setImgModal({
                            prompt: String(im.body.prompt ?? im.title),
                            size: String(im.body.size ?? "1024x1024"),
                            itemId: im.id,
                            text_lang: String(im.body.text_lang ?? "none"),
                            overlay_text: String(im.body.overlay_text ?? ""),
                            overlay_pos: String(im.body.overlay_pos ?? "top"),
                            model: String(im.body.model ?? ""),
                          });
                          setImgPreview(null);
                        }}>重做</a>,
                        <Popconfirm key="del" title="删除该位图配图？" onConfirm={() => delItem.mutate(im.id)}>
                          <a>删除</a>
                        </Popconfirm>,
                      ]}
                    >
                      <Space direction="vertical" size={4} style={{ width: "100%" }}>
                        <VaultStatusTag
                          vault={im.meta?.vault as Record<string, unknown> | undefined}
                          itemId={im.id}
                          kind="content"
                          onChanged={refresh}
                        />
                        <Typography.Text ellipsis style={{ fontSize: 12, display: "block" }}>{im.title}</Typography.Text>
                      </Space>
                    </SectionCard>
                  </List.Item>
                )}
              />
            )}
          </SectionCard>
          {/* 流程图 */}
          <SectionCard
            size="small"
            title={
              <Space wrap size={6}>
                <Typography.Text strong>本节流程图（{flows.length + embeddedFlows}）</Typography.Text>
                {embeddedFlows > 0 && (
                  <Tooltip title="这几段流程图内嵌在讲义正文的 ```mermaid 代码块里，随讲义一起渲染（预览页「只看 → 流程图」也能按段看到），不必在这里重复做。">
                    <Tag color="purple">讲义内嵌 {embeddedFlows} 段</Tag>
                  </Tooltip>
                )}
              </Space>
            }
            extra={
              <Space>
                <Button size="small" icon={<UploadOutlined />} onClick={() => setMermaidModal({ title: "", mermaid: "" })}>
                  粘贴 Mermaid
                </Button>
              </Space>
            }
          >
            {flows.length === 0 ? (
              <Empty
                description={embeddedFlows > 0
                  ? `本节没有独立的流程图条目；流程图内嵌在讲义正文里（共 ${embeddedFlows} 段），随讲义一起渲染。要单独的一张再点右上「粘贴 Mermaid」或上方「生成流程图」。`
                  : "还没有本节流程图。生成入口在上方「高级选项」，也可直接「粘贴 Mermaid」导入自绘图。"}
              />
            ) : (
              <List
                size="small"
                dataSource={flows}
                renderItem={(fc) => (
                  <List.Item>
                    <div style={{ width: "100%" }}>
                      <Space wrap style={{ width: "100%", justifyContent: "space-between" }}>
                        <Space wrap>
                          <Tag color="blue">{fc.title}</Tag>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            {String(fc.body.description ?? "") || String(fc.body.source ?? "")}
                          </Typography.Text>
                          <VaultStatusTag
                            vault={fc.meta?.vault as Record<string, unknown> | undefined}
                            itemId={fc.id}
                            kind="content"
                            onChanged={refresh}
                          />
                        </Space>
                        <Space size={8}>
                          <Button size="small" type="link" onClick={() => setFlowView(fc)}>查看源码</Button>
                          <Button
                            size="small"
                            type="link"
                            onClick={() => setFlowModal({ description: String(fc.body.description ?? fc.title), kind: String(fc.body.kind ?? "flow") })}
                          >
                            重做
                          </Button>
                          <Popconfirm title="删除该流程图？" onConfirm={() => delItem.mutate(fc.id)}>
                            <Button size="small" type="link" danger>删除</Button>
                          </Popconfirm>
                        </Space>
                      </Space>
                      <div style={{ marginTop: 8 }}>
                        <FlowDiagram code={String(fc.body.mermaid ?? "")} />
                      </div>
                    </div>
                  </List.Item>
                )}
              />
            )}
          </SectionCard>
          <ChartSection
            courseId={courseId}
            nodeId={effectiveNode.id}
            approved={approved}
            rows={rows}
            refresh={refresh}
          />
            </>
          )}
          {isChapter && children.length > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              本章内容按小节制作:请在上方「本章小节速览」里选一个小节进入(或点左侧大纲树的 1.1 / 1.2)。章节本身不单独存放正文与图件。
            </Typography.Text>
          )}
          <Button type="primary" ghost disabled={!approved} onClick={onNext} style={{ alignSelf: "flex-start" }}>
            本章节完成 → 课程预览
          </Button>
        </Space>
      </div>
      {/* 编辑大纲节点 */}
      <Modal
        title={`编辑节点：${editingNode?.key ?? ""} ${editingNode?.title ?? ""}`}
        open={!!editingNode}
        // 任务 48t：forceRender 让 <Form form={editForm}> 先挂载 —— 原来 openEditNode() 里
        // setEditingNode(n) 与 editForm.setFieldsValue() 同 tick 执行，Form 还没挂载就写值，
        // antd 会报「Instance created by 'useForm' is not connected to any Form element」。
        forceRender
        onCancel={() => setEditingNode(null)}
        onOk={() => {
          if (!editingNode) return;
          const v = editForm.getFieldsValue();
          saveNode.mutate({
            nodeId: editingNode.id,
            data: {
              title: v.title,
              objective: v.objective,
              teaching_strategy: v.teaching_strategy,
              assessment: v.assessment,
              prerequisites: String(v.prerequisites ?? "").split(/[,，]/).map((s) => s.trim()).filter(Boolean),
            },
          });
        }}
        confirmLoading={saveNode.isPending}
        width={560}
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="title" label="标题" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="objective" label="学习目标"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="teaching_strategy" label="教学策略"><Input /></Form.Item>
          <Form.Item name="assessment" label="评估方式"><Input /></Form.Item>
          <Form.Item name="prerequisites" label="前置知识点 key（逗号分隔）"><Input placeholder="如：1, 1.1" /></Form.Item>
        </Form>
      </Modal>
      {/* 编辑讲解 */}
      <Modal
        title={`编辑讲解：${textEdit?.title ?? ""}`}
        open={!!textEdit}
        onCancel={() => setTextEdit(null)}
        onOk={() => textEdit && patchItem.mutate({ id: textEdit.id, body: { markdown: textDraft } })}
        confirmLoading={patchItem.isPending}
        width={760}
      >
        <Input.TextArea rows={14} value={textDraft} onChange={(e) => setTextDraft(e.target.value)} placeholder="Markdown 正文" />
      </Modal>
      {/* 编辑完整讲义 */}
      <Modal
        title={`编辑完整讲义：${lectureEdit?.title ?? ""}`}
        open={!!lectureEdit}
        onCancel={() => setLectureEdit(null)}
        onOk={() => lectureEdit && saveLecture.mutate({ id: lectureEdit.id, markdown: lectureDraft })}
        confirmLoading={saveLecture.isPending}
        width={960}
        okText="保存讲义"
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 0 }}>
          直接编辑 Markdown 全文。图片用 ![](/images/xxx.png) 引用，流程图用 ```mermaid 代码块（保存后即按此渲染）。
        </Typography.Paragraph>
        <Input.TextArea
          rows={22}
          value={lectureDraft}
          onChange={(e) => setLectureDraft(e.target.value)}
          placeholder="## 讲义正文（Markdown）"
          style={{ fontFamily: "Consolas, Monaco, monospace", fontSize: 13 }}
        />
      </Modal>
      {/* 编辑习题 */}
      <Modal
        title="编辑习题"
        open={!!quizEdit}
        onCancel={() => setQuizEdit(null)}
        onOk={() => quizEdit && quizSave.mutate({
          id: quizEdit.id,
          body: {
            question: quizDraft.question,
            options: quizDraft.options.split("\n").map((s) => s.trim()).filter(Boolean),
            answer: quizDraft.answer,
            explanation: quizDraft.explanation,
          },
        })}
        confirmLoading={quizSave.isPending}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input.TextArea rows={2} value={quizDraft.question} onChange={(e) => setQuizDraft({ ...quizDraft, question: e.target.value })} placeholder="题干" />
          <Input.TextArea rows={4} value={quizDraft.options} onChange={(e) => setQuizDraft({ ...quizDraft, options: e.target.value })} placeholder="选项每行一个（非选择题留空）" />
          <Input value={quizDraft.answer} onChange={(e) => setQuizDraft({ ...quizDraft, answer: e.target.value })} placeholder="答案" />
          <Input.TextArea rows={2} value={quizDraft.explanation} onChange={(e) => setQuizDraft({ ...quizDraft, explanation: e.target.value })} placeholder="解析" />
        </Space>
      </Modal>
      {/* 流程图源码 */}
      <Modal title={`流程图源码：${flowView?.title ?? ""}`} open={!!flowView} footer={null} width={760} onCancel={() => setFlowView(null)}>
        <pre style={{ whiteSpace: "pre-wrap", maxHeight: 460, overflow: "auto" }}>{String(flowView?.body.mermaid ?? "")}</pre>
      </Modal>
      {/* 图片生成/重做 */}
      <Modal
        title={imgModal?.itemId ? `重做本节${FIGURE_TERM.image}（原地替换）` : `生成本节${FIGURE_TERM.image}`}
        open={!!imgModal}
        onCancel={() => { if (imgPreview) requestAutoSync(courseId); setImgModal(null); setImgPreview(null); }}
        footer={null}
        width={680}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Space>
            <Typography.Text style={{ minWidth: 56 }}>位图配图归属</Typography.Text>
            <Select
              style={{ flex: 1, minWidth: 260 }}
              value={effectiveNode.id}
              options={allNodesOptions}
              onChange={(id) => onSelectNode(String(id))}
            />
          </Space>
          <Input.TextArea
            rows={2}
            value={imgModal?.prompt ?? ""}
            onChange={(e) => setImgModal((d) => (d ? { ...d, prompt: e.target.value } : d))}
            placeholder="补充画面细节（选填；留空自动按本节大纲目标构图，画面默认无文字）"
          />
          <Space wrap>
            <Typography.Text>图中文字</Typography.Text>
            <Select
              style={{ width: 320 }}
              value={imgModal?.text_lang ?? "none"}
              options={IMG_TEXT_LANG_OPTIONS}
              onChange={(text_lang) => setImgModal((d) => (d ? { ...d, text_lang } : d))}
            />
            {(imgModal?.text_lang ?? "none") !== "none" && (
              <>
                <Input
                  style={{ width: 260 }}
                  value={imgModal?.overlay_text ?? ""}
                  onChange={(e) => setImgModal((d) => (d ? { ...d, overlay_text: e.target.value } : d))}
                  placeholder="叠加文字（留空=用本节标题）"
                  maxLength={60}
                />
                <Typography.Text>位置</Typography.Text>
                <Select
                  style={{ width: 120 }}
                  value={imgModal?.overlay_pos ?? "top"}
                  onChange={(overlay_pos) => setImgModal((d) => (d ? { ...d, overlay_pos } : d))}
                  options={[
                    { value: "top", label: "顶部" },
                    { value: "center", label: "中部" },
                    { value: "bottom", label: "底部" },
                  ]}
                />
              </>
            )}
          </Space>
          <Space wrap>
            <Typography.Text>图像模型</Typography.Text>
            <Select
              style={{ width: 320 }}
              value={imgModal?.model ?? ""}
              options={IMG_MODEL_OPTIONS}
              onChange={(model) => setImgModal((d) => (d ? { ...d, model } : d))}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              高质量模型按 OpenRouter 计费，仅需要画面内精确中文时选用
            </Typography.Text>
          </Space>
          <Space>
            <Typography.Text>尺寸</Typography.Text>
            <Select
              style={{ width: 200 }}
              value={imgModal?.size}
              onChange={(size) => setImgModal((d) => (d ? { ...d, size } : d))}
              options={["512x512", "1024x1024", "1024x1792"].map((v) => ({ value: v, label: v }))}
            />
          </Space>
          {imgGen.isPending ? (
            <div style={{ textAlign: "center", padding: "24px 0" }}>
              <Spin size="large" />
              <div style={{ marginTop: 12, color: "#64748b" }}>正在生成图片（约 10~30 秒），请稍候…</div>
            </div>
          ) : imgPreview ? (
            <div style={{ textAlign: "center" }}>
              <img src={imgSrc(imgPreview)} alt={imgPreview.title} style={{ maxWidth: "100%", maxHeight: 360, borderRadius: 8 }} />
              {String(imgPreview.body.text_lang ?? "none") !== "none" && (
                <Typography.Text type="secondary" style={{ display: "block", marginTop: 8, fontSize: 12 }}>
                  已本地叠加文字：{String(imgPreview.body.overlay_text ?? "")}（位置：{String(imgPreview.body.overlay_pos ?? "top")}）
                </Typography.Text>
              )}
              <Space style={{ marginTop: 16 }} wrap>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => imgModal && imgGen.mutate({ ...imgModal, itemId: imgModal.itemId ?? imgPreview.id })}
                >
                  不满意 · 换一张
                </Button>
                <Button type="primary" onClick={() => { requestAutoSync(courseId); setImgModal(null); setImgPreview(null); message.success("位图配图已确认入库"); }}>
                  满意 · 完成
                </Button>
              </Space>
            </div>
          ) : (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => imgModal && imgGen.mutate(imgModal)}>
              开始生成
            </Button>
          )}
        </Space>
      </Modal>
      {/* 流程图生成 */}
      <Modal
        title="生成本节流程图"
        open={!!flowModal}
        onCancel={() => setFlowModal(null)}
        onOk={() => flowModal && flowGen.mutate(flowModal)}
        confirmLoading={flowGen.isPending}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Select
            style={{ width: "100%" }}
            value={effectiveNode.id}
            options={allNodesOptions}
            onChange={(id) => onSelectNode(String(id))}
          />
          <Input.TextArea
            rows={3}
            value={flowModal?.description ?? ""}
            onChange={(e) => setFlowModal((d) => (d ? { ...d, description: e.target.value } : d))}
            placeholder="自然语言描述本节流程，如：先讲解概念 → 演示实验 → 学生练习 → 小结"
          />
          <Select
            style={{ width: 200 }}
            value={flowModal?.kind}
            onChange={(kind) => setFlowModal((d) => (d ? { ...d, kind } : d))}
            options={["flow", "loop", "branch", "hierarchy"].map((v) => ({ value: v, label: v }))}
          />
        </Space>
      </Modal>
      {/* 粘贴 Mermaid */}
      <Modal
        title="导入自绘 Mermaid（归属本节）"
        open={!!mermaidModal}
        onCancel={() => setMermaidModal(null)}
        onOk={() => mermaidModal && flowImport.mutate(mermaidModal)}
        confirmLoading={flowImport.isPending}
        width={760}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input
            value={mermaidModal?.title ?? ""}
            onChange={(e) => setMermaidModal((d) => (d ? { ...d, title: e.target.value } : d))}
            placeholder="标题（必填），如：本节能耗计算流程"
          />
          <Input.TextArea
            rows={8}
            value={mermaidModal?.mermaid ?? ""}
            onChange={(e) => setMermaidModal((d) => (d ? { ...d, mermaid: e.target.value } : d))}
            placeholder={"flowchart TD\n  A([开始]) --> B[处理]\n  B --> C([结束])"}
          />
        </Space>
      </Modal>
    </Space>
  );
}
