import { useMemo, useRef, useState } from "react";
import { Alert, Button, Checkbox, Collapse, List, Popconfirm, Progress, Space, Tag, Tooltip, Typography, message } from "antd";
import { ExperimentOutlined, PauseCircleOutlined, PlayCircleOutlined, ReloadOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import SectionCard from "./ui/SectionCard";
import { api } from "../api";
import type { PlanNode, StyleAnchor, UsageSummary } from "../types";
import { chapterNodes, leavesOf } from "../lib/planLeaves";
import { LECTURE_COST_UNIT, estimateCostText } from "../lib/genCost";
import { usageText } from "./UsageBadge";

type BatchResult = {
  ok?: number;
  failed?: Array<{ node_key: string; error: string }>;
  locked_skipped?: Array<{ node_key: string; title?: string; reason: string }>;
  skipped?: Array<{ node_key: string; title?: string; item_id?: string; reason: string }>;
  diagram_failed?: Array<{ node_key?: string; slot?: string; reason?: string }>;
  usage?: UsageSummary | null;
  total?: number;
  done?: number;
  current?: string | null;
  status?: string;
  chapter_key?: string;
  chapter_title?: string;
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export default function StyleAnchorPanel({
  courseId, nodes, approved, generatedNodeIds, activeChapter, onChanged, onSelectNode,
}: {
  courseId: string;
  nodes: PlanNode[];
  approved: boolean;
  generatedNodeIds: string[];
  activeChapter: PlanNode | null;
  onChanged: () => void;
  onSelectNode: (id: string) => void;
}) {
  const qc = useQueryClient();
  const { data: anchorData } = useQuery({
    queryKey: ["style-anchor", courseId],
    queryFn: () => api.getStyleAnchor(courseId),
    retry: false,
  });
  const anchor: StyleAnchor | null = anchorData?.style_anchor ?? null;
  // P4-1：设样板会自动给样板节加锁；没锁时批量会覆盖它，所以要显式确认
  const anchorLocked = anchorData?.locked ?? false;
  const overwriteAnchor = Boolean(anchor) && !anchorLocked;
  const [panel, setPanel] = useState<BatchResult | null>(null);
  const [running, setRunning] = useState<{ index: number; total: number; key: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [ignoreAnchor, setIgnoreAnchor] = useState(false);
  const [force, setForce] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [lockBusy, setLockBusy] = useState(false);
  const stopRef = useRef(false);

  const chapters = useMemo(() => chapterNodes(nodes), [nodes]);
  const leavesOfChapter = (chapterKey: string) => leavesOf(nodes, chapterKey);
  const generated = useMemo(() => new Set(generatedNodeIds), [generatedNodeIds]);
  const sampleNode = useMemo(() => {
    const first = chapters[0];
    const pool = first ? leavesOfChapter(first.key) : nodes;
    return pool[0] ?? nodes[0];
  }, [nodes, chapters]);
  const pendingChapters = useMemo(
    () => chapters.filter((ch) => leavesOfChapter(ch.key).some((leaf) => !generated.has(leaf.id))),
    [chapters, nodes, generated],
  );
  // 整门课都已生成过（pendingChapters 为 0）时，仍提供「按章重做全部」——
  // 否则老课程没有任何按章套用样板的入口，只能删课重来。
  const redoAll = pendingChapters.length === 0 && chapters.length > 0;
  const batchQueue = redoAll ? chapters : pendingChapters;
  const activeLeaves = activeChapter ? leavesOfChapter(activeChapter.key) : [];
  // 任务 86 §B3：**篇数口径 = 章内知识点数**（一章批量 = 该章每节各一篇讲义）。
  // 金额只是**提示**（已审核锁定的会被后端跳过 ⇒ 实际 ≤ 这个数），不接支付、不参与任何判断。
  const batchArticles = useMemo(
    () => batchQueue.reduce((sum, ch) => sum + leavesOfChapter(ch.key).length, 0), [batchQueue, nodes],
  );
  /** 按钮上的成本后缀（篇数为 0 时不给「预估 」这样的空尾巴）。 */
  const costOf = (count: number) => {
    const text = estimateCostText(count);
    return text ? ` · 预估 ${text}` : "";
  };
  const activeTodo = activeLeaves.filter((leaf) => !generated.has(leaf.id));
  const activeLocked = activeLeaves.length - activeTodo.length;

  async function pollJob(jobId: string, label: string): Promise<BatchResult | null> {
    for (let i = 0; i < 900; i++) {
      await sleep(1000);
      const state = await api.jobStatus(jobId);
      setPanel((state.result ?? null) as BatchResult | null);
      if (state.status === "success") return (state.result ?? null) as BatchResult | null;
      if (state.status === "failed") {
        message.error(state.error || `${label} 生成失败`);
        return null;
      }
    }
    message.warning(`${label} 仍在生成中，稍后可在本面板或「课程预览」查看结果`);
    return null;
  }

  async function submitChapter(chapter: PlanNode, index: number, total: number): Promise<BatchResult | null> {
    setRunning({ index, total, key: chapter.key });
    const queued = await api.generateChapterLectures({
      course_id: courseId, chapter_node_id: chapter.id, tone: "正式", force,
      allow_anchor_overwrite: overwriteAnchor,
    });
    const result = await pollJob(queued.job_id, `第 ${chapter.key} 章`);
    onChanged();
    if (result) {
      const skipped = result.locked_skipped?.length ?? 0;
      const fingerprintSkipped = result.skipped?.length ?? 0;
      message.success(
        `第 ${chapter.key} 章已完成：成功 ${result.ok ?? 0}/${result.total ?? 0} 个知识点` +
          (skipped ? `，跳过已锁定 ${skipped} 个` : "") +
          (fingerprintSkipped ? `，输入未变跳过 ${fingerprintSkipped} 个` : "") +
          (result.failed?.length ? `，失败 ${result.failed.length} 个` : ""),
      );
    }
    setRunning(null);
    return result;
  }

  async function genThisChapter() {
    if (!activeChapter) return;
    setBusy(true);
    stopRef.current = false;
    try {
      await submitChapter(activeChapter, 1, 1);
    } catch (e) {
      message.error(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function genRemaining() {
    setBusy(true);
    stopRef.current = false;
    try {
      const queue = batchQueue;
      for (let i = 0; i < queue.length; i++) {
        if (stopRef.current) {
          message.info(`已在第 ${i} 章后停止，已完成的章节保留，随时可继续`);
          break;
        }
        await submitChapter(queue[i], i + 1, queue.length);
      }
    } catch (e) {
      message.error(String(e));
    } finally {
      setBusy(false);
      setRunning(null);
    }
  }

  async function genSample() {
    if (!sampleNode) return;
    setBusy(true);
    try {
      if (generated.has(sampleNode.id)) {
        onSelectNode(sampleNode.id);
        message.info(`${sampleNode.key} 已有讲义：已打开该节，点讲义卡上的「以这篇为样板」即可`);
        return;
      }
      await api.generateLecture({ course_id: courseId, node_id: sampleNode.id, tone: "正式" });
      onChanged();
      onSelectNode(sampleNode.id);
      message.success("样板讲义已生成：满意后点讲义卡上的「以这篇为样板」固化风格");
    } catch (e) {
      message.error(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function retryNode(nodeKey: string) {
    const node = nodes.find((n) => n.key === nodeKey);
    if (!node) {
      message.warning(`找不到知识点 ${nodeKey}，请刷新后重试`);
      return;
    }
    setRetrying(nodeKey);
    try {
      await api.generateLecture({ course_id: courseId, node_id: node.id, tone: "正式" });
      onChanged();
      onSelectNode(node.id);
      message.success(`${nodeKey} 已单点重做`);
    } catch (e) {
      message.error(String(e));
    } finally {
      setRetrying(null);
    }
  }

  async function toggleAnchorLock(locked: boolean) {
    setLockBusy(true);
    try {
      await api.lockStyleAnchorSource(courseId, locked);
      await qc.invalidateQueries({ queryKey: ["style-anchor", courseId] });
      message.success(locked
        ? "样板节已加锁：按章 / 整课批量都会跳过它"
        : "样板节已解锁：下次批量会重做这一节（换样板时才需要）");
    } catch (e) {
      message.error(String(e));
    } finally {
      setLockBusy(false);
    }
  }

  async function clearAnchor() {
    try {
      await api.clearStyleAnchor(courseId);
      await qc.invalidateQueries({ queryKey: ["style-anchor", courseId] });
      message.success("已清除样板：回到「先生成一节样板」");
    } catch (e) {
      message.error(String(e));
    }
  }

  async function runIgnoreAnchorAll() {
    setBusy(true);
    try {
      const queued = await api.generateAllLectures({
        course_id: courseId, tone: "正式", ignore_style_anchor: true, force,
        allow_anchor_overwrite: true,
      });
      await pollJob(queued.job_id, "整门课");
      onChanged();
      message.success("整门课讲义已生成（本次未套用样板风格锚）");
    } catch (e) {
      message.error(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!approved) return null;

  const percent = panel?.total ? Math.round(((panel.done ?? 0) / panel.total) * 100) : 0;
  // 批量按钮的文案**只在这里写一次**（任务 86：模板字符串必须写 ${costOf(...)}，
  // 且这两个文案在源码里各自只出现一次，变异自检才能真的抓到回归）。
  const batchLabel = redoAll
    ? `按章重做全部（${batchQueue.length} 章${costOf(batchArticles)}）`
    : `按章生成剩余全部（${batchQueue.length} 章${costOf(batchArticles)}）`;
  const batchNeedAnchor = !anchor && !ignoreAnchor;

  return (
    <SectionCard size="small">
      <Space direction="vertical" style={{ width: "100%" }} size="small">
        {anchor ? (
          <Alert
            type="success"
            showIcon
            message={`当前样板：${anchor.source_node_key}《${anchor.source_title}》`}
            description={
              <Space wrap size={4}>
                <Tag color="blue">讲法：{anchor.tone}</Tag>
                <Tag color="purple">深度：{anchor.depth}</Tag>
                <Tag color="cyan">图：{anchor.figure_style}</Tag>
                <Tag color="orange">习题：{anchor.quiz_difficulty}</Tag>
                <Tag color={anchorLocked ? "green" : "red"}>
                  {anchorLocked ? "样板节已锁定（批量不覆盖）" : "样板节未加锁（批量会覆盖）"}
                </Tag>
                {anchor.terminology?.slice(0, 6).map((t) => <Tag key={t}>{t}</Tag>)}
                <Button size="small" onClick={() => { if (sampleNode) onSelectNode(sampleNode.id); message.info("打开任意一篇讲义，点卡上的「以这篇为样板」即可更换"); }}>
                  更换样板
                </Button>
                <Popconfirm
                  title={anchorLocked
                    ? "解锁样板节？解锁后按章 / 整课批量会把它一起重做（换样板时才需要）。"
                    : "给样板节加锁？加锁后按章 / 整课批量都会跳过它，不会覆盖这一版。"}
                  okText={anchorLocked ? "解锁" : "加锁"}
                  onConfirm={() => toggleAnchorLock(!anchorLocked)}
                >
                  <Button size="small" loading={lockBusy}>
                    {anchorLocked ? "解锁样板节" : "加锁样板节"}
                  </Button>
                </Popconfirm>
                <Popconfirm title="清除样板锚？之后的生成不再套用风格。" onConfirm={clearAnchor}>
                  <Button size="small" danger>清除样板</Button>
                </Popconfirm>
                <Typography.Text type="secondary" style={{ fontSize: 12, width: "100%" }}>
                  {anchorData?.lock_notice}
                </Typography.Text>
              </Space>
            }
          />
        ) : (
          <Alert
            type="info"
            showIcon
            message="还没设样板：建议先做一节样板，满意后固化风格，再按章批量"
            description={
              <Space wrap>
                <Button
                  type="primary"
                  icon={<ExperimentOutlined />}
                  loading={busy}
                  disabled={!sampleNode}
                  onClick={genSample}
                >
                  先生成一节样板{sampleNode ? `（${sampleNode.key} ${sampleNode.title}）` : ""}
                </Button>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  生成后到该节讲义卡点「以这篇为样板」；不满意就重做这一节，不影响整门课。
                </Typography.Text>
              </Space>
            }
          />
        )}

        <Space wrap>
          <Popconfirm
            title={
              `生成 ${activeChapter?.key ?? ""} 章共 ${activeLeaves.length} 个知识点（已定稿 ${activeLocked} 个会跳过）？`
              + `\n预估花费 ${estimateCostText(activeLeaves.length) || "≈¥0.00"}`
              + `（${activeLeaves.length} 篇 × ${LECTURE_COST_UNIT}，只做提示、不自动扣费）。`
              + (overwriteAnchor ? "\n注意：样板节未加锁，本次会把它一起重做。" : "")
            }
            okText="开始生成"
            disabled={!activeChapter || activeLeaves.length === 0}
            onConfirm={genThisChapter}
          >
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              loading={busy && running?.total === 1}
              disabled={!activeChapter || activeLeaves.length === 0}
            >
              生成本章（{activeLeaves.length} 个知识点{costOf(activeLeaves.length)}）
            </Button>
          </Popconfirm>
                    {batchNeedAnchor ? (
            // 缺样板锚时**不要**把按钮做成灰的（点了毫无反应，用户会以为坏了）。
            // 保持可点，点下去直接说清「缺什么、去哪点」——2026-10-21 用户实测反馈。
            <Tooltip title="批量前需要先有一节样板：生成样课后，到该节讲义卡上点「以这篇为样板」">
              <Button
                icon={<ThunderboltOutlined />}
                disabled={busy || batchQueue.length === 0}
                onClick={() => message.warning(
                  "还差一步：先点讲义卡上的「以这篇为样板」固化风格，再回来批量；"
                  + "若确实不想用样板，展开「高级选项」勾选「忽略样板，一次性生成整门课」。",
                )}
              >
                {batchLabel}
              </Button>
            </Tooltip>
          ) : (
            <Popconfirm
              title={(redoAll
                ? `整课重跑 ${batchArticles} 篇，预估 ${estimateCostText(batchArticles) || "≈¥0.00"}`
                  + `（已审核锁定的会跳过，实际不会超过这个数）？`
                : `按章生成剩余 ${batchQueue.length} 章共 ${batchArticles} 篇，`
                  + `预估 ${estimateCostText(batchArticles) || "≈¥0.00"}？`)
                + `\n共 ${batchQueue.length} 章 / ${batchArticles} 篇 × ${LECTURE_COST_UNIT}（只做提示、不自动扣费）。`
                + (overwriteAnchor ? "\n注意：样板节未加锁，本次会把它一起重做。" : "")}
              okText={redoAll ? "重做全部" : "开始生成"}
              // 2026-10-21 用户实测：`!redoAll` 会让「按章生成剩余全部」这个分支的
              // Popconfirm 永远 disabled —— 点了不弹确认框、什么也不发生，用户以为坏了。
              disabled={busy || batchQueue.length === 0}
              onConfirm={genRemaining}
            >
              <Button
                icon={<ThunderboltOutlined />}
                loading={busy && running?.total !== 1}
                danger={redoAll}
                disabled={busy || batchQueue.length === 0}
              >
                {batchLabel}
              </Button>
            </Popconfirm>
          )}
          {busy && (
            <Button danger icon={<PauseCircleOutlined />} onClick={() => { stopRef.current = true; message.info("将在当前章节结束后停止"); }}>
              停止
            </Button>
          )}
          {!anchor && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              按章批量需先设样板（或在高级选项里显式忽略样板）
            </Typography.Text>
          )}
        </Space>

        <Space wrap size={8}>
          <Checkbox checked={force} onChange={(e) => setForce(e.target.checked)}>
            强制重生成（忽略输入指纹）
          </Checkbox>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            默认按输入指纹去重：素材段落/大纲字段/样板锚/提示词与质检版本都没变的知识点会直接跳过，不产生新调用；
            改过大纲或换过样板的那几节仍会正常重生成。「按质检建议重生成」永远执行，不受此开关与指纹影响。
          </Typography.Text>
        </Space>

        {(running || panel) && (
          <div style={{ background: "#fafafa", border: "1px solid #f0f0f0", borderRadius: 6, padding: 10 }}>
            <Space direction="vertical" style={{ width: "100%" }} size={4}>
              <Space wrap>
                {running && <Tag color="processing">第 {running.index}/{running.total} 章 · {running.key}</Tag>}
                <Tag color="green">成功 {panel?.ok ?? 0}</Tag>
                <Tag color="red">失败 {panel?.failed?.length ?? 0}</Tag>
                <Tag color="default">跳过(已锁定) {panel?.locked_skipped?.length ?? 0}</Tag>
                <Tag color="default">跳过(输入未变) {panel?.skipped?.length ?? 0}</Tag>
              </Space>
              <Typography.Text style={{ fontSize: 12 }}>
                当前知识点：{panel?.current || "—"}（{panel?.done ?? 0}/{panel?.total ?? 0}）
              </Typography.Text>
              <Progress percent={percent} size="small" status={panel?.failed?.length ? "exception" : "active"} />
              {!!panel?.locked_skipped?.length && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  已跳过（人工定稿/审核锁定，批量不覆盖）：{panel.locked_skipped.map((s) => s.node_key).join("、")} —— 需要重做请打开该节讲义点「解锁并重做」。
                </Typography.Text>
              )}
              {!!panel?.skipped?.length && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  已跳过（{panel.skipped[0]?.reason || "输入未变"}，本次未产生新调用）：{panel.skipped.map((s) => s.node_key).join("、")} —— 需要重做请勾选上方「强制重生成」再跑。
                </Typography.Text>
              )}
              {usageText(panel?.usage) && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  本次用量：{usageText(panel?.usage)}
                </Typography.Text>
              )}
              {!!panel?.failed?.length && (
                <List
                  size="small"
                  dataSource={panel.failed}
                  renderItem={(item) => (
                    <List.Item
                      actions={[
                        <Button
                          key="retry"
                          size="small"
                          icon={<ReloadOutlined />}
                          loading={retrying === item.node_key}
                          onClick={() => retryNode(item.node_key)}
                        >
                          单点重做
                        </Button>,
                      ]}
                    >
                      <Typography.Text style={{ fontSize: 12 }}>
                        {item.node_key} 失败：{item.error}
                      </Typography.Text>
                    </List.Item>
                  )}
                />
              )}
            </Space>
          </div>
        )}

        <Collapse
          size="small"
          items={[
            {
              key: "advanced",
              label: <Typography.Text type="secondary" style={{ fontSize: 13 }}>高级选项（不常用）</Typography.Text>,
              children: (
                <Space direction="vertical" style={{ width: "100%" }} size="small">
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    一次性生成整门课最快，但风格方差大、返工面也大；正常流程请走「样板 → 按章」。
                  </Typography.Text>
                  <Checkbox checked={ignoreAnchor} onChange={(e) => setIgnoreAnchor(e.target.checked)}>
                    忽略样板，一次性生成整门课
                  </Checkbox>
                  <Popconfirm
                    title="将忽略样板风格锚，一次性覆盖生成整门课讲义（已锁定的会跳过）。确定继续？"
                    okText="开始生成"
                    onConfirm={runIgnoreAnchorAll}
                  >
                    <Button danger disabled={!ignoreAnchor} loading={busy}>
                      忽略样板，一次性生成整门课
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Space>
    </SectionCard>
  );
}
