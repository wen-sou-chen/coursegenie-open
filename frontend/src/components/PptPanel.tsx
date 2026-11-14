import { useState } from "react";
import SectionCard from "./ui/SectionCard";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Checkbox, Empty, Progress, Select, Space, Table, Tag, Tooltip, Typography, Upload } from "antd";
import { DownloadOutlined, EyeOutlined, ReadOutlined, ReloadOutlined, UploadOutlined } from "@ant-design/icons";
import { api } from "../api";
import { requestAutoSync } from "../syncNotes";
import { chapterLabel as fmtChapter } from "../lib/chapterLabel";
import { deckHtmlErrorNotice } from "../lib/previewFilters";
import {
  THEME_APPLY_NOTE, THEME_LOADING_LINE, courseThemeId, currentThemeLine, themeSelectOptions,
} from "../lib/pptTheme";
import type { PlanNode } from "../types";
import { DeckPreview, FigureFailures, LayoutNotes, SlideImagesBlock, engineTag, figureTag, slideLabel } from "./PptDeckBlocks";
import ScenarioBadge from "./ScenarioBadge";
import type { PptDeck } from "../types";

type BatchProgress = { index: number; total: number; label: string; status: string; error?: string | null };

// **只是首屏兜底**：后端 /api/ppt/themes 到位后一律以它为准；这里也不含「本课当前主题」
// （那是每门课各不相同的，写死必然错 —— 所以后端没答复时下拉显示「读取中」、不预选第一项）
const FALLBACK_THEMES = [
  { id: "clean", name: "清简教学", description: "白底低饱和、大留白" },
  { id: "professional", name: "专业培训", description: "深色标题条、蓝灰体系" },
  { id: "handdrawn", name: "手绘亲和", description: "暖色纸感、圆角" },
  { id: "modern", name: "现代简约", description: "深墨字、高饱和强调、圆角无标题条" },
  { id: "navy", name: "深蓝商务", description: "深蓝页眉带、白字标题、蓝绿克制" },
];
const ENGINE_OPTIONS = [
  { value: "python-pptx", label: "python-pptx（默认 · 离线可测）" },
  { value: "dashi", label: "dashi-ppt（可选 · 失败自动回退）" },
];

type Scope = "course" | "chapter";

/** 按章节号（`chapter_key`）找该章课件：老数据 node_id 为 NULL 时的唯一可靠匹配口径。 */
function deckOfChapter(decks: PptDeck[], key: string): PptDeck | undefined {
  return decks.find((d) => String(d.chapter_key ?? "") === String(key));
}

/** 整课 + 各章「已生成/共几章」——用于「补齐全部章节课件」按钮文案。 */
function decorateCounts(chapters: PlanNode[], decks: PptDeck[]) {
  const done = chapters.filter((n) => deckOfChapter(decks, n.key)).length;
  return { done, total: chapters.length, missing: chapters.length - done };
}

/** 单个目标（整课 / 某一章）的课件卡片：生成 / 重做 / 导入 / 下载 / 删除 + 失败重试。 */
function PptTargetPanel({ courseId, scope, chapterNodeId = null, title, disabled = false }: {
  courseId: string;
  scope: Scope;
  chapterNodeId?: string | null;
  title: string;
  disabled?: boolean;
}) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  // 任务 145：主题默认选中**本课当前主题**（后端唯一出处），不是第一个、也不是全局默认；
  // 用户改过就用改过的（保存成功后与后端回读值一致）
  const [themePicked, setThemePicked] = useState<string | null>(null);
  const [engine, setEngine] = useState<"python-pptx" | "dashi">("python-pptx");
  const [error, setError] = useState<string | null>(null);
  const { data } = useQuery({ queryKey: ["ppt", courseId], queryFn: () => api.listPpt(courseId) });
  const { data: themeData } = useQuery({
    queryKey: ["ppt-themes", courseId], queryFn: () => api.pptThemes(courseId), staleTime: 300_000,
  });
  const { data: plan } = useQuery({
    queryKey: ["plan", courseId], queryFn: () => api.getPlan(courseId), retry: false,
  });
  const chapterKey = scope === "chapter"
    ? ((plan?.nodes ?? []).find((n) => n.id === chapterNodeId)?.key ?? null)
    : null;
  const themeOptions = themeData?.items?.length ? themeData.items : FALLBACK_THEMES;
  const courseTheme = courseThemeId(themeData);            // 本课当前主题（后端说了算）
  const theme = themePicked ?? courseTheme;
  const themeLine = currentThemeLine(themeData);
  // 章节课件按 node_id（新数据）或 chapter_key（老数据 node_id 为 NULL）匹配，两者取其一
  const deck = scope === "course"
    ? (data?.course ?? [])[0]
    : (data?.chapters ?? []).find((d) => d.chapter_node_id === chapterNodeId)
      ?? (data?.chapters ?? []).find(
        (d) => String(d.chapter_key ?? "") === String(chapterKey ?? ""),
      );

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["ppt", courseId] });
    requestAutoSync(courseId);
  };

  const poll = async (jobId: string) => {
    message.info("课件生成中…");
    for (let i = 0; i < 36; i++) {
      await new Promise((r) => setTimeout(r, 5000));
      const status = await api.pptJobStatus(jobId);
      if (status.status === "success") {
        const fallback = (status.result?.engine_error as string | undefined) ?? "";
        if (fallback) message.warning(fallback);
        else message.success("课件已生成");
        setError(null);
        refresh();
        return;
      }
      if (status.status === "failed") {
        setError(status.error || "课件生成失败");
        refresh();
        return;
      }
    }
    message.warning("仍在生成中，请稍后手动刷新");
  };

  const generate = useMutation({
    mutationFn: (itemId?: string) => api.generatePpt({
      course_id: courseId,
      scope,
      engine,
      ...(theme ? { theme } : {}),
      ...(chapterNodeId ? { chapter_node_id: chapterNodeId } : {}),
      ...(itemId ? { item_id: itemId } : {}),
    }),
    onSuccess: ({ job_id }) => { void poll(job_id); },
    onError: (e) => setError(String(e)),
  });

  // 任务 145：改主题 = 改**课程级设置**（落库）+ 明确告诉用户「要重新生成才生效」
  const saveTheme = useMutation({
    mutationFn: (value: string) => api.setPptTheme(courseId, value),
    onSuccess: (res) => {
      setThemePicked(res.theme);
      message.success(res.message);
      qc.invalidateQueries({ queryKey: ["ppt-themes"] });
      qc.invalidateQueries({ queryKey: ["course", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });

  const importPpt = useMutation({
    mutationFn: (f: File) => api.importPpt(courseId, f, {
      scope, chapter_node_id: chapterNodeId ?? undefined,
    }),
    onSuccess: () => { message.success("已导入你的 PPTX（原地替换该位置）"); refresh(); },
    onError: (e) => message.error(String(e)),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteContent(id),
    onSuccess: () => { message.success("已删除课件"); refresh(); },
    onError: (e) => message.error(String(e)),
  });

  return (
    <SectionCard size="small" title={title}>
      <Space wrap>
        <Select style={{ width: 250 }} value={engine} onChange={setEngine} options={ENGINE_OPTIONS} disabled={disabled} />
        <Tooltip title={`本课主题（课程级设置）：${THEME_APPLY_NOTE}`}>
          <Select
            style={{ width: 220 }}
            value={theme || undefined}
            placeholder={THEME_LOADING_LINE}
            onChange={(value) => saveTheme.mutate(value)}
            options={themeSelectOptions(themeOptions, courseTheme)}
            loading={saveTheme.isPending}
            disabled={disabled || !themeData}
            data-testid="ppt-theme-select"
          />
        </Tooltip>
        <Button
          type="primary"
          disabled={disabled || generate.isPending}
          loading={generate.isPending}
          onClick={() => generate.mutate(deck?.id)}
        >
          {deck ? "重做" : "生成"}
        </Button>
        <Upload accept=".pptx" showUploadList={false} disabled={disabled}
          beforeUpload={(f) => { importPpt.mutate(f); return false; }}>
          <Button icon={<UploadOutlined />} disabled={disabled} loading={importPpt.isPending}>导入我的 PPTX</Button>
        </Upload>
        {deck?.urls?.pptx && (
          <a href={deck.urls.pptx}><DownloadOutlined /> 下载 PPTX</a>
        )}
        {deck && (
          <a onClick={() => del.mutate(deck.id)} style={{ color: "#c0392b" }}>删除</a>
        )}
      </Space>
      <div style={{ marginTop: 6 }}>
        {themeLine && (
          <Typography.Text type="secondary" style={{ fontSize: 12, display: "block" }}
            data-testid="ppt-theme-current">
            {themeLine}
          </Typography.Text>
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }} data-testid="ppt-theme-hint">
          {THEME_APPLY_NOTE}
        </Typography.Text>
        {/* 课程里存的是脏值：说清「回落到哪套 + 为什么」——不静默（口径同「使用场景」那套） */}
        {themeData?.course?.error && (
          <Alert type="warning" showIcon style={{ marginTop: 6 }}
            message={themeData.course.error} data-testid="ppt-theme-warning" />
        )}
      </div>
      {deck && <FigureFailures deck={deck} />}
      {deck && <LayoutNotes deck={deck} />}
      <div style={{ marginTop: 8 }}>
        {disabled ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>大纲通过后才能生成课件。</Typography.Text>
        ) : deck ? (
          <Space size={8} wrap>
            {/* 任务 86 §B1：这份课件按什么口径生成的（课件是 0-LLM 产物，记的是上游讲义口径） */}
            <ScenarioBadge meta={deck.meta} />
            {engineTag(deck)}
            {figureTag(deck)}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{slideLabel(deck)}</Typography.Text>
            {deck.engine_error && (
              <Typography.Text type="warning" style={{ fontSize: 12 }}>{deck.engine_error}</Typography.Text>
            )}
          </Space>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            尚未生成。重做 = 用当前大纲重新生成并原地替换该课件（旧版进 meta.history）。
          </Typography.Text>
        )}
      </div>
      {deck && <SlideImagesBlock deck={deck} />}
      {deck && <div style={{ marginTop: 8 }}><DeckPreview deck={deck} /></div>}
      {error && (
        <Alert
          style={{ marginTop: 8 }}
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => { setError(null); generate.mutate(deck?.id); }}>重试</Button>}
        />
      )}
    </SectionCard>
  );
}

/** 高级工具里的「整课课件 + 各章课件」总览（按 scope 分组，显示引擎与页数）。 */
function CoursePptTools({ courseId, disabled = false }: { courseId: string; disabled?: boolean }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [studentError, setStudentError] = useState<{ text: string; raw: string } | null>(null);
  const [studentLoading, setStudentLoading] = useState(false);
  const { data: plan } = useQuery({ queryKey: ["plan", courseId], queryFn: () => api.getPlan(courseId), retry: false });
  const { data } = useQuery({ queryKey: ["ppt", courseId], queryFn: () => api.listPpt(courseId) });
  const chapters = (plan?.nodes ?? []).filter((n) => n.type === "chapter" || n.type === "section");
  const decks = data?.chapters ?? [];
  // 一律按「章号」找课件：老数据的 node_id 是 NULL，只有 meta.chapter_key 可靠
  const deckOf = (key: string) => deckOfChapter(decks, key);
  const counts = decorateCounts(chapters, decks);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["ppt", courseId] });
    requestAutoSync(courseId);
  };

  const poll = async (jobId: string, label: string) => {
    message.info(`${label}课件生成中…`);
    for (let i = 0; i < 36; i++) {
      await new Promise((r) => setTimeout(r, 5000));
      const status = await api.pptJobStatus(jobId);
      if (status.status === "success") { message.success(`${label}课件已生成`); setError(null); refresh(); return; }
      if (status.status === "failed") { setError(`${label}：${status.error || "生成失败"}`); refresh(); return; }
    }
    message.warning("仍在生成中，请稍后手动刷新");
  };

  const generateChapter = useMutation({
    mutationFn: (v: { nodeId: string; itemId?: string; label: string }) =>
      api.generatePpt({
        course_id: courseId, scope: "chapter", chapter_node_id: v.nodeId, engine: "python-pptx",
        ...(v.itemId ? { item_id: v.itemId } : {}),
      }),
    onSuccess: ({ job_id }, v) => { void poll(job_id, v.label); },
    onError: (e) => setError(String(e)),
  });

  // 「补齐全部章节课件」：默认只补缺，勾选「已生成的也重做」= redo
  const [redo, setRedo] = useState(false);
  const [batch, setBatch] = useState<BatchProgress | null>(null);
  const [batchRunning, setBatchRunning] = useState(false);

  const pollBatch = async (jobId: string, total: number) => {
    const failures: string[] = [];
    let skipped = 0;
    for (let i = 0; i < 720; i++) {       // 最多约 60 分钟，逐章串行生成会把进度推进
      await new Promise((r) => setTimeout(r, 5000));
      const state = await api.pptJobStatus(jobId);
      const raw = (state.result?.progress ?? null) as BatchProgress | null;
      if (raw) setBatch(raw);
      if (state.status === "success") {
        const result = (state.result ?? {}) as {
          generated?: unknown[]; skipped?: unknown[]; failed?: { label?: string; error?: string }[];
        };
        const failed = result.failed ?? [];
        failures.push(...failed.map((f) => `${f.label ?? "?"}：${f.error ?? "生成失败"}`));
        skipped = (result.skipped ?? []).length;
        setRedo(false);
        refresh();
        return { generated: (result.generated ?? []).length, skipped, failures };
      }
      if (state.status === "failed") {
        failures.push(state.error || "任务失败");
        refresh();
        return { generated: 0, skipped, failures };
      }
      setBatch((prev) => prev ?? { index: 0, total, label: "", status: "running", error: null });
    }
    failures.push("任务仍在进行，已停止等待，请点「刷新」查看最新进度");
    refresh();
    return { generated: 0, skipped, failures };
  };

  const fillAll = useMutation({
    mutationFn: () => api.generateChapterPpts(courseId, { redo, engine: "python-pptx" }),
    onSuccess: async ({ job_id }) => {
      setBatchRunning(true);
      setError(null);
      setBatch({ index: 0, total: chapters.length, label: "", status: "running", error: null });
      const { generated, skipped, failures } = await pollBatch(job_id, chapters.length);
      setBatchRunning(false);
      if (failures.length) {
        setError(`有 ${failures.length} 章未成功：${failures.join("；")}`);
        message.warning(`章节课件补齐结束：成功 ${generated} 章、跳过 ${skipped} 章、失败 ${failures.length} 章（已在下方列出原因）`);
        return;
      }
      message.success(`章节课件已补齐：新生成 ${generated} 章、跳过 ${skipped} 章；正在同步到笔记…`);
    },
    onError: (e) => { setBatchRunning(false); setError(String(e)); },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteContent(id),
    onSuccess: () => { message.success("已删除课件"); refresh(); },
    onError: (e) => message.error(String(e)),
  });

  /** 任务 151：学生版 —— 整门课的单文件 HTML，新标签页 + blob 打开（与讲师模式同一套做法）。 */
  const openStudent = async () => {
    setStudentError(null);
    const tab = window.open("", "_blank");
    setStudentLoading(true);
    try {
      const html = await api.pptStudentHtml(courseId);
      const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
      if (tab) tab.location.href = url;
      else window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      tab?.close();
      const notice = deckHtmlErrorNotice(e, false);
      setStudentError({ text: notice.text.replace("网页版课件", "学生版"), raw: notice.raw });
    } finally {
      setStudentLoading(false);
    }
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }} size="middle">
      <SectionCard size="small" title="学生版（只读）">
        <Space wrap align="center">
          <Button icon={<ReadOutlined />} loading={studentLoading}
                  onClick={() => { void openStudent(); }}>
            学生版
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            学生版：地图 → 微单元（含即时自测）→ 复习卡（离线单文件）
          </Typography.Text>
        </Space>
        {studentError && (
          <Alert type="error" showIcon style={{ marginTop: 8 }}
                 message={studentError.text} description={studentError.raw || undefined} />
        )}
      </SectionCard>
      <PptTargetPanel courseId={courseId} scope="course" title="整课课件（PPT）" disabled={disabled} />
      <SectionCard size="small" title={`各章课件（PPT）· ${counts.done}/${counts.total}`}>
        <Space wrap align="center" style={{ marginBottom: 10 }}>
          <Tooltip
            title={redo
              ? "会把每一章都用当前大纲重新生成一遍（原地替换，旧版进 meta.history）"
              : "只补没生成的章；已生成的章跳过，不会重复花钱与花时间"}
          >
            <Button
              type="primary"
              icon={<ReloadOutlined />}
              disabled={disabled || !chapters.length || batchRunning}
              loading={batchRunning}
              onClick={() => fillAll.mutate()}
            >
              补齐全部章节课件（{chapters.length} 章，已生成 {counts.done} 章）
            </Button>
          </Tooltip>
          <Checkbox
            checked={redo}
            disabled={disabled || batchRunning}
            onChange={(e) => setRedo(e.target.checked)}
          >
            已生成的也重做
          </Checkbox>
          {!counts.missing && !redo && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              全部章节都已有课件；要重做请先勾选「已生成的也重做」。
            </Typography.Text>
          )}
        </Space>
        {batch && (batchRunning || batch.error) && (
          <Alert
            type={batch.error ? "error" : "info"}
            showIcon
            style={{ marginBottom: 10 }}
            message={batch.error
              ? `第 ${batch.index}/${batch.total} 章失败`
              : `正在补齐章节课件：第 ${Math.min(batch.index + 1, batch.total)}/${batch.total} 章`}
            description={(
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Typography.Text style={{ fontSize: 12 }}>
                  {batch.status === "skipped" ? "已跳过（已生成）" : batch.status === "generated" ? "已生成" : "生成中…"}
                  {batch.label ? ` · ${batch.label}` : ""}
                </Typography.Text>
                <Progress
                  percent={batch.total ? Math.round((batch.index / batch.total) * 100) : 0}
                  size="small"
                  status={batch.error ? "exception" : "active"}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  进度：已完成 {batch.index}/{batch.total} 章 · 每章串行生成，中途失败会逐章列出原因。
                </Typography.Text>
              </Space>
            )}
          />
        )}
        {chapters.length === 0 ? (
          <Empty description="还没有大纲章节：先在第 2 步生成并通过大纲" />
        ) : (
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            expandable={{
              rowExpandable: (n) => Boolean(deckOf(n.key)),
              expandedRowRender: (n) => {
                const deck = deckOf(n.key);
                return deck ? (
                  <div>
                    <SlideImagesBlock deck={deck} />
                    <FigureFailures deck={deck} />
                    <DeckPreview deck={deck} />
                  </div>
                ) : null;
              },
              expandIcon: ({ expanded, onExpand, record }) => (
                deckOf(record.key) ? (
                  <a onClick={(e) => onExpand(record, e)}>
                    <EyeOutlined /> {expanded ? "收起" : "预览"}
                  </a>
                ) : null
              ),
            }}
            dataSource={chapters}
            columns={[
              { title: "章节", render: (_, n) => fmtChapter(n) },
              {
                title: "课件",
                render: (_, n) => {
                  const deck = deckOf(n.key);
                  if (!deck) return <Tag>未生成</Tag>;
                  return (
                    <Space size={6} wrap>
                      {engineTag(deck)}
                      {figureTag(deck)}
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>{slideLabel(deck)}</Typography.Text>
                      {deck.engine_error && <Typography.Text type="warning" style={{ fontSize: 12 }}>{deck.engine_error}</Typography.Text>}
                    </Space>
                  );
                },
              },
              {
                title: "操作",
                render: (_, n) => {
                  const deck = deckOf(n.key);
                  const label = fmtChapter(n);
                  return (
                    <Space>
                      <a
                        onClick={() => generateChapter.mutate({ nodeId: n.id, itemId: deck?.id, label })}
                      >
                        <ReloadOutlined /> {deck ? "重做" : "生成"}
                      </a>
                      {deck?.urls?.pptx && <a href={deck.urls.pptx}><DownloadOutlined /> 下载</a>}
                      {deck && (
                        <a onClick={() => del.mutate(deck.id)} style={{ color: "#c0392b" }}>删除</a>
                      )}
                    </Space>
                  );
                },
              },
            ]}
          />
        )}
      </SectionCard>
      {error && <Alert type="error" showIcon message={error} />}
    </Space>
  );
}

/**
 * PPT 面板：
 * - scope="chapter" + chapterNodeId → 第 3 步章节点面板里的「本章课件」；
 * - 默认（scope="course"）→ 高级工具里的「整课课件 + 各章课件」总览。
 */
export default function PptPanel({ courseId, scope = "course", chapterNodeId = null, disabled = false, title }: {
  courseId: string;
  scope?: Scope;
  chapterNodeId?: string | null;
  disabled?: boolean;
  title?: string;
}) {
  if (scope === "chapter" && chapterNodeId) {
    return (
      <PptTargetPanel
        courseId={courseId}
        scope="chapter"
        chapterNodeId={chapterNodeId}
        title={title ?? "本章课件（PPT）"}
        disabled={disabled}
      />
    );
  }
  return <CoursePptTools courseId={courseId} disabled={disabled} />;
}
