import { useEffect, useMemo, useRef, useState } from "react";
import SectionCard from "../ui/SectionCard";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, App, Button, Collapse, Empty, Progress, Segmented, Space, Table, Tag, Tooltip, Typography,
} from "antd";
import { ArrowLeftOutlined, BarChartOutlined, CheckOutlined, DownloadOutlined, EditOutlined, FileTextOutlined, PartitionOutlined, PictureOutlined, QuestionCircleOutlined, ReadOutlined, ReloadOutlined, ShareAltOutlined, VerticalAlignTopOutlined } from "@ant-design/icons";
import { api } from "../../api";
import FlowDiagram from "../FlowDiagram";
import ChartRenderer, { ChartDataIssuesTag } from "../ChartRenderer";
import VaultStatusTag from "../VaultStatusTag";
import DiagramFailureTag from "../DiagramFailureTag";
import MdWithFlow from "../MdWithFlow";
import { useDiagramMaps } from "../../diagramMaps";
import type { DiagramMaps } from "../../diagramMaps";
import ConsistencyCheck from "../ConsistencyCheck";
import ConceptDetailModal from "../ConceptDetailModal";
import ConceptGraphModal from "../ConceptGraphModal";
import FigureZoomModal from "../FigureZoomModal";
import { DeckPreview, FigureFailures, LayoutNotes, SlideImagesBlock, engineTag, figureTag, slideLabel } from "../PptDeckBlocks";
import ScenarioBadge from "../ScenarioBadge";
import { chapterLabel as fmtChapter, nodeLabel as fmtNode } from "../../lib/chapterLabel";
import { flowchartCount, mermaidBlockList } from "../../lib/contentCounts";
import {
  chipLabel, clampScrollTarget, FILTER_LABEL, filterEchoText, isFilterChipHidden,
} from "../../lib/previewFilters";
import type { ReviewFilter } from "../../lib/previewFilters";
import {
  PREVIEW_VIEW_OPTIONS, deckPanelActiveKeys, deckPanelTitle, defaultOpenPanels, normalizePreviewView,
  pointCounts, readPreviewView, visiblePanels, writePreviewView,
} from "../../lib/previewGrouping";
import type { OpenMode, PanelKey, PreviewView } from "../../lib/previewGrouping";
import { jumpToPanel, rafFrame } from "../../lib/panelJump";
import { FIGURE_TERM } from "../../lib/figureTerms";
import { resolveZoomTarget } from "../../lib/figureZoom";
import type { ZoomTarget } from "../../lib/figureZoom";
import { prefersReducedMotion, useFigureAnim, useFigureEntrance } from "../../lib/useEnterOnce";
import { requestAutoSync } from "../../syncNotes";
import type { ConceptRow, ContentRow, PlanNode, PptDeck } from "../../types";
function imgSrc(row: ContentRow): string {
  const path = String(row.body?.path ?? "");
  return path ? `/${path}` : "";
}
/** 卡内某一类的小标题（任务 113；「按类型」的区块标题与「按章节」的折叠面板标题共用这一份）。 */
function PanelHead({ icon, title, count }: { icon?: ReactNode; title: ReactNode; count: number }) {
  return (
    <Space wrap style={{ marginBottom: 8 }}>
      {icon}
      <Typography.Text strong>{title}</Typography.Text>
      <Tag>{count}</Tag>
    </Space>
  );
}

/**
 * 面板正文：count === 0 时渲染**空态文案**（任务 113 要求两种视图都不许丢空态文案），
 * 否则渲染内容。两种视图共用这一个落点 ⇒「空态文案」只有一处出处。
 */
function PanelBody({ count, emptyText, children }: { count: number; emptyText: string; children: ReactNode }) {
  if (count === 0) {
    return <Typography.Text type="secondary" style={{ fontSize: 13 }}>{emptyText}</Typography.Text>;
  }
  return <>{children}</>;
}

/**
 * 卡内的一类内容：「按类型」视图里是一个区块，「按章节」视图里是一个折叠面板 ——
 * 标题 / 条数 / 空态文案只有这一份，两种视图都从它渲染（换编排不许换内容）。
 */
type Panel = {
  key: PanelKey;
  /** 卡片标题（图标 + 名称 + 条数）：「按类型」当区块标题、「按章节」当折叠面板标题 —— 同一份。 */
  header: ReactNode;
  count: number;
  emptyText: string;
  children: ReactNode;
};

/** 「按类型」视图的区块外壳（＝改造前的样式，一字未动）。 */
function Section({ panel }: { panel: Panel }) {
  return (
    <div style={{ borderTop: "1px solid #f0f0f0", paddingTop: 12, marginTop: 12 }}>
      {panel.header}
      <PanelBody count={panel.count} emptyText={panel.emptyText}>{panel.children}</PanelBody>
    </div>
  );
}
function QuizView({ q }: { q: ContentRow }) {
  const b = (q.body ?? {}) as {
    question?: string; options?: string[]; answer?: string; explanation?: string;
    qtype?: string; difficulty?: string;
  };
  const options = Array.isArray(b.options) ? b.options : [];
  const idx = /^\d+$/.test(String(b.answer ?? "")) ? Number(b.answer) : -1;
  const answerText = idx >= 0 && idx < options.length ? options[idx] : String(b.answer ?? "");
  // 任务 90：答案与解析统一按真值渲染 —— 空值连标签一起不渲染（同 ShareView 的随堂练习卡）。
  const hasAnswer = Boolean(answerText.trim());
  return (
    <SectionCard size="small" type="inner" style={{ marginBottom: 8 }}>
      <Space wrap>
        <Tag color="gold">作业题</Tag>
        <Tag>{String(b.qtype ?? "choice")} · {String(b.difficulty ?? "")}</Tag>
        <VaultStatusTag vault={q.meta?.vault as Record<string, unknown> | undefined} itemId={q.id} kind="quiz" />
      </Space>
      <Typography.Paragraph style={{ marginTop: 8, marginBottom: 4 }}>
        <Typography.Text strong>{b.question}</Typography.Text>
      </Typography.Paragraph>
      {options.length > 0 && (
        <ul style={{ margin: "4px 0 8px", paddingLeft: 20 }}>
          {options.map((opt, i) => (
            <li key={i} style={{ fontSize: 13 }}>
              {String.fromCharCode(65 + i)}. {opt}
            </li>
          ))}
        </ul>
      )}
      {hasAnswer ? (
        <Typography.Paragraph style={{ marginBottom: 4, fontSize: 13 }}>
          答案：<Typography.Text strong>{answerText}</Typography.Text>
        </Typography.Paragraph>
      ) : null}
      {b.explanation && (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
          解析：{b.explanation}
        </Typography.Paragraph>
      )}
    </SectionCard>
  );
}
type ReviewKind = Exclude<ReviewFilter, "all">;
const KIND_META: Record<Exclude<ReviewFilter, "all">, { label: string; color: string }> = {
  lecture: { label: "讲义", color: "magenta" },
  text: { label: "正文", color: "blue" },
  image: { label: FIGURE_TERM.image, color: "green" },
  flowchart: { label: "流程图", color: "purple" },
  chart: { label: FIGURE_TERM.chart, color: "cyan" },
  quiz: { label: "作业", color: "gold" },
  diagram: { label: FIGURE_TERM.diagram, color: "geekblue" },
};
/** 计数为 0 时，小标签灰掉并说明「为什么是 0、去哪生成」（不许做成点了没反应的按钮）。 */
const KIND_ZERO_HINT: Record<Exclude<ReviewFilter, "all">, string> = {
  lecture: "本章/本节还没有讲义：第 3 步选中小节点点「生成完整讲义」",
  text: "没有独立正文：用「完整讲义」时正文已含在讲义里，属正常",
  image: `${FIGURE_TERM.imageClosed}：改用概念图 / 数据图 / 流程图，不影响交付`,
  flowchart: "还没有流程图：第 3 步「生成完整讲义」会把流程图内嵌进讲义正文（也可单独生成）",
  chart: "还没有数据图：第 3 步「做数据图」或按章批量生成",
  quiz: "还没有作业题：第 3 步点「出作业题」",
  diagram: "还没有概念图：第 3 步「生成完整讲义」会按需带上概念图",
};

/** 「位图配图」在策略关闭期是**静态说明**（不可点），不是按钮（文案沿用既有「配图 已关闭」口径）。 */
function ImageClosedTag() {
  return (
    <Tooltip title="位图配图（旧称「配图 / 拍照图」）按既定策略关闭：用概念图 / 数据图 / 流程图替代，不影响交付；确需手工配图请到第 3 步「生成位图配图」或导入图片。">
      <Tag style={{ color: "#8c8c8c", background: "#f5f5f5", borderColor: "#e0e0e0", cursor: "help" }}>
        配图 已关闭
      </Tag>
    </Tooltip>
  );
}
function KindBadge({
  kind, count, active, onClick, hint, disabled,
}: {
  kind: Exclude<ReviewFilter, "all">;
  count: number;
  active: boolean;
  onClick: (kind: Exclude<ReviewFilter, "all">) => void;
  hint?: string;
  /** 计数为 0（或该类已关闭）时不可点：灰掉 + Tooltip 说明，不做「看着像按钮却点不开」。 */
  disabled?: boolean;
}) {
  const meta = KIND_META[kind];
  const off = disabled || count === 0;
  const hintText = hint ?? (off
    ? KIND_ZERO_HINT[kind]
    : `点一下 = 只看本章/本节的「${meta.label}」（${count} 条），再点一下取消`);
  const tag = (
    <Tag
      color={active && !off ? meta.color : undefined}
      style={{
        cursor: off ? "not-allowed" : "pointer",
        userSelect: "none",
        opacity: off ? 0.45 : active ? 1 : 0.8,
        fontWeight: active && !off ? 600 : undefined,
      }}
      onClick={(e) => {
        e.stopPropagation();
        if (!off) onClick(kind);
      }}
    >
      {chipLabel(kind, count, active && !off)}
    </Tag>
  );
  return <Tooltip title={hintText}>{tag}</Tooltip>;
}

/**
 * 顶部统计标签（用户原话「上面有很多按钮但是很多都打不开」）：
 * 每一个都有真实动作 —— 切换下面「只看」筛选并显示选中态，或滚动到对应区块；
 * 0 条的置灰 + Tooltip 说明该做什么，不做成「看着像按钮却没反应」的样子。
 */
function StatTag({
  label, count, active, onClick, tooltip, disabledHint,
}: {
  label: string;
  count: number;
  active?: boolean;
  onClick?: () => void;
  tooltip: string;
  disabledHint?: string;
}) {
  // 0 条一律不可点（灰掉 + Tooltip 说清为什么是 0、去哪生成），有内容才可点，且点了必须有结果
  const off = count === 0;
  const clickable = Boolean(onClick) && !off;
  const tag = (
    <Tag
      color={active && !off ? "blue" : undefined}
      style={{
        cursor: clickable ? "pointer" : "not-allowed",
        opacity: off ? 0.45 : 1,
        fontWeight: active && !off ? 600 : undefined,
        userSelect: "none",
      }}
      onClick={() => { if (clickable) onClick?.(); }}
    >
      {active && !off ? `✓ ${label} ${count}` : `${label} ${count}`}
    </Tag>
  );
  return <Tooltip title={off ? disabledHint ?? tooltip : tooltip}>{tag}</Tooltip>;
}

/**
 * 筛选态回显 + 一键清除（任务 74 B1.1）。
 *
 * 用户原话：「点开只弹一句『只看某某某』」、**没有当前筛选回显、没有一键清除** ——
 * 于是「连点流程图三次」实测 9 → 7 → 9 → 7（toggle 其实生效）却没人看得出来。
 * `filter === "all"` 时**不渲染**（没有筛选就没有这条）。
 */
function FilterEcho({ filter, count, onClear }: {
  filter: ReviewFilter;
  count: number;
  onClear: () => void;
}) {
  if (filter === "all") return null;
  return (
    <span data-testid="filter-echo" style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <Tag color="blue" style={{ fontWeight: 600 }}>{filterEchoText(filter, count)}</Tag>
      <Button size="small" onClick={onClear}>✕ 清除筛选</Button>
    </span>
  );
}
function TextView({ row, maps }: { row: ContentRow; maps: DiagramMaps }) {
  const md = String((row.body as { markdown?: string })?.markdown ?? "");
  return (
    <div>
      <Space size={6} wrap style={{ marginBottom: 4 }}>
        <Typography.Text strong style={{ fontSize: 13 }}>{row.title}</Typography.Text>
        <VaultStatusTag
          vault={row.meta?.vault as Record<string, unknown> | undefined}
          itemId={row.id}
          kind="content"
        />
      </Space>
      <div
        style={{
          background: "#fafafa", border: "1px solid #f0f0f0", borderRadius: 8,
          padding: "8px 12px", maxHeight: 340, overflow: "auto", fontSize: 13, lineHeight: 1.7,
        }}
      >
        {md ? (
          <MdWithFlow md={md} {...maps} collapseAnswers />
        ) : (
          <Typography.Text type="secondary">（正文内容为空）</Typography.Text>
        )}
      </div>
    </div>
  );
}
function NodeReview({ node, rows, filter, onZoom, view, openMode }: {
  node: PlanNode; rows: ContentRow[]; filter: ReviewFilter; view: PreviewView; openMode: OpenMode;
  /** 任务 76 B1：点图放大（概念图 / 数据图 / 位图配图 / 流程图统一走这里）。 */
  onZoom: (target: ZoomTarget, chapter: string) => void;
}) {
  const lectures = rows.filter((r) => r.kind === "lecture");
  const maps = useDiagramMaps(rows);
  const { diagramSrcs, chartSrcs, chartTables } = maps;
  const texts = rows.filter((r) => r.kind === "text");
  const images = rows.filter((r) => r.kind === "image");
  const flows = rows.filter((r) => r.kind === "flowchart");
  const charts = rows.filter((r) => r.kind.startsWith("chart_"));
  const quizzes = rows.filter((r) => r.kind === "quiz");
  const diagrams = rows.filter((r) => r.kind === "diagram");
  // 「流程图」的真实产出：内嵌在讲义/正文正文里的 ```mermaid 段（库里 kind='flowchart' 是 0 条）。
  // 与顶部统计共用 lib/contentCounts.ts 的同一个函数，章节头与顶部数字必须一致。
  const embeddedFlows = mermaidBlockList([...lectures, ...texts]);
  const flowTotal = embeddedFlows.length + flows.length;
  const others = rows.filter(
    (r) =>
      !r.kind.startsWith("chart_") &&
      !["lecture", "text", "image", "flowchart", "quiz", "diagram"].includes(r.kind),
  );
  // 任务 113：卡内各类型的**条数与顺序**一律走 lib/previewGrouping.ts ——
  // 「按章节」（默认）= 讲义 → 正文 → 概念图 → 流程图 → 数据图 → 习题；「按类型」= 改造前的规则原样。
  const counts = pointCounts(rows);
  const shownPanels = visiblePanels(view, counts, filter);
  const panels: Record<PanelKey, Panel> = {
    lecture: {
      key: "lecture",
      header: <PanelHead icon={<ReadOutlined />} title="完整讲义（图文混排）" count={lectures.length} />,
      count: lectures.length,
      emptyText: "本节暂无讲义",
      children: (
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          {lectures.map((lec) => {
            const b = lec.body as { markdown?: string; summary?: string; keywords?: string[]; tone?: string };
            return (
              <SectionCard
                key={lec.id}
                size="small"
                type="inner"
                title={
                  <Space wrap>
                    <Typography.Text strong>{lec.title}</Typography.Text>
                    {b.tone ? <Tag color="blue">语气：{b.tone}</Tag> : null}
                    {(b.keywords ?? []).map((k) => <Tag key={k}>{k}</Tag>)}
                    <VaultStatusTag
                      vault={lec.meta?.vault as Record<string, unknown> | undefined}
                      itemId={lec.id}
                      kind="content"
                    />
                    <DiagramFailureTag
                      meta={lec.meta as Record<string, unknown> | undefined}
                      itemId={lec.id}
                    />
                    {/* 任务 86 §B1：预览页的讲义卡也标口径 */}
                    <ScenarioBadge meta={lec.meta as Record<string, unknown> | undefined} />
                  </Space>
                }
              >
                {b.summary ? (
                  <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 0, marginBottom: 8 }}>
                    {b.summary}
                  </Typography.Paragraph>
                ) : null}
                <MdWithFlow
                  md={b.markdown ?? ""}
                  diagramSrcs={diagramSrcs}
                  chartSrcs={chartSrcs}
                  chartTables={chartTables}
                  collapseAnswers
                />
              </SectionCard>
            );
          })}
        </Space>
      ),
    },
    text: {
      key: "text",
      header: <PanelHead icon={<FileTextOutlined />} title="正文讲解" count={texts.length} />,
      count: texts.length,
      emptyText: "本节暂无正文",
      children: (
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          {texts.map((t) => <TextView key={t.id} row={t} maps={maps} />)}
        </Space>
      ),
    },
    image: {
      key: "image",
      header: <PanelHead icon={<PictureOutlined />} title={FIGURE_TERM.image} count={images.length} />,
      count: images.length,
      emptyText: `本节暂无${FIGURE_TERM.image}`,
      children: (
        <Space wrap size={12}>
          {images.map((im) => {
            const src = imgSrc(im);
            return (
              <SectionCard
                key={im.id}
                size="small"
                style={{ width: 240 }}
                cover={
                  src ? (
                    <img alt={im.title} src={src} data-fig-enter="" style={{ height: 150, objectFit: "cover" }} />
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="文件缺失" style={{ padding: "12px 0" }} />
                  )
                }
              >
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <VaultStatusTag
                    vault={im.meta?.vault as Record<string, unknown> | undefined}
                    itemId={im.id}
                    kind="content"
                  />
                  <Typography.Text ellipsis style={{ fontSize: 12, display: "block" }}>{im.title}</Typography.Text>
                </Space>
              </SectionCard>
            );
          })}
        </Space>
      ),
    },
    flowchart: {
      key: "flowchart",
      header: <PanelHead icon={<ShareAltOutlined />} title="流程图" count={flowTotal} />,
      count: flowTotal,
      emptyText: "本节暂无流程图：第 3 步「生成完整讲义」会把流程图内嵌进讲义正文，也可单独点「生成流程图」。",
      children: (
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          {embeddedFlows.length > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              本节 {embeddedFlows.length} 段流程图内嵌在讲义/正文里（就是上面那几篇的 ```mermaid 代码块，随讲义一起渲染）。
            </Typography.Text>
          )}
          {embeddedFlows.map((b, i) => (
            <div key={`embedded-${i}`} data-figure-title={b.title} data-fig-enter="">
              <Space size={6} wrap style={{ marginBottom: 4 }}>
                <Tag color="purple">讲义内嵌</Tag>
                <Typography.Text strong style={{ fontSize: 13 }}>{b.title}</Typography.Text>
              </Space>
              <FlowDiagram code={b.code} />
            </div>
          ))}
          {flows.map((fc) => (
            <div key={fc.id} data-figure-title={fc.title} data-fig-enter="">
              <Space size={6} wrap style={{ marginBottom: 4 }}>
                <Typography.Text strong style={{ fontSize: 13 }}>{fc.title}</Typography.Text>
                <VaultStatusTag
                  vault={fc.meta?.vault as Record<string, unknown> | undefined}
                  itemId={fc.id}
                  kind="content"
                />
              </Space>
              <FlowDiagram code={String((fc.body as { mermaid?: string })?.mermaid ?? "")} />
            </div>
          ))}
        </Space>
      ),
    },
    chart: {
      key: "chart",
      header: <PanelHead icon={<BarChartOutlined />} title={FIGURE_TERM.chart} count={charts.length} />,
      count: charts.length,
      emptyText: `本节暂无${FIGURE_TERM.chart}`,
      children: (
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          {charts.map((c) => (
            <SectionCard
              key={c.id}
              size="small"
              type="inner"
              title={c.title}
              extra={
                <Space size={6} wrap>
                  <ChartDataIssuesTag meta={c.meta as Record<string, unknown> | undefined} />
                  <VaultStatusTag
                    vault={c.meta?.vault as Record<string, unknown> | undefined}
                    itemId={c.id}
                    kind="content"
                  />
                </Space>
              }
            >
              <div data-figure-title={c.title} data-fig-enter="">
                <ChartRenderer row={c} />
              </div>
            </SectionCard>
          ))}
        </Space>
      ),
    },
    quiz: {
      key: "quiz",
      header: <PanelHead icon={<QuestionCircleOutlined />} title="作业与习题" count={quizzes.length} />,
      count: quizzes.length,
      emptyText: "本节暂无作业",
      children: <>{quizzes.map((q) => <QuizView key={q.id} q={q} />)}</>,
    },
    diagram: {
      key: "diagram",
      header: <PanelHead icon={<PartitionOutlined />} title="概念图" count={diagrams.length} />,
      count: diagrams.length,
      emptyText: "本节暂无概念图：第 3 步「生成完整讲义」会按需带上概念图。",
      children: (
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            概念图平时在讲义正文的引用位置渲染（上面「完整讲义」里就有）；这里单独列出，方便一眼看全。
          </Typography.Text>
          <Space wrap size={12}>
            {diagrams.map((d) => {
              const src = diagramSrcs[d.id];
              return (
                <SectionCard
                  key={d.id}
                  size="small"
                  style={{ width: 320 }}
                  title={<Typography.Text style={{ fontSize: 12 }}>{d.title}</Typography.Text>}
                >
                  {src ? (
                    <img
                      alt={d.title}
                      src={src}
                      title="点一下放大"
                      data-fig-enter=""
                      style={{ maxWidth: "100%", height: "auto", cursor: "zoom-in" }}
                    />
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="图文件缺失，可回第 3 步重做" />
                  )}
                </SectionCard>
              );
            })}
          </Space>
        </Space>
      ),
    },
  };
  const panelList = shownPanels.map((key) => panels[key]);
  const filteredEmpty =
    filter !== "all" &&
    (filter === "chart"
      ? charts.length === 0
      : filter === "flowchart"
        ? flowTotal === 0
        : rows.filter((r) => r.kind === filter).length === 0);
  // 任务 76：本节点的图统一挂一个点击（讲义正文内嵌概念图、数据图 canvas、流程图 svg、概念图列表）
  const chapterLabel = `${node.key} ${node.title}`.trim();
  /**
   * 任务 76 B2：「只看概念图」时自动滚到的那一处锚点。
   * 「按章节」视图里概念图面板默认折叠，但筛选态会自动展开它 ⇒ 锚点仍在，滚动照旧。
   */
  // 任务 114：每个面板都挂 `data-panel-anchor="<类型>"` —— 章节头的「只看某类」标签点下去要先展开它再滚过去；
  // 概念图那层 `data-diagram-anchor` 是任务 76 的锚点，原样保留（两个属性各管各的）。
  const anchored = (panel: Panel, content: ReactNode) =>
    panel.key === "diagram"
      ? <div key={panel.key} data-panel-anchor={panel.key} data-diagram-anchor={diagrams.length > 0 ? "1" : undefined}>{content}</div>
      : <div key={panel.key} data-panel-anchor={panel.key}>{content}</div>;
  return (
    <div
      data-testid="review-node"
      onClick={(e) => {
        const target = resolveZoomTarget(e.target);
        if (target) onZoom(target, chapterLabel);
      }}
    >
      {node.objective && (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 4 }}>
          本节目标：{node.objective}
        </Typography.Paragraph>
      )}
      {filteredEmpty && (
        <Typography.Text type="secondary">
          {filter === "flowchart"
            ? "本节暂无流程图（讲义内嵌 0 段）：到第 3 步「生成完整讲义」会连流程图一起生成，也可单独点「生成流程图」。"
            : `本节暂无${FILTER_LABEL[filter as Exclude<ReviewFilter, "all">]}内容`}
        </Typography.Text>
      )}
      {view === "chapter" ? (
        // 默认视图：一个知识点一张卡，**卡内各类型就地收拢**成折叠面板（讲义展开、其余折叠；
        // 「展开全部 / 收起全部」在区块标题栏）。同一章的东西不再跨区。
        <Collapse
          size="small"
          key={`${filter}-${openMode}`}
          defaultActiveKey={defaultOpenPanels(shownPanels, filter, openMode)}
          items={panelList.map((p) => ({
            key: p.key,
            label: p.header,
            children: anchored(p, <PanelBody count={p.count} emptyText={p.emptyText}>{p.children}</PanelBody>),
          }))}
        />
      ) : (
        panelList.map((p) => anchored(p, <Section key={p.key} panel={p} />))
      )}
      {filter === "all" && others.map((o) => (
        <div key={o.id} style={{ marginTop: 8 }}>
          <Tag color="cyan">{o.kind}</Tag>
          <Typography.Text style={{ fontSize: 13 }}>{o.title}</Typography.Text>
        </div>
      ))}
    </div>
  );
}
export default function PreviewStep({
  courseId, onOpenChapter, onNext,
}: { courseId: string; onOpenChapter: (nodeId: string) => void; onNext: () => void }) {
  const { message } = App.useApp();   // 任务 48t：context 版实例（静态 message.* 会报 antd 警告）
  const qc = useQueryClient();
  const pptRef = useRef<HTMLDivElement | null>(null);
  const conceptRef = useRef<HTMLDivElement | null>(null);
  // 顶部统计标签与下方「只看」是同一个筛选状态（原来另有 activeKind 且从不更新 → 点了看不见选中态）
  const [pptRedo, setPptRedo] = useState(false);
  const [pptJobs, setPptJobs] = useState<Record<string, string>>({});
  const [openedConcept, setOpenedConcept] = useState<ConceptRow | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  // 任务 76 B1：点图放大（概念图 / 数据图 / 位图配图 / 流程图共用一个弹窗）
  const [zoom, setZoom] = useState<{ target: ZoomTarget; chapter: string } | null>(null);
  const { data: course } = useQuery({ queryKey: ["course", courseId], queryFn: () => api.getCourse(courseId), retry: false });
  const { data: plan, isLoading: planLoading } = useQuery({ queryKey: ["plan", courseId], queryFn: () => api.getPlan(courseId), retry: false });
  const { data: rows = [] } = useQuery({
    queryKey: ["content", courseId],
    queryFn: () => api.listContent(courseId),
  });
  const { data: materials = [] } = useQuery({
    queryKey: ["materials", courseId],
    queryFn: () => api.listMaterials(courseId),
  });
  const nodes = plan?.nodes ?? [];
  const { data: conceptData } = useQuery({
    queryKey: ["concepts", courseId], queryFn: () => api.listConcepts(courseId), retry: false,
  });
  const { data: pptData } = useQuery({
    queryKey: ["ppt", courseId], queryFn: () => api.listPpt(courseId), retry: false,
  });
  const conceptStats = conceptData?.stats;
  const approved = course?.status === "approved";
  // 任务 74 B4：内部模式（course.meta.delivery.external === false）才显示逐条技术明细
  const internalMode = (course?.meta as { delivery?: { external?: boolean } } | undefined)?.delivery?.external === false;
  // 大纲未通过时后端会拒绝生成课件：这里统一禁用并说明原因，不留「点了没反应」的按钮
  const disabledGenerate = !approved;
  const [filter, setFilter] = useState<ReviewFilter>("all");
  // 任务 74 B2：跳转前记下 scrollY；长页面给「回到顶部」（实测修前 backTop: false、floatBtn: []）
  const [previewY, setPreviewY] = useState<number | null>(null);
  const [showTop, setShowTop] = useState(false);
  // 任务 113：预览编排 —— 默认「按章节聚合」，「按类型」是既有分区；选择记在 localStorage。
  const [view, setView] = useState<PreviewView>(() => readPreviewView());
  // 章卡这一层的展开方式：「auto」跟着筛选走（默认）／「all」展开全部／「none」收起全部
  const [openMode, setOpenMode] = useState<OpenMode>("auto");
  /**
   * 任务 114：跳转要能**自己打开**目标折叠面板（用户点「课件」链接没反应，正是因为目标折着、滚过去也看不见）。
   * - `openChapters` = 章卡这一层展开着哪些章：原先是 `defaultActiveKey` 非受控 ⇒ 改成受控才谈得上「跳转时展开」，
   *   默认值仍由既有的 `chapterDefaultOpen()` 给（默认展开第 1 章 / 筛选命中章，行为与改造前一致）；
   * - `openDecks` = 各章「本章课件」是否展开：**默认折叠这条规则没变**（见 DECK_FOLDED_BY_DEFAULT）。
   */
  const [openChapters, setOpenChapters] = useState<string[]>([]);
  const [openDecks, setOpenDecks] = useState<Record<string, boolean>>({});
  const switchView = (next: PreviewView) => {
    setView(next);
    writePreviewView(next);
    setOpenMode("auto");   // 换视图时把「展开全部／收起全部」收回默认，免得带着上一屏的状态
  };
  // 任务 79：图的入场动效 —— 开关存 localStorage（默认开；不跟随系统静默禁用，只提示）
  const [animOn, setAnimOn] = useFigureAnim();
  // 观察范围**只限第 4 步这块容器**（`MdWithFlow` / `SlideImagesBlock` 在别的步骤也用，
  // 不能全 document 扫，否则第 3 步工作台、分享页的图也会动）。回调 ref ⇒ 挂载后 effect 才拿得到元素。
  const [previewRoot, setPreviewRoot] = useState<HTMLDivElement | null>(null);
  useFigureEntrance(animOn, previewRoot);
  useEffect(() => {
    const onScroll = () => setShowTop(window.scrollY > 360);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  const chapters = useMemo(
    () => nodes.filter((n) => !n.key.includes(".")),
    [nodes],
  );
  // 内容可能绑定在旧大纲的节点上：优先按 node_id 精确匹配，否则按 node_key（章节号）归到最新大纲同名节点
  const rowsOf = (node: PlanNode) =>
    rows.filter((r) => r.node_id === node.id || (r.node_key && r.node_key === node.key));
  const kindCount = (list: ContentRow[], kind: string) => list.filter((r) => r.kind === kind).length;
  /** 「流程图」一律走共享口径（讲义/正文里的 ```mermaid 段），不看 kind='flowchart'。 */
  const flowCount = (list: ContentRow[]) => flowchartCount(list);
  // 覆盖率以「可产出内容的知识点」(key 含小数点)为准：章节是容器、不直接挂内容，
  // 否则章节永远显示“未产出”，把 6 个知识点全做完误显示成 67%。
  const contentPoints = nodes.filter((n) => n.key.includes("."));
  const pool = contentPoints.length ? contentPoints : nodes;
  const producedNodeIds = new Set<string>();
  for (const node of pool) {
    if (rowsOf(node).length) producedNodeIds.add(node.id);
  }
  const coverage = pool.length ? Math.round((producedNodeIds.size / pool.length) * 100) : 0;
  const planKeys = new Set(nodes.map((n) => n.key));
  const unbound = rows.filter((r) => !r.node_id && !(r.node_key && planKeys.has(r.node_key)));
  const total = (kind: string) => rows.filter((r) => r.kind === kind).length;
  // 「概念图」= 讲义里引用的 diagram 条目；没有它时置灰并说明去哪生成
  const diagramCount = rows.filter((r) => r.kind === "diagram").length;
  const totalChart = rows.filter((r) => r.kind.startsWith("chart_")).length;
  // 顶部统计一律按真实产出口径——流程图数 = 讲义/正文里的 ```mermaid 段数
  //（与章节头、第 3 步工作台共用 lib/contentCounts.ts 同一个函数，三处口径必须一致）
  const totalMermaid = flowCount(rows);
  /** 「只看」筛选的条数口径：流程图按 mermaid 段数（与顶部一致），其它按 kind。 */
  const filterCount = (kind: ReviewFilter) =>
    kind === "flowchart" ? totalMermaid
      : kind === "chart" ? totalChart
        : kind === "all" ? rows.length
          : total(kind);
  /** 某条内容属不属于当前筛选（流程图按讲义内嵌的 ```mermaid 段算：与顶部统计同一口径）。 */
  const rowInFilter = (r: ContentRow, kind: ReviewFilter) =>
    kind === "all" ? true
      : kind === "chart" ? r.kind.startsWith("chart_")
        : kind === "flowchart" ? flowchartCount([r]) > 0
          : r.kind === kind;
  const applyFilter = (kind: Exclude<ReviewFilter, "all">) =>
    setFilter((prev) => (prev === kind ? "all" : kind));
  /** 顶部统计里的「只看」类标签：同一个筛选口径，选中态要看得出来。 */
  const toggleKind = (kind: Exclude<ReviewFilter, "all">) =>
    setFilter((prev) => (prev === kind ? "all" : kind));
  /** 跳转到课件 / 概念区块（顶部统计标签的第二个动作）；跳转前记下位置，便于一键返回。 */
  const scrollTo = (ref: React.RefObject<HTMLDivElement | null>, remember = true) => {
    if (remember) setPreviewY(clampScrollTarget(window.scrollY));
    ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  /** 回到跳转前的位置（实测点一次「概念 9」= 4 → 8913，修前只能手动慢慢翻回去）。 */
  const backToPreview = () => {
    window.scrollTo({ top: clampScrollTarget(previewY ?? 0), behavior: "smooth" });
    setPreviewY(null);
  };
  /**
   * 任务 76 B2：切到「只看概念图」后**自动滚到概念图区块**（用户原话「有的是排在 PPT 的后面，
   * 总之不能够马上看到」）。复用上面的 `scrollTo` ⇒ 任务 74 的「← 返回预览（回到刚才的位置）」
   * 照旧把人带回原位，不另造按钮；折叠面板展开有动画，动画结束后再对一次位。
   */
  useEffect(() => {
    if (filter !== "diagram") return;
    const anchor = () => document.querySelector<HTMLDivElement>("[data-diagram-anchor]");
    if (!anchor()) return;                                  // 本课没有概念图：留在原地，别乱跳
    scrollTo({ current: anchor() });
    const timer = window.setTimeout(() => scrollTo({ current: anchor() }, false), 350);
    return () => window.clearTimeout(timer);
  }, [filter]);
  /** 任务 76 B1：点到的图（img / canvas / 内联 svg）→ 弹窗；标题取图的 alt，副标题是所属章节。 */
  const openZoom = (target: ZoomTarget, chapter: string) => setZoom({ target, chapter });
  // 切换筛选后自动展开含有该类内容的章节，让筛选结果一眼可见
  const initialOpenForFilter = (): string[] => {
    if (filter === "all") return chapters.slice(0, 1).map((c) => c.id);
    const matched = chapters
      .filter((ch) =>
        nodes.some(
          (n) =>
            (n.id === ch.id || n.key.startsWith(`${ch.key}.`)) &&
            rowsOf(n).some((r) => rowInFilter(r, filter)),
        ),
      )
      .map((c) => c.id);
    return matched.length ? matched : chapters.slice(0, 1).map((c) => c.id);
  };

  // ---- 课件（PPT）：整课 1 份 + 各章 1 份，未生成的章在这里直接点生成 ----
  const courseDeck: PptDeck | undefined = (pptData?.course ?? [])[0];
  const chapterDecks = pptData?.chapters ?? [];
  const deckOfChapter = (key: string) =>
    chapterDecks.find((d) => String(d.chapter_key ?? "") === String(key));
  const decksDone = chapters.filter((c) => deckOfChapter(c.key)).length;
  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ["ppt", courseId] });
    qc.invalidateQueries({ queryKey: ["concepts", courseId] });
    requestAutoSync(courseId);
  };
  /** 显式动作：按当前 PPT 主题色板重新给数据图上色（重新拉 /ppt/themes，不改任何数据）。 */
  const refreshPalette = () => {
    void qc.invalidateQueries({ queryKey: ["ppt-themes"] });
    message.success("已按当前主题重新给数据图上色（数据未改动）");
  };
  const pollPpt = async (jobId: string, label: string) => {
    setPptJobs((prev) => ({ ...prev, [label]: "生成中" }));
    for (let i = 0; i < 72; i++) {
      await new Promise((r) => setTimeout(r, 5000));
      const status = await api.pptJobStatus(jobId);
      if (status.status === "success") {
        setPptJobs((prev) => ({ ...prev, [label]: "成功" }));
        const fallback = (status.result?.engine_error as string | undefined) ?? "";
        if (fallback) message.warning(fallback);
        else message.success(`${label}课件已生成`);
        refreshAll();
        return;
      }
      if (status.status === "failed") {
        setPptJobs((prev) => ({ ...prev, [label]: `失败：${status.error || "生成失败"}` }));
        refreshAll();
        return;
      }
    }
    setPptJobs((prev) => ({ ...prev, [label]: "仍在生成，请稍后刷新" }));
    message.warning("仍在生成中，请稍后手动刷新");
  };
  const makeDeck = useMutation({
    mutationFn: (v: { nodeId: string; label: string }) =>
      api.generatePpt({ course_id: courseId, scope: "chapter", chapter_node_id: v.nodeId,
                        engine: "python-pptx" }),
    onSuccess: ({ job_id }, v) => { void pollPpt(job_id, v.label); },
    onError: (e, v) => setPptJobs((prev) => ({ ...prev, [v.label]: `失败：${String(e)}` })),
  });
  const exportConcepts = useMutation({
    mutationFn: () => api.kbExport(courseId, false),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["concepts", courseId] });
      message.success(`概念已导出到笔记（${res.output_rel}，共 ${res.files.length} 个文件）`);
    },
    onError: (e) => message.error(`导出失败：${String(e)}`),
  });
  const relationCount = (c: ConceptRow) =>
    Object.values(c.body?.relations ?? {}).reduce((n, list) => n + (list?.length ?? 0), 0);
  /**
   * 单章课件一行：生成 / 重做 / 下载 + 课件正文。
   * 任务 113 §B4：`folded` 时课件正文**默认折叠**（一节课 13 份课件不许铺成一面墙），
   * 折叠行上给「本章课件 · N 页」角标；展开后原位还是共用积木那套（页级大纲 / 缩略图 /
   * 预览按钮与网页版入口 / 放大弹窗），一个入口都没少。
   */
  /** 章内课件折叠面板的 key（`deck-body-<章 id>`）：渲染与跳转共用这一处命名。 */
  const deckBodyKey = (chapterId: string) => `deck-body-${chapterId}`;
  /** 跳转前的统一动作：记下当前位置 —— 任务 74 的「← 返回预览（回到刚才的位置）」照旧能把人带回来。 */
  const rememberJumpFrom = () => setPreviewY(clampScrollTarget(window.scrollY));
  /**
   * 任务 114：跳到本章课件 —— **先展开（章卡 + 课件面板）→ 等一帧 → 按展开后的位置滚**。
   * 章卡要是折着的，里面的课件面板根本没挂载 ⇒ 落点为空；所以「展开章卡」这一步不能省
   * （省了就是用户看到的那句「点那个课件的链接，完全没有反应」）。
   */
  const jumpToDeck = (ch: PlanNode) => {
    const key = deckBodyKey(ch.id);
    void jumpToPanel({
      key,
      isOpen: () => openChapters.includes(ch.id) && !!openDecks[ch.key],
      expand: () => {
        setOpenChapters((prev) => (prev.includes(ch.id) ? prev : [...prev, ch.id]));
        setOpenDecks((prev) => ({ ...prev, [ch.key]: true }));
      },
      nextFrame: rafFrame,
      headOf: () => document.querySelector(`[data-deck-anchor="${key}"]`),
      scrollTo: (el) => el.scrollIntoView({ behavior: "smooth", block: "start" }),
      remember: rememberJumpFrom,
    });
  };
  /**
   * 任务 114：点章节头的「只看某类」标签 —— 打开该类（卡内面板会被自动展开）+ 保证本章卡是开的，
   * 再滚到本章第一个该类面板。再点一下 = 取消「只看」，这时不跳（免得把人甩走）。
   */
  const jumpToKind = (ch: PlanNode, kind: Exclude<ReviewFilter, "all">) => {
    if (filter === kind) {
      applyFilter(kind);
      return;
    }
    void jumpToPanel({
      key: kind,
      isOpen: () => filter === kind && openChapters.includes(ch.id),
      expand: () => {
        setFilter(kind);
        setOpenChapters((prev) => (prev.includes(ch.id) ? prev : [...prev, ch.id]));
      },
      nextFrame: rafFrame,
      headOf: () => document.querySelector(`[data-chapter-anchor="${ch.key}"] [data-panel-anchor="${kind}"]`),
      scrollTo: (el) => el.scrollIntoView({ behavior: "smooth", block: "start" }),
      remember: rememberJumpFrom,
    });
  };
  const renderDeckRow = (ch: PlanNode, folded = false) => {
    const deck = deckOfChapter(ch.key);
    const label = fmtChapter(ch);
    const job = pptJobs[label];
    const bodyKey = `deck-body-${ch.id}`;
    const body = (
      <>
        {job && <Typography.Text type="secondary" style={{ fontSize: 12 }}>{job}</Typography.Text>}
        {disabledGenerate && (
          <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 4 }}>
            大纲尚未通过：请先回到第 2 步点「大纲通过」，之后这里就能直接生成课件。
          </Typography.Text>
        )}
        {deck ? (
          <>
            <Space size={8} wrap>
              <ScenarioBadge meta={deck.meta} />
              {engineTag(deck)}
              {figureTag(deck)}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{slideLabel(deck)}</Typography.Text>
            </Space>
            <LayoutNotes deck={deck} internal={internalMode} />
            <FigureFailures deck={deck} />
            <SlideImagesBlock deck={deck} width={132} />
            <div style={{ marginTop: 8 }}><DeckPreview deck={deck} internal={internalMode} /></div>
          </>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            这一章还没有课件：点右上「生成这一章」即可（也可到高级工具一次补齐全部章节）。
          </Typography.Text>
        )}
      </>
    );
    // 任务 114：这一块是**章卡第一块**（见 chapterItem），并且折叠块改成**受控** —— 章节头的「课件」标签
    // 点下去要能自己把它打开；默认仍然是折叠的（`deckPanelActiveKeys(key)` 的默认值没变）。
    const deckOpen = !!openDecks[ch.key];
    return (
      <div key={`deck-${ch.id}`} data-deck-anchor={bodyKey}>
      <SectionCard
        size="small"
        type="inner"
        title={<Space wrap size={6}>
          <Typography.Text strong>{label}</Typography.Text>
          {!deck && <Tag>未生成</Tag>}
        </Space>}
        extra={
          <Space wrap size={8}>
            {deck
              ? <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  disabled={disabledGenerate}
                  onClick={() => makeDeck.mutate({ nodeId: ch.id, label })}
                >
                  重做
                </Button>
              : <Button
                  size="small"
                  type="primary"
                  icon={<ReloadOutlined />}
                  loading={job === "生成中"}
                  disabled={disabledGenerate || job === "生成中"}
                  onClick={() => makeDeck.mutate({ nodeId: ch.id, label })}
                >
                  生成这一章
                </Button>}
            {deck?.urls?.pptx && (
              <a href={deck.urls.pptx}><DownloadOutlined /> 下载</a>
            )}
          </Space>
        }
      >
        {folded ? (
          <Collapse
            size="small"
            items={[{
              key: bodyKey,
              label: deckPanelTitle(deck?.slide_count),
              children: body,
            }]}
            activeKey={deckPanelActiveKeys(bodyKey, deckOpen)}
            onChange={(keys) => {
              const list = Array.isArray(keys) ? keys : [keys];
              setOpenDecks((prev) => ({ ...prev, [ch.key]: list.includes(bodyKey) }));
            }}
          />
        ) : body}
      </SectionCard>
      </div>
    );
  };
  const conceptsByChapter = useMemo(() => {
    const all = conceptData?.concepts ?? [];
    const groups = chapters.map((ch) => ({
      key: ch.key,
      label: fmtChapter(ch),
      // 概念可能同时出现在多章：只要含本章号就归入本章
      items: all.filter((c) => (c.chapters ?? []).some((k) => String(k).split(".")[0] === String(ch.key))),
    }));
    const known = new Set(groups.flatMap((g) => g.items.map((c) => c.id)));
    const rest = all.filter((c) => !known.has(c.id));
    if (rest.length) {
      groups.push({ key: "__other", label: "未归章（整课级概念）", items: rest });
    }
    return groups;
  }, [conceptData, chapters]);
  if (planLoading) {
    return <SectionCard loading style={{ minHeight: 240 }} />;
  }

  if (!plan) {
    return (
      <SectionCard>
        <Empty description="还没有大纲：请先回到第 2 步生成并通过大纲，再到第 3 步逐节制作。" />
      </SectionCard>
    );
  }
  /**
   * 知识点卡（两种视图共用同一份）：
   * 「按章节」（默认）卡内各类型是折叠面板；「按类型」卡内各类型是原来的区块。
   */
  const pointCards = (members: PlanNode[], view: PreviewView) => (
    <Space direction="vertical" style={{ width: "100%" }} size="small">
      {members
        // 章节是容器：不直接渲染它自己的空内容卡（内容都在知识点上），避免“本节暂无讲义/图”误导
        .filter((member) => member.key.includes(".") || rowsOf(member).length > 0)
        // 任务 76：章容器只挂了「课件 PPT」那一行 ⇒ 按「只看概念图」筛选时它也会渲染一张卡、
        // 写着「本节暂无概念图」，而图就在它下面的小节里 —— 这正是用户说的「有的他不出来」。
        // 筛选态下只留真有该类内容的小节/知识点卡。
        .filter((member) => filter === "all" || member.key.includes(".")
          || rowsOf(member).some((r) => rowInFilter(r, filter)))
        .map((member) => (
          <SectionCard
            key={member.id}
            size="small"
            type="inner"
            title={
              <Space wrap>
                <Typography.Text strong>{member.key} {member.title}</Typography.Text>
                <Tag color={member.key.includes(".") ? "purple" : "blue"}>
                  {member.key.includes(".") ? "小节/知识点" : "章节"}
                </Tag>
              </Space>
            }
            extra={
              <Button size="small" icon={<EditOutlined />} onClick={() => onOpenChapter(member.id)}>
                修改这一节
              </Button>
            }
          >
            <NodeReview
              node={member}
              rows={rowsOf(member)}
              filter={filter}
              onZoom={openZoom}
              view={view}
              openMode={openMode}
            />
          </SectionCard>
        ))}
    </Space>
  );

  /** 章头（两种视图共用）：章号标题 + 小节数 + 课件角标 + 「只看」小标签 + 覆盖率。 */
  const chapterHead = (ch: PlanNode, members: PlanNode[], chapterDeck: PptDeck | undefined, view: PreviewView) => {
    const owned = members.flatMap((m) => rowsOf(m));
    const pointMembers = members.filter((m) => m.key.includes("."));
    const chPool = pointMembers.length ? pointMembers : members;
    const chCoverage = chPool.length
      ? Math.round((chPool.filter((m) => rowsOf(m).length).length / chPool.length) * 100)
      : 0;
    return (
      <Space wrap>
        <Typography.Text strong>{fmtChapter(ch)}</Typography.Text>
        <Tag>小节 {members.length - 1}</Tag>
        {/* 任务 114：章节头的课件标签**可点** = 展开本章课件并滚到它
            （用户 2026-09-30 原话：「我去点那个课件的链接，完全没有反应，还是要在很后面去找」）。
            「按类型」视图里课件不在本章卡内，标签保持原样、只当状态看。 */}
        <Tooltip title={view === "chapter"
          ? `点一下 = 展开本章课件并滚到它（本章课件就在章卡最前面${chapterDeck ? `，共 ${chapterDeck.slide_count ?? 0} 页` : "，尚未生成"}）`
          : "本章课件状态（在下面的「课件（PPT）」区块里预览 / 重做 / 生成）"}>
          <Tag
            color={chapterDeck ? "geekblue" : undefined}
            style={view === "chapter"
              ? { cursor: "pointer", userSelect: "none", fontWeight: 600 }
              : undefined}
            onClick={(e) => {
              e.stopPropagation();
              if (view === "chapter") jumpToDeck(ch);
            }}
          >
            {chapterDeck ? `课件（${chapterDeck.slide_count ?? 0} 页）` : "课件（未生成）"}
          </Tag>
        </Tooltip>
        {/* 任务 114：「按章节」视图里这些标签点下去 = 只看该类 + 展开该类面板 + 滚到本章第一个该类面板；
            「按类型」视图保持原有的「只筛选、不滚动」。 */}
        <KindBadge kind="lecture" count={kindCount(owned, "lecture")} active={filter === "lecture"} onClick={(kind) => (view === "chapter" ? jumpToKind(ch, kind) : applyFilter(kind))} />
        {/* 「正文」恒为 0（正文都在讲义里）：定稿直接隐藏，页面上 13 处「正文 0」不再出现 */}
        <KindBadge kind="quiz" count={kindCount(owned, "quiz")} active={filter === "quiz"} onClick={(kind) => (view === "chapter" ? jumpToKind(ch, kind) : applyFilter(kind))} />
        <ImageClosedTag />
        <KindBadge
          kind="flowchart"
          count={flowCount(owned)}
          active={filter === "flowchart"}
          onClick={(kind) => (view === "chapter" ? jumpToKind(ch, kind) : applyFilter(kind))}
          hint={flowCount(owned) > 0
            ? `点一下 = 只看本章「流程图」（共 ${flowCount(owned)} 段，内嵌在讲义正文里，点开即按段渲染）`
            : KIND_ZERO_HINT.flowchart}
        />
        <KindBadge kind="chart" count={owned.filter((r) => r.kind.startsWith("chart_")).length} active={filter === "chart"} onClick={(kind) => (view === "chapter" ? jumpToKind(ch, kind) : applyFilter(kind))} />
        <Progress
          percent={chCoverage}
          size="small"
          style={{ width: 110 }}
          status={chCoverage === 100 ? "success" : chCoverage === 0 ? "exception" : "active"}
        />
      </Space>
    );
  };

  /**
   * 一张**章卡**：
   * - 「按章节」= 同一个知识点的「讲义 / 正文 / 图 / 习题」全在一张卡里，**本章课件也放进本章卡**
   *   （§B2：不给「页顶先列一遍全部课件」留口子），课件正文默认折叠；
   * - 「按类型」= 只有知识点卡（课件仍在页面顶部的「课件（PPT）」区块，原样）。
   */
  const chapterItem = (ch: PlanNode, view: PreviewView) => {
    const members = nodes.filter((n) => n.id === ch.id || n.key.startsWith(`${ch.key}.`));
    const chapterDeck = deckOfChapter(ch.key);
    const children = view === "chapter" ? (
      <div data-chapter-anchor={ch.key}>
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          {/* 任务 114：课件是章卡**第一块**（用户：「每一章节的课件，应该放在这个章节开头的部分」） */}
          {renderDeckRow(ch, true)}
          {pointCards(members, view)}
        </Space>
      </div>
    ) : pointCards(members, view);
    return { key: ch.id, label: chapterHead(ch, members, chapterDeck, view), children };
  };

  /** 「未归类内容」（无章节编号的早期素材）：两种视图都在，一条不许丢。 */
  const unboundItem = (view: PreviewView) => ({
    key: "__unbound",
    label: (
      <Space wrap>
        <Typography.Text strong>未归类内容（无章节编号，多为早期生成的素材）</Typography.Text>
        <KindBadge kind="lecture" count={kindCount(unbound, "lecture")} active={filter === "lecture"} onClick={applyFilter} />
        <KindBadge kind="quiz" count={kindCount(unbound, "quiz")} active={filter === "quiz"} onClick={applyFilter} />
        <ImageClosedTag />
        <KindBadge
          kind="flowchart"
          count={flowCount(unbound)}
          active={filter === "flowchart"}
          onClick={applyFilter}
          hint={flowCount(unbound) > 0
            ? `点一下 = 只看这里的「流程图」（共 ${flowCount(unbound)} 段，内嵌在讲义正文里）`
            : KIND_ZERO_HINT.flowchart}
        />
        <KindBadge kind="chart" count={unbound.filter((r) => r.kind.startsWith("chart_")).length} active={filter === "chart"} onClick={applyFilter} />
      </Space>
    ),
    children: (
      <Space direction="vertical" style={{ width: "100%" }} size="small">
        {unbound.map((r) => (
          <div key={r.id}>
            <NodeReview
              node={{ id: r.node_id ?? "unbound", key: "（未绑定）", type: "misc", title: r.title,
                      objective: "", teaching_strategy: "", assessment: "", prerequisites: [] }}
              rows={[r]}
              filter={filter}
              onZoom={openZoom}
              view={view}
              openMode={openMode}
            />
          </div>
        ))}
      </Space>
    ),
  });

  /** 当前视图下的全部卡片：每章一张卡 + （有的话）未归类内容一张卡。 */
  const reviewItems = (view: PreviewView) => {
    const items = chapters.map((ch) => chapterItem(ch, view));
    if (unbound.length) items.push(unboundItem(view));
    return items;
  };
  /**
   * 章卡这一层的默认展开：跟着筛选走（命中哪几章就展开哪几章，默认展开第 1 章）；
   * 点了「展开全部 / 收起全部」就按点了的来。
   */
  const chapterDefaultOpen = (): string[] => {
    if (openMode === "all") return chapters.map((c) => c.id);
    if (openMode === "none") return [];
    return initialOpenForFilter();
  };
  /**
   * 任务 114：章卡这一层从「非受控 + 换 key 重挂载」改成**受控**（跳转要能自己把某章展开）。
   * 默认展开口径一字未改：视图 / 筛选 /「展开全部·收起全部」一变，就回到 `chapterDefaultOpen()`
   * （默认展开第 1 章；筛选态展开命中的章）。
   */
  useEffect(() => {
    setOpenChapters(chapterDefaultOpen());
  }, [view, filter, openMode, chapters.length]);

  /**
   * 整课课件（课程级）的正文：两种视图共用同一份（「按类型」在课件区块里直接铺开，
   * 「按章节」只在视图顶部留一个**默认折叠**的小块）。
   */
  const courseDeckBody = courseDeck ? (
    <>
      <Space size={8} wrap>
        <ScenarioBadge meta={courseDeck.meta} />
        {engineTag(courseDeck)}
        {figureTag(courseDeck)}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {slideLabel(courseDeck)}
        </Typography.Text>
      </Space>
      <LayoutNotes deck={courseDeck} internal={internalMode} />
      <FigureFailures deck={courseDeck} />
      <SlideImagesBlock deck={courseDeck} width={132} />
      <div style={{ marginTop: 8 }}><DeckPreview deck={courseDeck} internal={internalMode} /></div>
    </>
  ) : (
    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
      还没有整课课件：到「高级工具 → PPT」生成，或先按章补齐（每章课件会带来整课口径的图与页）。
    </Typography.Text>
  );

  /** 整课课件卡片（「按类型」视图里用；「按章节」用的是下面那个折叠小块）。 */
  const courseDeckCard = (
    <SectionCard
      size="small"
      type="inner"
      title={<Space wrap size={6}>
        <Typography.Text strong>整课课件</Typography.Text>
        {!courseDeck && <Tag>未生成</Tag>}
      </Space>}
      extra={
        <Space wrap size={8}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            在下面的「高级工具 → PPT」里生成 / 重做整课课件
          </Typography.Text>
          {courseDeck?.urls?.pptx && (
            <a href={courseDeck.urls.pptx}><DownloadOutlined /> 下载</a>
          )}
        </Space>
      }
    >
      {courseDeckBody}
    </SectionCard>
  );

  /** 「按类型」视图的课件区块：整课 1 份 + 各章 1 份（原样保留，折叠不动它）。 */
  const pptBlock = (
    <div ref={pptRef}>
      <SectionCard
        size="small"
        title={<Space wrap size={8}>
          <Typography.Text strong>课件（PPT）</Typography.Text>
          <Tag color="geekblue">各章 {decksDone}/{chapters.length}</Tag>
          {!approved && <Tag color="orange">大纲未通过，暂不能生成</Tag>}
        </Space>}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          {courseDeckCard}
          {chapters.length === 0 ? (
            <Empty description="还没有大纲章节：先在第 2 步生成并通过大纲" />
          ) : (
            chapters.map((ch) => renderDeckRow(ch))
          )}
        </Space>
      </SectionCard>
    </div>
  );

  /** 概念（概念库）区块：两种视图都在，位置各按各的编排走（一个区块都没丢）。 */
  const conceptBlock = (
    <div ref={conceptRef}>
      <SectionCard
        size="small"
        title={<Space wrap size={8}>
          <Typography.Text strong>概念（概念库）</Typography.Text>
          <Tag>{conceptStats?.count ?? 0}</Tag>
          <Tag color={(conceptStats?.broken_links ?? 0) > 0 ? "red" : "green"}>
            坏链 {conceptStats?.broken_links ?? 0}
          </Tag>
          <Button
            size="small"
            icon={<PartitionOutlined />}
            disabled={!(conceptData?.concepts ?? []).length}
            onClick={() => setGraphOpen(true)}
          >
            查看图谱
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            loading={exportConcepts.isPending}
            disabled={!(conceptData?.concepts ?? []).length}
            onClick={() => exportConcepts.mutate()}
          >
            导出到笔记
          </Button>
        </Space>}
      >
        {!(conceptData?.concepts ?? []).length ? (
          <Empty description="还没有概念：第 3 步点「抽取概念」" />
        ) : (
          <Space direction="vertical" style={{ width: "100%" }} size="small">
            {conceptsByChapter.map((group) => (
              <div key={group.key}>
                <Typography.Text strong style={{ fontSize: 13 }}>{group.label}</Typography.Text>
                {group.items.length === 0 ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    本章暂无概念
                  </Typography.Text>
                ) : (
                  <Table
                    rowKey="id"
                    size="small"
                    pagination={false}
                    style={{ marginTop: 6 }}
                    dataSource={group.items}
                    columns={[
                      {
                        title: "概念",
                        render: (_, c: ConceptRow) => (
                          <Tooltip title="点名字打开：可看/改定义、要点、误区、别名与关系（保存后自动同步到笔记）">
                            <Typography.Link strong onClick={() => setOpenedConcept(c)}>
                              {c.name}
                            </Typography.Link>
                          </Tooltip>
                        ),
                      },
                      {
                        title: "别名",
                        render: (_, c: ConceptRow) => c.aliases?.length ? c.aliases.join("、") : "—",
                      },
                      { title: "关系", render: (_, c: ConceptRow) => relationCount(c) || "—" },
                      {
                        title: "状态",
                        render: (_, c: ConceptRow) => (
                          c.edited ? <Tag color="orange">人工编辑</Tag> : <Tag>自动抽取</Tag>
                        ),
                      },
                      {
                        title: "出现章节",
                        render: (_, c: ConceptRow) => (
                          <Space size={4} wrap>
                            {(c.chapters ?? []).map((k) => <Tag key={k} color="blue">{k}</Tag>)}
                          </Space>
                        ),
                      },
                    ]}
                  />
                )}
              </div>
            ))}
          </Space>
        )}
        <div style={{ marginTop: 8 }}>
          <Button size="small" type="link" icon={<VerticalAlignTopOutlined />} onClick={() => scrollTo(pptRef)}>
            ↑ 回到课件 / 预览
          </Button>
        </div>
      </SectionCard>
    </div>
  );

  /**
   * 审阅区（两种视图共用这一个 Shell，保证「只看」筛选、回显、清除筛选、卡片列表都在）：
   * 只有编排与标题随视图变；「按类型」的标题仍是原来的「逐章逐节审阅」。
   */
  const reviewCard = (
    <SectionCard
      size="small"
      title={<Space wrap size={8}>
        <Typography.Text strong>{view === "chapter" ? "按章节审阅（一章一块）" : "逐章逐节审阅"}</Typography.Text>
        <Tag>{chapters.length} 章</Tag>
      </Space>}
      extra={view === "chapter" ? (
        <Space wrap size={8}>
          <Button size="small" onClick={() => setOpenMode("all")}>展开全部</Button>
          <Button size="small" onClick={() => setOpenMode("none")}>收起全部</Button>
        </Space>
      ) : undefined}
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        {view === "chapter" && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            每章一块：同一个知识点的「讲义 / 正文 / 概念图 / 流程图 / 数据图 / 作业」都收在同一张卡里，
            本章课件也在本章卡里（默认折叠）；卡内「讲义」默认展开，其余点标题展开。
          </Typography.Text>
        )}
        <Space wrap align="center">
          <Typography.Text strong>只看：</Typography.Text>
          <Segmented
            value={filter}
            onChange={(v) => setFilter(String(v) as ReviewFilter)}
            options={[
              { value: "all", label: "全部" },
              { value: "lecture", label: `讲义 ${total("lecture")}` },
              // 「正文」只在真有独立正文时才出现（本课恒为 0 ⇒ 定稿隐藏，不再渲染点不开的「正文 0」）
              ...(isFilterChipHidden("text", total("text"))
                ? []
                : [{ value: "text", label: `正文 ${total("text")}` }]),
              { value: "flowchart", label: `流程图 ${totalMermaid}` },
              { value: "chart", label: `数据图 ${totalChart}` },
              { value: "quiz", label: `作业 ${total("quiz")}` },
            ]}
          />
          <FilterEcho filter={filter} count={filterCount(filter)} onClear={() => setFilter("all")} />
        </Space>
        <Collapse
          key={`${view}-${filter}-${openMode}`}
          items={reviewItems(view)}
          activeKey={openChapters}
          onChange={(keys) => setOpenChapters(Array.isArray(keys) ? keys : [keys])}
          expandIconPosition="end"
        />
      </Space>
    </SectionCard>
  );
  return (
    <Space direction="vertical" style={{ width: "100%" }} ref={setPreviewRoot} data-testid="preview-root">
      {!approved && (
        <Alert type="warning" showIcon message="大纲尚未通过" description="请先回到第 2 步点「大纲通过」锁定大纲，审核与导出都以锁定大纲为准。" />
      )}
      <SectionCard size="small">
        <Space wrap style={{ justifyContent: "space-between", width: "100%" }} align="center">
          <Space wrap>
            <Typography.Text strong>第 4 步 · 整课审核（预览全部内容）</Typography.Text>
            <Tag color={approved ? "green" : "orange"}>{approved ? "大纲已通过" : "大纲待通过"}</Tag>
          </Space>
          <Space wrap>
            <Button ghost icon={<EditOutlined />} onClick={() => chapters[0] && onOpenChapter(chapters[0].id)}>
              回工作台修改
            </Button>
            <Button
              type="primary"
              icon={<CheckOutlined />}
              disabled={!approved}
              onClick={() => {
                if (!rows.length) {
                  message.warning("还没有任何内容，先到第 3 步逐节制作，再回来审核");
                  return;
                }
                onNext();
              }}
            >
              审核合格 · 存入笔记
            </Button>
          </Space>
        </Space>
        {/* 任务 113：编排开关（默认「按章节聚合」，选择记在 localStorage；「按类型」是既有分区） */}
        <Space wrap align="center" style={{ marginTop: 10 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>预览编排：</Typography.Text>
          <Segmented
            data-testid="preview-view-switch"
            value={view}
            onChange={(v) => switchView(normalizePreviewView(v))}
            options={PREVIEW_VIEW_OPTIONS}
          />
          <Tooltip title="按章节（默认）：一章一块，同一个知识点的讲义 / 正文 / 图 / 作业都在同一张卡里，本章课件也在本章卡里；按类型：改造前的分区视图（全部讲义 → 正文 → 图 → 习题，课件单独一块）。选择会记住。">
            <QuestionCircleOutlined style={{ color: "#8c8c8c" }} />
          </Tooltip>
        </Space>
        <Typography.Text type="secondary" style={{ display: "block", fontSize: 12, marginTop: 8 }}>
          练习题的答案与解析已收起，点「看答案」即可展开（导出与学员版口径不变）。
        </Typography.Text>
        <Space wrap style={{ marginTop: 12 }}>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            素材 {materials.length} 份 · 大纲节点 {nodes.length} 个 · 知识点产出 {producedNodeIds.size}/{pool.length}（覆盖率 {coverage}%）
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>跳到：</Typography.Text>
          <StatTag
            label="概念"
            count={conceptStats?.count ?? 0}
            onClick={() => scrollTo(conceptRef)}
            tooltip="点这里跳到下面的「概念（概念库）」区块"
            disabledHint="还没有概念：第 3 步点「抽取概念」"
          />
          <Tag color={(conceptStats?.broken_links ?? 0) > 0 ? "red" : "green"}>
            概念坏链 {conceptStats?.broken_links ?? 0}
          </Tag>
          {(conceptStats?.unresolved_relations ?? 0) > 0 && (
            <Tag color="orange">未解析关系 {conceptStats?.unresolved_relations}</Tag>
          )}
          <StatTag
            label="课件"
            count={decksDone}
            onClick={() => scrollTo(pptRef)}
            tooltip="点这里跳到课件区块（按章节：视图顶部的整课课件 + 各章卡里的本章课件；按类型：下面的「课件（PPT）」）"
            disabledHint="还没有大纲章节：先在第 2 步生成并通过大纲"
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>只看：</Typography.Text>
          <StatTag
            label="讲义"
            count={total("lecture")}
            active={filter === "lecture"}
            onClick={() => toggleKind("lecture")}
            tooltip="点一下 = 下面只看「讲义」，再点一下取消"
            disabledHint="还没有讲义：第 3 步逐节生成，或按章批量生成"
          />
          <StatTag
            label="概念图"
            count={diagramCount}
            active={filter === "diagram"}
            onClick={() => toggleKind("diagram")}
            tooltip="点一下 = 下面只看「概念图」（教材里引用概念图的章节），再点一下取消"
            disabledHint="还没有概念图：第 3 步「按章生成」时会自动带上概念图"
          />
          <StatTag
            label="数据图"
            count={totalChart}
            active={filter === "chart"}
            onClick={() => toggleKind("chart")}
            tooltip="点一下 = 下面只看「数据图」，再点一下取消"
            disabledHint="还没有数据图：第 3 步「做数据图」或按章批量生成"
          />
          <StatTag
            label="流程图"
            count={totalMermaid}
            active={filter === "flowchart"}
            onClick={() => toggleKind("flowchart")}
            tooltip={`点一下 = 下面只看「流程图」（共 ${totalMermaid} 段，内嵌在讲义正文里，点开会按段渲染出来），再点一下取消`}
            disabledHint="本课还没有流程图：第 3 步「生成完整讲义」会把流程图内嵌进讲义正文，也可单独「生成流程图」"
          />
          <StatTag
            label="作业"
            count={total("quiz")}
            active={filter === "quiz"}
            onClick={() => toggleKind("quiz")}
            tooltip="点一下 = 下面只看「作业」，再点一下取消"
            disabledHint="还没有作业题：第 3 步「出作业题」生成"
          />
          <ImageClosedTag />
          <Tooltip title="按当前 PPT 主题色板给所有数据图重新上色（只改观感、不改数据；老图表不重生成也是彩色的，这个按钮用于切换主题后立即刷新）">
            <Button size="small" icon={<ReloadOutlined />} onClick={refreshPalette}>
              按当前主题刷新配色
            </Button>
          </Tooltip>
        </Space>
        <div style={{ marginTop: 8 }}>
          <FilterEcho filter={filter} count={filterCount(filter)} onClear={() => setFilter("all")} />
        </div>
        <ConsistencyCheck courseId={courseId} />
      </SectionCard>
      {view === "chapter" ? (
        <>
          {/* 课程级只有这一块「整课课件」；各章课件就在各章卡里（§B2：不再页顶先列一遍全部课件） */}
          <div ref={pptRef}>
            <SectionCard
              size="small"
              title={<Space wrap size={8}>
                <Typography.Text strong>整课课件（课程级）</Typography.Text>
                <Tag color="geekblue">{courseDeck ? `${courseDeck.slide_count ?? 0} 页` : "未生成"}</Tag>
              </Space>}
              extra={courseDeck?.urls?.pptx
                ? <a href={courseDeck.urls.pptx}><DownloadOutlined /> 下载</a>
                : undefined}
            >
              <Collapse
                size="small"
                items={[{
                  key: "course-deck-body",
                  label: deckPanelTitle(courseDeck?.slide_count),
                  children: courseDeckBody,
                }]}
                defaultActiveKey={deckPanelActiveKeys("course-deck-body")}
              />
            </SectionCard>
          </div>
          {reviewCard}
          {conceptBlock}
        </>
      ) : (
        <>
          {pptBlock}
          {conceptBlock}
          {reviewCard}
        </>
      )}
      <Alert
        type="info"
        showIcon
        message="审核要点"
        description="逐章展开：正文是否准确、概念图 / 数据图是否合适、流程图能否讲清过程、作业题答案是否正确。改到满意后点右上「审核合格 · 存入笔记」；要继续修改就点任一「修改这一节」。"
      />
      {/* 任务 74 B2：长页面的两个浮动入口（用户实测点「概念 9」后 scrollY 4 → 8913、回不去） */}
      <div style={{ position: "fixed", right: 24, bottom: 24, zIndex: 30, display: "flex",
                    flexDirection: "column", gap: 8, alignItems: "flex-end" }}>
        {previewY !== null && (
          <Button type="primary" icon={<ArrowLeftOutlined />} onClick={backToPreview}>
            ← 返回预览（回到刚才的位置）
          </Button>
        )}
        {showTop && (
          <Button icon={<VerticalAlignTopOutlined />} onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
            回到顶部
          </Button>
        )}
        {/* 任务 79 B4：图的入场动效开关（与上面两个浮动按钮同区）。默认一定播；
            系统开了「减少动画」**只提示、不禁用**（样板那轮"跟随系统静默禁用"踩过坑）。 */}
        <Space size={8} align="center">
          {animOn && prefersReducedMotion() && (
            <Typography.Text type="secondary" style={{
              fontSize: 12, background: "rgba(255, 255, 255, 0.92)", padding: "2px 8px",
              borderRadius: 6, boxShadow: "0 1px 4px rgba(0, 0, 0, 0.12)",
            }}>
              系统开了「减少动画」，这里照播（可点右边按钮关掉）
            </Typography.Text>
          )}
          <Button
            data-testid="anim-toggle"
            title="图的入场动效：图进入视口时淡入 + 上浮 + 遮罩扫出，每张只播一次"
            onClick={() => setAnimOn(!animOn)}
          >
            动画：{animOn ? "开" : "关"}
          </Button>
        </Space>
      </div>
      {/* 概念的查看/修改与图谱：与第 5 步概念库用同一个弹窗，改动保存后同步笔记 */}
      <ConceptDetailModal courseId={courseId} concept={openedConcept} onClose={() => setOpenedConcept(null)} />
      <ConceptGraphModal courseId={courseId} open={graphOpen} onClose={() => setGraphOpen(false)} />
      {/* 任务 76 B1：点图弹大图（概念图 / 数据图 / 位图配图 / 流程图共用一个弹窗，× / Esc / 遮罩都能关） */}
      <FigureZoomModal
        open={Boolean(zoom)}
        target={zoom?.target ?? null}
        chapter={zoom?.chapter ?? ""}
        onClose={() => setZoom(null)}
      />
    </Space>
  );
}
