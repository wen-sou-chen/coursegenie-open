/**
 * 课件展示的共用积木：缩略图网格 + 页级预览 + 诊断标签。
 *
 * 同一份「怎么看课件」的实现被三处共用（高级工具 PPT 面板、第 3 步本章课件、
 * 第 4 步预览页的课件区），避免各写一套导致行为不一致。
 *
 * 任务 109（用户 2026-09-27 问「没有看到动画效果，是还没做进去吗」）：PPT 文件里一个动画都
 * 没有（`<p:timing>` / `animEffect` / `p:transition` 全 0），动效只做在网页版课件里
 * ⇒ 入口提为与「预览」同级的主按钮 + 按钮旁边常显一句话说清差异。
 */
import { useState } from "react";
import { Alert, Button, Drawer, Empty, Image, Spin, Space, Tag, Typography } from "antd";
import { EyeOutlined, PlayCircleOutlined, ReadOutlined } from "@ant-design/icons";
import { api } from "../api";
import type { PptDeck, PptPreview, PptSlide } from "../types";
import { deckHtmlErrorNotice, deckPreviewNotice, layoutNoteRows, layoutNoteSummary } from "../lib/previewFilters";
import { formatLocalTime } from "../lib/datetime";

/** 引擎标签：dashi（可选引擎）/ upload（教师上传）/ python-pptx（默认）。 */
export function engineTag(deck: PptDeck) {
  const engine = deck.engine ?? deck.body.engine ?? "";
  const color = engine === "dashi" ? "blue" : engine === "upload" ? "purple" : "green";
  return <Tag color={color}>{engine || "-"}</Tag>;
}

/** 已嵌入图片数（含每点页数配置）。 */
export function figureTag(deck: PptDeck) {
  const count = deck.figure_count ?? 0;
  if (!count) return null;
  return (
    <Tag color="cyan">
      已嵌入 {count} 张图{deck.slides_per_point ? ` · 每点 ${deck.slides_per_point} 页` : ""}
    </Tag>
  );
}

/** 「页数 · 生成时间」一行摘要。 */
export function slideLabel(deck: PptDeck): string {
  const pages = deck.slide_count ?? deck.meta?.slide_count;
  const at = deck.generated_at ?? deck.meta?.generated_at;
  // 任务 84 §B1：生成时间来自后端 `datetime.now(timezone.utc).isoformat()`（带 +00:00 的 UTC），
  // 原先裸切字符串（旧写法见 `backend/docs/task84-deck-time.md`）⇒ 连时区一起丢掉、少 8 小时。
  const parts = [pages ? `${pages} 页` : "", at ? formatLocalTime(String(at)) : ""];
  return parts.filter(Boolean).join(" · ");
}

/** 图未嵌入课件的显式诊断（不静默）：在课件卡片上列出原因。 */
export function FigureFailures({ deck }: { deck: PptDeck }) {
  const failures = deck.figure_failures ?? [];
  if (!failures.length) return null;
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginTop: 8 }}
      message={`有 ${failures.length} 张图未能嵌入课件（已退回文字版）`}
      description={
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {failures.map((f, i) => (
            <li key={`${f.node_key}-${f.figure}-${i}`} style={{ fontSize: 12 }}>
              {f.node_key} · {f.figure}：{f.reason}
            </li>
          ))}
        </ul>
      }
    />
  );
}

/**
 * 主题与版式诊断（任务 74 B4）：**汇总一行常显；逐条明细只在内部模式、且默认折叠**。
 *
 * 用户原话「版式诊断 / 降级记录是噪音」：修前这里把 136 行**实现口径**（哪一页被砍、砍了
 * 几条、挪去哪）默认全展开铺满页面。现在对外只给一行汇总，明细换人话 + 按需展开。
 */
export function LayoutNotes({ deck, internal = false }: { deck: PptDeck; internal?: boolean }) {
  const notes = deck.layout_notes ?? [];
  const [expanded, setExpanded] = useState(false);
  const themeLabel = deck.theme_name
    ? `${deck.theme_name}${deck.theme ? `（${deck.theme}）` : ""}`
    : "";
  const families = new Set(deck.layouts ?? []);
  if (!themeLabel && !notes.length && !deck.theme_error && !families.size) return null;
  const rows = layoutNoteRows(notes, internal, expanded);
  return (
    <div style={{ marginTop: 8 }}>
      <Space size={8} wrap>
        {themeLabel && <Tag color="geekblue">主题：{themeLabel}</Tag>}
        {families.size > 0 && <Tag>版式 {families.size} 种</Tag>}
        {notes.length > 0 && <Tag color="orange">{layoutNoteSummary(notes)}</Tag>}
        {internal && notes.length > 0 && (
          <Button type="link" size="small" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "收起明细" : "展开看明细"}
          </Button>
        )}
      </Space>
      {deck.theme_error && (
        <Alert type="info" showIcon style={{ marginTop: 6 }} message={`主题回退：${deck.theme_error}`} />
      )}
      {rows.length > 0 && (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {rows.map((line, i) => (
            <li key={i} style={{ fontSize: 12 }} title={String(notes[i]?.reason ?? "")}>{line}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 幻灯片图片（PowerPoint COM 导出的 slide-NN.png）：缩略图网格，点击放大。 */
export function SlideImagesBlock({ deck, width = 156 }: { deck: PptDeck; width?: number }) {
  const slides = (deck.slides ?? []).filter((s) => Boolean(s.image_url));
  if (deck.images_error) {
    return (
      <Typography.Text type="warning" style={{ display: "block", fontSize: 12, marginTop: 6 }}>
        {deck.images_error}
      </Typography.Text>
    );
  }
  if (!slides.length) {
    return (
      <Typography.Text type="secondary" style={{ display: "block", fontSize: 12, marginTop: 6 }}>
        暂无幻灯片图片：点「重做」重新生成即可（需本机装 PowerPoint）。
      </Typography.Text>
    );
  }
  return (
    <div style={{ marginTop: 8 }}>
      <Image.PreviewGroup>
        <Space wrap size={10}>
          {slides.map((s) => (
            // 任务 79：幻灯片缩略图也算"图"，进入视口时播一次入场（整块一起淡入上浮）
            <div key={s.index} style={{ width }} data-fig-enter="">
              <Image
                src={s.image_url}
                alt={`第 ${s.index} 页 ${s.title}`}
                width={width}
                height={Math.round(width * 0.5625)}
                style={{ objectFit: "cover", border: "1px solid #eee", borderRadius: 4 }}
              />
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: "block", maxWidth: width }}
                ellipsis={{ tooltip: `${s.index}. ${s.title}` }}
              >
                {s.index}. {s.title || "（无标题）"}
              </Typography.Text>
            </div>
          ))}
        </Space>
      </Image.PreviewGroup>
    </div>
  );
}

/**
 * 页级预览（任务 74 B3）：改成**抽屉**展示 —— 长页面里「就地展开」用户根本发现不了
 * （实测点了之后滚动 0px 变化、无 loading、首行还是内部术语「PDF 预览未开启（PPT_PREVIEW_PDF=false）」）。
 *
 * 打开即有 loading；首行说人话；后端原话只在内部模式追加。
 */
export function DeckPreview({ deck, internal = false }: { deck: PptDeck; internal?: boolean }) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState(0);
  const [pdf, setPdf] = useState<PptPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [htmlError, setHtmlError] = useState<{ text: string; raw: string } | null>(null);
  const [htmlLoading, setHtmlLoading] = useState(false);
  const [presenterError, setPresenterError] = useState<{ text: string; raw: string } | null>(null);
  const [presenterLoading, setPresenterLoading] = useState(false);

  const localSlides: PptSlide[] = deck.slides ?? [];
  const slides: PptSlide[] = localSlides.length ? localSlides : (pdf?.slides ?? []);
  const index = slides.length ? Math.min(current, slides.length - 1) : 0;
  const slide: PptSlide | undefined = slides[index];
  const notice = deckPreviewNotice(pdf, internal);

  const load = async () => {
    if (pdf !== null || loading) return;
    setLoading(true);
    try {
      setPdf(await api.pptPreview(deck.id));
    } catch (e) {
      setPdf({ ok: false, enabled: true, reason: `预览接口调用失败：${String(e)}`,
               fallback: "outline", slides: [] });
    } finally {
      setLoading(false);
    }
  };

  const openDrawer = () => {
    setOpen(true);
    void load();
  };

  /**
   * 任务 94：网页版预览（带入场动效）—— 就是交付包里 `课件/HTML/` 那一份的原文。
   *
   * 口径 = **新标签页打开整页**（不做抽屉内 iframe）：iframe 里 A4 打印样式与整页滚动
   * 都会打折扣，而这份 HTML 本来就是「投影 / 打印 / 手机」三用的单文件。
   *
   * 为什么不把 `/api/ppt/{id}/html` 直接写进 href：该接口与既有 ppt 路由**同鉴权**
   * （`require_api_key`），而本项目约定 **key 只进 localStorage、绝不进 URL**
   * （见 `auth.ts` 抬头）⇒ 这里带 `X-API-Key` 取回原文，再用 blob URL 在新标签页打开：
   * 单机（`AUTH_ENABLED=false`）与开了鉴权都一样能用，URL 里也不会出现 key。
   */
  const openHtmlPreview = async () => {
    setHtmlError(null);
    // 新标签页必须在**点击的同一个事件里**先开好：await 之后再 window.open 会被弹窗拦截。
    const tab = window.open("", "_blank");
    setHtmlLoading(true);
    try {
      const html = await api.pptDeckHtml(deck.id);
      const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
      if (tab) tab.location.href = url;
      else window.open(url, "_blank");   // 首个窗口被拦时再试一次（至少不静默失败）
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      tab?.close();
      // 任务 109：不把 String(e)（可能是「接口 500：…Traceback…」）直接贴给用户，分档在 lib 里。
      setHtmlError(deckHtmlErrorNotice(e, internal));
    } finally {
      setHtmlLoading(false);
    }
  };

  /** 任务 150：讲师模式 —— 单文件 HTML，新标签页打开；与网页版预览同样走 blob + 鉴权头。 */
  const openPresenter = async () => {
    setPresenterError(null);
    const tab = window.open("", "_blank");
    setPresenterLoading(true);
    try {
      const html = await api.pptPresenterHtml(deck.id);
      const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
      if (tab) tab.location.href = url;
      else window.open(url, "_blank");
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) {
      tab?.close();
      const notice = deckHtmlErrorNotice(e, internal);
      setPresenterError({ text: notice.text.replace("网页版课件", "讲师模式"), raw: notice.raw });
    } finally {
      setPresenterLoading(false);
    }
  };

  return (
    <div>
      <Space size={8} wrap>
        <Button icon={<EyeOutlined />} onClick={openDrawer}>
          {open ? "关闭预览" : "预览"}
        </Button>
        <Button type="primary" icon={<PlayCircleOutlined />} loading={htmlLoading}
                onClick={() => { void openHtmlPreview(); }}>
          网页版预览（带动效、可放大）
        </Button>
        <Button icon={<ReadOutlined />} loading={presenterLoading}
                onClick={() => { void openPresenter(); }}>
          讲师模式
        </Button>
        {loading && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>正在加载课件预览…</Typography.Text>
        )}
      </Space>
      {/*
        任务 109：常显一句话说清差异 —— 用户 2026-09-27 的原问题就是「动效在哪」，
        不点任何按钮也要能看见（所以这句放在抽屉**外面**）。
      */}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 6, marginBottom: 0 }}>
        PPT / PDF 是静态文稿；入场动效只在网页版课件里
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 2, marginBottom: 0 }}>
        讲师模式：逐条揭示 + 点开看讲义原文 + 导图导航 + 讲者视图（离线单文件）
      </Typography.Paragraph>
      {htmlError && (
        <Alert type="error" showIcon style={{ marginTop: 8 }}
               message={htmlError.text} description={htmlError.raw || undefined} />
      )}
      {presenterError && (
        <Alert type="error" showIcon style={{ marginTop: 8 }}
               message={presenterError.text} description={presenterError.raw || undefined} />
      )}
      <Drawer
        open={open}
        onClose={() => setOpen(false)}
        width={760}
        title={`课件预览：${deck.title || "本课课件"}`}
      >
        {/*
          任务 109：抽屉里那个入口与主按钮用同一句文案（用户点「预览」看到的是静态页，
          就地能切到网页版）。
          任务 107：PPT / PDF 这条路的插图只能是位图，放大会受像素限制；交付包
          `课件/HTML/` 那份网页版课件里的图是内联 SVG（矢量），放大不糊 ⇒ 抽屉里直接
          给一个入口，复用任务 94 的 `api.pptDeckHtml`（不另写渲染）。
        */}
        <Space size={8} wrap style={{ marginBottom: 8 }}>
          <Button icon={<PlayCircleOutlined />} loading={htmlLoading}
                  onClick={() => { void openHtmlPreview(); }}>
            网页版预览（带动效、可放大）
          </Button>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            插图放大会受像素限制；矢量版里的图可以无限放大
          </Typography.Text>
        </Space>
        {htmlError && (
          <Alert type="error" showIcon style={{ marginBottom: 8 }}
                 message={htmlError.text} description={htmlError.raw || undefined} />
        )}
        {loading ? (
          <div style={{ textAlign: "center", padding: "48px 0" }}>
            <Spin size="large" />
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
              正在加载课件预览…
            </Typography.Paragraph>
          </div>
        ) : (
          <>
            {pdf?.ok && pdf.url && (
              <iframe
                src={pdf.url}
                title="课件 PDF 预览"
                style={{ width: "100%", height: 420, border: "1px solid #ececec", borderRadius: 4, marginBottom: 8 }}
              />
            )}
            {!(pdf?.ok && pdf.url) && notice.text && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 8 }}
                message={notice.text}
                description={notice.raw || undefined}
              />
            )}
            {deck.slides_error && (
              <Alert type="warning" showIcon style={{ marginBottom: 8 }}
                message="页级大纲读取失败" description={deck.slides_error} />
            )}
            {slides.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这份课件没有可预览的页级大纲" />
            ) : (
              <div style={{ display: "flex", gap: 12 }}>
                <div style={{ width: 120, maxHeight: 320, overflowY: "auto", borderRight: "1px solid #f0f0f0", paddingRight: 8 }}>
                  {slides.map((s, i) => (
                    <div
                      key={s.index}
                      onClick={() => setCurrent(i)}
                      style={{ cursor: "pointer", padding: "4px 6px", borderRadius: 4, fontSize: 12,
                               background: i === index ? "#e6f4ff" : "transparent" }}
                    >
                      {s.index}. {s.title || "（无标题）"}
                    </div>
                  ))}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Typography.Text strong>{slide?.title || "（无标题）"}</Typography.Text>
                  <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                    {(slide?.bullets ?? []).map((b, i) => (
                      <li key={i} style={{ fontSize: 13 }}>{b}</li>
                    ))}
                  </ul>
                  {slide?.notes_excerpt && (
                    <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
                      备注摘要：{slide.notes_excerpt}
                    </Typography.Paragraph>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </Drawer>
    </div>
  );
}
