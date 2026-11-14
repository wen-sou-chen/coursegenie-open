# -*- coding: utf-8 -*-
"""教学数据图表的「数据归一化 + 主题上色」唯一口径（前端 lib/chartTheme.ts 与之对齐）。

解决两个真实问题（任务 27）：

1. **全是灰的**：LLM 给的 ``datasets[*]`` 没有 ``backgroundColor``，chart.js 默认色是
   浅灰 ``rgba(0,0,0,0.1)``，而 PPT 原生图表 / 概念图是有主题色的 → 两边不一致。
   这里按 ``ppt_theme`` 的 ``palette.colors``（与概念图、PPT 同一份 6 色）**按 dataset
   序号**写 ``borderColor`` / ``backgroundColor``（折线用半透明填充）。

2. **画错位置**：``labels`` 与 ``datasets[*].data`` 长度不一致（真实库里 3/14 张），
   网页端按 labels 画 → 柱子与标签错位。这里以**最长的一方为准**（span =
   max(labels 长度, 各条 data 长度)）对齐，只补不删，保证任意一条 dataset 都能画满刻度：

   - 补标签：缺的用 ``第N项``（N = 该位置的 1 起序号，前端同规则）；
   - 补数值：缺的补 0（不编造数据，只是占位）；
   - ``labels`` 多出来的照常保留，对应位置数据补 0（不丢标签、不截断分类）。

原始问题记进 ``meta.chart_data_issues``（``[{field, expected, actual}]``），**不静默**。
老数据不迁移：读取时（``app.services.content.build_content_rows``）用同一函数归一化。
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.services.ppt_theme import resolve_theme

# 折线/柱状用的半透明填充（同色系、不抢眼）
LINE_FILL_ALPHA = 0.35

CHART_DATA_ISSUES_KEY = "chart_data_issues"

_NUM_TYPES = ("chart_bar", "chart_line", "chart_pie")
_PINNED_MINDMAP = ("root", "branches")
_PINNED_TIMELINE = ("steps",)
_PINNED_TABLE = ("headers", "rows")


def active_palette() -> tuple[list[str], str, str | None]:
    """当前主题色板：``(6 色 hex 列表, 主题 id, 主题回退原因)``。"""
    resolved, theme_error = resolve_theme(get_settings().ppt_theme)
    return list(resolved["palette"]["colors"]), str(resolved["id"]), theme_error


def _as_number(value: Any) -> float:
    """数值化：数值直通，字符串尽力解析，非法值记 0（与前端 ``Number(v) || 0`` 同口径）。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _fill_alpha(hex_color: str, alpha: float) -> str:
    """``'4C8DF6'`` → ``'rgba(76,141,246,0.35)'``（颜色本身由主题固定，无需兜底解析）。"""
    raw = str(hex_color or "").lstrip("#")
    if len(raw) != 6:
        return f"rgba(76,141,246,{alpha})"
    try:
        r, g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return f"rgba(76,141,246,{alpha})"
    return f"rgba({r},{g},{b},{alpha})"


def _issues_for(raw_data: Any, labels: list[Any], series: list[list[Any]]) -> list[dict]:
    span = max([len(labels)] + [len(s) for s in series]) if (labels or series) else 0
    out: list[dict] = []
    if len(labels) != span:
        out.append({"field": "labels", "expected": span, "actual": len(labels)})
    for idx, values in enumerate(series):
        expected = span if span else len(values)
        if len(values) != expected:
            out.append({"field": f"datasets[{idx}].data", "expected": expected,
                        "actual": len(values)})
    if not series:
        out.append({"field": "datasets", "expected": 1, "actual": 0})
    return out


def chart_data_issues(raw_data: Any) -> list[dict]:
    """归一化前的问题清单（老数据的 ``meta`` 可能没有，由读取层现算）。"""
    if not isinstance(raw_data, dict):
        return [{"field": "data", "expected": 1, "actual": 0}]
    pinned = _PINNED_MINDMAP + _PINNED_TIMELINE + _PINNED_TABLE
    if any(key in raw_data for key in pinned):
        return []
    labels = list(raw_data.get("labels") or [])
    datasets = raw_data.get("datasets")
    datasets = datasets if isinstance(datasets, list) else []
    series = [list(ds.get("data") or []) for ds in datasets if isinstance(ds, dict)]
    return _issues_for(raw_data, labels, series)


def normalize_chart_data(chart_type: str, raw_data: Any,
                         colors: list[str] | None = None) -> tuple[dict, list[dict]]:
    """归一化 + 上色：返回 ``(可直接落库/渲染的 data, 问题清单)``。

    非数值型图表（mindmap / timeline / table）原样返回，不做任何改动。
    """
    data = dict(raw_data) if isinstance(raw_data, dict) else {}
    if str(chart_type) not in ("bar", "line", "pie"):
        return data, []
    palette = list(colors) if colors else active_palette()[0]
    labels = list(data.get("labels") or [])
    datasets = data.get("datasets")
    datasets = datasets if isinstance(datasets, list) else []
    series = [list(ds.get("data") or []) if isinstance(ds, dict) else [] for ds in datasets]
    issues = _issues_for(data, labels, series)

    span = max([len(labels)] + [len(s) for s in series]) if (labels or series) else 0
    new_labels = [str(v) for v in labels[:span]]
    while len(new_labels) < span:
        new_labels.append(f"第{len(new_labels) + 1}项")
    data["labels"] = new_labels

    is_line = str(chart_type) == "line"
    new_datasets: list[dict] = []
    for idx, ds in enumerate(datasets):
        if not isinstance(ds, dict):
            continue
        values = series[idx][:span]
        while len(values) < span:
            values.append(0)
        color = palette[idx % len(palette)]
        new_datasets.append({
            **ds,
            "data": [_as_number(v) for v in values],
            "borderColor": ds.get("borderColor") or f"#{color}",
            "backgroundColor": ds.get("backgroundColor") or (
                _fill_alpha(color, LINE_FILL_ALPHA) if is_line else f"#{color}"
            ),
        })
    data["datasets"] = new_datasets
    return data, issues
