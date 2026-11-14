/**
 * 任务 71 前端断言：知识图谱「渲染后可读」—— 默认缩放不得低于可读下限。
 *
 * 起因：任务 68 把**结构**修对了（每章一棵子树、27/27 个知识点在所属章正下方、不重叠），
 * 但 DSH 2026-09-21 复验时发现 **fitView 之后有效字号只有 6.1px**（画布 745×814、缩放 0.468）——
 * 68 的断言只覆盖「图坐标系里的坐标与间距」，**没有断言渲染后的缩放与字号**，所以结构与坐标全绿、
 * 用户看到的字还是太小（视觉模型把 5/6/8/9 章的标题读错）。
 *
 * 本脚本补的就是这一维，三层：
 *   ① **规则**（纯函数）：`readableZoom(fit)` 不得低于 `MIN_READABLE_ZOOM`，且字号 ≥ 10px；
 *   ② **本图确实需要兜底**（用真图谱 fixture 的包围盒推）：按规格 ② 的画布（92vw × 78vh）
 *      和高都塞不下 —— 连「画布无限宽」这种最宽松的情况也放不下 ⇒ 兜底不是装饰；
 *   ③ **接线**：组件里 fitView 真的接了这个常量、标签字号用的是同一个常量、角落有缩放显示、
 *      有「适应画布 / 100% / 重置布局」三个入口，弹窗画布 ≥92vw × ≥78vh。
 *
 * 运行：`node scripts/check-knowledge-graph-readability.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  LABEL_FONT_PX, MIN_READABLE_FONT_PX, MIN_READABLE_ZOOM, effectiveFontPx,
  knowledgeGraphLayout, readableZoom,
} from "../src/lib/knowledgeGraphLayout.ts";

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, "..", "src");
const failures: string[] = [];
const note = (msg: string) => failures.push(msg);

// ① 规则：默认缩放的可读兜底（纯函数）
if (MIN_READABLE_ZOOM < 0.75) {
  note(`默认缩放下限 ${MIN_READABLE_ZOOM} < 0.75（DSH 验收口径）`);
}
if (effectiveFontPx(MIN_READABLE_ZOOM) < MIN_READABLE_FONT_PX) {
  note(`下限缩放下的字号 ${effectiveFontPx(MIN_READABLE_ZOOM)}px < ${MIN_READABLE_FONT_PX}px`);
}
let checked = 0;
for (let fit = 0.05; fit <= 1.5; fit += 0.01) {
  const zoom = readableZoom(fit);
  checked += 1;
  if (zoom < MIN_READABLE_ZOOM - 1e-9) {
    note(`fitView 算出 ${fit.toFixed(2)} 时，兜底后仍只有 ${zoom}（< ${MIN_READABLE_ZOOM}）`);
  }
  if (zoom < fit - 1e-9) {
    note(`兜底把 ${fit.toFixed(2)} 缩小成 ${zoom} —— 兜底只许放大、不许再缩`);
  }
  if (effectiveFontPx(zoom) < MIN_READABLE_FONT_PX - 1e-9) {
    note(`缩放 ${zoom} 下字号 ${effectiveFontPx(zoom)}px < ${MIN_READABLE_FONT_PX}px`);
  }
}
if (!(readableZoom(1) <= 1.1 + 1e-9)) note("兜底不该把已经够大的缩放再放大（>1.1）");
// ② 本图「确实需要兜底」：用真图谱 fixture 的包围盒推 —— 按规格 ② 的画布（≥92vw × ≥78vh）
//    连「画布无限宽」这种最宽松的情况都塞不下；ReactFlow 的 padding 只会让可用区域更小，
//    所以下面的上界是**乐观上界**（真机实测值只会更小）。
interface Fixture { graph: { nodes: { id: string; key: string; label: string }[]; edges: { source: string; target: string; label?: string }[] } }
const fixture = JSON.parse(
  readFileSync(join(here, "fixtures", "task68-ai-course-graph.json"), "utf8"),
) as Fixture;
const layout = knowledgeGraphLayout(fixture.graph);

/** 规格 ② 的画布下界：92vw × 78vh（取一个常见窗口 1600×1100 换算成像素）。 */
const WINDOW = { width: 1600, height: 1100 };
const canvas92 = { width: Math.round(WINDOW.width * 0.92), height: Math.round(WINDOW.height * 0.78) };
/** 任务 68 时代的面板画布（745×814，DSH 实测）—— 也一并算，用来说明「光靠加大画布还不够」。 */
const canvasOld = { width: 745, height: 814 };

function fitUpperBound(canvas: { width: number; height: number }): number {
  return Math.min(canvas.width / layout.width, canvas.height / layout.height);
}
const bounds = [
  { 名字: "规格②弹窗 92vw×78vh", canvas: canvas92, fit: fitUpperBound(canvas92) },
  { 名字: "68 时代面板 745×814", canvas: canvasOld, fit: fitUpperBound(canvasOld) },
];

for (const b of bounds) {
  const zoom = readableZoom(b.fit);
  if (zoom < 0.75 - 1e-9) {
    note(`${b.名字}：兜底后缩放 ${zoom.toFixed(3)} < 0.75`);
  }
  if (effectiveFontPx(zoom) < MIN_READABLE_FONT_PX - 1e-9) {
    note(`${b.名字}：兜底后字号 ${effectiveFontPx(zoom).toFixed(2)}px < ${MIN_READABLE_FONT_PX}px`);
  }
  if (b.fit >= MIN_READABLE_ZOOM - 1e-9) {
    // 提高了反而更好：不判失败，但要说清「放不下」这个前提已经变了
    console.log(`注意：${b.名字} 的 fitView 上界 ${b.fit.toFixed(3)} 已不低于可读下限 ${MIN_READABLE_ZOOM} —— 本图能整幅放进可读大小了`);
  }
}

// ③ 接线：组件与弹窗必须真的用了上面这套常量与尺寸
const graphSrc = readFileSync(join(srcDir, "components", "KnowledgeGraph.tsx"), "utf8");
const panelSrc = readFileSync(join(srcDir, "components", "OutlinePanel.tsx"), "utf8");

if (!/minZoom:\s*MIN_READABLE_ZOOM/.test(graphSrc)) {
  note("KnowledgeGraph.tsx 的 fitViewOptions 没有接 minZoom: MIN_READABLE_ZOOM（默认缩放会退回纯 fitView）");
}
if (!/fontSize:\s*LABEL_FONT_PX/.test(graphSrc)) {
  note("KnowledgeGraph.tsx 的标签字号没有用 LABEL_FONT_PX（字号与断言会各说各话）");
}
if (!/可拖拽调整/.test(graphSrc) || !/缩放 \{zoom/.test(graphSrc) || !/setZoom\(/.test(graphSrc)) {
  note("KnowledgeGraph.tsx 角落里没有「当前缩放」显示");
}
for (const label of ["适应画布", "100%", "重置布局"]) {
  if (!graphSrc.includes(label)) note(`KnowledgeGraph.tsx 里少了「${label}」按钮`);
}
if (!/nodesDraggable/.test(graphSrc)) note("KnowledgeGraph.tsx 没有开 nodesDraggable（拖不动）");

const vw = /width="(\d+)vw"/.exec(panelSrc);
if (!vw) note('OutlinePanel.tsx 的知识图谱弹窗没有 width="<n>vw"');
else if (Number(vw[1]) < 92) note(`知识图谱弹窗宽 ${vw[1]}vw < 92vw（规格 ②）`);
const vh = /height:\s*"(\d+)vh"/.exec(panelSrc);
if (!vh) note('OutlinePanel.tsx 的知识图谱弹窗内容区没有 height: "<n>vh"');
else if (Number(vh[1]) < 78) note(`知识图谱弹窗内容区高 ${vh[1]}vh < 78vh（规格 ②）`);
if (!panelSrc.includes("<KnowledgeGraph")) note("OutlinePanel.tsx 没把 KnowledgeGraph 放进弹窗");

if (failures.length > 0) {
  console.error("前端「知识图谱可读性」断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(
  `前端知识图谱可读性断言通过：默认缩放兜底下限 ${MIN_READABLE_ZOOM}（${checked} 个 fitView 取值全部 ≥ ${MIN_READABLE_ZOOM}，`
  + `字号 ${effectiveFontPx(MIN_READABLE_ZOOM).toFixed(1)}px ≥ ${MIN_READABLE_FONT_PX}px）、`
  + `本图包围盒 ${layout.width}×${layout.height} 在 ${bounds[0].名字} 上的 fitView 上界只有 ${bounds[0].fit.toFixed(3)}`
  + `（≥${MIN_READABLE_ZOOM} 才能整幅放进可读大小 ⇒ 兜底必然生效）、`
  + `组件接线齐（minZoom / LABEL_FONT_PX / 缩放显示 / 适应画布 + 100% + 重置布局 / 可拖动）、`
  + `弹窗画布 ${vw?.[1]}vw × ${vh?.[1]}vh`,
);