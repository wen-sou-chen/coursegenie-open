/** 只读分享页（M5a）：/share/{token} —— 免登录、只读、无任何编辑入口。
 *
 * 呈现口径与第 4 步预览保持一致（讲义图文 + 概念图 + 数据图 + 课件缩略图），
 * 但所有按钮都是只读的；带密码的链接先弹密码框。
 */
import { useMemo, useState } from "react";
import {
  Alert, Button, Card, Collapse, Empty, Image, Input, Result, Segmented, Space, Spin, Tag, Typography,
} from "antd";
import { useQuery } from "@tanstack/react-query";
import { ClockCircleOutlined, EyeOutlined } from "@ant-design/icons";
import ChartRenderer from "../components/ChartRenderer";
import FlowDiagram from "../components/FlowDiagram";
import MdWithFlow from "../components/MdWithFlow";
import { useDiagramMaps } from "../diagramMaps";
import { publicMediaPrefix, publicShareApi, ShareApiError } from "../shareApi";
import type { AudienceView, ShareDeckRow, ShareOverview } from "../shareApi";
/* 任务 83 §B2：时间格式化收敛到唯一出处（原先这个文件里自己写了一份 formatTime）。 */
import { formatLocalTime } from "../lib/datetime";
import type { ContentRow, PlanNode } from "../types";

const OUTER = { maxWidth: 1100, margin: "0 auto", padding: "16px 16px 48px" } as const;

function imgSrc(row: ContentRow): string {
  const path = String((row.body as { path?: string })?.path ?? "");
  if (!path) return "";
  return path.startsWith("/") ? path : `/${path}`;
}

function SlideGrid({ deck }: { deck: ShareDeckRow }) {
  const slides = (deck.slides ?? []).filter((s) => Boolean(s.image_url));
  const outline = (deck.slides ?? []).map((s) => ({
    key: String(s.index),
    label: `${s.index}. ${s.title}`,
    children: (
      <Space direction="vertical" size={4} style={{ width: "100%" }}>
        {(s.bullets ?? []).length > 0 && (
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            {(s.bullets ?? []).map((b, i) => <li key={i} style={{ fontSize: 13 }}>{b}</li>)}
          </ul>
        )}
        {s.notes_excerpt ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            演讲备注：{s.notes_excerpt}
          </Typography.Text>
        ) : null}
      </Space>
    ),
  }));
  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      title={
        <Space wrap>
          <Typography.Text strong>{deck.title}</Typography.Text>
          <Tag>{deck.scope === "course" ? "整课课件" : `第 ${deck.chapter_key ?? "-"} 章课件`}</Tag>
          <Tag>{deck.slide_count ?? deck.page_count} 页</Tag>
          {deck.engine ? <Tag color="green">{deck.engine}</Tag> : null}
        </Space>
      }
      extra={deck.generated_at ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {formatLocalTime(deck.generated_at)}
        </Typography.Text>
      ) : null}
    >
      {slides.length > 0 ? (
        <Image.PreviewGroup>
          <Space wrap size={10}>
            {slides.map((s) => (
              <div key={s.index} style={{ width: 168 }}>
                <Image src={s.image_url} width={168} style={{ borderRadius: 6 }} />
                <Typography.Text ellipsis style={{ fontSize: 12, display: "block" }}>
                  {s.index}. {s.title}
                </Typography.Text>
              </div>
            ))}
          </Space>
        </Image.PreviewGroup>
      ) : (
        <Collapse size="small" items={outline} />
      )}
    </Card>
  );
}

function QuizBlock({ row }: { row: ContentRow }) {
  const b = (row.body ?? {}) as {
    question?: string; options?: string[]; answer?: string; explanation?: string;
  };
  const options = Array.isArray(b.options) ? b.options : [];
  const idx = /^\d+$/.test(String(b.answer ?? "")) ? Number(b.answer) : -1;
  const answerText = idx >= 0 && idx < options.length ? options[idx] : String(b.answer ?? "");
  // 任务 90：学员版没有 answer 字段（后端连键一起去掉）⇒ 空值连「答案：」标签一起不渲染。
  // 「答案」与「解析」两行必须同一口径（原先解析有真值判断、答案是无条件渲染）。
  const hasAnswer = Boolean(answerText.trim());
  return (
    <Card size="small" type="inner" style={{ marginBottom: 8 }}>
      <Tag color="gold">随堂练习</Tag>
      <Typography.Paragraph style={{ marginTop: 8, marginBottom: 4 }}>
        <Typography.Text strong>{b.question}</Typography.Text>
      </Typography.Paragraph>
      {options.length > 0 && (
        <ul style={{ margin: "4px 0 8px", paddingLeft: 20 }}>
          {options.map((opt, i) => (
            <li key={i} style={{ fontSize: 13 }}>{String.fromCharCode(65 + i)}. {opt}</li>
          ))}
        </ul>
      )}
      {hasAnswer ? (
        <Typography.Paragraph style={{ marginBottom: 4, fontSize: 13 }}>
          答案：<Typography.Text strong>{answerText}</Typography.Text>
        </Typography.Paragraph>
      ) : null}
      {b.explanation ? (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
          解析：{b.explanation}
        </Typography.Paragraph>
      ) : null}
    </Card>
  );
}

function NodeBlock({ node, rows, prefix }: { node: PlanNode; rows: ContentRow[]; prefix: string }) {
  const maps = useDiagramMaps(rows, prefix);
  const lectures = rows.filter((r) => r.kind === "lecture");
  const texts = rows.filter((r) => r.kind === "text");
  const images = rows.filter((r) => r.kind === "image");
  const flows = rows.filter((r) => r.kind === "flowchart");
  const charts = rows.filter((r) => r.kind.startsWith("chart_"));
  const quizzes = rows.filter((r) => r.kind === "quiz");
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      {node.objective ? (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          本节目标：{node.objective}
        </Typography.Paragraph>
      ) : null}
      {lectures.map((lec) => {
        const b = lec.body as { markdown?: string; summary?: string; keywords?: string[] };
        return (
          <Card key={lec.id} size="small" title={lec.title}>
            {b.summary ? (
              <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginTop: 0 }}>
                {b.summary}
              </Typography.Paragraph>
            ) : null}
            <MdWithFlow
              md={b.markdown ?? ""}
              diagramSrcs={maps.diagramSrcs}
              chartSrcs={maps.chartSrcs}
              chartTables={maps.chartTables}
            />
          </Card>
        );
      })}
      {texts.map((t) => {
        const md = String((t.body as { markdown?: string })?.markdown ?? "");
        return (
          <Card key={t.id} size="small" title={t.title}>
            {md ? <MdWithFlow md={md} {...maps} /> : <Typography.Text type="secondary">（正文为空）</Typography.Text>}
          </Card>
        );
      })}
      {images.length > 0 && (
        <Space wrap size={10}>
          {images.map((im) => {
            const src = imgSrc(im);
            return src ? (
              <div key={im.id} style={{ width: 220 }}>
                <Image src={src} width={220} style={{ borderRadius: 6 }} />
                <Typography.Text style={{ fontSize: 12 }}>{im.title}</Typography.Text>
              </div>
            ) : (
              <Empty key={im.id} image={Empty.PRESENTED_IMAGE_SIMPLE} description="文件缺失" />
            );
          })}
        </Space>
      )}
      {flows.map((fc) => (
        <div key={fc.id}>
          <Typography.Text strong style={{ fontSize: 13 }}>{fc.title}</Typography.Text>
          <FlowDiagram code={String((fc.body as { mermaid?: string })?.mermaid ?? "")} />
        </div>
      ))}
      {charts.map((c) => (
        <Card key={c.id} size="small" type="inner" title={c.title}>
          <ChartRenderer row={c} />
        </Card>
      ))}
      {quizzes.map((q) => <QuizBlock key={q.id} row={q} />)}
    </Space>
  );
}

function Banner({ overview }: { overview: ShareOverview }) {
  return (
    <Alert
      type="info"
      showIcon
      icon={<EyeOutlined />}
      style={{ marginBottom: 16 }}
      message={
        <Space wrap>
          <Tag color="blue">只读分享</Tag>
          <Typography.Text strong>{overview.course.title}</Typography.Text>
          <Tag>{overview.course.audience || "未填受众"}</Tag>
          <Tag>{overview.course.duration_minutes} 分钟</Tag>
          <Tag>章节 {overview.chapter_count}</Tag>
          <Tag>知识点 {overview.point_count}</Tag>
          <Tag>概念 {overview.concept_count}</Tag>
        </Space>
      }
      description={
        <Space wrap size={6}>
          <ClockCircleOutlined />
          <Typography.Text style={{ fontSize: 13 }}>
            由 CourseGenie 生成 · 有效期至 {formatLocalTime(overview.share.expires_at, "永不过期")} ·
            只读不可编辑（本次链接已被访问 {overview.share.access_count} 次）
          </Typography.Text>
        </Space>
      }
    />
  );
}

const VIEW_LABEL: Record<AudienceView, string> = {
  teacher: "当前：教师版（含答案与解析）",
  student: "当前：学员版（已隐藏答案与解析）",
};

/** 任务 86 §B4：分享页顶部的「教师版 / 学员版」切换 —— **默认教师版（= 现状）**。 */
function AudienceSwitch({
  view, onChange,
}: { view: AudienceView; onChange: (v: AudienceView) => void }) {
  return (
    <Card size="small" style={{ marginBottom: 16 }} data-testid="audience-switch">
      <Space wrap size={12} align="center">
        <Typography.Text strong>答案可见性</Typography.Text>
        <Segmented
          value={view}
          onChange={(v) => onChange(v as AudienceView)}
          options={[{ label: "教师版", value: "teacher" }, { label: "学员版", value: "student" }]}
        />
        <Tag color={view === "student" ? "green" : "orange"} data-testid="audience-current">
          {VIEW_LABEL[view]}
        </Tag>
      </Space>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: "8px 0 0" }}>
        {view === "student"
          ? "学员版：只做确定性文本处理（讲义正文里「答案 / 解析 / 评分要点」整行去掉、选择题正确项不再标 ✓），"
            + "题目与作答区保留；不调模型、不改内容。"
          : "教师版：与工作台完全一致（含答案与解析）。把链接发给学生前，请先切到「学员版」。"}
      </Typography.Paragraph>
    </Card>
  );
}

export default function ShareView({ token }: { token: string }) {
  const [password, setPassword] = useState<string | undefined>(undefined);
  const [draft, setDraft] = useState("");
  // 任务 86 §B4：**默认教师版 = 现状**（绝不默认去答案）。
  const [view, setView] = useState<AudienceView>("teacher");
  const key = password ?? "";

  const overviewQuery = useQuery({
    queryKey: ["share-overview", token, key],
    queryFn: () => publicShareApi.overview(token, password),
    retry: false,
  });
  const ready = overviewQuery.isSuccess;
  const planQuery = useQuery({
    queryKey: ["share-plan", token, key],
    queryFn: () => publicShareApi.plan(token, password),
    enabled: ready,
    retry: false,
  });
  const contentQuery = useQuery({
    queryKey: ["share-content", token, key, view],
    queryFn: () => publicShareApi.content(token, password, view),
    enabled: ready,
    retry: false,
  });
  const pptQuery = useQuery({
    queryKey: ["share-ppt", token, key],
    queryFn: () => publicShareApi.ppt(token, password),
    enabled: ready,
    retry: false,
  });

  const prefix = publicMediaPrefix(token);
  const rows = useMemo(() => {
    const list = contentQuery.data ?? [];
    return list.map((row) => {
      const body = { ...(row.body ?? {}) } as Record<string, unknown>;
      const path = body.path;
      if (typeof path === "string" && path && !path.startsWith("/") && !path.startsWith("http")) {
        body.path = `${prefix}/${path}`;
      }
      return { ...row, body };
    });
  }, [contentQuery.data, prefix]);

  const nodes = planQuery.data?.nodes ?? [];
  const chapters = nodes.filter((n) => !n.key.includes("."));
  const rowsOf = (node: PlanNode) =>
    rows.filter((r) => r.node_id === node.id || (r.node_key && r.node_key === node.key));
  const planKeys = new Set(nodes.map((n) => n.key));
  const unbound = rows.filter((r) => !r.node_id && !(r.node_key && planKeys.has(r.node_key)));
  const courseDecks = pptQuery.data?.course ?? [];

  const error = overviewQuery.error;
  const passwordNeeded = error instanceof ShareApiError && error.status === 401;

  if (passwordNeeded) {
    return (
      <div style={OUTER}>
        <Card style={{ maxWidth: 420, margin: "80px auto" }}>
          <Typography.Title level={4}>该分享需要密码</Typography.Title>
          <Typography.Paragraph type="secondary">
            这是 CourseGenie 生成的只读分享链接，请输入访问密码。
          </Typography.Paragraph>
          <Input.Password
            value={draft}
            placeholder="访问密码"
            onChange={(e) => setDraft(e.target.value)}
            onPressEnter={() => setPassword(draft)}
          />
          <Button type="primary" block style={{ marginTop: 12 }} onClick={() => setPassword(draft)}>
            进入查看
          </Button>
        </Card>
      </div>
    );
  }

  if (overviewQuery.isPending) {
    return <div style={OUTER}><Spin style={{ marginTop: 120 }} tip="加载中…" /></div>;
  }

  if (overviewQuery.isError || !overviewQuery.data) {
    const status = error instanceof ShareApiError ? error.status : 0;
    const gone = status === 410;
    return (
      <div style={OUTER}>
        <Result
          status={gone ? "warning" : "error"}
          title={gone ? "分享链接已失效" : "无法访问该分享链接"}
          subTitle={error instanceof Error ? error.message : "链接无效"}
        />
      </div>
    );
  }

  const items = chapters.map((ch) => {
    const members = nodes.filter((n) => n.id === ch.id || n.key.startsWith(`${ch.key}.`));
    const owned = members.flatMap((m) => rowsOf(m));
    return {
      key: ch.id,
      label: (
        <Space wrap>
          <Typography.Text strong>{ch.key} {ch.title}</Typography.Text>
          <Tag>{owned.length} 条内容</Tag>
        </Space>
      ),
      children: (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {members.map((m) => {
            const list = rowsOf(m);
            if (!list.length) return null;
            return (
              <div key={m.id}>
                <Typography.Title level={5} style={{ marginTop: 0 }}>
                  {m.key} {m.title}
                </Typography.Title>
                <NodeBlock node={m} rows={list} prefix={prefix} />
              </div>
            );
          })}
        </Space>
      ),
    };
  });

  return (
    <div style={OUTER}>
      <Banner overview={overviewQuery.data} />
      <AudienceSwitch view={view} onChange={setView} />
      {courseDecks.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Typography.Title level={4}>课件</Typography.Title>
          {courseDecks.map((deck) => <SlideGrid key={deck.id} deck={deck} />)}
        </div>
      )}
      <Typography.Title level={4}>课程内容</Typography.Title>
      {items.length === 0 ? (
        <Empty description="该课程还没有可查看的内容" />
      ) : (
        <Collapse items={items} defaultActiveKey={items.length ? [items[0].key] : []} />
      )}
      {unbound.length > 0 && (
        <Card size="small" title="其他内容" style={{ marginTop: 12 }}>
          <NodeBlock
            node={{ id: "unbound", key: "（未绑定）", type: "misc", title: "其他内容",
                    objective: "", teaching_strategy: "", assessment: "", prerequisites: [] }}
            rows={unbound}
            prefix={prefix}
          />
        </Card>
      )}
      <Typography.Paragraph type="secondary" style={{ marginTop: 24, fontSize: 12 }}>
        本页为只读分享视图，所有修改操作均不可用；如需修改请回到 CourseGenie 工作台。
      </Typography.Paragraph>
    </div>
  );
}
