/**
 * 任务 48z 前端断言：面板「加载中」不许伪装成「没有数据」，首屏不许等整库检查。
 *
 * 起因（DSH 第 19 轮 UI 走查实测）：交付包接口首屏 5.03 秒里，面板显示的是
 * 「上次生成：还没有生成过」「还没有交付包：点右上角『一键生成交付包』」——
 * DSH 自己都被骗过（把接口返回的 12 条历史记成「没有历史包」）。根因有二：
 * ① GET 里同步跑 `kb.verify_course_notes`（逐项比对整库 150 项，实测 4.33 秒）；
 * ② 组件把「数据没到」当成「数据是空」。
 *
 * 这里不启 DOM，只做源码级形状断言（与 check-delivery-mode.ts 同一套路），
 * 防「改了后端忘了改前端 / 又把空态写回加载分支」这类静默回归。
 *
 * 运行：`node scripts/check-panel-loading.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = (rel: string) => readFileSync(join(here, "..", "src", rel), "utf8");

const failures: string[] = [];

// ---------------------------------------------------------------- 接口层
const deliveryApi = src("deliveryApi.ts");
if (!deliveryApi.includes("`/delivery/course/${courseId}/consistency`")) {
  failures.push("deliveryApi 没有独立的 consistency 接口（首屏只能同步等整库检查）");
}
if (!deliveryApi.includes("?with_consistency=1")) {
  failures.push("deliveryApi.overview 缺 with_consistency 开关（老口径一次拿齐没了）");
}
if (/overview: \(courseId: string\) =>\s*request<DeliveryOverview>\(`\/delivery\/course\/\$\{courseId\}`\)/.test(deliveryApi)) {
  failures.push("deliveryApi.overview 又回到「默认同步取一致性」（首屏重新被 4 秒阻塞）");
}

const api = src("api.ts");
if (!api.includes("`/audit/course/${courseId}/overview`")) {
  failures.push("api 没有体检轻量概览接口（体检面板只能白等 4 秒）");
}

// ---------------------------------------------------------------- 交付包面板
const delivery = src("components/DeliveryPanel.tsx");
if (!delivery.includes("deliveryApi.consistency(courseId)")) {
  failures.push("DeliveryPanel 没有调用独立的 consistency 接口");
}
if (!/data\?\.consistency \?\? consistencyQuery\.data/.test(delivery)) {
  failures.push("DeliveryPanel 没把「独立接口拿到的一致性」接上（那行提示会永远是空）");
}
if (!delivery.includes("正在检查笔记一致性…")) {
  failures.push("DeliveryPanel 缺「正在检查笔记一致性…」加载态");
}
if (!delivery.includes("正在读取历史交付包…")) {
  failures.push("DeliveryPanel 的历史包表缺加载态文案（加载中会显示「还没有交付包」）");
}
if (!delivery.includes('上次生成：{isLoading ? "读取中…"')) {
  failures.push("DeliveryPanel 的「上次生成」没按 isLoading 分支（加载中会说「还没有生成过」）");
}
if (/\{latest \? formatTime\(latest\.generated_at\) : "还没有生成过"\}/.test(delivery)) {
  failures.push("DeliveryPanel 又出现无条件的「上次生成：还没有生成过」肯定句");
}
if (!delivery.includes("loading={isLoading}")) {
  failures.push("DeliveryPanel 的历史包表没有 loading 态");
}

// ---------------------------------------------------------------- 体检面板
const audit = src("components/AuditPanel.tsx");
if (!audit.includes("api.courseAuditOverview(courseId)")) {
  failures.push("AuditPanel 没有先请求轻量概览（首屏仍要等整库一致性检查）");
}
if (!audit.includes("const checking = !data;")) {
  failures.push("AuditPanel 缺 checking 分支（完整报告没到时无法区分「还在算」与「没有数据」）");
}
if (!audit.includes("正在检查笔记一致性与链接健康…")) {
  failures.push("AuditPanel 的结论卡缺加载态（会先显示 0 分）");
}
if (!audit.includes("正在检查笔记一致性…（逐项比对整个笔记库")) {
  failures.push("AuditPanel 的第 3 段（一致性）缺加载态");
}
if (!audit.includes("正在检查一致性与链接健康，完成后给出建议动作…")) {
  failures.push("AuditPanel 的第 8 段（建议动作）缺加载态");
}
if (/\{data\.consistency\.ok \?/.test(audit)) {
  failures.push("AuditPanel 又直接读 data.consistency（完整报告没到时会炸/空白）");
}

if (failures.length > 0) {
  console.error("前端面板加载态断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端面板加载态断言通过：交付包/体检首屏不再等整库检查，加载中不再显示「还没有交付包」这类肯定句");
