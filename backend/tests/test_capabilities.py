"""任务 116：外部依赖（Edge / PowerPoint）的**统一探测 + 人话降级**。

为什么有这条：交付包 PDF 走 Edge、课件预览 PDF 走 PowerPoint COM —— 把应用搬到别人的电脑上，
这两个几乎肯定有一个没装。以前 `/api/health` 里看不到它们的可用性，失败了也未必看得出
「是环境缺组件」还是「我的课有问题」。

本文件覆盖任务书 §二.4 的 ②④：
② 探测在「假装没有 Edge / 没有 PowerPoint / 没有 pywin32」时返回不可用 + 人话原因；
④ 相关 API（`/api/health`）**不返回 500**，缺组件时照样 200 并给出人话。
（① `SERVE_FRONTEND` 默认值断言在 `tests/test_serve_frontend.py`；
  ③ 缺 Edge 出包不整包失败在 `tests/test_delivery.py`。）

探测本身的三条纪律也各有一条用例钉住：判定与出包同源、只查存在性、**绝不启动进程**。

**任务 117** 在同一份 `capability_report()` 里加了第三项 `llm`（模型配置：`LLM_PROVIDER`
+ 对应 Key），本文件相关断言随之从 2 项扩到 3 项；模型配置自己的用例见 `tests/test_llm_config.py`。
**任务 118** 再追加两项 `ytdlp` / `js_runtime`（「从链接导入 YouTube」用）：本文件相关断言随之
从 3 项扩到 5 项；这两项的探测细节见 `tests/test_material_link.py` 与 `tests/test_capabilities.py`
里的 `test_probe_only_spawns_version_queries`（yt-dlp 的 `--version` 是**唯一**允许起的进程）。
"""
from __future__ import annotations

import importlib.util
import subprocess

from app.services import capabilities as cap
from app.services import llm as llm_mod
from app.services import delivery as delivery_mod
from app.services import ppt as ppt_mod
from app.services import link_import as link_mod

# 三项共有的「给人看」字段（`exe` 只有 edge / powerpoint 有 —— llm 没有可执行文件，
# 它的专属字段是 provider / needs_key / mock，见 test_report_shape... 里的显式断言）。
REQUIRED_FIELDS = ("key", "label", "available", "probe",
                   "affects", "unaffected", "install", "reason")


def _item(report: dict, key: str) -> dict:
    return next(item for item in report["items"] if item["key"] == key)


def test_report_shape_and_agrees_with_the_real_environment():
    """探测结果与「出包 / 预览真正用的那个判定」逐项一致（不另写第二套），且字段齐全。"""
    report = cap.capability_report()
    assert [item["key"] for item in report["items"]] == ["llm", "edge", "powerpoint",
                                                         "ytdlp", "js_runtime"]

    llm_item = _item(report, cap.LLM_KEY)
    assert llm_item["available"] is (llm_mod.llm_config_problem() is None)
    assert llm_item["provider"] == llm_mod.normalized_provider()
    assert "exe" not in llm_item  # 模型配置没有「可执行文件」这一说

    edge = _item(report, cap.EDGE_KEY)
    assert edge["available"] is (delivery_mod.find_edge() is not None)
    assert edge["exe"] == (delivery_mod.find_edge() or "")

    point = _item(report, cap.POWERPOINT_KEY)
    assert point["available"] is (ppt_mod.powerpoint_exe() is not None
                                  and importlib.util.find_spec("win32com") is not None)
    assert point["exe"] == (ppt_mod.powerpoint_exe() or "")

    for item in report["items"]:
        for field in REQUIRED_FIELDS:
            assert field in item, (item["key"], field)
        assert item["affects"] and item["unaffected"], item["key"]
        if item["available"]:
            assert item["reason"] is None, item["key"]
        else:
            assert item["reason"] and "不是你的课有问题" in item["reason"], item["key"]
            assert item["install"], item["key"]
    assert (report["hint"] is None) is report["all_available"]
    assert report["missing"] == [i["key"] for i in report["items"] if not i["available"]]


def test_edge_missing_gives_human_reason_and_install_hint(monkeypatch):
    """② 假装没有 Edge ⇒ 不可用 + 人话（缺什么 / 影响什么 / 怎么装）。"""
    monkeypatch.setattr(delivery_mod, "find_edge", lambda: None)

    item = cap.edge_capability()
    assert item["available"] is False
    assert item["exe"] == ""
    assert "Edge" in item["reason"] and "不是你的课有问题" in item["reason"]
    assert "PDF" in item["reason"]
    assert "Edge" in item["install"]
    assert any("PDF" in row for row in item["affects"])
    assert any("预览" in row for row in item["unaffected"])

    report = cap.capability_report()
    assert report["all_available"] is False
    assert cap.EDGE_KEY in report["missing"]
    assert "Edge" in report["hint"]


def test_powerpoint_missing_gives_human_reason_and_no_pdf(monkeypatch):
    """② 假装没有 PowerPoint ⇒ 不可用 + 人话（回退页级大纲，不是课坏了）。"""
    monkeypatch.setattr(ppt_mod, "powerpoint_exe", lambda: None)

    item = cap.powerpoint_capability()
    assert item["available"] is False
    assert "PowerPoint" in item["reason"] and "不是你的课有问题" in item["reason"]
    assert "回退" in item["reason"] or "大纲" in item["reason"]
    assert "Office" in item["install"]
    assert any("课件" in row for row in item["affects"])

    assert cap.POWERPOINT_KEY in cap.capability_report()["missing"]


def test_powerpoint_without_pywin32_is_reported_separately(monkeypatch):
    """装了 PowerPoint 但没有 pywin32 ⇒ 也说清是哪一层缺（并给 pip 那一句）。"""
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "win32com"
                        else real_find_spec(name, *a, **k))

    item = cap.powerpoint_capability()
    assert item["available"] is False
    assert item["pywin32"] is False
    assert "pywin32" in item["reason"] and "不是你的课有问题" in item["reason"]
    assert "pywin32" in item["install"]


def test_probe_only_spawns_version_queries(monkeypatch):
    """探测必须**便宜且无副作用**：不启动 PowerPoint / Edge；任务 118 起只允许多一次版本查询。

    任务 118 §二.3 要求「检测 yt-dlp 与 deno 是否可用（只查可执行文件/版本，不联网）」，而本机
    实测存在「文件在、但跑不起来」的假可用（`...\Python313\Scripts\yt-dlp.exe` 的解释器已删）
    ⇒ 允许 `yt-dlp --version` 这一类**版本查询**；除此之外**一个进程都不许起**。
    """
    calls: list[list[str]] = []

    class _Done:
        returncode = 1
        stdout = ""
        stderr = ""

    def spy(args, *a, **kwargs):
        calls.append([str(part) for part in (args if isinstance(args, (list, tuple)) else [args])])
        return _Done()

    link_mod.YTDLP_PROBE_CACHE.clear()
    link_mod.JS_RUNTIME_CACHE.clear()
    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(subprocess, "Popen", spy)
    report = cap.capability_report()
    assert len(report["items"]) == 5          # 没被打断 = 探测本身没抛
    assert calls, "应当至少查过一次 yt-dlp 版本"
    for argv in calls:
        assert argv[1:] == ["--version"], argv  # 只许 `--version`，别的参数一律不许


def test_health_exposes_capabilities_and_never_500s(client, monkeypatch):
    """④ 走真接口：`/api/health` 带上能力清单；缺组件时照样 200 + 人话，绝不 500。"""
    body = client.get("/api/health")
    assert body.status_code == 200
    payload = body.json()
    assert payload["ok"] is True
    caps = payload["capabilities"]
    assert caps["items"] and caps["missing"] == [i["key"] for i in caps["items"]
                                                 if not i["available"]]
    assert set(caps["missing"]) <= {cap.EDGE_KEY, cap.POWERPOINT_KEY,
                                    cap.YTDLP_KEY, cap.JS_RUNTIME_KEY}

    # 两个都「缺」：接口仍 200，且 hint 是能给人看的一句话
    monkeypatch.setattr(delivery_mod, "find_edge", lambda: None)
    monkeypatch.setattr(ppt_mod, "powerpoint_exe", lambda: None)
    degraded = client.get("/api/health")
    assert degraded.status_code == 200
    caps = degraded.json()["capabilities"]
    assert caps["all_available"] is False
    assert {cap.EDGE_KEY, cap.POWERPOINT_KEY} <= set(caps["missing"])
    assert "不是你的课有问题" in caps["hint"]
    for item in caps["items"]:
        assert item["install"], item["key"]
