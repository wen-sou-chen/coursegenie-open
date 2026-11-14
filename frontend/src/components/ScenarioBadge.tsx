/**
 * 任务 86 §B1：**「这一篇是按什么口径生成的」徽标**（0 LLM，只读 `meta`）。
 *
 * 为什么要有它：用户点了使用场景却「看不到哪里有变化」—— 因为界面上**没有任何地方**
 * 记着这一篇是按什么口径生成的。徽标把这件事显性化：hover 能看到「生成时的口径 /
 * 提示词版本 / 生成时间」。
 *
 * ⚠️ 两条口径（**别改**）：
 * ① **老内容没有 `meta.scenario_used` ⇒ 一律显示「通用(未标记)」**，不许猜、不许默认成某个场景；
 * ② 徽标显示的是**生成时**的口径，不是课程当前设置 —— 改了场景但没重新生成，徽标**不该变**
 *   （「改了要重新生成才生效」这件事由第 3 步那行提示说清楚，不靠徽标撒谎）。
 *
 * 口径的写入点是后端 `services/scenario.py`，前端只是读数（同一套规则见 `lib/scenarios.ts`）。
 */
import type { CSSProperties } from "react";
import { Tag, Tooltip } from "antd";
import {
  SCENARIO_UNMARKED_BADGE, generatedAtOf, promptVersionOf, scenarioBadgeOf, scenarioLabel,
  usedScenarioOf,
} from "../lib/scenarios";
import { formatLocalTime } from "../lib/datetime";

/** hover 内容：生成时的口径 + 提示词版本 + 生成时间（三行，缺项写「未记录」）。 */
export function scenarioBadgeTooltip(meta?: Record<string, unknown> | null): string {
  const used = usedScenarioOf(meta);
  const badge = scenarioBadgeOf(meta);
  const version = promptVersionOf(meta);
  const at = generatedAtOf(meta);
  const tone = used
    ? `生成时的口径：${used.selected.map((key) => scenarioLabel(key)).join(" + ")}`
      + `（主：${scenarioLabel(used.primary)}）`
    : `生成时的口径：未记录（显示为「${SCENARIO_UNMARKED_BADGE}」，不猜具体场景）`;
  return [
    tone,
    `提示词版本：${version || "未记录"}`,
    `生成时间：${at ? formatLocalTime(at) : "未记录"}`,
  ].join("\n");
}

export default function ScenarioBadge({
  meta, style,
}: {
  meta?: Record<string, unknown> | null;
  style?: CSSProperties;
}) {
  const badge = scenarioBadgeOf(meta);
  const unmarked = badge === SCENARIO_UNMARKED_BADGE;
  return (
    <Tooltip title={scenarioBadgeTooltip(meta)}>
      <Tag
        data-testid="scenario-badge"
        color={unmarked ? undefined : "geekblue"}
        style={{ marginInlineEnd: 0, ...(style ?? {}) }}
      >
        {badge}
      </Tag>
    </Tooltip>
  );
}
