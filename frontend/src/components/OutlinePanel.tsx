import { useEffect, useMemo, useRef, useState } from "react";
import SectionCard from "./ui/SectionCard";
import AudienceField from "./AudienceField";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Button, Checkbox, Col, Form, Input, InputNumber, Modal, Row, Select, Space, Tag, Tooltip, Tree, Typography,
} from "antd";
import {
  ArrowDownOutlined, ArrowUpOutlined, CheckOutlined, DeleteOutlined, EditOutlined, PlusOutlined,
  HistoryOutlined, ReloadOutlined, UndoOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { requestAutoSync } from "../syncNotes";
import KnowledgeGraph from "./KnowledgeGraph";
import PlanHistoryModal from "./PlanHistoryModal";
import { formatLocalTime } from "../lib/datetime";
import { chapterMembers, chapterNodes } from "../lib/planLeaves";
import { suggestChapters } from "../lib/suggestChapters";
import type { Plan, PlanCheck, PlanNode, PlanNodeImpact } from "../types";

/** 节点移动参数（同章上/下移，或跨章移动/升降层级）。 */
type MoveSpec = {
  direction?: "up" | "down";
  reparent?: boolean;
  to_parent_id?: string | null;
  position?: number;
  confirm?: boolean;
};

const CONTENT_KIND_LABEL: Record<string, string> = {
  lecture: "讲义",
  text: "正文",
  diagram: "概念图",
  chart_bar: "柱状图",
  chart_line: "折线图",
  chart_table: "数据表",
  chart_mindmap: "思维导图",
  chart_timeline: "时间线",
  flowchart: "流程图",
  image: "位图配图",
  ppt: "课件",
  quiz: "习题",
};

// 任务 75 §B3：未覆盖清单**默认只显示前 8 条**（其余「展开看全部」）；句子碎片由后端
// 过滤掉、条数用 filtered_out_count 如实说明；覆盖率低时先给一句结论，别一上来甩几十条。
const UNCOVERED_DEFAULT_VISIBLE = 8;
const UNCOVERED_LOW_RATIO = 0.5;
const UNCOVERED_LOW_NOTICE =
  "素材以方法 / 条目词为主、大纲是体系课：字面覆盖偏低属常见，请人工确认是否缺章节；"
  + "可勾选「语义等价判定」重算，或按下面的建议补知识点。";
function kindSummary(counts: Record<string, number>): string {
  const parts = Object.entries(counts ?? {}).map(
    ([kind, n]) => `${CONTENT_KIND_LABEL[kind] ?? kind} ${n}`);
  return parts.length ? `（${parts.join(" / ")}）` : "";
}

/** 后端二次确认用的 409 detail 是结构化对象，前端从错误文案里还原出来。 */
function parseImpactError(err: unknown): { message: string; impact?: PlanNodeImpact } | null {
  const raw = String(err instanceof Error ? err.message : err);
  try {
    const parsed = JSON.parse(raw) as { message?: string; impact?: PlanNodeImpact };
    if (parsed && typeof parsed === "object") {
      return { message: String(parsed.message ?? ""), impact: parsed.impact };
    }
  } catch {
    // 普通字符串错误（如 400 参数不合法）走下面的兜底
  }
  return null;
}
const STYLE_OPTIONS = ["正式", "轻松", "幽默"].map((v) => ({ value: v, label: v }));
function PlanFormFields({ suggested }: { suggested: number }) {
  return (
    <>
      <Form.Item
        name="audience"
        label="目标受众（给谁看）"
        tooltip="生成前先想清楚讲给谁听；留空则沿用建课时的受众。"
      >
        <AudienceField />
      </Form.Item>
      <Form.Item name="goal" label="课程目标" rules={[{ required: true, message: "填写课程目标" }]}>
        <Input.TextArea placeholder="如：让初中生理解光合作用的原理与意义" rows={3} />
      </Form.Item>
      <Form.Item
        name="num_chapters"
        label="章节数"
        initialValue={suggested}
        extra={`按素材规模建议 ${suggested} 章；章节数是上限建议，不保证覆盖全部素材要点（生成后会做覆盖自检）。`}
      >
        <InputNumber min={1} max={12} style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item name="style" label="风格" initialValue="正式">
        <Select options={STYLE_OPTIONS} />
      </Form.Item>
      {/* 任务 70 §B3：语义等价判定**可选、默认关**；开启也只走本地词面相似度（不调模型）。 */}
      <Form.Item
        name="semantic"
        valuePropName="checked"
        initialValue={false}
        extra="默认关闭（只做字面比对）。开启后按用词相似度判等价，仍在本地算，不调模型、不产生用量。"
      >
        <Checkbox>语义等价判定（默认关）</Checkbox>
      </Form.Item>
    </>
  );
}
export default function OutlinePanel({ courseId }: { courseId: string }) {
  const qc = useQueryClient();
  // 任务 81：本页新增了「立即自检」的提示条，顺手把它从 antd 静态 message 迁到 App.useApp()
  // （静态实例不跟随动态主题，且会撞 check-antd-context.ts 的技术债上限）。
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [regenForm] = Form.useForm();
  const [regenOpen, setRegenOpen] = useState(false);
  const [showGraph, setShowGraph] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showAllUncovered, setShowAllUncovered] = useState(false);
  const [editing, setEditing] = useState<PlanNode | null>(null);
  const [addTarget, setAddTarget] = useState<{ parent: PlanNode | null } | null>(null);
  const [pending, setPending] = useState<{ kind: "delete" | "move"; node: PlanNode;
    impact: PlanNodeImpact; move?: MoveSpec } | null>(null);
  const [addForm] = Form.useForm();
  const [editForm] = Form.useForm();
  const { data: course, isLoading: courseLoading } = useQuery({
    queryKey: ["course", courseId],
    queryFn: () => api.getCourse(courseId),
    retry: false,
  });
  const { data: plan, isLoading: planLoading, refetch } = useQuery({
    queryKey: ["plan", courseId],
    queryFn: () => api.getPlan(courseId),
    retry: false,
  });
  // 任务 70 §B2：自检结论改成读**落库记录**（getPlanCheck）⇒ 刷新 / 换设备后仍在；
  // 还没生成过时 check 为 null，界面给明确空态（原先存内存 state，刷新即丢）。
  const { data: planCheck } = useQuery({
    queryKey: ["planCheck", courseId],
    queryFn: () => api.getPlanCheck(courseId),
    retry: false,
  });
  // 素材规模 → 章节数建议值（P3-2：不再写死 4 章）
  const { data: materials } = useQuery({
    queryKey: ["materials", courseId],
    queryFn: () => api.listMaterials(courseId),
    retry: false,
  });
  const suggestedChapters = suggestChapters(materials);
  const suggestedApplied = useRef(false);
  const approved = course?.status === "approved";
  const hasOutline = (plan?.nodes?.length ?? 0) > 0;
  const check: PlanCheck | null = planCheck?.check ?? plan?.check ?? null;
  const checkGeneratedAt = planCheck?.check_generated_at ?? plan?.check_generated_at ?? "";
  // 任务 75 §B3：未覆盖明细**按类型分组**、默认只渲染前 N 条（可展开全部）；
  // 覆盖率低时先给一句结论（后端已把句子碎片过滤掉，条数用 filtered_out_count 如实说明）。
  const uncoveredGroups = useMemo(() => {
    const rows = check?.material_uncovered_detail ?? [];
    const visible = showAllUncovered ? rows : rows.slice(0, UNCOVERED_DEFAULT_VISIBLE);
    const groups: { kind: string; label: string; rows: typeof rows }[] = [];
    for (const row of visible) {
      const last = groups[groups.length - 1];
      if (last && last.kind === row.kind) last.rows.push(row);
      else groups.push({ kind: row.kind, label: row.kind_label, rows: [row] });
    }
    return groups;
  }, [check, showAllUncovered]);
  const uncoveredLow = !!check && check.material_points_total > 0
    && check.material_uncovered_count / check.material_points_total >= UNCOVERED_LOW_RATIO;
  const firstNode = (plan?.nodes ?? [])[0];
  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ["course", courseId] });
    qc.invalidateQueries({ queryKey: ["planCheck", courseId] });
    refetch();
  };
  // 任务 48t：这两个预填只能在「还没有大纲」时写 —— 那时 <Form form={form}> 才挂载；
  // 已有大纲时该表单不渲染，再调 setFieldsValue/getFieldValue 会报
  // antd「Instance created by 'useForm' is not connected to any Form element」。
  // 受众预填：课程数据异步到达后回填（只在用户尚未输入时）
  useEffect(() => {
    if (hasOutline) return;
    const audience = course?.audience ?? "";
    if (audience && !String(form.getFieldValue("audience") ?? "").trim()) {
      form.setFieldsValue({ audience });
    }
  }, [course, form, hasOutline]);
  // 素材异步到手后回填一次建议值（只在用户还没填过时）
  useEffect(() => {
    if (hasOutline) return;
    if (suggestedApplied.current) return;
    suggestedApplied.current = true;
    if (!form.getFieldValue("num_chapters")) {
      form.setFieldsValue({ num_chapters: suggestedChapters });
    }
  }, [suggestedChapters, form, hasOutline]);
  const generate = useMutation({
    mutationFn: (v: { audience?: string; goal: string; num_chapters: number; style: string; semantic?: boolean }) =>
      api.generatePlan({ course_id: courseId, audience: v.audience ?? "", goal: v.goal,
        num_chapters: v.num_chapters, style: v.style, semantic: v.semantic === true }),
    onSuccess: (r) => {
      message.success(r.check ? "大纲已生成并保存（自检结论已落库，刷新后仍在）" : "大纲已生成并保存");
      setRegenOpen(false);
      refreshAll();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const approve = useMutation({
    mutationFn: (v: boolean) => api.approvePlan(courseId, v),
    onSuccess: (r) => {
      message.success(r.status === "approved" ? "大纲已通过：后续数据图与内容都按这份大纲生成" : "已撤销通过：可继续修改或重新生成大纲");
      refreshAll();
    },
    onError: (e) => message.error(String(e)),
  });
  // 任务 81 §B3：空态里的「立即自检」—— 按**当前素材**给已有大纲补跑一次自检并落库
  // （POST /plan/{id}/check：不重新生成大纲、不调模型、不产生用量），成功后刷新落库结论。
  const runCheck = useMutation({
    mutationFn: () => api.runPlanCheck(courseId),
    onSuccess: () => {
      message.success("自检完成：结论已落库，刷新 / 换设备都还在");
      qc.invalidateQueries({ queryKey: ["planCheck", courseId] });
    },
    onError: (e) => message.error(`自检失败：${String(e)}`),
  });
  const saveNode = useMutation({
    mutationFn: (d: { nodeId: string; data: Partial<PlanNode> }) => api.updatePlanNode(d.nodeId, d.data),
    onSuccess: () => {
      message.success("节点已更新");
      setEditing(null);
      refreshAll();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const createNode = useMutation({
    mutationFn: (d: { title: string; objective?: string; parent_id?: string | null }) =>
      api.createPlanNode({ course_id: courseId, ...d }),
    onSuccess: (r) => {
      const renamed = Object.keys(r.renumbered ?? {}).length;
      message.success(`节点已新增${renamed ? `，编号已重排（${renamed} 个节点改号）` : ""}`);
      setAddTarget(null);
      refreshAll();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const moveNode = useMutation({
    mutationFn: (d: { nodeId: string; data: MoveSpec }) => api.movePlanNode(d.nodeId, d.data),
    onSuccess: (r) => {
      message.success(`已移动：现在是 ${r.node.key} ${r.node.title}；编号已重排`);
      setPending(null);
      refreshAll();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const deleteNode = useMutation({
    mutationFn: (d: { nodeId: string; cascade: boolean }) => api.deletePlanNode(d.nodeId, d.cascade),
    onSuccess: (r) => {
      message.success(`已删除 ${r.deleted?.length ?? 1} 个节点；编号已重排`);
      setPending(null);
      refreshAll();
      requestAutoSync(courseId);
    },
    onError: (e) => message.error(String(e)),
  });
  const openAdd = (parent: PlanNode | null) => {
    addForm.setFieldsValue({ title: "", objective: "" });
    setAddTarget({ parent });
  };
  const previewDelete = async (node: PlanNode) => {
    try {
      const impact = await api.planNodeImpact(node.id);
      setPending({ kind: "delete", node, impact });
    } catch (e) {
      message.error(String(e));
    }
  };
  const previewMove = async (node: PlanNode, move: MoveSpec) => {
    try {
      // 先按「不确认」提交：后端会算好「移动后的编号变化 + 影响面」再用 409 挡回来
      await api.movePlanNode(node.id, move);
      refreshAll();
    } catch (err) {
      const parsed = parseImpactError(err);
      if (parsed?.impact && (parsed.impact.key_change_count || parsed.impact.content_total
        || parsed.impact.notes.count)) {
        setPending({ kind: "move", node, impact: parsed.impact, move });
        return;
      }
      message.error(parsed?.message || String(err));
    }
  };
  const renderImpact = (impact: PlanNodeImpact) => (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      <Typography.Text>
        影响节点：{impact.nodes.map((n) => `${n.key} ${n.title}`).join("、")}
        {impact.node_count > 1 ? `（含 ${impact.node_count - 1} 个子节点）` : ""}
      </Typography.Text>
      <Typography.Text>
        绑定内容：{impact.content_total} 个{kindSummary(impact.content_counts)}；随堂习题：
        {impact.quizzes} 道{impact.locked ? `；其中已锁定讲义 ${impact.locked} 篇` : ""}
      </Typography.Text>
      <Typography.Text>
        笔记库文件：{impact.notes.bound ? `${impact.notes.count} 个` : "未绑定笔记库"}
        {impact.notes.note ? `（${impact.notes.note}）` : ""}
      </Typography.Text>
      {impact.notes.files.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {impact.notes.files.slice(0, 6).join("；")}
          {impact.notes.files.length > 6 ? ` …共 ${impact.notes.count} 个` : ""}
        </Typography.Text>
      )}
      {impact.key_changes.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          编号变化：{impact.key_changes.slice(0, 6).map((c) => `${c.from}→${c.to}`).join("、")}
          {impact.key_changes.length > 6 ? " …" : ""}；下次导出会按新编号重写笔记文件名。
        </Typography.Text>
      )}
    </Space>
  );
  const openRegen = (numChapters?: number) => {
    const chapterCount = (plan?.nodes ?? []).filter((n) => !n.key.includes(".")).length || 4;
    regenForm.setFieldsValue({
      audience: course?.audience ?? "",
      goal: plan?.goal ?? "",
      num_chapters: numChapters ?? chapterCount,
      style: "正式",
      semantic: false,
    });
    setRegenOpen(true);
  };
  const openEdit = (node: PlanNode) => {
    setEditing(node);
    editForm.setFieldsValue({
      title: node.title,
      objective: node.objective,
      teaching_strategy: node.teaching_strategy,
      assessment: node.assessment,
      prerequisites: (node.prerequisites ?? []).join(", "),
    });
  };
  const nodeOptions = useMemo(
    () => (plan?.nodes ?? []).map((n) => ({ value: n.id, label: `${n.key} ${n.title}` })),
    [plan],
  );
  const rowActions = (node: PlanNode, isChapter: boolean) => (
    <Space size={0} style={{ marginLeft: 8 }}>
      {isChapter && (
        <Tooltip title="在本章下新增知识点">
          <Button size="small" type="text" icon={<PlusOutlined />}
                  onClick={(e) => { e.stopPropagation(); openAdd(node); }} />
        </Tooltip>
      )}
      <Tooltip title="同组上移（会重排编号）">
        <Button size="small" type="text" icon={<ArrowUpOutlined />}
                onClick={(e) => { e.stopPropagation(); previewMove(node, { direction: "up" }); }} />
      </Tooltip>
      <Tooltip title="同组下移（会重排编号）">
        <Button size="small" type="text" icon={<ArrowDownOutlined />}
                onClick={(e) => { e.stopPropagation(); previewMove(node, { direction: "down" }); }} />
      </Tooltip>
      <Tooltip title="删除节点（先看影响面）">
        <Button size="small" type="text" danger icon={<DeleteOutlined />}
                onClick={(e) => { e.stopPropagation(); previewDelete(node); }} />
      </Tooltip>
    </Space>
  );
  const treeData = useMemo(() => {
    const all = plan?.nodes ?? [];
    const chapters = chapterNodes(all);
    const rows = chapters.map((chapter) => ({
      key: chapter.id,
      title: <span>{`${chapter.key} ${chapter.title}`}{rowActions(chapter, true)}</span>,
      children: chapterMembers(all, chapter.key)
        .filter((n) => n.id !== chapter.id)
        .map((point) => ({
          key: point.id,
          title: <span>{`${point.key} ${point.title}`}{rowActions(point, false)}</span>,
          children: [],
        })),
    }));
    // 没有挂到任何章下的孤儿节点（历史数据）也要看得见，别让它们凭空消失
    const linked = new Set(rows.flatMap((row) => [row.key, ...row.children.map((c) => c.key)]));
    for (const node of all) {
      if (!linked.has(node.id)) {
        rows.push({ key: node.id, title: <span>{`${node.key} ${node.title}`}{rowActions(node, true)}</span>, children: [] });
      }
    }
    return rows;
  }, [plan]);
  if (courseLoading || planLoading) {
    return <SectionCard loading style={{ minHeight: 220 }} />;
  }
  return (
    <Row gutter={16}>
      <Col span={8}>
        {!hasOutline ? (
          <SectionCard title="生成大纲（会注入原始资料素材）">
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="生成前先确认下面几项"
              description="系统会按你的资料与「受众 / 目标」生成第一版大纲，生成后不满意可重新生成或逐节点修改。"
            />
            <Form form={form} layout="vertical" onFinish={(v) => generate.mutate(v)}>
              <PlanFormFields suggested={suggestedChapters} />
              <Button type="primary" htmlType="submit" loading={generate.isPending} block>
                生成大纲
              </Button>
            </Form>
          </SectionCard>
        ) : (
          <SectionCard title="大纲生成结果">
            <Space direction="vertical" style={{ width: "100%" }} size="middle">
              <Space wrap>
                <Typography.Text strong>状态：</Typography.Text>
                {approved
                  ? <Tag color="green">大纲已通过（锁定）</Tag>
                  : <Tag color="orange">已生成 · 待通过</Tag>}
              </Space>
              <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
                课程目标：{plan?.goal}
              </Typography.Paragraph>
              <Alert
                type={approved ? "success" : "warning"}
                showIcon
                message={approved ? "大纲已锁定，可直接进入下一步" : "大纲还没通过"}
                description={
                  approved
                    ? "如需改动请先「撤销通过」，再重新生成或修改节点。"
                    : "把大纲改到满意后点「大纲通过」锁定，后续图表与内容都按它生成。"
                }
              />
              <Space wrap>
                <Button icon={<ReloadOutlined />} disabled={approved} onClick={() => openRegen()}>
                  重新生成
                </Button>
                <Button icon={<PlusOutlined />} onClick={() => openAdd(null)}>
                  新增章节
                </Button>
                <Button
                  icon={<EditOutlined />}
                  disabled={!firstNode}
                  onClick={() => firstNode && openEdit(firstNode)}
                >
                  逐节点修改
                </Button>
                {approved ? (
                  <Button icon={<UndoOutlined />} loading={approve.isPending} onClick={() => approve.mutate(false)}>
                    撤销通过
                  </Button>
                ) : (
                  <Button
                    type="primary"
                    icon={<CheckOutlined />}
                    loading={approve.isPending}
                    onClick={() => approve.mutate(true)}
                  >
                    大纲通过
                  </Button>
                )}
              </Space>
            </Space>
          </SectionCard>
        )}
      </Col>

      {/* 任务 70 §B2：自检结论**常显** —— 读的是落库记录（GET /plan/{id}/check），
          刷新 / 换设备 / 换人打开都还在；没生成过时给明确空态（原先存内存 state，刷新即丢）。 */}
      <SectionCard title="生成自检（章节数 + 素材要点覆盖 · 落库常显）" style={{ marginTop: 16 }}>
        {check ? (
          <Alert
            type={check.chapter_match && check.material_uncovered_count === 0 ? "success" : "warning"}
            showIcon
            message={(
              <Space wrap size={8}>
                <span>生成自检：章节数 + 素材要点覆盖</span>
                {checkGeneratedAt && (
                  <Typography.Text type="secondary" style={{ fontWeight: "normal", fontSize: 12 }}>
                    上次自检 {formatLocalTime(checkGeneratedAt)}
                  </Typography.Text>
                )}
                <Tag color={check.semantic_enabled ? "blue" : "default"}>
                  语义等价判定：{check.semantic_enabled ? "开" : "关（默认）"}
                </Tag>
              </Space>
            )}
            description={(
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Typography.Text>{check.chapter_notice}</Typography.Text>
                <Typography.Text>{check.material_notice}</Typography.Text>
                {/* 任务 69 §B1.3：本次素材注入情况（用了 N/M 份、注入/原始字数、未参与清单）——
                    原先超限完全静默；接口返回与界面同一处可见。 */}
                {check.material_inject_notice && (
                  <Typography.Text>{check.material_inject_notice}</Typography.Text>
                )}
                {/* 任务 70 §B3：要点按类型汇总（方法 / 步骤 / 编号 / 枚举 / 定义 / 概念 / 案例），
                    直接看出扩面识别到了什么、每类覆盖了几条。 */}
                {check.material_points_by_kind.length > 0 && (
                  <Space wrap size={4}>
                    <Typography.Text type="secondary">要点分布：</Typography.Text>
                    {check.material_points_by_kind.map((row) => (
                      <Tag key={row.kind}>{row.label} {row.covered_count}/{row.total}</Tag>
                    ))}
                  </Space>
                )}
                {/* 任务 70 §B3 + 任务 75 §B3：未覆盖清单**按类型分组**、默认只显示前 8 条
                    （其余「展开看全部」）；后端已把句子碎片过滤掉，条数用 filtered_out_count 如实说明。 */}
                {check.material_uncovered_detail.length > 0 && (
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    {uncoveredLow && (
                      <Typography.Text type="warning" style={{ fontSize: 12 }}>
                        {UNCOVERED_LOW_NOTICE}
                      </Typography.Text>
                    )}
                    <Typography.Text strong>
                      没覆盖的素材要点 + 建议（{check.material_uncovered_detail.length} 条
                      {(check.filtered_out_count ?? 0) > 0
                        ? `；另有 ${check.filtered_out_count} 条句子碎片已过滤` : ""}）：
                    </Typography.Text>
                    {uncoveredGroups.map((group) => (
                      <div key={group.kind}>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {group.label}
                        </Typography.Text>
                        {group.rows.map((row) => (
                          <Typography.Text key={row.point} style={{ display: "block" }}>
                            · {row.point} —— {row.suggestion}
                          </Typography.Text>
                        ))}
                      </div>
                    ))}
                    {check.material_uncovered_detail.length > UNCOVERED_DEFAULT_VISIBLE && (
                      <Button type="link" size="small" style={{ padding: 0 }}
                              onClick={() => setShowAllUncovered((v) => !v)}>
                        {showAllUncovered
                          ? "收起"
                          : `展开看全部（${check.material_uncovered_detail.length} 条）`}
                      </Button>
                    )}
                  </Space>
                )}
                {(check.material_uncovered_count > 0 || !check.chapter_match) && (
                  <>
                    <Typography.Text type="secondary">
                      {check.action_hint}（建议 {check.suggested_chapters} 章）
                    </Typography.Text>
                    <Button size="small" disabled={approved}
                            onClick={() => openRegen(check.suggested_chapters)}>
                      按此补生成
                    </Button>
                  </>
                )}
              </Space>
            )}
          />
        ) : hasOutline ? (
          /* 任务 81 §A3：分清两种情况 —— ① 有大纲但没跑过自检（原先写成「在本页生成一次大纲后」，
             与上面「大纲已通过（锁定）」「39 个节点都在」自相矛盾）；② 压根还没有大纲。 */
          <Alert
            type="info"
            showIcon
            message="这份大纲还没跑过自检"
            description={
              <Space direction="vertical" size={6}>
                <Typography.Text>
                  自检只核对两件事：章节数是否兑现（请求 vs 实际）+ 素材要点覆盖清单与未覆盖建议。
                  它与「生成大纲」是两回事 —— 大纲已经在这儿了（上面状态：
                  {approved ? "大纲已通过（锁定）" : "已生成 · 待通过"}），只是这份结论还没算过。
                  点「立即自检」按当前素材补算一次（不重新生成大纲、不调模型），结论随课程落库，刷新 / 换设备都还在。
                </Typography.Text>
                <Button size="small" type="primary" loading={runCheck.isPending}
                        onClick={() => runCheck.mutate()}>
                  立即自检
                </Button>
              </Space>
            }
          />
        ) : (
          <Alert
            type="info"
            showIcon
            message="还没有大纲，所以也没有自检结论"
            description="先在上方生成一版大纲 —— 生成时会顺带跑一次自检，结论就常显在这里（刷新 / 换设备都还在）。"
          />
        )}
      </SectionCard>
      <Col span={16}>
        <SectionCard
          title="大纲与知识图谱"
          extra={
            <Space>
              {plan && (
                <Button size="small" icon={<EditOutlined />} onClick={() => firstNode && openEdit(firstNode)}>
                  编辑节点
                </Button>
              )}
              <Button size="small" onClick={() => setShowGraph(true)}>
                查看知识图谱
              </Button>
              <Button size="small" icon={<HistoryOutlined />} disabled={!hasOutline}
                      onClick={() => setShowHistory(true)}>
                历史版本
              </Button>
            </Space>
          }
        >
          {plan ? (
            <Tree
              treeData={treeData}
              defaultExpandAll
              onSelect={(keys) => {
                const hit = (plan?.nodes ?? []).find((n) => n.id === keys[0]);
                if (hit) openEdit(hit);
              }}
            />
          ) : (
            <Typography.Text type="secondary">尚无大纲，请在左侧确认受众/目标后生成。</Typography.Text>
          )}
        </SectionCard>
      </Col>
      {/* 任务 71 ②：图谱不再挤在右侧面板（745px 宽）里，改成弹窗 —— 宽 92vw、内容区高 78vh，
          默认缩放的可读兜底在 KnowledgeGraph 内部（MIN_READABLE_ZOOM）。 */}
      <Modal
        title="知识图谱（可拖动 / 滚轮缩放）"
        open={showGraph}
        onCancel={() => setShowGraph(false)}
        footer={null}
        width="92vw"
        destroyOnHidden
        styles={{ content: { padding: 0 }, header: { padding: "12px 16px" }, body: { height: "78vh", padding: 0 } }}
      >
        {plan && <KnowledgeGraph graph={plan.graph} />}
      </Modal>
      <Modal
        title="重新生成大纲（二次确认）"
        open={regenOpen}
        onCancel={() => setRegenOpen(false)}
        footer={null}
        width={640}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="将用新大纲替换当前大纲"
          description="已绑定旧章节生成的正文 / 概念图 / 数据图 / 流程图 / 习题会随旧大纲一并移除。如已通过，请先撤销通过再重新生成。"
        />
        <Form form={regenForm} layout="vertical" onFinish={(v) => generate.mutate(v)}>
          <PlanFormFields suggested={suggestedChapters} />
        </Form>
        <Space style={{ width: "100%", justifyContent: "flex-end" }}>
          <Button onClick={() => setRegenOpen(false)}>取消</Button>
          <Button type="primary" danger loading={generate.isPending} onClick={() => regenForm.submit()}>
            确认重新生成
          </Button>
        </Space>
      </Modal>
      <Modal
        title={`编辑大纲节点${editing ? `：${editing.key} ${editing.title}` : ""}`}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={() => {
          if (!editing) return;
          const v = editForm.getFieldsValue();
          saveNode.mutate({
            nodeId: editing.id,
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
        width={640}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Select
            style={{ width: "100%" }}
            placeholder="切换要修改的节点"
            value={editing?.id}
            options={nodeOptions}
            onChange={(id) => {
              const hit = (plan?.nodes ?? []).find((n) => n.id === id);
              if (hit) openEdit(hit);
            }}
          />
          <Form form={editForm} layout="vertical">
            <Form.Item name="title" label="标题" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <Form.Item name="objective" label="学习目标">
              <Input.TextArea rows={2} />
            </Form.Item>
            <Form.Item name="teaching_strategy" label="教学策略">
              <Input />
            </Form.Item>
            <Form.Item name="assessment" label="评估方式">
              <Input />
            </Form.Item>
            <Form.Item name="prerequisites" label="前置知识点 key（逗号分隔，如 1, 1.1）">
              <Input placeholder="例如：1（表示依赖第 1 章）" />
            </Form.Item>
          </Form>
        </Space>
      </Modal>
      <Modal
        title={addTarget?.parent
          ? `新增知识点：挂在「${addTarget.parent.key} ${addTarget.parent.title}」下`
          : "新增章节"}
        open={!!addTarget}
        onCancel={() => setAddTarget(null)}
        footer={null}
        width={560}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="编号会自动重排"
          description="新增后整份大纲重新编号（1 / 1.1 / 1.2 / 2 …）。已生成的讲义与笔记仍绑在原来的节点上，导出时会按新编号重写文件名。"
        />
        <Form form={addForm} layout="vertical"
              onFinish={(v) => createNode.mutate({
                title: v.title, objective: v.objective, parent_id: addTarget?.parent?.id ?? null,
              })}>
          <Form.Item name="title" label="标题" rules={[{ required: true, message: "填写标题" }]}>
            <Input placeholder={addTarget?.parent ? "如：方法三：手动 RAG" : "如：第 4 章 实战演练"} />
          </Form.Item>
          <Form.Item name="objective" label="学习目标（可选）">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Space style={{ width: "100%", justifyContent: "flex-end" }}>
            <Button onClick={() => setAddTarget(null)}>取消</Button>
            <Button type="primary" htmlType="submit" loading={createNode.isPending}>新增</Button>
          </Space>
        </Form>
      </Modal>
      <Modal
        title={pending?.kind === "delete" ? "删除节点（二次确认）" : "移动节点（二次确认）"}
        open={!!pending}
        onCancel={() => setPending(null)}
        footer={null}
        width={640}
      >
        {pending && (
          <>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={pending.kind === "delete"
                ? "删除后无法从界面撤销"
                : "移动会重排编号，导出文件名随之变化"}
              description={pending.kind === "delete"
                ? "确认后会连带删除下面列出的内容条目与习题；笔记库里的文件会在下次导出时被清理。"
                : "讲义正文与图不会丢（仍绑在同一个节点上），但笔记文件名会按新编号重写。"}
            />
            {renderImpact(pending.impact)}
            <Space style={{ width: "100%", justifyContent: "flex-end", marginTop: 16 }}>
              <Button onClick={() => setPending(null)}>取消</Button>
              {pending.kind === "delete" ? (
                <Button
                  danger
                  type="primary"
                  loading={deleteNode.isPending}
                  onClick={() => deleteNode.mutate({
                    nodeId: pending.node.id,
                    cascade: pending.impact.content_total > 0 || pending.impact.quizzes > 0,
                  })}
                >
                  确认删除
                </Button>
              ) : (
                <Button
                  type="primary"
                  loading={moveNode.isPending}
                  onClick={() => moveNode.mutate({
                    nodeId: pending.node.id,
                    data: { ...(pending.move ?? {}), confirm: true },
                  })}
                >
                  确认移动
                </Button>
              )}
            </Space>
          </>
        )}
      </Modal>
      <PlanHistoryModal courseId={courseId} open={showHistory}
                        onClose={() => setShowHistory(false)} />
    </Row>
  );
}
