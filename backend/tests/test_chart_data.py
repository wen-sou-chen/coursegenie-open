"""任务 27：图表上色 + 数据长度归一化 + 流程图口径（三条真因的回归测试）。

真因（DSH 用真实库复核，数字可复现）：

1. 14/14 个 chart_* 条目的 ``datasets[*].backgroundColor`` 为空 → chart.js 默认浅灰；
2. 内容条目 ``kind='flowchart'`` 0 条，真实流程图是讲义正文里的 ```mermaid 段；
3. 3/14 个图表 ``labels`` 与 ``datasets[*].data`` 长度不符 → 柱子与标签错位。
"""
from __future__ import annotations

from app.services.chart_data import (
    CHART_DATA_ISSUES_KEY,
    active_palette,
    chart_data_issues,
    normalize_chart_data,
)
from app.services.content_flow import MERMAID_BLOCKS_KEY, count_mermaid, flowchart_count


def _hex_set(colors: list[str]) -> set[str]:
    return {c if c.startswith("#") else f"#{c}" for c in colors}


def test_bar_chart_is_colored_from_theme_palette():
    """柱状图保存后每条 dataset 都有 borderColor/backgroundColor，且取自主题色板。"""
    colors, theme_id, _ = active_palette()
    assert theme_id and len(colors) == 6
    data, issues = normalize_chart_data("bar", {
        "labels": ["甲", "乙"], "datasets": [{"label": "A", "data": [1, 2]},
                                             {"label": "B", "data": [3, 4]}],
    })
    assert issues == []
    palette = _hex_set(colors)
    first, second = data["datasets"]
    assert first["borderColor"] in palette and first["backgroundColor"] in palette
    assert second["borderColor"] in palette and second["backgroundColor"] in palette
    # 多系列用不同色，且同一系列内各数据点同色
    assert first["backgroundColor"] != second["backgroundColor"]


def test_line_chart_fill_is_semi_transparent():
    """折线：borderColor 是主题色，backgroundColor 是同色半透明填充（不是灰）。"""
    colors, _, _ = active_palette()
    data, _ = normalize_chart_data("line", {
        "labels": ["1月", "2月"], "datasets": [{"label": "温度", "data": [10, 12]}],
    })
    ds = data["datasets"][0]
    assert ds["borderColor"] == f"#{colors[0]}"
    assert ds["backgroundColor"].startswith("rgba(")
    assert ds["backgroundColor"].endswith(",0.35)")
    assert ds["backgroundColor"] != "rgba(0,0,0,0.1)"


def test_labels_and_data_lengths_are_aligned():
    """真实畸形数据：labels=1 / data=2 → 归一化后两条长度一致（网页端不再错位）。"""
    raw = {"labels": ["化引子水温"], "datasets": [{"label": "温度", "data": [30, 35]}]}
    assert chart_data_issues(raw) == [{"field": "labels", "expected": 2, "actual": 1}]
    data, issues = normalize_chart_data("bar", raw)
    assert issues == [{"field": "labels", "expected": 2, "actual": 1}]
    assert len(data["labels"]) == 2
    assert data["labels"][0] == "化引子水温" and data["labels"][1] == "第2项"
    for ds in data["datasets"]:
        assert len(ds["data"]) == len(data["labels"])


def test_normalize_keeps_existing_colors_and_pads_series():
    """已有颜色不覆盖；缺数值补 0；多出来的标签保留；字符串数值化。"""
    data, issues = normalize_chart_data("bar", {
        "labels": ["A", "B", "C"],
        "datasets": [{"data": ["3", "x"], "backgroundColor": "#123456"}],
    })
    assert issues == [{"field": "datasets[0].data", "expected": 3, "actual": 2}]
    assert data["datasets"][0]["backgroundColor"] == "#123456"
    assert data["datasets"][0]["data"] == [3.0, 0.0, 0.0]
    padded, padded_issues = normalize_chart_data("bar", {
        "labels": ["A", "B", "C"], "datasets": [{"data": [1]}],
    })
    # 只补不删：标签保留 3 个，短的 data 补 0（并显式记下问题）
    assert padded["labels"] == ["A", "B", "C"]
    assert padded["datasets"][0]["data"] == [1.0, 0.0, 0.0]
    assert padded_issues == [{"field": "datasets[0].data", "expected": 3, "actual": 1}]


def test_non_numeric_chart_types_are_untouched():
    """表格 / 思维导图 / 时间轴不做数值归一化（否则会被清空）。"""
    for ctype, payload in (("table", {"headers": ["指标"], "rows": [["1"]]}),
                           ("mindmap", {"root": "根", "branches": [{"title": "分支"}]}),
                           ("timeline", {"steps": [{"title": "第一步"}]})):
        data, issues = normalize_chart_data(ctype, payload)
        assert data == payload and issues == []


def test_mermaid_count_does_not_depend_on_flowchart_kind():
    """流程图数 = 讲义/正文里的 ```mermaid 段数，与是否存在 kind='flowchart' 条目无关。"""
    markdown = ("# 1.2 多菌共生\n\n```mermaid\nflowchart TD\n  A --> B\n```\n\n正文\n\n"
                "```mermaid\nflowchart LR\n  C --> D\n```\n")
    assert count_mermaid(markdown) == 2
    rows = [{"kind": "lecture", "body": {"markdown": markdown}, "meta": {}},
            {"kind": "lecture", "body": {"markdown": "没有流程图"}, "meta": {}},
            {"kind": "text", "body": {"markdown": "```mermaid\nflowchart TD\n X --> Y\n```"}, "meta": {}}]
    assert flowchart_count(rows) == 3  # 两篇讲义各 2/0 段 + 一篇正文 1 段（正文同样渲染流程图）
    assert flowchart_count([{"kind": "flowchart", "body": {"mermaid": "flowchart TD\n A-->B"}, "meta": {}}]) == 0
    assert flowchart_count([{"kind": "lecture", "body": {}, "meta": {MERMAID_BLOCKS_KEY: 4}}]) == 4


def test_chart_route_colors_and_records_issues(client, course_id):
    """接口闭环：生成柱状图 → 有色 + meta.chart_data_issues=[]；手工改畸形数据 → 记录问题。"""
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "酵母发酵温度控制", "num_chapters": 1,
    })
    assert plan.status_code == 200, plan.text
    node_id = plan.json()["nodes"][0]["id"]
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200

    made = client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node_id, "chart_type": "bar", "instruction": "温度对比",
    })
    assert made.status_code == 200, made.text
    row = made.json()
    palette = _hex_set(active_palette()[0])
    assert row["body"]["data"]["datasets"], row["body"]["data"]
    for ds in row["body"]["data"]["datasets"]:
        assert ds["backgroundColor"] in palette and ds["borderColor"] in palette
    assert row["meta"][CHART_DATA_ISSUES_KEY] == []
    assert MERMAID_BLOCKS_KEY not in row["meta"]  # 流程图段数只统计讲义条目

    broken = client.patch(f"/api/chart/{row['id']}", json={
        "data": {"labels": ["只有一项"], "datasets": [{"label": "A", "data": [1, 2]}]},
    })
    assert broken.status_code == 200, broken.text
    body = broken.json()
    assert len(body["body"]["data"]["labels"]) == 2
    assert body["meta"][CHART_DATA_ISSUES_KEY] == [{"field": "labels", "expected": 2, "actual": 1}]


def test_lecture_rows_carry_mermaid_blocks_meta(client, course_id, gen_lecture_row):
    """讲义条目在读取层带上 meta.mermaid_blocks（前端口径的唯一来源）。"""
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "光合作用", "num_chapters": 1,
    })
    assert plan.status_code == 200, plan.text
    node_id = plan.json()["nodes"][0]["id"]
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200
    gen_lecture_row(course_id, node_id)

    rows = client.get(f"/api/content/{course_id}").json()
    lectures = [r for r in rows if r["kind"] == "lecture"]
    assert lectures, rows
    for row in lectures:
        assert row["meta"][MERMAID_BLOCKS_KEY] == count_mermaid(row["body"]["markdown"])


def test_ppt_themes_exposes_active_palette(client):
    """/api/ppt/themes 带 active 色板：前端按它给老图表兜底上色（与概念图/PPT 同一份）。"""
    payload = client.get("/api/ppt/themes").json()
    colors, theme_id, _ = active_palette()
    assert payload["active"]["id"] == theme_id
    assert payload["active"]["colors"] == colors
    assert payload["items"] and all(item["colors"] for item in payload["items"])
