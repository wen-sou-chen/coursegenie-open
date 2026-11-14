import { useEffect, useState } from "react";
import { Button, Modal, Segmented, Space, Spin, Tag, Typography } from "antd";
import FlowDiagram from "./FlowDiagram";
import {
  FIGURE_ZOOM_BODY_HEIGHT, FIGURE_ZOOM_WIDTH, ZOOM_FIT_OPTIONS, figureZoomHeading, zoomImageStyle,
} from "../lib/figureZoom";
import type { ZoomFit, ZoomTarget } from "../lib/figureZoom";
import { ENTRANCE_CLASS, useFigureAnim } from "../lib/useEnterOnce";

/**
 * 点图放大（任务 76 B1）：概念图 / 数据图 / 位图配图 / 流程图**共用同一个弹窗**。
 *
 * 用户原话：「点击概念图的时候就把概念图那一块单独弹出来……总之不能够马上看到」。
 * 沿用页面里已有的弹窗口径（`ConceptGraphModal`：Modal + 标题 + 页脚按钮），
 * 关闭支持 **× / Esc / 点遮罩**（antd Modal 默认三样都在，这里不再关掉任何一样）。
 *
 * 「打开就有反馈」沿用任务 74 课件抽屉那一套：**弹窗立刻出来 + 标题立刻可见 + 图未到位时给 loading**，
 * 不做成「点了像没反应」；图按视口放大（宽约 92vw），长图 / 1:1 时在**弹窗内部**滚动，页面位置不乱跳。
 */
export default function FigureZoomModal({
  open, target, chapter, onClose,
}: {
  open: boolean;
  /** 要放大的是哪张图（`resolveZoomTarget` 给的：img / canvas / 内联 svg 都归一成 src）。 */
  target: ZoomTarget | null;
  /** 所属章节（老师要能知道这是哪一节的概念图）。 */
  chapter?: string;
  onClose: () => void;
}) {
  const [fit, setFit] = useState<ZoomFit>("fit");
  const [loaded, setLoaded] = useState(false);
  // 任务 79 B②：弹窗打开时让图**再淡入一次**（与"动画：开/关"同一个开关）
  const [animOn] = useFigureAnim();
  const [playSeq, setPlaySeq] = useState(0);
  const src = target?.src ?? "";
  // 流程图（mermaid）不走 `<img>`：弹窗里**重新渲染同一份源码**，HTML 标签才不会错位
  const isMermaid = target?.kind === "mermaid";
  const showImg = Boolean(src) && !isMermaid;
  const heading = figureZoomHeading(target?.alt, chapter);

  // 换一张图：重新给 loading（别拿旧图冒充），看法回到「适应窗口」（打开就是放大的，不是原样的小图）
  useEffect(() => {
    setLoaded(false);
    setFit("fit");
  }, [src]);

  // 每次打开（或换图）都重播一次：key 变了 ⇒ 容器重新挂载 ⇒ CSS 动画从头走
  useEffect(() => {
    if (open) setPlaySeq((n) => n + 1);
  }, [open, src]);

  return (
    <Modal
      open={open && (showImg || isMermaid)}
      onCancel={onClose}
      width={FIGURE_ZOOM_WIDTH}
      title={
        <Space wrap size={8}>
          <Typography.Text strong data-testid="figure-zoom-title">{heading.title}</Typography.Text>
          <Tag color="geekblue" data-testid="figure-zoom-chapter">{heading.subtitle}</Tag>
        </Space>
      }
      footer={[
        ...(isMermaid ? [] : [
          <Segmented
            key="fit"
            size="small"
            value={fit}
            options={ZOOM_FIT_OPTIONS}
            onChange={(v) => setFit(String(v) as ZoomFit)}
          />,
        ]),
        <Button key="close" onClick={onClose}>关闭</Button>,
      ]}
      styles={{
        body: {
          maxHeight: FIGURE_ZOOM_BODY_HEIGHT, overflow: "auto",
          padding: 12, borderRadius: 8, background: "#fafafa", textAlign: "center",
        },
      }}
    >
      <div key={playSeq} className={animOn && open ? ENTRANCE_CLASS : undefined}>
        {isMermaid && (
          <div className="figure-zoom-live" style={{ background: "#fff", borderRadius: 8, padding: 12, textAlign: "left" }}>
            <FlowDiagram code={target?.code ?? ""} />
          </div>
        )}
        <div style={{ position: "relative", minHeight: showImg ? 160 : 0 }}>
        {showImg && !loaded && (
          // 叠在图位置上：加载完不把图往下顶（打开就有反馈，但不动版）
          <div style={{
            position: "absolute", inset: 0, display: "flex", flexDirection: "column",
            alignItems: "center", justifyContent: "center", gap: 10,
          }}>
            <Spin size="large" />
            <Typography.Text type="secondary">正在放大这张图…</Typography.Text>
          </div>
        )}
        {showImg && (
          <img
            data-testid="figure-zoom-img"
            alt={heading.title}
            src={src}
            onLoad={() => setLoaded(true)}
            onError={() => setLoaded(true)}
            style={{
              display: "block", margin: "0 auto", borderRadius: 8, background: "#fff",
              visibility: loaded ? "visible" : "hidden",
              ...zoomImageStyle(fit),
            }}
          />
        )}
        </div>
      </div>
    </Modal>
  );
}
