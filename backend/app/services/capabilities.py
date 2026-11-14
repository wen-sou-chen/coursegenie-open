"""外部依赖能力探测（任务 116）：把「这台电脑缺了什么」变成能看懂的一句话。

为什么有这条：交付包的 PDF 走 **Microsoft Edge**（headless `--print-to-pdf`）、
课件预览 PDF / 每页图片走 **PowerPoint COM**（`ppt_com.py`）—— 把应用搬到别人的电脑上，
这两个几乎肯定有一个没装。以前没有统一的「探测 + 人话提示 + 明确降级」：
`/api/health` 里看不到它们的可用性，失败了用户也未必看得出是「环境缺组件」而不是「课有问题」。

任务 117 追加一项 **`llm`（模型配置）**：`LLM_PROVIDER` + 对应 Key 有没有配好。判定同样
**不新造第二套** —— 走 `app.services.llm.llm_config_problem()`，与 `build_llm_client()` 真正用的是
同一个函数；文案里**只有变量名，没有任何 Key 值/前缀**。

三条纪律（对应任务书 §二.2）：

1. **便宜且无副作用**：只查可执行文件在不在、pywin32 装没装，**绝不真的启动** PowerPoint/Edge；
   连「版本号」都不查（查版本就得起进程，不值当）。
2. **不新造第二套判定**：Edge 走 `delivery.find_edge()`、PowerPoint 走 `ppt.powerpoint_exe()`，
   与出包 / 预览实际用的是**同一个出处**，避免「探测说可用、真跑却用另一套」。
3. **结果进既有健康口径**：由 `main.create_app()` 挂进 `GET /api/health` 的 `capabilities` 字段
   （本项目没有 capabilities 端点，不为此新造一个）。

每个探测项给人看的字段：`available` / `affects`（哪些功能会受影响）/
`unaffected`（哪些不受影响）/ `install`（怎么装，一句话）/ `reason`（缺了什么，人话）；
`exe` 只有 edge / powerpoint 有，llm 项多 `provider` / `needs_key` / `mock`。
任务 118 追加两项 **`ytdlp` / `js_runtime`**（「从链接导入 YouTube」用）。这两项里 yt-dlp
要**跑一次 `--version`** —— 任务 118 §二.3 明确要求「检测 yt-dlp 与 deno 是否可用（只查可执行
文件/版本，不联网）」，而且实测本机就有「文件在、但跑不起来」的假可用（`...\Python313\Scripts\
yt-dlp.exe` 的解释器已被删）⇒ 只查存在性会说谎。它**仍然不联网**，结果按进程缓存，
`GET /api/health` 不会每读一次就起一次进程。
"""
from __future__ import annotations

import importlib.util
from typing import Any

from app.core.config import get_settings

EDGE_KEY = "edge"
POWERPOINT_KEY = "powerpoint"
LLM_KEY = "llm"
YTDLP_KEY = "ytdlp"
JS_RUNTIME_KEY = "js_runtime"

EDGE_INSTALL = "装 Microsoft Edge（Windows 10/11 自带，装了就有）；或设 DELIVERY_EDGE_EXE 指向 msedge.exe"
POWERPOINT_INSTALL = "装 Microsoft Office（含 PowerPoint）；或设 POWERPOINT_EXE 指向 POWERPNT.EXE"
PYWIN32_INSTALL = "pip install pywin32（Python 调 PowerPoint COM 的桥）"


def edge_capability() -> dict[str, Any]:
    """Edge（无头打印 PDF）能不能用 + 缺了会怎样。"""
    from app.services import delivery as delivery_mod

    exe = delivery_mod.find_edge()
    return {
        "key": EDGE_KEY,
        "label": "Microsoft Edge（无头打印：讲义 / 课时 / 学生版讲义转 PDF）",
        "available": bool(exe),
        "exe": exe or "",
        "probe": "查 msedge.exe 是否存在（可用 DELIVERY_EDGE_EXE 覆盖）；只查存在性，不启动进程",
        "affects": ["交付包里的讲义 PDF", "课时 Word 转 PDF", "学生版讲义 PDF"],
        "unaffected": ["网页端预览与只读分享页", "课件（pptx / 网页版 HTML）", "图表与概念图",
                       "笔记库与 Markdown 源文件", "素材、习题、质检报告"],
        "install": EDGE_INSTALL,
        "reason": None if exe else (
            "本机没找到 Microsoft Edge（--print-to-pdf）：交付包里「讲义 PDF」这一项会跳过并写明原因，"
            "其余内容照常出（并给你可直接打印的 HTML）—— 这是环境缺组件，不是你的课有问题"
        ),
    }


def powerpoint_capability() -> dict[str, Any]:
    """PowerPoint COM（课件预览 PDF / 每页图片）能不能用 + 缺了会怎样。"""
    from app.services import ppt as ppt_mod

    exe = ppt_mod.powerpoint_exe()
    has_pywin32 = importlib.util.find_spec("win32com") is not None
    if not exe:
        reason = ("本机没找到 PowerPoint（POWERPNT.EXE）：课件预览会回退成「文字页级大纲」，"
                  "网页端预览 / 分享 / 交付包其余内容都照常 —— 这是环境缺组件，不是你的课有问题")
        install = POWERPOINT_INSTALL
    elif not has_pywin32:
        reason = ("本机装了 PowerPoint，但 Python 侧没有 pywin32（COM 桥）：课件预览同样会回退成"
                  "「文字页级大纲」—— 这是环境缺组件，不是你的课有问题")
        install = PYWIN32_INSTALL
    else:
        reason = None
        install = ""
    return {
        "key": POWERPOINT_KEY,
        "label": "PowerPoint COM（课件预览 PDF + 每页幻灯片图片）",
        "available": bool(exe) and has_pywin32,
        "exe": exe or "",
        "pywin32": has_pywin32,
        "probe": "查 POWERPNT.EXE 是否存在（可用 POWERPOINT_EXE 覆盖）+ pywin32 是否装了；"
                 "只查存在性，绝不启动 PowerPoint 进程",
        "affects": ["课件预览的 PDF（网页内嵌）", "课件每页的幻灯片图片（放大看）"],
        "unaffected": ["网页端预览的页级大纲（自动回退）", "讲义 / 课时 / 学生版与它们的 PDF",
                       "图表与概念图", "笔记库与 Markdown 源文件"],
        "install": install,
        "reason": reason,
    }


def llm_capability() -> dict[str, Any]:
    """模型配置（``LLM_PROVIDER`` + 对应 Key）配好没有 + 缺了会怎样。

    判定**不新造第二套**：走 ``app.services.llm.llm_config_problem()`` —— 与
    ``build_llm_client()`` 真正用的是同一个函数。返回里**只有变量名**，没有 Key 的值/前缀。
    """
    from app.services import llm as llm_mod

    settings = get_settings()
    provider = llm_mod.normalized_provider(settings)
    problem = llm_mod.llm_config_problem(settings)
    return {
        "key": LLM_KEY,
        "label": "模型配置（LLM_PROVIDER + 对应 API Key）",
        "available": problem is None,
        "provider": provider or "（空）",
        "needs_key": llm_mod.LLM_PROVIDER_KEYS.get(provider, ""),
        "mock": provider == llm_mod.LLM_MOCK_PROVIDER,
        "probe": "只读配置：LLM_PROVIDER 与对应 Key 是否非空；**不发任何网络请求**，"
                 "返回里只回变量名、**永不回 Key 的值或前缀**",
        "affects": ["生成大纲", "生成讲义", "生成课件 / 图表 / 概念图 / 习题（一切要调模型的动作）"],
        "unaffected": ["浏览与预览已有内容", "交付包导出（PDF 走 Edge，不调模型）", "只读分享页",
                       "素材管理", "数据备份 / 恢复", "质检与用量报告（读已有数据）"],
        "install": llm_mod.LLM_CONFIG_HINT,
        "reason": problem["message"] if problem else None,
    }


def ytdlp_capability() -> dict[str, Any]:
    """yt-dlp 能不能用 + 缺了会怎样（**只影响「从链接导入」**，其它功能一律照常）。"""
    from app.services import link_import as link_mod

    probe = link_mod.ytdlp_probe()
    return {
        "key": YTDLP_KEY,
        "label": "yt-dlp（从链接导入 YouTube：抓字幕 / 下音频）",
        "available": bool(probe["available"]),
        "version": probe.get("version") or "",
        "cmd": " ".join(probe.get("cmd") or []),
        "probe": "查可执行文件 + 跑一次 `--version`（**只查版本、不联网**；结果按进程缓存）",
        "affects": ["从链接导入（抓字幕 / 没字幕时下音频）"],
        "unaffected": ["上传文件 / 粘贴文本 / AI 补充素材", "生成 / 交付 / 质检 / 分享",
                       "已有音视频素材的本地转写（用已上传的原件，不碰网络）"],
        "install": link_mod.YTDLP_INSTALL,
        "reason": None if probe["available"] else probe["reason"],
    }


def js_runtime_capability() -> dict[str, Any]:
    """JS 运行时（deno / Node.js）在不在 + 缺了会怎样 —— yt-dlp 抓 YouTube 需要它。"""
    from app.services import link_import as link_mod

    info = link_mod.js_runtime_probe()
    return {
        "key": JS_RUNTIME_KEY,
        "label": "JavaScript 运行时（deno / Node.js —— yt-dlp 抓 YouTube 需要）",
        "available": bool(info["available"]),
        "deno": info.get("deno") or "",
        "node": info.get("node") or "",
        "probe": "查 deno / node 可执行文件是否存在（PATH + ~/.deno/bin）；**不跑进程、不联网**",
        "affects": ["从链接导入（抓字幕 / 没字幕时下音频）"],
        "unaffected": ["上传文件 / 粘贴文本 / AI 补充素材", "生成 / 交付 / 质检 / 分享",
                       "已有音视频素材的本地转写（用已上传的原件，不碰网络）"],
        "install": link_mod.JS_RUNTIME_INSTALL,
        "reason": None if info["available"] else info["reason"],
    }


def capability_report() -> dict[str, Any]:
    """一屏能力清单：缺什么 / 影响什么 / 不受影响 / 怎么装。给 ``GET /api/health`` 用。"""
    items = [llm_capability(), edge_capability(), powerpoint_capability(),
             ytdlp_capability(), js_runtime_capability()]
    missing = [item for item in items if not item["available"]]
    return {
        "items": items,
        "all_available": not missing,
        "missing": [item["key"] for item in missing],
        "hint": None if not missing else "；".join(str(item["reason"]) for item in missing),
    }
