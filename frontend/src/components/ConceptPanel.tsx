import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Empty, Popover, Space, Table, Tag, Tooltip, Typography } from "antd";
import { DeleteOutlined, EditOutlined, FileTextOutlined, PartitionOutlined, ReloadOutlined } from "@ant-design/icons";
import SectionCard from "./ui/SectionCard";
import ConceptGraphModal from "./ConceptGraphModal";
import ConceptDetailModal from "./ConceptDetailModal";
import { api } from "../api";
import { requestAutoSync } from "../syncNotes";
import type { ConceptRow } from "../types";

function ChapterTags({ chapters }: { chapters: string[] }) {
  if (!chapters.length) return <Typography.Text type="secondary">—</Typography.Text>;
  return (
    <Space size={4} wrap>
      {chapters.map((key) => <Tag key={key} color="blue">{key}</Tag>)}
    </Space>
  );
}

function StatusTag({ edited }: { edited: boolean }) {
  return edited ? <Tag color="orange">人工编辑</Tag> : <Tag>自动抽取</Tag>;
}

/**
 * 概念库（原子笔记 v1）面板：
 * - chapterKey 为空 → 整课概念列表（第 5 步 / 高级工具）；
 * - chapterKey 有值 → 只列该章出现的概念（第 3 步章节点面板）。
 * 生成 / 重新抽取走后端异步 job（LLM 抽取），失败显式上报；大纲未通过时整体禁用。
 */
export default function ConceptPanel({
  courseId, chapterKey = null, disabled = false, title = "概念库（原子笔记）",
}: {
  courseId: string;
  chapterKey?: string | null;
  disabled?: boolean;
  title?: string;
}) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  const [opened, setOpened] = useState<ConceptRow | null>(null);
  const { data: course } = useQuery({
    queryKey: ["course", courseId], queryFn: () => api.getCourse(courseId), retry: false,
  });
  const { data } = useQuery({
    queryKey: ["concepts", courseId], queryFn: () => api.listConcepts(courseId), retry: false,
  });
  // P6-1：图谱里的「孤点」与「指向库外的关系目标」，以前只能在图里看着，没有补抽入口
  const graph = useQuery({
    queryKey: ["conceptGraphLite", courseId],
    queryFn: () => api.conceptGraph(courseId, "mermaid"),
    enabled: (data?.concepts ?? []).length > 0,
    retry: false,
  });
  const approved = course?.status === "approved";
  const locked = disabled || !approved;
  const all = data?.concepts ?? [];
  const rows = chapterKey
    ? all.filter((c) => c.chapters.some((key) => key.split(".")[0] === chapterKey))
    : all;
  const stats = data?.stats;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["concepts", courseId] });
    requestAutoSync(courseId);
  };

  const poll = async (jobId: string) => {
    message.info("概念抽取中（LLM 生成定义 / 要点 / 关系）…");
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      const status = await api.conceptJobStatus(jobId);
      if (status.status === "success") {
        const filtered = Number(status.result?.filtered_count ?? 0);
        const names = (status.result?.names as string[] | undefined) ?? [];
        message.success(
          `概念库已更新：${names.length} 个概念` + (filtered ? `（过滤 ${filtered} 个不达标候选）` : ""),
        );
        setError(null);
        refresh();
        return;
      }
      if (status.status === "failed") {
        setError(status.error || "概念抽取失败");
        refresh();
        return;
      }
    }
    message.warning("仍在抽取中，请稍后手动刷新");
  };

  const generate = useMutation({
    mutationFn: () => api.generateConcepts(courseId),
    onSuccess: ({ job_id }) => { void poll(job_id); },
    onError: (e) => setError(String(e)),
  });

  const markEdited = useMutation({
    mutationFn: (id: string) => api.updateConcept(id, {}),
    onSuccess: () => { message.success("已标记为人工编辑（重抽时不再覆盖）"); refresh(); },
    onError: (e) => message.error(String(e)),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteConcept(id),
    onSuccess: () => { message.success("已删除该概念"); refresh(); },
    onError: (e) => message.error(String(e)),
  });

  // P6-1：库外关系目标 / 孤点 → 一键补抽（只动这批名字，不会删掉概念库其余条目）
  const isolated = graph.data?.isolated ?? [];
  const droppedTargets = Array.from(
    new Set((graph.data?.dropped_relations ?? []).map((row) => row.target).filter(Boolean)),
  );
  const supplement = useMutation({
    mutationFn: (names: string[]) => api.supplementConcepts(courseId, names),
    onSuccess: (res) => {
      const extra = res.missing.length ? `；未得到 ${res.missing.join("、")}` : "";
      message.success(
        `已补抽 ${res.concepts} 个概念（新建 ${res.created} / 更新 ${res.updated}）`
        + `；过滤 ${res.filtered_count} 个不达标候选${extra}`,
      );
      refresh();
      qc.invalidateQueries({ queryKey: ["conceptGraphLite", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });

  return (
    <SectionCard size="small" title={title}>
      <Space wrap style={{ marginBottom: 8 }}>
        <Button
          type="primary"
          icon={<ReloadOutlined />}
          disabled={locked || generate.isPending}
          loading={generate.isPending}
          onClick={() => generate.mutate()}
        >
          {all.length ? "重新抽取（整课）" : "抽取概念"}
        </Button>
        <Button
          icon={<PartitionOutlined />}
          disabled={locked || !all.length}
          onClick={() => setGraphOpen(true)}
        >
          查看图谱
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          概念 {stats?.count ?? all.length} 个
          {chapterKey ? ` · 本章 ${rows.length} 个` : ""}
          {stats ? ` · 已织双链 ${stats.linked_relations} 条` : ""}
        </Typography.Text>
        {!!rows.length && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            点概念名即可<span style={{ color: "#1677ff" }}>打开查看 / 修改</span>定义、要点、误区、别名与关系（保存后自动同步到笔记）
          </Typography.Text>
        )}
        {stats && <Tag color={stats.broken_links ? "red" : "green"}>坏链 {stats.broken_links}</Tag>}
        {isolated.length > 0 && (
          <Popover
            title="孤点：在概念库里，但没有任何关系，图上是孤立点"
            content={(
              <div style={{ maxWidth: 420, fontSize: 12 }}>
                {isolated.map((name) => <div key={name}>{name}</div>)}
              </div>
            )}
          >
            <Tag color="gold">孤点 {isolated.length}</Tag>
          </Popover>
        )}
        {isolated.length > 0 && (
          <Button
            size="small"
            disabled={locked || supplement.isPending}
            loading={supplement.isPending}
            onClick={() => supplement.mutate(isolated)}
          >
            给孤点补关系
          </Button>
        )}
        {droppedTargets.length > 0 && (
          <Popover
            title="关系里写了、但概念库里没有的目标（现在只能当纯文本）"
            content={(
              <div style={{ maxWidth: 420, fontSize: 12 }}>
                {droppedTargets.map((name) => <div key={name}>{name}</div>)}
              </div>
            )}
          >
            <Tag color="orange">库外关系目标 {droppedTargets.length}</Tag>
          </Popover>
        )}
        {droppedTargets.length > 0 && (
          <Button
            size="small"
            type="primary"
            ghost
            disabled={locked || supplement.isPending}
            loading={supplement.isPending}
            onClick={() => supplement.mutate(droppedTargets)}
          >
            一键补抽这些概念
          </Button>
        )}
        {!!stats?.unresolved?.length && (
          <Popover
            title="关系目标在概念库里不存在：已按纯文本渲染（导出后仍 0 坏链）"
            content={(
              <div style={{ maxWidth: 420 }}>
                {stats.unresolved.map((item, index) => (
                  <div key={index} style={{ fontSize: 12 }}>
                    {item.concept} · {item.section}：{item.target}
                  </div>
                ))}
              </div>
            )}
          >
            <a style={{ fontSize: 12 }}>未解析关系 {stats.unresolved_relations}</a>
          </Popover>
        )}
      </Space>
      {!approved && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message="大纲尚未通过"
          description="请先在第 2 步点「大纲通过」锁定大纲，概念库以锁定大纲与讲义为依据。"
        />
      )}
      {rows.length === 0 ? (
        <Empty
          description={chapterKey
            ? "本章暂无概念：点「抽取概念」会在整课范围内抽取（概念库是单一真相源）"
            : "还没有概念：先通过大纲，再点「抽取概念」（LLM 抽取，失败会显式报错）"}
        />
      ) : (
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={rows}
          columns={[
            {
              title: "概念",
              render: (_, row: ConceptRow) => (
                <Tooltip title={`点开可查看/修改定义、要点、误区、别名与关系 · 业务 id：${row.concept_id}`}>
                  <Typography.Link strong onClick={() => setOpened(row)}>{row.name}</Typography.Link>
                </Tooltip>
              ),
            },
            {
              title: "别名",
              render: (_, row: ConceptRow) => (row.aliases.length ? row.aliases.join("、") : "—"),
            },
            { title: "出现章节", render: (_, row: ConceptRow) => <ChapterTags chapters={row.chapters} /> },
            { title: "状态", render: (_, row: ConceptRow) => <StatusTag edited={row.edited} /> },
            {
              title: "操作",
              render: (_, row: ConceptRow) => (
                <Space>
                  <a onClick={() => setOpened(row)}><FileTextOutlined /> 打开 / 修改</a>
                  <a onClick={() => markEdited.mutate(row.id)}><EditOutlined /> 标记人工编辑</a>
                  <a onClick={() => del.mutate(row.id)} style={{ color: "#c0392b" }}>
                    <DeleteOutlined /> 删除
                  </a>
                </Space>
              ),
            },
          ]}
        />
      )}
      {error && <Alert type="error" showIcon style={{ marginTop: 8 }} message={error} />}
      <ConceptGraphModal courseId={courseId} open={graphOpen} onClose={() => setGraphOpen(false)} />
      <ConceptDetailModal courseId={courseId} concept={opened} onClose={() => setOpened(null)} />
    </SectionCard>
  );
}
