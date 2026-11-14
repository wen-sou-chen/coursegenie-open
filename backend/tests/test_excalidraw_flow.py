# -*- coding: utf-8 -*-
"""Excalidraw 流程图转换（mermaid → .excalidraw JSON）测试。"""
from app.services.excalidraw_flow import mermaid_to_excalidraw_json

SAMPLE = """
flowchart TD
    A[叶绿体吸收光能] --> B[光反应阶段]
    B --> C{产物去向}
    C -->|产生氧气| D[氧气释放]
    C -->|生成ATP和[H]| E[暗反应阶段]
    E --> F[固定CO2]
    F --> G[合成有机物]
"""


def test_convert_builds_valid_elements():
    data = mermaid_to_excalidraw_json(SAMPLE)
    assert data is not None
    assert data["type"] == "excalidraw"
    assert data["version"] == 2
    types = [el["type"] for el in data["elements"]]
    assert types.count("rectangle") >= 5
    assert "diamond" in types
    assert "arrow" in types
    texts = "".join(el.get("text", "") for el in data["elements"] if el["type"] == "text")
    for keyword in ("叶绿体吸收光能", "光反应阶段", "暗反应阶段", "产物去向",
                    "氧气释放", "生成ATP和[H]", "固定CO2", "合成有机物"):
        assert keyword in texts
    ids = [el["id"] for el in data["elements"]]
    assert len(ids) == len(set(ids)), "元素 id 必须唯一"


def test_invalid_mermaid_returns_none():
    assert mermaid_to_excalidraw_json("") is None
    assert mermaid_to_excalidraw_json("not a graph at all") is None
    assert mermaid_to_excalidraw_json("flowchart TD") is None