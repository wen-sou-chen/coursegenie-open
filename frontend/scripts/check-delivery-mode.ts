/**
 * 任务 48p 前端口径断言：交付页能选「对外 / 内部复盘」，且默认对外。
 *
 * 起因：交付包里 00-交付说明 / 01-验收单 把课程累计 LLM 用量与金额
 * （≈¥0.89）直接写给客户看了 —— 客户第一反应是「你成本不到一块钱」。
 * 后端已加交付模式（course.meta.delivery.external，**默认对外**），
 * 这里不启 DOM，只做形状断言，防「改了组件忘了传参」这类静默失效：
 * 1. `src/deliveryApi.ts` 的 build 必须能带 external，且真的进请求体；
 * 2. `src/components/DeliveryPanel.tsx` 必须把开关的值传给 build；
 * 3. 两种模式的文案都在（对外 = 不含 LLM 用量与金额）。
 *
 * 运行：`node scripts/check-delivery-mode.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = (rel: string) => readFileSync(join(here, "..", "src", rel), "utf8");

const failures: string[] = [];

const api = src("deliveryApi.ts");
if (!/external\?: boolean\b/.test(api)) {
  failures.push("deliveryApi.build 不再接受 external 参数（前端无法切模式）");
}
// 任务 86：同一个 body 里多了 student_lectures ⇒ 不再钉死整条字面量，
// 但仍然要求「pdf 是第一个键」且「external 必须是里面的键」（48p 的契约没放松）。
if (!/JSON\.stringify\(\{ pdf, allow_inconsistent: allowInconsistent, external[^}]*\}\)/.test(api)) {
  failures.push("deliveryApi.build 的请求体里没有 external（切了模式也不生效）");
}
if (!/delivery_mode\?: DeliveryMode;/.test(api)) {
  failures.push("deliveryApi 丢掉了 delivery_mode 字段（前端显示不出当前模式）");
}

const panel = src("components/DeliveryPanel.tsx");
// 同上：允许调用尾部多带实参（86 的 student_lectures），前四个实参的顺序不许动。
if (!/deliveryApi\.build\(courseId, v\.pdf, v\.allowInconsistent \?\? false, v\.external[,)]/.test(panel)) {
  failures.push("DeliveryPanel 没把开关的值传给 build（切了模式也不生效）");
}
if (!panel.includes("external: effectiveExternal")) {
  failures.push("DeliveryPanel 的两个出包按钮没带上当前模式");
}
for (const text of ["对外交付（默认）：客户包里不含 LLM 用量与金额", "内部复盘：写明 LLM 用量与金额（只给自己看）"]) {
  if (!panel.includes(text)) failures.push(`DeliveryPanel 缺少模式提示文案：${text}`);
}
if (!/mode\?\.external \?\? true/.test(panel)) {
  failures.push("DeliveryPanel 的默认值不是「对外」（mode?.external ?? true）");
}

if (failures.length > 0) {
  console.error("前端交付模式断言失败：");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log("前端交付模式断言通过：交付页可切对外/内部复盘，默认对外，开关值随出包写回课程");
