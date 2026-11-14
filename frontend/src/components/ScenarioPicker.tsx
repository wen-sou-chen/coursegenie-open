/**
 * 任务 85：课程「使用场景」选择器（**建课表单 / 课程详情页 / 第 2 步大纲页三处复用同一个组件**）。
 *
 * 交互口径（§B1）：五选多 + 指定主场景（主场景必须 ∈ 已选项）+ **默认不预选**；
 * 每项 hover 出 tooltip（一句话说明同时是 tooltip 文案）；
 * 空 = 「未指定场景（按通用口径生成）」，**不偷偷补默认场景**。
 * 组件只加这一项，不再让用户填别的字段。
 *
 * 任务 86 §B2（纯文案，0 成本）：用户 2026-09-23 反馈「点了场景**没看到哪里有变化**」——
 * 根因是**没人告诉他这个开关到底管什么**。所以这里固定写两段：
 * ① 每个选项旁标它管什么（`lms` 尤其要标清楚：它**只管出包形态**，提示词一字不加）；
 * ② 选择器下方写清「会影响 / 不会影响」—— 尤其是**已生成的内容必须重新生成才会变**。
 */
import { App, Button, Checkbox, Select, Space, Tooltip, Typography } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { CourseScenario } from "../types";
import { SCENARIOS, SCENARIO_UNSET, scenarioPayload } from "../lib/scenarios";

/** 受控控件：`value` / `onChange` 与 antd Form.Item 直接对接（建课表单就是 `<Form.Item name="scenario">`）。 */
export default function ScenarioPicker({
  value, onChange, disabled,
}: {
  value?: CourseScenario | null;
  onChange?: (v: CourseScenario | null) => void;
  disabled?: boolean;
}) {
  const selected = (value?.selected ?? []).filter((key) => SCENARIOS.some((s) => s.key === key));
  const primary = selected.includes(String(value?.primary)) ? String(value?.primary) : (selected[0] ?? "");
  const emit = (keys: string[], nextPrimary: string) => {
    onChange?.(keys.length ? scenarioPayload(nextPrimary, keys) : null);
  };

  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      <Checkbox.Group
        disabled={disabled}
        value={selected}
        onChange={(keys) => emit(keys as string[], primary)}
      >
        <Space wrap size={[18, 4]}>
          {SCENARIOS.map((item) => (
            <Tooltip key={item.key} title={item.hint}>
              <Checkbox value={item.key}>
                {item.label}
                {/* 任务 86 §B2：`lms` 现在对提示词一字不加（只选它 ⇒ 生成口径与未选完全相同），
                    不标出来用户点了必然以为坏了；它该管的是出包形态。 */}
                {item.key === "lms" && (
                  <Typography.Text
                    type="secondary"
                    style={{ fontSize: 12, marginInlineStart: 4 }}
                    data-testid="lms-note"
                  >
                    （只影响出包形态：按课时打 SCORM 包，不改内容）
                  </Typography.Text>
                )}
              </Checkbox>
            </Tooltip>
          ))}
        </Space>
      </Checkbox.Group>

      {/* 任务 86 §B2：选择器下方固定那段「会变 / 不会变」（三处复用 = 三处都有） */}
      <Typography.Text
        type="secondary"
        style={{ fontSize: 12, lineHeight: 1.7 }}
        data-testid="scenario-scope-note"
      >
        会影响：① 重新生成时的<strong>写作口气与要求</strong>；② 导出 / 交付时的<strong>形态</strong>（学员版讲义、逐字稿、
        时长表、字幕、SCORM 打包）。
        <br />
        不会影响：<strong>已生成的内容（必须重新生成才会变）</strong>、章节点结构、图表与习题数量、质检判据与分数口径。
      </Typography.Text>
      {selected.length > 0 ? (
        <Space wrap size={6}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>主场景</Typography.Text>
          <Tooltip title="主场景决定讲述口气，其余已选场景作为附加要求叠加">
            <Select
              size="small"
              disabled={disabled}
              value={primary}
              style={{ width: 190 }}
              onChange={(key) => emit(selected, String(key))}
              options={selected.map((key) => ({
                value: key,
                label: SCENARIOS.find((s) => s.key === key)?.label ?? key,
              }))}
            />
          </Tooltip>
          <Button size="small" type="link" disabled={disabled} onClick={() => emit([], "")}>
            清空场景
          </Button>
        </Space>
      ) : (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>{SCENARIO_UNSET}</Typography.Text>
      )}
    </Space>
  );
}

/** 详情页 / 大纲页的落库版：改动即 PATCH `meta.scenario`（空 ⇒ `{}`，后端按「未选择」处理）。 */
export function ScenarioEditor({
  courseId, value,
}: { courseId: string; value?: CourseScenario | null }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (next: CourseScenario | null) =>
      api.updateCourse(courseId, {
        meta: { scenario: next && next.selected.length ? next : {} },
      }),
    onSuccess: () => {
      message.success("使用场景已更新：之后的生成按新场景的口气与要求出内容");
      qc.invalidateQueries({ queryKey: ["course", courseId] });
    },
    onError: (e) => message.error(String(e)),
  });
  return <ScenarioPicker value={value} onChange={(next) => save.mutate(next)} disabled={save.isPending} />;
}
