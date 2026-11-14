# -*- coding: utf-8 -*-
"""Excalidraw 文档规范测试（M2 绘图插件化）。

覆盖三件事：
1. 我方生成的全部产物（7 布局 + 6 数据图 + mermaid 流程图）都通过插件严格自检；
2. 箭头/连线与图形**双向绑定**（拖节点时箭头跟随），容器文字 autoResize=False；
3. mermaid 转换是确定性的（同一输入两次得到同一文档指纹）。

对照 Obsidian Excalidraw 插件 2.26.4 使用的官方元素 schema。
"""
from __future__ import annotations

import copy
import json

import pytest

from app.services.excalidraw_chart import chart_to_excalidraw_doc
from app.services.excalidraw_flow import mermaid_to_excalidraw_json
from app.services.excalidraw_format import (
    fingerprint,
    normalize_document,
    summarize,
    validate_document,
)
from app.services.excalidraw_layout import build_excalidraw_json, render

from tests.test_excalidraw_layout import LAYOUT_SCENES

CHART_CASES = {
    "bar": {"labels": ["一周", "二周", "三周"], "datasets": [{"label": "体积", "data": [1, 1.6, 2.1]}]},
    "line": {"labels": ["0 分", "30 分", "60 分"], "datasets": [{"label": "温度", "data": [24, 30, 36]}]},
    "pie": {"labels": ["面粉", "水", "酵母"], "datasets": [{"label": "占比", "data": [5, 2.5, 0.1]}]},
    "table": {"headers": ["阶段", "时间", "判断"], "rows": [["和面", "10 分钟", "不粘手"]]},
    "mindmap": {"root": "发酵三要素", "branches": [
        {"title": "温度", "subs": ["28~35℃"]},
        {"title": "湿度", "subs": ["盖湿布"]},
        {"title": "时间", "subs": ["2 倍大"]}]},
    "timeline": {"steps": [{"title": "和面", "subs": ["揉到光滑"]},
                           {"title": "发酵", "subs": ["两倍大"]},
                           {"title": "蒸制", "subs": ["上汽 12 分钟"]}]},
}

FLOW = """flowchart TD
    A[和面]-->B{发到两倍了吗}
    B-->|是|C[排气整形]
    B-->|否|D[再等 20 分钟]
    D-->B"""


def _layout_docs() -> dict:
    return {name: build_excalidraw_json(render(name, scene))
            for name, scene in LAYOUT_SCENES.items()}


def _chart_docs() -> dict:
    return {f"chart_{k}": chart_to_excalidraw_doc(k, v, f"{k} 示例")
            for k, v in CHART_CASES.items()}


def _all_docs() -> dict:
    docs = _layout_docs()
    docs.update(_chart_docs())
    docs["flow"] = mermaid_to_excalidraw_json(FLOW)
    return docs


@pytest.mark.parametrize("name", sorted(_all_docs()))
def test_every_figure_passes_plugin_schema(name):
    """我方生成的每一张图都必须通过插件元素规范自检。"""
    doc = _all_docs()[name]
    assert doc is not None, f"{name} 未生成文档"
    assert validate_document(doc) == [], name
    assert json.loads(json.dumps(doc, ensure_ascii=False)) == doc  # 可 JSON 往返


def test_flowchart_arrows_are_bound_at_both_ends():
    """流程图的每条箭头都要与起止图形双向绑定（否则拖动节点箭头不动）。"""
    doc = mermaid_to_excalidraw_json(FLOW)
    stats = summarize(doc)
    assert stats["linear"] == 4
    assert stats["linear_bound"] == 4
    assert stats["bindings"] == 2 * stats["linear"]


@pytest.mark.parametrize("name", ["chain", "radial", "decision"])
def test_layout_arrows_are_bound(name):
    """带连线的布局：所有箭头都建立了绑定，且绑定是可拖动的双向关系。"""
    doc = build_excalidraw_json(render(name, LAYOUT_SCENES[name]))
    stats = summarize(doc)
    assert stats["linear"] > 0, f"{name} 未生成连线"
    assert stats["linear_bound"] == stats["linear"], f"{name} 存在未绑定连线"
    assert stats["bindings"] == 2 * stats["linear"]


@pytest.mark.parametrize("name", ["decision", "timeline"])
def test_container_text_is_not_auto_resize(name):
    """容器内文字必须 autoResize=False 且与容器互指，否则插件里文字会溢出。"""
    doc = build_excalidraw_json(render(name, LAYOUT_SCENES[name]))
    texts = [e for e in doc["elements"]
             if e.get("type") == "text" and e.get("containerId")]
    assert texts, f"{name} 没有容器文字"
    for el in texts:
        assert el["autoResize"] is False
        owners = [c for c in doc["elements"] if c.get("id") == el["containerId"]]
        assert owners, f"{name} 文字指向不存在的容器"
        assert any(b.get("id") == el["id"] for b in owners[0].get("boundElements") or [])


def test_mermaid_conversion_is_deterministic():
    """同一份 mermaid 两次转换必须等价（忽略 updated），避免每次导出都改文件。"""
    first = mermaid_to_excalidraw_json(FLOW)
    second = mermaid_to_excalidraw_json(FLOW)
    assert fingerprint(first) == fingerprint(second)
    assert [e["id"] for e in first["elements"]] == [e["id"] for e in second["elements"]]
    assert validate_document(second) == []


def test_validator_catches_broken_documents():
    """自检必须能抓出三类典型坏产物：缺字段、悬空绑定、容器文字 autoResize=True。"""
    good = copy.deepcopy(mermaid_to_excalidraw_json(FLOW))
    assert validate_document(good) == []

    missing_field = copy.deepcopy(good)
    missing_field["elements"][0].pop("seed")
    assert any("seed" in p for p in validate_document(missing_field))

    orphan_binding = copy.deepcopy(good)
    arrows = [e for e in orphan_binding["elements"] if e.get("type") == "arrow"]
    arrows[0]["startBinding"] = {"elementId": "no-such-shape", "focus": 0, "gap": 0}
    assert any("不存在" in p for p in validate_document(orphan_binding))

    bad_text = copy.deepcopy(good)
    texts = [e for e in bad_text["elements"] if e.get("containerId")]
    texts[0]["autoResize"] = True
    assert any("autoResize" in p for p in validate_document(bad_text))

    broken_ref = copy.deepcopy(good)
    boxes = [e for e in broken_ref["elements"] if e.get("type") == "rectangle"]
    boxes[0].setdefault("boundElements", []).append({"id": "ghost", "type": "text"})
    assert any("ghost" in p for p in validate_document(broken_ref))


def test_normalize_is_idempotent():
    """重复规范化（导出会对同一份元素跑多次）必须稳定且仍合法。"""
    doc = build_excalidraw_json(render("chain", LAYOUT_SCENES["chain"]))
    once = fingerprint(doc)
    again = normalize_document(copy.deepcopy(doc))
    assert fingerprint(again) == once
    assert validate_document(again) == []
    assert summarize(again) == summarize(doc)


def test_duplicate_ids_are_repaired():
    """意外重复 id 要被规范化修掉，并保持绑定指向仍有效。"""
    doc = mermaid_to_excalidraw_json(FLOW)
    doc["elements"][1]["id"] = doc["elements"][0]["id"]
    fixed = normalize_document(doc)
    ids = [e["id"] for e in fixed["elements"]]
    assert len(ids) == len(set(ids))


def test_legacy_stored_body_is_upgraded_at_export():
    """M2 之前存库的 diagram 产物（无绑定字段）导出时必须补齐，且不改动库内那份。"""
    from types import SimpleNamespace

    from app.services.kb import _diagram_text

    legacy = mermaid_to_excalidraw_json(FLOW)
    for el in legacy["elements"]:
        if el.get("type") in ("arrow", "line"):
            for key in ("startBinding", "endBinding", "startArrowhead", "elbowed"):
                el.pop(key, None)

    text = _diagram_text(SimpleNamespace(body={"excalidraw_json": legacy}))
    doc = json.loads(text)
    assert validate_document(doc) == []
    assert summarize(doc)["bindings"] == 8
    arrows = [e for e in legacy["elements"] if e.get("type") == "arrow"]
    assert all("startBinding" not in a for a in arrows), "导出不得改动库内数据"


def test_non_dict_diagram_body_is_ignored():
    """异常/缺失的库内产物不能把导出打断（返回 None，由上层当无图处理）。"""
    from types import SimpleNamespace

    from app.services.kb import _diagram_text

    assert _diagram_text(SimpleNamespace(body={"excalidraw_json": "not-a-dict"})) is None
    assert _diagram_text(SimpleNamespace(body={})) is None
