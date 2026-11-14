"""完整讲义（Lecture）生成服务。

以「一篇图文混排的完整讲义」为一次生成单元：LLM 一次输出整篇 Markdown。
讲义默认零 AI 位图（P1 定版：主视觉全 Excalidraw 矢量）——LLM 不应写
![](...gen://...) 位图占位符；若个别模型仍输出，服务会将其剥离、不生成 PNG。
每篇讲义固定自动配一张 Excalidraw 矢量概念图（kind="diagram"），在「## 小结」前插入
![一图看懂](diagram://{diagram_id})（小结标题层级不一时兼容处理）；
再把正文里「看图更快」的内容拆成 2~4 张就近卡片，并按内容自动分流：
纯概念走 diagram（role=card，7 种布局），适合数据的内容走 chart_*（bar/pie/line/
table/mindmap/timeline，role=card，导出为 SVG 内嵌 + Excalidraw 可编辑原件）。
讲义带出的随堂练习落为 QuizItem 行。

讲义再生成 = 原地替换该节点讲义：先删上一版讲义创建的习题/概念图，再重新生成，
旧讲义全文进 meta.history（最多 5 条），避免越生成越堆积。
"""
from __future__ import annotations

import json
import logging
import re
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, OutlineNode, QuizItem
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.chart import CHART_TYPES, persist_charts_from_data
from app.services.diagram import (LAYOUTS, generate_diagram, generate_diagrams,
                                  persist_diagrams_from_data)
from app.services.kb import attach_vault_write, preserved_entries
from app.services.lecture_qc import (
    NO_BASIS_MARK,
    attach as attach_lecture_qc,
    build_for_item as build_lecture_qc,
    concepts_of as lecture_concepts,
    has_structured_block,
    last_citation,
    prompt_section as qc_prompt_section,
    refresh_item as refresh_lecture_qc,
    stored_qc as stored_lecture_qc,
    terminology_prompt_section,
)
from app.services.llm import build_llm_client, find_control_chars
from app.services.material import inject_material
from app.services.planner import list_nodes
from app.services.quiz_rules import (
    LEVELS,
    OPEN_TYPES,
    TYPE_LABEL,
    mark_manual,
    quiz_level,
    quiz_minutes,
    quiz_rubric,
    resolve_quiz,
    row_meta,
    workload_limit,
)
from app.services.storage_files import (
    delete_matching_items,
    delete_rows_and_files,
    flush_scheduled_removals,
    item_file_refs,
    schedule_removal,
)
from app.services import usage as usage_log
from app.services import k12
from app.services import scenario
from app.services.style_anchor import get_style_anchor, style_anchor_prompt

logger = logging.getLogger(__name__)

# 概念卡允许分流到的数据图类型（纯概念仍走 diagram 服务）
DATA_CHART_TYPES = set(CHART_TYPES)

# —— 象限图判据（任务 127，落到代码里）——
# 复测（``验收-121-122-独立复测`` ③）：象限图在本课 **0 命中** —— 判据当时只是
# ``CARDS_SYSTEM`` / ``CHART_SYSTEM`` 里的人话，**代码里没有任何关键词 / 内容规则**，
# 0 LLM 下规划器永远给「概念卡 chain + 数据图 bar」；``meta.layout_notes`` 里也**没有一条**
# 记录「为什么是象限图」（等于摆设）。现在把判据落到这里：讲义正文命中 ⇒ 数据卡选象限图，
# 并把**命中的词与理由**写进图条目的 ``meta.layout_notes``（kind="figure-kind"）；
# **没命中就照旧不选**（这条规则只负责「选不选」，图的内容仍走既有图生成路径）。
#
# 判据（两条任一）：
#   a) 出现「取舍类词」——权衡 / 取舍 / 优先级 / 优先次序 / 轻重缓急 / 性价比 / 投入产出 /
#      两轴 / 四象限（正面对照词，命中即选）；
#   b) 出现 ≥ QUADRANT_MIN_DIMS 个不同的「两维词」（成本 / 收益 / 风险 / 价值 / 效果 /
#      投入 / 产出 / 重要性 / 紧急）且合计出现 ≥ QUADRANT_MIN_HITS 次 ——
#      「同一个词反复出现」不算「两维」，所以两条一起卡。
# 阈值来历（真课 27 篇讲义实测，AI 幻觉治理实战）：b) 命中 5 篇（18%）；放宽成「≥2 个不同的
# 两维词」会命中 11 篇（41%）—— 把「只是反复提到风险」的也算了进来，那就等于没判据 ⇒ 取 3。
QUADRANT_TRADE_WORDS = ("权衡", "取舍", "优先级", "优先次序", "轻重缓急", "性价比",
                        "投入产出", "两轴", "四象限")
QUADRANT_DIM_WORDS = ("成本", "收益", "风险", "价值", "效果", "投入", "产出", "重要性", "紧急")
QUADRANT_MIN_DIMS = 3
QUADRANT_MIN_HITS = 5
#: 认引用标注：它们是「（来源:《…》第 N 段）」式出处，会把维度词的出现次数算多
#: （与 ppt 侧判据剥引用的口径一致）。
_RE_QUADRANT_CITE = re.compile(r"（来源[:：][^）]*）|《[^》]*》第\s*\d+\s*段")

#: 0-LLM 兜底：通行的重要 × 紧急四象限（象限名与说明与 ``CARDS_SYSTEM`` / ``CHART_SYSTEM``
#: 教给模型的词**逐字一致**，不新造词）。生产链路上规划器按讲义内容给真实象限；
#: 这条兜底只在「判据命中、但规划器没给象限卡」时用（mock / 模型没按约束走两种情形）。
QUADRANT_FALLBACK_DATA: dict = {
    "x_low": "不紧急", "x_high": "紧急", "y_low": "不重要", "y_high": "重要",
    "quadrants": [{"name": "计划做", "desc": "重要但不紧急"},
                  {"name": "马上做", "desc": "又重要又紧急"},
                  {"name": "不要做", "desc": "既不重要也不紧急"},
                  {"name": "委托做", "desc": "紧急但不重要"}],
}


def quadrant_criteria(text: str) -> dict | None:
    """象限图判据（任务 127）：讲义正文命中「两维权衡 / 取舍 / 优先级」⇒ 返回理由，否则 None。

    返回 ``{"hits": [...], "dims": {...}, "reason": "…"}``：``hits`` 是命中的取舍类词，
    ``dims`` 是各两维词的出现次数，``reason`` 是写进 ``meta.layout_notes`` 的人话理由
    （含命中的词，便于人工核对「为什么是象限图」）。**确定、可测、无随机、无 LLM。**
    """
    body = _RE_QUADRANT_CITE.sub("", str(text or ""))
    hits = [word for word in QUADRANT_TRADE_WORDS if word in body]
    dims = {word: body.count(word) for word in QUADRANT_DIM_WORDS if body.count(word)}
    total = sum(dims.values())
    if hits:
        reason = (f"讲义正文命中取舍类词「{'、'.join(hits)}」⇒ 数据卡选象限图"
                  f"（两轴四象限：把两个维度的取舍摊成四格）")
    elif len(dims) >= QUADRANT_MIN_DIMS and total >= QUADRANT_MIN_HITS:
        detail = "、".join(f"{word}×{count}" for word, count in
                           sorted(dims.items(), key=lambda kv: (-kv[1], kv[0])))
        reason = (f"讲义正文出现 {len(dims)} 个不同的两维词、合计 {total} 次（{detail}）"
                  f"⇒ 数据卡选象限图（两轴四象限：把两个维度的取舍摊成四格）")
    else:
        return None
    return {"hits": hits, "dims": dims, "reason": reason}


def quadrant_card_data() -> dict:
    """0-LLM 兜底的四象限数据（返回副本，调用方改了不影响模块常量）。"""
    return {**QUADRANT_FALLBACK_DATA,
            "quadrants": [dict(q) for q in QUADRANT_FALLBACK_DATA["quadrants"]]}

# 配图失败记录挂在讲义 meta 上：网页端据此显示「本图生成失败 · 重试」并可单张重试
FAIL_KEY = "diagram_failures"
# 任务 48w：内容里残留的 XML 控制字符（本该是 LaTeX 反斜杠）记在这里，供网页端/排查用；
# 不是质检判据（10 项口径与权重一律不动）。
CONTENT_ISSUES_KEY = "content_issues"

# 任务 23-B：输入指纹版本号——提示词模板或质检规则变化时必须 +1，
# 否则「模板变了但输入指纹没变」会误跳过重生成。
# 任务 41：讲义提示词新增提取优先级 / 踩过的坑 / 边界声明 / 矛盾护栏 → v3；
# 质检口径新增 substance / actionable 两项 → qc-v2。
# 任务 47：来源要求覆盖新增小节 / 执行步骤 / 表格行 → v4；
# 任务 47c：来源括号纪律（括号内只写「标题 #段落号」、无依据不写「来源:」前缀）→ v5。
# 任务 57：内联措辞自解释「（来源:《素材标题》第 N 段）」→ v6。
# 任务 48b：表格/mermaid 结构硬要求 → v7；
# 任务 58：术语表带定义 + 正文术语首次出现给括号解释 → v8（不升则 27 篇会按指纹跳过新措辞）。
# 任务 48d：核心讲解的「可操作」硬要求 + 明确「术语解释条目不算步骤」→ v9。
# 任务 48f：点名「### 举例」与操作类小节必须逐条标注 + 复述沿用同一条来源 +
# 禁止空条目 + 规则 4-D 末尾交稿自查清单 → v10（不升则 48g 会按指纹跳过新措辞）。
# 任务 51：K-12 课时口径（学段/年级语气 + 按课时控篇幅，见 app/services/k12.py）→ v11。
# 任务 53：公式一律 LaTeX（$…$ / $$…$$；交付件 HTML/PDF 转 MathML，见 delivery.py）→ v12。
# 任务 55：题型扩展 + 分层作业（level 三层 / minutes 预计用时 / 开放题 rubric）→ v13。
# 任务 85：使用场景口径（五选多 + 主场景的「口气与要求」段，见 app/services/scenario.py）→ v14。
# 不升则第 27 篇起会按输入指纹跳过新措辞；未选场景的课拼装结果与 v13 逐字相同（指纹也不变）。
# 任务 105：来源锚点口径（音视频素材写时间戳，素材锚点说明随之进提示词）——
#   **本任务不升版本号**：① 锚点说明随**注入文本**进 input_fingerprint（有素材的课
#   不会被误跳过，见 material.anchor_notice）；② 素材类任务有「不擅自改
#   PROMPT_TEMPLATE_VERSION」的既有护栏（tests/test_material_injection.py）；③ 老讲义里
#   的「第 N 段」照旧能解析，不必强制重生成。QC_RULES_VERSION 同样不动（判据/阈值未变）。
PROMPT_TEMPLATE_VERSION = "lecture-v14"
QC_RULES_VERSION = "qc-v2"

LECTURE_SYSTEM = """LECTURE_JSON
你是资深课程讲师。为给定的知识点撰写「一篇完整的图文混排讲义」，只输出 JSON：
{"markdown": "完整讲义 Markdown 全文", "summary": "一句话摘要", "keywords": ["关键词"],
 "quizzes": [{"qtype": "choice|blank|short|calc|draw|material|experiment",
   "difficulty": "easy|medium|hard", "level": "基础|提升|挑战", "minutes": 3,
   "question": "题干", "options": ["选项..."], "answer": "答案或正确选项序号",
   "rubric": "开放题（draw/material/experiment）的评分要点", "explanation": "解析"}]}

规则：
1) markdown 从 ## 开始，含引入、分点讲解（列表/表格/引用）、小结，直接可放进 Obsidian；
   不要用代码块把整篇包起来，只写本章节内容，素材中其他章节仅作背景。
2) 不使用 AI 位图（系统不会生成 PNG 插图）：需要图示时用文字/表格/引用讲清，
   流程或结构图直接写 ```mermaid 代码块；每篇讲义末尾会自动配一张矢量概念图，
   不要在正文写 ![...](gen://...) 之类的图片占位符。
3) quizzes 为 0~5 道随堂练习：**基础 / 提升 / 挑战三层都要有**（`level` 只能写这三档，
   难度递增：基础=识记，提升=应用，挑战=迁移/开放）；每题带 `minutes`（预计用时，分钟）；
   题型可用 choice（选择）/ blank（填空）/ short（简答）/ calc（计算）/ draw（作图）/
   material（材料分析）/ experiment（实验探究）；含答案与解析，choice 的 answer 用正确选项序号（0 起）；
   **开放题（draw / material / experiment）必须写 `rubric` 评分要点**（看什么、给几分，
   例如「① 标出两条河道 ② 写出流量单位 ③ 结论与数据一致」），不要只写「言之有理即可」；
   开放题不写 answer。
4) 来源标注（**每一类内容都要标，不许漏**）：
   A. 标什么：凡来自素材的事实、数字、参数、案例、失败经历、结论，不论出现在哪里 ——
      普通正文、列表项（含嵌套子项）、执行步骤的步骤行、「### 举例」、「### 踩过的坑」、
      「### 能力边界」，以及**自建的操作类小节**（「操作指南」「实施步骤」「操作流程」
      「操作规范」「关键操作步骤」这类）—— 都必须在紧邻句末就地标注。
      **「### 举例」与操作类小节不是标注豁免区**：整节**每一条**都要各自标来源 ——
      哪怕它只是对上方已标注表格 / 正文的同义复述，也按下面 🔁 的口径沿用同一条来源。
      （**表格与 ```mermaid 不在这一条里**：它们按 E 的口径标，不要按「逐行标」理解。）
      🔁 **复述沿用来源**：同一讲里**复述**前面已标注过的结论时（举例、小结、踩过的坑
      里的复述），**沿用同一条来源即可** —— 照抄那一行的「（来源:《素材标题》第 N 段）」，
      **不许因为「上面已经标过」就留空**（留空按未标注处理）；**也不许为复述另编一条
      新来源**（C 的硬禁止不变）。
   B. 写法（**锚点按素材类型走，两种口径可能同时出现**）：
      · **文本素材**（每段前有 `[序号]`）→「（来源:《素材标题》第 N 段）」，
        例如「（来源:《破解AI幻觉》第 33 段）」—— N 取素材里每段前的 [序号]；
      · **音视频素材**（每段行首是 `[mm:ss]`）→「（来源:《素材标题》06:47）」——
        时间戳**照抄该段段首方括号里的原文**（超过 1 小时是 `105:03` 这种三位分钟），
        **不要**改写成「第 N 段」；
      · 素材里没有依据 →「（无直接依据,建议人工确认）」。
      **标题一律带书名号《》并换成素材真实标题**（照抄注入文本的标题行）。
      🅰 **人工素材与 AI 补充要分开**（任务 69）：注入文本分成两段 ——
        「=== 人工提供的素材(以它为准;事实性表述的依据只认这里)===」是**唯一的事实依据**；
        「=== AI 补充参考(仅供结构与表述参考,不得作为事实依据)===」里的东西**只是模型生成的参考**。
        引用 AI 补充参考时必须写成「（来源:AI补充——《素材标题》第 N 段）」（照抄标题里的
        「AI补充——」前缀，不许丢掉）—— 这样读者一眼看得出「这条依据本身是模型生成的」。
        **只有 AI 补充参考支撑的事实、数字、参数、案例、结论，一律按「素材里没有依据」处理**：
        写成「（无直接依据,建议人工确认）」，**不许把 AI 补充当事实依据**。
        🅱 **无来处的段落更要按「没依据」处理**：AI 补充素材里标着「(无依据·模型补充)」的段落
        （模型自己都说了没有来处），**它撑起来的句子一律写「（无直接依据,建议人工确认）」**，
        **不许**写成「（来源:AI补充——《素材标题》第 N 段）」（那等于把没依据的内容
        伪装成有依据）—— 这是本节最硬的一条。
        两部分都提到同一件事且说法不一致时，**以人工素材为准**。
      ⚠️「素材标题」是占位符，**必须替换成素材的真实标题**（照抄素材标题原文），
      严禁原样输出「素材标题」这四个字（那样等于没标来源，读者点不开）。
      段落号**只写一个数字**：不要写「第 42-45 段」「第 42、45 段」这类区间/多段（系统解析不了，
      等于没标），也不要写「第 60、75 段」这种顿号并列（同样解析不了），
      要引多段就**拆成多条各自标注**。
      🔴 **锚点只写一个**（一样不许区间 / 并列）：时间戳不要写成「06:47-08:12」或
      「06:47、07:20」，**更不许自己编** —— 只能照抄该段段首方括号里的原文；
      对不上（写了素材里没有的时刻）系统按「来源对不上」处理，等于没标。
      两个括号也别混写（「（来源:《X》第 80 段；无直接依据,建议人工确认）」两个都认不出来）。
      🔴 **硬纪律一：括号里只放「标题 + 一个锚点」**。来源括号内**只写**「《素材标题》第 N 段」
      **或**「《素材标题》06:47」（音视频素材），
      不夹带任何其他东西 —— 改进方法、备注、适用范围、第二本书、第二个锚点，都不许写进同一个括号
      （写进去系统就认不出这条来源，等于没标）。要补这些说明，就**另起一句、各自标注**：
      写「……风险等级上升（来源:《破解AI幻觉》第 12 段）。改进方法是建立场景分级速查表
      （来源:《破解AI幻觉》第 18 段）。」，**不要写**
      「（来源:《破解AI幻觉》第 12 段；改进方法：建立场景分级速查表）」。
      🔴 **硬纪律二：没有依据时不许写「来源:」前缀**。素材里找不到依据的**直接写**
      「（无直接依据,建议人工确认）」，**不许写成**「（来源: 无直接依据,建议人工确认）」、
      「（来源: 建议人工确认）」这类混写 —— 免责标注不是来源，「来源:」后面只允许出现
      真实的「《素材标题》第 N 段」或「《素材标题》06:47」（音视频素材的时间戳）。
   C. 硬禁止：① 给无法溯源的内容编来源；② 把通用常识或你自己的经验伪装成素材出处；
      ③ 留空不标。**宁可写「（无直接依据,建议人工确认）」，也不许编** ——
      编造来源比不标来源严重得多（来源可追溯是我们对用户的核心承诺）。
   D. 最容易漏的五处（交稿前逐条自查）：
      ① 嵌套子项（缩进第二层）要各自标注，不能只在父项末尾标一次就当作整段都有依据；
      ② 「### 小结」「### 踩过的坑」「### 能力边界」**不是标注豁免区**，整节每一条都要标
         （小结复述前面的结论，就沿用那条的来源）；
      ③ 短句 / 名词性碎片 / 枚举条目也算关键点，**一条一个标注**：写
         「- 技术本质（概率生成机制）（来源:《破解AI幻觉》第 33 段）」，不要写成光秃秃的
         「- 技术本质（概率生成机制）」；
      ④ 别把标签单独写成一条：写「- **决策误导风险**：指模型给出看似合理却错误的建议
         （来源:《破解AI幻觉》第 12 段）」这种「标签 + 一句话 + 标注」的整条，而不是先写
         「- **决策误导风险**」、下面再挂子项；小节标题用 `####`，不要用列表项冒充标题。
         （确实要用「短标签 + 子项」的分组写法时，标签必须是**一个短名词短语** ——
         不加逗号、句号、分号，如「- **行为阈值**：」；写成「- **第一, 温度**：」这种带标点的
         会被当成普通条目，必须自己标来源。子项仍然要各自标来源。
         标签里也不要出现动词性断言（会 / 能 / 导致 / 造成 / 使得 / 提高 / 降低 …）：
         写「- **行为阈值**：」可以，写「- **成本会翻三倍**：」「- **温差导致漂移**：」不行 ——
         那是结论，要按第 4 条就地标来源。
         标签后面挂的子项必须用 `- ` 列表项逐条写（缩进更深），**不要写成缩进散文/普通段落**；
         不打算挂子项就别单独立一条，写成「标签 + 一句话 + 来源」的整条。）
         🔴 空条目禁令：像 `- **步骤2：参数设置**` 这样**只有标签、后面没有任何内容**的
         条目一律不许出现（实测仍有漏网）—— 没内容就**删掉整条**，不要留一个空标签占位；
         它也不是小节标题（小节标题一律用 `####`）。
      ⑤ 「### 举例」与操作类小节（「操作指南」「实施步骤」「操作流程」「操作规范」
         「关键操作步骤」这类自建小节）**是最容易整节留空的地方** —— 模型常以为
         「上面表格已经标过」就不标了：它们同样**不是标注豁免区**，整节每条都要标
         （复述就沿用同一条来源，见 A 的 🔁）。
      📋 交稿前逐条数一遍：① 每条分点是否都有「（来源:…）」或「（无直接依据,建议人工确认）」？
      ② 「### 举例」「操作指南/实施步骤/操作流程/操作规范/关键操作步骤」里的条目是否也各自标了（复述就沿用同一条来源）？
      ③ 有没有只有标签、后面没有内容的空条目（有就删掉整条）？④ 表格下是否有统一一行来源？
      ⑤ 「### 核心讲解」是否有表格或 ```mermaid，且至少 1 条「动作 + 带量纲信号 + 产出物」？
      ⑥ 小节顺序「引入 → 核心讲解 → 举例 → 踩过的坑 → 小结」是否一个不少？
   E. 表格与图示（**别因为怕标来源就不敢用表格**）：
      · 表格：**只需在表下统一写一行**「（来源:《破解AI幻觉》第 12 段）」，**不要每行标**
        （一个括号只放一个段落号；要引多段就并排多写一个括号，如
        「（来源:《破解AI幻觉》第 12 段）（来源:《破解AI幻觉》第 18 段）」）；
      · ```mermaid 代码块：是结构/流程示意，**不需要**标来源，图里也不用逐框标；
      · 表格不是标注豁免区（整张表靠表下那一行追溯到素材），但也不是「逐行标注区」。
   F. 展示「标注格式模板 / 错误示范」这类**不是真实引用**的示例文本时，**整段用 ``` 代码块**
      包起来 —— 包括「（来源:《素材标题》第 N 段）」「（无直接依据,建议人工确认）」这种占位示例；
      不要写成列表项、也不要只加行内反引号 `…`（混进正文列表会被当成本讲的关键点）。
   G. 分点讲解照旧用列表 / 表格承载，不要为了少标来源就把分点改写成大段散文
      （关键点、步骤要能被逐条看见、逐条标注）。
  5) 术语一律使用下方「课程术语表」的标准名；**术语第一次出现时必须给读者一个括号解释**
     （大白话，可直接用术语表里的定义；也可用自己的话说），之后再出现就直接用标准名、
     不要反复解释；别名只在首次出现时用括号注明一次。术语表里没列到、但读者可能看不懂的
     专业词，同样要在首次出现处补一句白话解释。

6) 提取优先级（重要，顺序不能颠倒）：优先写「素材里才有、通用资料里查不到」的内容 ——
   ① 一手案例（谁、在什么场景、做了什么、结果如何）；
   ② 失败教训与返工（踩过的坑，以及后来怎么改的）；
   ③ 能力边界（什么情况不适用、做到哪一步就该找专家）；
   ④ 内在矛盾（同一问题上两种做法都有人坚持，各自的真实分歧）。
   通用观点、金句、「XX 很重要」这类话优先级最低；每写一条通用观点，必须紧跟一个
   素材里的具体出处或具体场景（写成来源标注），配不出来就删掉，不要留着凑字数。

7) 小节顺序（标题必须逐字出现）：「### 引入」→「### 核心讲解」（列表/表格/引用）
   →「### 举例」→「### 踩过的坑」→「### 小结」。
   🔴 结构硬要求（做不到就是不合格）：「### 核心讲解」里**必须至少有 1 个 Markdown 表格，
   或 1 个 ```mermaid 代码块**。凡是「几类对象的对比 / 一组参数 / 判定条件 / 先后步骤 /
   分层结构」这类内容，一律用表格或 ```mermaid 承载；**把它们写成纯列表视为不合格**
   （列表只用来做要点分条，不能拿列表顶替表格）。
   · 不合格（反面示例）：「- 温度低于 12℃ 时补光 2 小时」「- 温度高于 28℃ 时通风降温」
     把「条件 → 动作」逐条罗列成列表；表格与 ```mermaid 都不出现。
   · 合格写法：同样内容写成 3 列表格（场景 | 判断信号 | 动作），表下统一写一行来源。
   🔴 可操作硬要求（做不到同样是不合格）：「### 核心讲解」里**除术语解释外，必须至少保留
   1 条可照做的操作说明**，写法 = **动作动词 + 判断信号/参数（带量纲或单位，如
   「温度降到 0.2 以下」「超过 300 字」「低于 12℃」）+ 产出物（做完得到什么文件 /
   表格 / 结论）**；参数、阈值、判定条件优先写进表格，让读者能对号入座。
   · **术语解释条目不算步骤**：写「**温度参数**：控制模型采样随机性的超参数」这类
     「名词 + 定义」的条目只是在解释概念，**不能拿它顶替操作说明、也不算一条步骤**；
     通篇只有术语解释、没有可照做的操作，这一节就是不合格。
   · 合格写法：「- 当幻觉率超过 5% 时，把温度锁到 0.2 以下，并把三次输出整理成《对比表》」
     —— 动作（锁 / 整理）+ 量纲（5% / 0.2）+ 产出物（对比表）齐全。
   「### 踩过的坑」写素材里的具体失败 / 返工 / 误判 + 一句「后来怎么改的」；
   素材里出现过出错、失败、错误率、返工、事故这类事实的，必须写成坑（不许改用下面的兜底句）；
   素材里确实没有这类内容，才只写一行「素材未提及，建议补充采访」，严禁编造。
   本节的**每一条**都要按第 4 条标来源或免责标注（不是只标第一句）。
   新增小节是**加**不是**换**：学习目标要求的内容（原理、影响、方法）照常讲完，
   需要图示/对比的地方照旧用表格或 ```mermaid。

8) 讲义最后（小结之后）加一小节回答「这一节不覆盖什么 / 什么情况要找专家」：
   有来源按第 4 条标注来源，没有来源就标「（无直接依据,建议人工确认）」；
   本节的**每一条**都要带来源或免责标注（不是只标第一句）。

9) 遇到两种做法都有道理时，禁止写「各有优劣」「因情况而异」「视情况而定」这类和稀泥句，
   必须写清楚：出现什么信号时用 A、出现什么信号时用 B（给可判断的条件、阈值或场景）。

10) 步骤类内容必须能被另一个人照着独立执行：写清动作动词 + 判断信号或参数
   （如「当温度低于 12℃ 时」）+ 产出物（做完得到什么文件 / 表格 / 结论）。
   **术语解释条目不算步骤**（「**温度参数**：控制模型采样随机性的超参数」这种
   「名词 + 定义」只是解释概念，见第 7 条），别用它充数。

11) 完整性：正文（不含代码块）不少于 900 字；学习目标要求的每一层（是什么 / 为什么 /
   有什么影响 / 怎么做）都要讲到，不能因为多写「踩过的坑」就砍掉别的部分；
   素材里出现可对比的数字或流程时，照旧用表格或 ```mermaid 呈现。

12) 数学 / 物理 / 化学的公式一律写 **LaTeX**：行内公式用 $…$（如 $x^2+y^2=r^2$），
    独立成行的公式用 $$…$$（如 $$\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$$）；化学式与单位上下标
    同理（$H_2O$、$m/s^2$）。
    · 不合格（反面示例）：用 `^2` 或 `x2` 表示平方、把根号写成「根号下 b 方减 4ac」这种
      中文描述、用 `mermaid` 或表格里的纯文本代替公式、用图片/截图代替公式。
    · 合格写法：推导写成 $$…$$ 独立公式；「参数 → 含义 → 取值范围」写进表格，表里的取值
      照样用 $…$ 写（如 $0 \\le \\theta < 2\\pi$）。
    · 正文里的金额、百分比等普通 $ 不要与公式混写：金额写「30 元」，「30 元」不要写成 $30。"""

CARDS_SYSTEM = """CARDS_JSON
你是资深教学设计师。通读一篇讲义，把「用一张小图表达比读文字更快」的内容挑出来，
规划 2~4 张就近嵌入的「卡片」，并判断每张最合适的图类型，只输出 JSON：
{"cards": [{"anchor": "该概念在讲义中首次出现的原文片段（标题或正文原句，≤20字）",
   "caption": "卡片小标题（≤12字）", "chart_type": "...", ...类型专属字段}]}

chart_type 取值与内容格式：
1) "concept"（纯概念/机制，默认）：
   {"layout": "chain|comic|radial|ring|timeline|compare|decision",
    "nodes": [{"emoji": "单个字符或省略", "title": "要点名（≤12字）",
               "subs": ["补充说明 1~2 条，每条 ≤14字"]}],
    "edges": [{"from": 0, "to": 1, "label": "连线说明（≤6字）"}]}
2) "bar"（不同对象/时期的数值对比，数据必须是讲义原文给出的）：
   {"data": {"labels": ["类别..."], "datasets": [{"label": "系列名", "data": [数字...]}]}}
3) "line"（随时间/顺序变化的趋势）：同上 labels/datasets。
4) "pie"（占比/构成关系）：datasets 单系列数值（百分比或可求和为整体）。
5) "table"（参数/要素罗列）：{"data": {"headers": ["列1", "列2"], "rows": [["值", "值"]]}}
6) "mindmap"（分类/层级/组成部分）：{"data": {"root": "中心主题",
   "branches": [{"emoji": "单个字符或省略", "title": "一级分支（≤10字）",
                 "subs": ["二级要点 1~2 条，每条 ≤14字"]}]}}，分支 3~6 个。
7) "timeline"（时间先后/阶段进程）：{"data": {"steps": [{"emoji": "单个字符或省略",
   "title": "阶段名（≤10字）", "subs": ["该阶段要点 1~2 条，每条 ≤14字"]}]}}，3~6 步。
8) "quadrant"（两轴权衡/取舍：重要性 × 紧急度、成本 × 效果这类比较与决策）：
   {"data": {"x_low": "横轴左端标签（≤4字）", "x_high": "横轴右端标签（≤4字）",
             "y_low": "纵轴下端标签（≤4字）", "y_high": "纵轴上端标签（≤4字）",
             "quadrants": [{"name": "象限名（≤8字）", "desc": "一句话说明（≤12字）"}]}}，
   4 个象限按「左上 → 右上 → 左下 → 右下」的顺序给。

判断规则示例：
- 正文出现可比较的具体数值（时长 6-12h vs 1-2h、温度区间、产量、比例）→ bar/line；
- 百分比/占比/成分构成 → pie；参数表与对比指标清单 → table；
- 分类、组成部分、层级归属 → mindmap；时间顺序、操作阶段、发展脉络 → timeline；
- 两个维度上的权衡取舍（重要性 × 紧急度、成本 × 效果、收益 × 风险）→ quadrant；
- 其余抽象概念、机制、流程（难用数据表达）→ concept（在 7 种布局里选）。

通用规则：
1) 只挑“看图更快”的内容；总卡片 2~4 张，其中数据类图（bar/pie/line/table/
   mindmap/timeline/quadrant）最多 2 张，避免数据图过多冲淡概念；
2) anchor 必须是讲义原文能找到的片段（尽量短），不得自己造词；
3) 数据图数字必须忠于讲义/素材原文，禁止编造；数值点控制在 3~12 个；
4) concept 的 nodes/edges 遵循 DIAGRAM_JSON 约定：compare 两个节点各带 side 与 rows，
   decision 节点用 kind=start/check/good/bad；一律中文；
5) 不要把讲义已有的表格/清单整段照抄成图；没有合适的内容时返回 {"cards": []}。"""

_GEN_IMG_REF = re.compile(r"!\[[^\]]*\]\(\s*gen://[^)\]\]]*\)")
_RE_CARD_REF = re.compile(r"!\[[^\]]*\]\(\s*diagram://([A-Za-z0-9_-]+)\s*\)")
_RE_CHART_REF = re.compile(r"!\[[^\]]*\]\(\s*chart://([A-Za-z0-9_-]+)\s*\)")
_RE_ANY_CARD_REF = re.compile(r"!\[[^\]]*\]\(\s*(?:diagram|chart)://([A-Za-z0-9_-]+)\s*\)")
_RE_CONCEPT_DIAGRAM = re.compile(r"!\[一图看懂\]\(\s*diagram://([A-Za-z0-9_-]+)\s*\)")
# 讲义小结标题层级不固定（## 小结 / ### 小结 / ### 本章小结 等），统一按「任意级含小结」匹配
_RE_SUMMARY_HEAD = re.compile(r"^[ \t]{0,3}#{2,4}[ \t]+[^#\n]*小结[ \t]*$", re.M)
_RE_PRACTICE_HEAD = re.compile(r"^[ \t]{0,3}#{2,4}[ \t]+随堂练习[ \t]*$", re.M)
# 任意标题（带层级），用来判断「随堂练习」小节在哪里结束（任务 56 存量重渲染）
_RE_ANY_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+")


def _failure_reason(exc: Exception) -> tuple[str, str]:
    """把异常归类成 (stage, 可读原因)：llm-json / layout / network / other。"""
    text = str(exc) or exc.__class__.__name__
    if "排版自检" in text:
        return "layout", f"排版自检未通过：{text}"
    if isinstance(exc, json.JSONDecodeError) or "JSON" in text.upper():
        return "llm-json", f"LLM 返回的 JSON 无法解析：{text}"
    if isinstance(exc, httpx.HTTPError):
        return "network", f"网络/接口失败：{text}"
    return "other", text


def _failures_of(item: ContentItem) -> list[dict]:
    rows = (item.meta or {}).get(FAIL_KEY) or []
    return [r for r in rows if isinstance(r, dict)]


def _record_failure(item: ContentItem, slot: str, stage: str, reason: str,
                    *, retry: dict | None = None) -> dict:
    """记录一张图/一张卡的失败（不提交，由调用方统一 commit）。"""
    record: dict = {
        "id": uuid4().hex[:12],
        "slot": slot,
        "stage": stage,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if retry:
        record["retry"] = retry
    item.meta = {**dict(item.meta or {}), FAIL_KEY: [*_failures_of(item), record]}
    return record


def _content_issues(markdown: str) -> list[dict]:
    """正文里若残留 XML 不允许的控制字符（除 ``\\t``/``\\n``/``\\r``），记一条可观测项。

    任务 48w 第三层：``extract_json`` 已经修掉「单反斜杠被吃」，这里兜的是
    其它来源（历史数据、手改、提示词里带进来的字符）。**只记不判**，不进质检判据。
    """
    chars = find_control_chars(markdown)
    if not chars:
        return []
    summary = "、".join(f"U+{ord(c):04X}×{chars.count(c)}" for c in sorted(set(chars)))
    return [{
        "kind": "control-char",
        "count": len(chars),
        "chars": summary,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }]


def _clear_failure(db: Session, item: ContentItem, failure_id: str) -> None:
    left = [r for r in _failures_of(item) if r.get("id") != failure_id]
    item.meta = {**dict(item.meta or {}), FAIL_KEY: left}
    db.commit()
    db.refresh(item)


def _lecture_user_prompt(
    node: OutlineNode,
    tone: str,
    material: str = "",
    anchor: dict | None = None,
    terminology: str = "",
    qc: dict | None = None,
    course: Course | None = None,
) -> str:
    """讲义提示词：知识点 + 写作边界 + 语气 + 素材 +（可选）样板锚/术语表/质检问题。

    任务 51：传 ``course`` 且该课带 K-12 元数据时，插入「课时口径」段（学段语气 + 篇幅上限）；
    任务 85：该课选过「使用场景」时插入「场景要求」段（口气与要求，未选场景 = 空串）；
    ``course=None``（含既有测试调用）与职场课一样，一字不加。
    """
    k12_section = k12.lecture_prompt_section(course) if course is not None else ""
    scenario_section = scenario.scenario_prompt_block(course) if course is not None else ""
    return (
        f"当前知识点：{node.title}（编号 {node.node_key}）\n"
        f"学习目标：{node.objective}\n"
        f"写作边界：只写本知识点内容；素材中其他章节只作背景参考。\n"
        "内容优先级：一手案例 / 失败教训 / 能力边界 / 内在矛盾优先；"
        "通用观点必须配素材里的具体出处或场景，否则不写；"
        "讲义要有「### 踩过的坑」小节（素材里有出错 / 失败 / 错误率 / 返工类事实时必须写成坑，"
        "确实没有才写「素材未提及，建议补充采访」）；"
        "来源标注：正文、列表项（含嵌套子项）、执行步骤、踩过的坑、能力边界"
        "都要就地标「（来源:《素材标题》第 N 段）」或「（无直接依据,建议人工确认）」"
        "（表格只在表下统一标一行、不要逐行标；```mermaid 不需要标来源），"
        "括号里只写「《素材标题》第 N 段」或「《素材标题》06:47」"
        "（文本素材 N = 该段前的 [序号]；音视频素材照抄该段段首的 [mm:ss] 原文），"
        "不夹带改进方法/备注等说明（要写就另起一句各自标注）；"
        "素材里没有依据时直接写「（无直接依据,建议人工确认）」，不要写「来源:」前缀；"
        "宁可写「无直接依据」也不许编造来源、不许留空；"
        "结尾要回答「这一节不覆盖什么 / 什么情况要找专家」；"
        "禁止「各有优劣 / 因情况而异」式和稀泥句，要写清什么信号用 A、什么信号用 B。\n"
        f"语气：{tone}{k12_section}{scenario_section}{material}"
        f"{style_anchor_prompt(anchor)}{terminology}"
        f"{qc_prompt_section(qc)}"
    )


def _locked_of(item: ContentItem | None) -> bool:
    """讲义是否已加锁（人工编辑过或审核通过）→ 批量生成跳过，避免覆盖定稿。"""
    return bool(item is not None and (item.meta or {}).get("locked"))


def locked_overwrite_blocked(item: ContentItem | None) -> str:
    """单篇重跑前的「审核锁」拦截话术（任务 91）。

    返回非空字符串 = 该篇已锁定，单篇覆盖必须先明确解锁（或显式传
    ``allow_overwrite_locked=True``），路由据此返回 409；返回空串 = 没锁，照常重跑。
    语义与批量路径的 ``skip_locked`` 一致（它跳过锁定节点，这里要求先解锁）。
    """
    if not _locked_of(item):
        return ""
    reason = str((item.meta or {}).get("locked_reason") or "").strip()
    why = f"（锁定原因：{reason}）" if reason else ""
    return (
        "这一节已锁定，重跑会覆盖老师审过的那一版" + why + "："
        "请先点「解锁并重做」，或在「重新生成」确认框里确认覆盖。"
    )


# 任务 91：单篇覆盖成功后的提示（路由写进 job.result 供界面展示）
LOCKED_OVERWRITE_NOTE = "该节原本已审核锁定：正文已重新生成，「审核通过」徽标已摘除，需重新审核。"


def _node_fields(node: OutlineNode) -> dict:
    """大纲节点参与指纹的字段（改了目标/策略/考核 → 输入变了 → 不跳过）。"""
    return {
        "node_key": str(getattr(node, "node_key", "") or ""),
        "title": str(getattr(node, "title", "") or ""),
        "objective": str(getattr(node, "objective", "") or ""),
        "teaching_strategy": str(getattr(node, "teaching_strategy", "") or ""),
        "assessment": str(getattr(node, "assessment", "") or ""),
    }


def input_fingerprint(
    *,
    node: OutlineNode,
    material: str = "",
    anchor: dict | None = None,
    qc: dict | None = None,
    k12: str = "",
    scenario: str = "",
) -> str:
    """任务 23-B 输入指纹：sha1(素材段落 + 大纲节点字段 + 样板锚 + 模板版本 + 质检要求版本)。

    指纹一致 = 这次生成会喂给模型的输入与上次完全相同 → 批量生成可跳过。
    任务 85：使用场景段进指纹（改了场景 ⇒ 输入变了 ⇒ 不跳过；未选场景 = 空串，指纹不变）。
    """
    payload = {
        "template": PROMPT_TEMPLATE_VERSION,
        "qc_rules": QC_RULES_VERSION,
        "node": _node_fields(node),
        "material": str(material or ""),
        "anchor": anchor or {},
        "qc_issues": qc_prompt_section(qc) if qc else "",
        "k12": str(k12 or ""),
        "scenario": str(scenario or ""),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _effective_anchor(course: Course, anchor: dict | None,
                      ignore_style_anchor: bool) -> dict | None:
    return None if ignore_style_anchor else (anchor or get_style_anchor(course))


def set_lecture_locked(db: Session, item: ContentItem, locked: bool,
                       reason: str = "") -> ContentItem:
    """给讲义加/解锁：加锁后按章批量会跳过它（结果里列 locked_skipped）。"""
    meta = {**(item.meta or {}), "locked": bool(locked)}
    if locked and reason:
        meta["locked_reason"] = reason
    if not locked:
        meta.pop("locked_reason", None)
    item.meta = meta
    db.commit()
    db.refresh(item)
    return item


def generate_lecture(
    db: Session,
    course: Course,
    node: OutlineNode,
    tone: str = "正式",
    item_id: str | None = None,
    material: str = "",
    anchor: dict | None = None,
    ignore_style_anchor: bool = False,
    use_qc: bool = False,
) -> ContentItem:
    """为单个知识点生成完整讲义（图文 + 习题一体化），并落库返回条目。

    anchor 为课程样板风格锚（None 时从 course.meta 读）；ignore_style_anchor=True
    表示这次生成刻意不套样板（「忽略样板，一次性生成整门课」）。

    use_qc=True 表示「按质检建议重生成」：把上一版 ``meta.qc`` 里的 warn/fail
    逐条注入提示词，要求本次逐条修正。
    """
    # 任务 23-A：本次生成的全部 LLM 调用（讲义 + 概念图 + 卡片）都记进同一采集器
    with usage_log.collect() as collector:
        item = _generate_lecture_inner(db, course, node, tone, item_id, material,
                                       anchor, ignore_style_anchor, use_qc)
        if usage_log.attach(item, collector) is not None:
            db.commit()
            db.refresh(item)
    return item


def _generate_lecture_inner(
    db: Session,
    course: Course,
    node: OutlineNode,
    tone: str = "正式",
    item_id: str | None = None,
    material: str = "",
    anchor: dict | None = None,
    ignore_style_anchor: bool = False,
    use_qc: bool = False,
) -> ContentItem:
    """generate_lecture 的实现体（外层负责用量采集与落库）。"""
    client = build_llm_client()
    style = _effective_anchor(course, anchor, ignore_style_anchor)
    concepts = lecture_concepts(db, course)
    previous = _target_lecture(db, course, node, item_id)
    qc_in = stored_lecture_qc(previous) if (use_qc and previous is not None) else None
    k12_section = k12.lecture_prompt_section(course)
    scenario_section = scenario.scenario_prompt_block(course)
    fingerprint = input_fingerprint(node=node, material=material, anchor=style, qc=qc_in,
                                    k12=k12_section, scenario=scenario_section)
    user = _lecture_user_prompt(node, tone, material, style,
                                terminology_prompt_section(concepts), qc_in, course=course)
    data = usage_log.complete_json(client, LECTURE_SYSTEM, user,
                                   max_tokens=4000, purpose="lecture")
    markdown = str(data.get("markdown") or "").strip() or f"## {node.title}"

    item = previous
    if item is None:
        item = ContentItem(
            course_id=course.id,
            node_id=node.id,
            kind="lecture",
            title=node.title,
            body={},
            meta={"node_key": node.node_key},
        )
        db.add(item)
        db.flush()
    else:
        _drop_owned(db, item)
    # 新一轮生成：清掉上一轮遗留的配图失败记录，重新收集。
    # 任务 86：同时记下**当次生成生效的口径**（场景 + 提示词版本）与生成时间 ——
    # 界面徽标 / 笔记 frontmatter / 交付说明汇总都从这里取，老内容没有这些键（显示「通用(未标记)」）。
    item.meta = {**dict(item.meta or {}), FAIL_KEY: [],
                 "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 **scenario.run_meta(course, PROMPT_TEMPLATE_VERSION)}

    # 默认零 AI 位图：剥离 LLM 残留的 gen:// 占位，不生成 PNG
    markdown = _strip_gen_refs(markdown)
    image_ids: list[str] = []
    diagram_ids: list[str] = []
    chart_ids: list[str] = []
    try:
        # 每篇讲义自动配一张 Excalidraw 矢量概念图（替代旧版 AI 位图自动兜底）
        # 任务 125：拆图后一篇讲义的概念图不止一张 ⇒ 逐张挂归属（meta_extra）并逐张引用进
        # 讲义；只认第 1 张的话，拆出的后几张在交付包里就是没人引用的孤儿文件。
        diagrams = generate_diagrams(
            db, course, node, instruction=str(data.get("summary") or node.objective),
            meta_extra={"from_lecture": item.id, "role": "concept"},
        )
        db.commit()
        for one in diagrams:
            diagram_ids.append(one.id)
            markdown = _inject_diagram_markdown(markdown, one.id)
    except Exception as exc:  # noqa: BLE001 —— 概念图失败不阻断讲义主体
        logger.warning("diagram 自动配图失败，讲义照常产出：node=%s err=%s", node.node_key, exc)
        stage, reason = _failure_reason(exc)
        _record_failure(item, "concept", stage, reason, retry={
            "kind": "concept",
            "instruction": str(data.get("summary") or node.objective),
        })
    try:
        # 概念卡按内容自动分流：纯概念走 diagram，数值/分类/阶段等走 chart_* 数据图
        markdown, card_diagram_ids, card_chart_ids = attach_concept_cards(
            db, course, node, item, markdown)
        diagram_ids += card_diagram_ids
        chart_ids += card_chart_ids
    except Exception as exc:  # noqa: BLE001 —— 概念卡/数据卡失败不阻断讲义主体
        logger.warning("concept cards 生成失败，讲义照常产出：node=%s err=%s", node.node_key, exc)
        stage, reason = _failure_reason(exc)
        _record_failure(item, "cards-plan", stage, reason, retry={"kind": "cards-plan"})
    markdown = _relocate_concept_diagram_refs(markdown)
    # 任务 48b：结构块兜底（Markdown 表格 / Mermaid）——0-LLM、幂等、失败不阻断主体
    try:
        markdown, structured = ensure_structured_block(
            markdown, node=node, charts=_node_chart_options(db, node))
    except Exception as exc:  # noqa: BLE001 —— 兜底失败照常产出讲义，结论记 meta
        logger.warning("结构块兜底失败，讲义照常产出：node=%s err=%s", node.node_key, exc)
        structured = {"applied": False, "source": "none", "reason": f"兜底失败：{exc}"}
    quiz_rows, quiz_issues = _persist_quizzes(db, course, node, data.get("quizzes") or [],
                                              client=client)
    md = markdown + _quiz_section(
        quiz_rows,
        limit=workload_limit(str(k12.k12_meta(course).get("stage") or ""),
                             str(getattr(course, "subject", "") or "")))

    body = {
        "markdown": md,
        "summary": str(data.get("summary") or ""),
        "keywords": list(data.get("keywords") or []),
        "tone": tone,
    }
    if item.body:
        history = list(item.meta.get("history") or [])
        history.insert(0, {"body": item.body})
        item.meta = {**dict(item.meta or {}), "history": history[:5]}
    item.body = body
    item.title = node.title
    style_meta: dict = {}
    if style:
        style_meta["style_anchor_ref"] = {
            "source_node_key": style.get("source_node_key"),
            "source_item_id": style.get("source_item_id"),
        }
    item.meta = {
        **dict(item.meta or {}),
        "node_key": node.node_key,
        "quiz_ids": [q.id for q in quiz_rows],
        "quiz_issues": quiz_issues,
        "image_ids": image_ids,
        "diagram_ids": diagram_ids,
        "chart_ids": chart_ids,
        # 任务 48b：本次结构块兜底的结论（applied / source / chart_item_id / reason）
        STRUCTURED_KEY: structured,
        # 任务 48w：正文里残留的 XML 控制字符（空列表 = 干净）
        CONTENT_ISSUES_KEY: _content_issues(md),
        # 任务 23-B：记录本次输入指纹，批量生成据此判断「输入未变可跳过」
        "input_fingerprint": fingerprint,
        "input_fingerprint_at": datetime.now(timezone.utc).isoformat(),
        **style_meta,
    }
    # 任务 22：质检 + 来源锚定——结果挂 meta.qc / meta.sources（失败不阻断讲义产出）
    try:
        attach_lecture_qc(build_lecture_qc(db, course, node, item, md), item)
    except Exception:  # noqa: BLE001
        logger.warning("讲义质检失败，讲义照常产出：node=%s", node.node_key)
    # 任务 91：正文已被这一版覆盖 ⇒ 旧的「审核通过」徽标不再成立，必须摘掉
    # （否则界面显示「审核通过」而正文是没审过的那一版）。批量路径（skip_locked=True）根本不会走到这里。
    if _locked_of(item):
        meta = dict(item.meta or {})
        meta["locked"] = False
        meta.pop("locked_reason", None)
        item.meta = meta
    db.commit()
    db.refresh(item)
    flush_scheduled_removals(db)
    try:
        attach_vault_write(db, item, "lecture")
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for lecture %s", item.id)
    return item


def edit_lecture(db: Session, item: ContentItem, markdown: str) -> ContentItem:
    """教师就地编辑讲义 Markdown 全文（人工编辑 = 自动加锁，批量不再覆盖）。"""
    item.body = {**(item.body or {}), "markdown": markdown}
    item.meta = {**(item.meta or {}), "locked": True, "locked_reason": "人工编辑"}
    db.commit()
    db.refresh(item)
    try:  # 任务 22：手改后按新正文复检，避免质检徽标停留在旧结果
        course = db.get(Course, item.course_id)
        if course is not None:
            refresh_lecture_qc(db, course, item)
    except Exception:  # noqa: BLE001 —— 质检失败不影响保存
        logger.warning("讲义手改后质检失败：%s", item.id)
    try:
        attach_vault_write(db, item, "lecture")
    except Exception:  # noqa: BLE001
        logger.warning("vault incremental write skipped for lecture %s", item.id)
    return item


def delete_lecture(db: Session, item: ContentItem) -> None:
    """删除讲义及其上一版创建的配图/习题（保留节点本身与手工内容）。"""
    _drop_owned(db, item)
    schedule_removal(db, item_file_refs(item))
    db.delete(item)
    db.commit()
    flush_scheduled_removals(db)


def _generate_nodes(
    db: Session,
    course: Course,
    nodes: list[OutlineNode],
    *,
    tone: str,
    material: str,
    anchor: dict | None = None,
    ignore_style_anchor: bool = False,
    skip_locked: bool = True,
    force: bool = False,
    progress=None,
) -> dict:
    """生成一批知识点的完整讲义：单点失败不中断，locked/输入未变 的节点跳过并列出。

    任务 23：
    - 生成前比对输入指纹，与上次一致则跳过（skipped），除非 force=True；
    - llm_concurrency > 1 时按并发跑（每个线程独立 DB 会话），仍然单点失败隔离；
    - 汇总本次任务的用量（result.usage，含 per_node）。

    progress(done, total, node, status) 每进入/结束一个节点回调一次，
    供路由把「第 i/N 章、当前知识点、成败计数」写进任务面板。
    """
    total = len(nodes)
    concurrency = _concurrency()
    results: list[tuple[int, dict]] = []
    if concurrency > 1 and total > 1:
        results = _generate_nodes_concurrent(
            db, course, nodes, tone=tone, material=material, anchor=anchor,
            ignore_style_anchor=ignore_style_anchor, skip_locked=skip_locked,
            force=force, concurrency=concurrency, progress=progress)
    else:
        for index, node in enumerate(nodes, 1):
            if progress is not None:
                progress(index, total, node, "running")
            result = _generate_one(db, course, node, tone=tone, material=material,
                                   anchor=anchor,
                                   ignore_style_anchor=ignore_style_anchor,
                                   skip_locked=skip_locked, force=force)
            results.append((index, result))
            if progress is not None:
                progress(index, total, node, result["status"])
            if result["status"] == "ok":
                time.sleep(0.3)  # 降低多节点连发触发 LLM 限流的概率
    db.commit()
    return _summarize_nodes(results, total)


def _concurrency() -> int:
    """本次批量生成的并发度（配置 llm_concurrency，1~4，默认 1 = 顺序）。"""
    try:
        return max(1, min(4, int(get_settings().llm_concurrency)))
    except Exception:  # noqa: BLE001 —— 配置异常时退回顺序执行
        return 1


def _generate_one(
    db: Session,
    course: Course,
    node: OutlineNode,
    *,
    tone: str,
    material: str,
    anchor: dict | None = None,
    ignore_style_anchor: bool = False,
    skip_locked: bool = True,
    force: bool = False,
) -> dict:
    """生成单个知识点（或按规则跳过），返回结构化结果，绝不抛异常。"""
    key = node.node_key
    current = _target_lecture(db, course, node, None)
    if skip_locked and _locked_of(current):
        return {
            "status": "locked",
            "node_key": key,
            "title": node.title,
            "item_id": current.id if current else None,
            "reason": "已审核锁定（人工编辑过），批量生成不覆盖",
        }
    style = _effective_anchor(course, anchor, ignore_style_anchor)
    fingerprint = input_fingerprint(node=node, material=material, anchor=style,
                                    k12=k12.lecture_prompt_section(course),
                                    scenario=scenario.scenario_prompt_block(course))
    if not force and current is not None:
        stored = str((current.meta or {}).get("input_fingerprint") or "")
        if stored and stored == fingerprint:
            return {
                "status": "skipped",
                "node_key": key,
                "title": node.title,
                "item_id": current.id,
                "reason": "输入未变",
                "fingerprint": fingerprint,
            }
    try:
        item = generate_lecture(db, course, node, tone=tone, material=material,
                                anchor=anchor, ignore_style_anchor=ignore_style_anchor)
    except Exception as exc:  # noqa: BLE001 —— 单节点失败不中断整门课
        db.rollback()
        return {"status": "failed", "node_key": key, "title": node.title,
                "error": str(exc)}
    meta = item.meta or {}
    failures = [{
        "node_key": key,
        "failure_id": record.get("id"),
        "slot": record.get("slot"),
        "stage": record.get("stage"),
        "reason": record.get("reason"),
    } for record in _failures_of(item)]
    notes = [{"node_key": key, "file": entry["file"], "reason": entry["reason"]}
             for entry in preserved_entries((meta.get("vault") or {}).get("preserved"))]
    return {
        "status": "ok",
        "node_key": key,
        "title": node.title,
        "item_id": item.id,
        "images": len(meta.get("image_ids") or []),
        "diagrams": len(meta.get("diagram_ids") or []),
        "charts": len(meta.get("chart_ids") or []),
        "diagram_failed": failures,
        "notes_skipped": notes,
        "usage": meta.get("usage") or {},
    }


def _generate_nodes_concurrent(
    db: Session,
    course: Course,
    nodes: list[OutlineNode],
    *,
    tone: str,
    material: str,
    anchor: dict | None,
    ignore_style_anchor: bool,
    skip_locked: bool,
    force: bool,
    concurrency: int,
    progress=None,
) -> list[tuple[int, dict]]:
    """并发生成（每线程独立 DB 会话）；进度回调回到主线程串行执行。"""
    total = len(nodes)
    course_id = course.id
    refs = [(index, node.id, node.node_key, node.title)
            for index, node in enumerate(nodes, 1)]
    results: dict[int, dict] = {}
    guard = threading.Lock()

    def worker(index: int, node_id: str, node_key: str, title: str) -> tuple[int, dict]:
        worker_db = SessionLocal()
        try:
            worker_course = worker_db.get(Course, course_id)
            worker_node = worker_db.get(OutlineNode, node_id)
            if worker_course is None or worker_node is None:
                return index, {"status": "failed", "node_key": node_key,
                               "title": title, "error": "课程或知识点已不存在"}
            return index, _generate_one(
                worker_db, worker_course, worker_node, tone=tone, material=material,
                anchor=anchor, ignore_style_anchor=ignore_style_anchor,
                skip_locked=skip_locked, force=force)
        except Exception as exc:  # noqa: BLE001 —— 单点失败不影响其它节点
            return index, {"status": "failed", "node_key": node_key,
                           "title": title, "error": str(exc)}
        finally:
            worker_db.close()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker, *ref) for ref in refs]
        for future in as_completed(futures):
            index, result = future.result()
            with guard:
                results[index] = result
                if progress is not None:
                    node = db.get(OutlineNode, refs[index - 1][1]) or nodes[index - 1]
                    progress(index, total, node, result["status"])
    return [(index, results[index]) for index in sorted(results)]


def _summarize_nodes(results: list[tuple[int, dict]], total: int) -> dict:
    """把逐节点结果折成批量返回值（含 skipped 清单与任务级用量）。"""
    ok_count = 0
    image_total = diagram_total = chart_total = 0
    failed: list[dict] = []
    diagram_failed: list[dict] = []
    notes_skipped: list[dict] = []
    locked_skipped: list[dict] = []
    skipped: list[dict] = []
    per_node: list[dict] = []
    usage_summaries: list[dict] = []
    for _index, result in results:
        status = result.get("status")
        if status == "ok":
            ok_count += 1
            image_total += int(result.get("images") or 0)
            diagram_total += int(result.get("diagrams") or 0)
            chart_total += int(result.get("charts") or 0)
            diagram_failed += list(result.get("diagram_failed") or [])
            notes_skipped += list(result.get("notes_skipped") or [])
            usage = result.get("usage") or {}
            if usage:
                usage_summaries.append(usage)
                per_node.append({
                    "node_key": result.get("node_key"),
                    "item_id": result.get("item_id"),
                    "llm_calls": int(usage.get("llm_calls") or 0),
                    "tokens": int(usage.get("total_tokens") or 0),
                    "cost_cny": float(usage.get("cost_cny") or 0.0),
                    "ms": int(usage.get("duration_ms") or 0),
                })
        elif status == "skipped":
            skipped.append({"node_key": result.get("node_key"),
                            "title": result.get("title"),
                            "item_id": result.get("item_id"),
                            "reason": result.get("reason") or "输入未变"})
        elif status == "locked":
            locked_skipped.append({"node_key": result.get("node_key"),
                                   "title": result.get("title"),
                                   "item_id": result.get("item_id"),
                                   "reason": result.get("reason")})
        else:
            failed.append({"node_key": result.get("node_key"),
                           "error": result.get("error") or "生成失败"})
    return {
        "ok": ok_count,
        "failed": failed,
        "images": image_total,
        "diagrams": diagram_total,
        "charts": chart_total,
        "diagram_failed": diagram_failed,
        "notes_skipped": notes_skipped,
        "locked_skipped": locked_skipped,
        "skipped": skipped,
        "total": total,
        "usage": usage_log.batch_usage(per_node, usage_summaries) if usage_summaries
        else usage_log.batch_usage([], []),
    }


def _material_targets(db: Session, course: Course, nodes: list[OutlineNode]) -> list[str]:
    """注入选材的目标（任务 69 §B1.1）：这批要写的节点标题 + 全课章节标题。

    带上全部章节标题是为了「每一章都有相关素材进入」—— 分批生成（按章重跑）时也只传
    本批节点，选材仍会覆盖到全课的相关素材。
    """
    targets: list[str] = []
    for node in list(nodes) + [n for n in list_nodes(db, course.id)
                              if "." not in str(n.node_key or "")]:
        title = str(getattr(node, "title", "") or "").strip()
        if title and title not in targets:
            targets.append(title)
    return targets


def generate_all_lectures(
    db: Session,
    course: Course,
    tone: str = "正式",
    node_ids: list[str] | None = None,
    *,
    skip_locked: bool = True,
    anchor: dict | None = None,
    ignore_style_anchor: bool = False,
    force: bool = False,
    progress=None,
) -> dict:
    """为整门课批量生成完整讲义（顺序执行，单节点失败不中断整门课）。

    目标节点 = 已通过大纲的叶子知识点（无子节点的节点）；若大纲只有章节没有
    知识点则回退到全部章节节点。返回 {"ok": 成功数, "failed": [...],
    "images": AI 位图总数, "diagrams": 概念图总数, "charts": 数据图总数,
    "diagram_failed": [{node_key, slot, reason, failure_id}] 配图失败清单,
    "notes_skipped": [{node_key, file, reason}] 笔记未更新清单,
    "locked_skipped": [{node_key, reason}] 已加锁被跳过的清单,
    "skipped": [{node_key, reason:"输入未变"}] 输入未变被跳过的清单,
    "usage": 任务级用量（含 per_node）}。
    """
    nodes = _lecture_target_nodes(db, course, node_ids)
    return _generate_nodes(
        db, course, nodes, tone=tone,
        material=inject_material(db, course, scope="lecture",
                                 targets=_material_targets(db, course, nodes), numbered=True),
        anchor=anchor, ignore_style_anchor=ignore_style_anchor,
        skip_locked=skip_locked, force=force, progress=progress,
    )


def chapter_leaf_nodes(db: Session, course: Course, chapter: OutlineNode) -> list[OutlineNode]:
    """章节下的知识点（叶子）；章节本身没有小节时就用它自己。"""
    nodes = list_nodes(db, course.id)
    keys = {n.node_key for n in nodes}
    prefix = f"{chapter.node_key}."
    members = [n for n in nodes
               if n.node_key == chapter.node_key or n.node_key.startswith(prefix)]
    leaves = [n for n in members if not any(k.startswith(f"{n.node_key}.") for k in keys)]
    return leaves or members or [chapter]


def generate_chapter_lectures(
    db: Session,
    course: Course,
    chapter: OutlineNode,
    tone: str = "正式",
    *,
    skip_locked: bool = True,
    anchor: dict | None = None,
    ignore_style_anchor: bool = False,
    force: bool = False,
    progress=None,
) -> dict:
    """按章批量：只生成该章节下的知识点（一章一批，失败可单章重跑）。"""
    nodes = chapter_leaf_nodes(db, course, chapter)
    result = _generate_nodes(
        db, course, nodes, tone=tone,
        material=inject_material(db, course, scope="lecture",
                                 targets=_material_targets(db, course, nodes), numbered=True),
        anchor=anchor, ignore_style_anchor=ignore_style_anchor,
        skip_locked=skip_locked, force=force, progress=progress,
    )
    result["scope"] = "chapter"
    result["chapter_key"] = chapter.node_key
    result["chapter_title"] = chapter.title
    return result


def _lecture_target_nodes(
    db: Session,
    course: Course,
    node_ids: list[str] | None,
) -> list[OutlineNode]:
    """批量讲义的目标节点：显式 node_ids > 叶子知识点 > 全部节点。"""
    if node_ids:
        return (
            db.query(OutlineNode)
            .filter(OutlineNode.id.in_(node_ids))
            .order_by(OutlineNode.order_index)
            .all()
        )
    nodes = list_nodes(db, course.id)
    keys = {n.node_key for n in nodes}
    leaves = [n for n in nodes if not any(k.startswith(f"{n.node_key}.") for k in keys)]
    return leaves or nodes


def attach_concept_cards(
    db: Session,
    course: Course,
    node: OutlineNode,
    lecture_item: ContentItem,
    markdown: str,
) -> tuple[str, list[str], list[str]]:
    """为讲义规划 2~4 张就近卡片，按内容自动分流：纯概念 → diagram，数据类 → chart。

    返回 (markdown, 概念卡 diagram id 列表, 数据图 chart id 列表)。失败单卡跳过。
    """
    client = build_llm_client()
    # 只在讲义正文区规划/插入卡片，习题区不作为 anchor 目标
    quiz_marker = "\n## 随堂练习"
    content_md = markdown.split(quiz_marker, 1)[0] if quiz_marker in markdown else markdown
    tail = (quiz_marker + markdown.split(quiz_marker, 1)[1]) if quiz_marker in markdown else ""
    quadrant = quadrant_criteria(content_md)
    user = (
        f"当前知识点：{node.title}（编号 {node.node_key}）\n"
        f"学习目标：{(node.objective or '').strip()}\n"
        f"讲义正文如下（用于定位 anchor、挑选概念与取值）：\n\n{content_md[:8000]}"
    )
    if quadrant:
        user += ("\n\n【后端判据（必须遵守）】" + quadrant["reason"]
                 + "：本讲义的数据图卡必须用 chart_type=\"quadrant\"。")
    try:
        data = usage_log.complete_json(client, CARDS_SYSTEM, user,
                                       max_tokens=3400, purpose="cards")
    except Exception as exc:  # noqa: BLE001 —— 规划失败跳过
        logger.warning("cards 规划失败，跳过：node=%s err=%s", node.node_key, exc)
        stage, reason = _failure_reason(exc)
        _record_failure(lecture_item, "cards-plan", stage, reason,
                        retry={"kind": "cards-plan"})
        return markdown, [], []
    raw_cards = (data or {}).get("cards") if isinstance(data, dict) else None
    if not isinstance(raw_cards, list):
        return markdown, [], []
    md = content_md
    diagram_card_ids: list[str] = []
    chart_card_ids: list[str] = []
    seq = 0
    quadrant_used = False
    for raw in raw_cards[:4]:
        if not isinstance(raw, dict):
            continue
        anchor_text = str(raw.get("anchor") or "").strip()
        caption = str(raw.get("caption") or "").strip()[:16] or "概念卡"
        chart_type = str(raw.get("chart_type") or "").strip().lower()
        if chart_type not in DATA_CHART_TYPES:
            chart_type = "concept"
        if quadrant and chart_type == "quadrant":
            quadrant_used = True
        elif quadrant and not quadrant_used and chart_type != "concept":
            # 规划器没按判据给象限卡（mock / 模型没听话）：就地改型，数据用 0-LLM 兜底模板
            raw = {**raw, "chart_type": "quadrant", "data": quadrant_card_data()}
            chart_type = "quadrant"
            quadrant_used = True
        seq += 1
        meta_base = {
            "from_lecture": lecture_item.id,
            "role": "card",
            "card_anchor": anchor_text,
            "card_seq": seq,
            "chart_type": chart_type,
        }
        if quadrant and chart_type == "quadrant":
            meta_base["layout_notes"] = [{"kind": "figure-kind", "reason": quadrant["reason"]}]
        try:
            if chart_type == "concept":
                marker, fig_ids = _persist_card_concept(
                    db, course, node, caption, raw, meta_base)
            else:
                marker, fig_ids = _persist_card_chart(
                    db, course, node, chart_type, caption, raw, meta_base)
        except Exception as exc:  # noqa: BLE001 —— 单张卡渲染失败跳过
            logger.warning("card 渲染失败，跳过：%s", exc)
            stage, reason = _failure_reason(exc)
            _record_failure(lecture_item, f"card:{seq}", stage, reason, retry={
                "kind": "card",
                "card_seq": seq,
                "chart_type": chart_type,
                "caption": caption,
                "anchor": anchor_text,
                "raw": raw,
            })
            continue
        if not fig_ids or not marker:
            continue
        new_md, placed = _insert_after_anchor(md, anchor_text, marker)
        if not placed:
            new_md = _inject_before_summary(md, marker)
            placed = new_md != md
        if not placed:
            for fig_id in fig_ids:      # 锚点放不下 ⇒ 拆出来的每一张都要丢掉，不留孤儿
                _drop_card_item(db, fig_id)
            continue
        md = new_md
        if chart_type == "concept":
            diagram_card_ids += fig_ids
        else:
            chart_card_ids += fig_ids
    return md + tail, diagram_card_ids, chart_card_ids


def _card_marker(tag: str, caption: str, scheme: str, items: list[ContentItem]) -> str:
    """卡片在讲义里的引用行：拆图后**一张一行**（带上「（图 i/N）」，任务 125）。"""
    out = []
    for item in items:
        title = str(item.title or "")
        mark = title[title.rfind("（图 "):] if "（图 " in title else ""
        out.append(f"\n![{tag}：{caption}{mark}]({scheme}://{item.id})")
    return "".join(out)


def _persist_card_concept(
    db: Session,
    course: Course,
    node: OutlineNode,
    caption: str,
    raw: dict,
    meta_base: dict,
) -> tuple[str, list[str]]:
    """概念卡：layout 校验后走 diagram 服务渲染（沿用现有概念卡链路）。

    返回 ``(marker, ids)``：超限拆图时 ids 是多张、marker 里逐张引用（只引用第 1 张会让
    后面几张在交付包里成孤儿，任务 125）。
    """
    layout = str(raw.get("layout") or "").strip()
    if layout not in LAYOUTS:
        return "", []
    items = persist_diagrams_from_data(
        db, course, node, layout, caption, raw,
        title=f"{node.title} · 概念卡",
        meta_extra=meta_base,
    )
    return _card_marker("概念卡", caption, "diagram", items), [i.id for i in items]


def _persist_card_chart(
    db: Session,
    course: Course,
    node: OutlineNode,
    chart_type: str,
    caption: str,
    raw: dict,
    meta_base: dict,
) -> tuple[str, list[str]]:
    """数据卡：结构化 data 走 chart 服务（bar/pie/line/table/mindmap/timeline）。

    返回 ``(marker, ids)``：口径与 :func:`_persist_card_concept` 一致（拆图逐张引用）。
    """
    data = raw.get("data")
    if not isinstance(data, dict):
        return "", []
    items = persist_charts_from_data(
        db, course, node, chart_type, caption, data,
        title=str(raw.get("title") or caption or f"{node.title} · {chart_type} 图")[:80],
        instruction=f"{node.title} · {caption}",
        source="lecture-card",
        meta_extra=meta_base,
    )
    return _card_marker("数据卡", caption, "chart", items), [i.id for i in items]


def _drop_card_item(db: Session, item_id: str) -> None:
    """锚点完全放不下时丢弃刚创建的卡片条目（diagram 或 chart_*）。"""
    row = db.get(ContentItem, item_id)
    if row is not None:
        delete_rows_and_files(db, [row])
        db.flush()

def regenerate_cards_for_lecture(db: Session, lecture_item: ContentItem) -> int:
    """对已有讲义重建「概念卡/数据卡」：先清旧卡再规划插入，返回新卡数量。"""
    old_diagram_cards, old_chart_cards = _owned_card_ids(db, lecture_item)
    drop = old_diagram_cards + old_chart_cards
    md = _strip_card_refs(str((lecture_item.body or {}).get("markdown") or ""), drop)
    if drop:
        rows = (
            db.query(ContentItem)
            .filter(ContentItem.id.in_(drop))
            .all()
        )
        delete_rows_and_files(db, rows)
        db.flush()
    course = db.get(Course, lecture_item.course_id)
    node = db.get(OutlineNode, lecture_item.node_id)
    if course is None or node is None:
        raise ValueError("讲义所属课程或知识点不存在，无法重建概念卡")
    md, card_diagram_ids, card_chart_ids = attach_concept_cards(
        db, course, node, lecture_item, md)
    concept_ids = _owned_concept_ids(db, lecture_item)
    md = _relocate_concept_diagram_refs(md)
    lecture_item.body = {**(lecture_item.body or {}), "markdown": md}
    lecture_item.meta = {**dict(lecture_item.meta or {}),
                         "diagram_ids": concept_ids + card_diagram_ids,
                         "chart_ids": card_chart_ids}
    db.commit()
    db.refresh(lecture_item)
    flush_scheduled_removals(db)
    try:
        attach_vault_write(db, lecture_item, "lecture")
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for lecture %s", lecture_item.id)
    return len(card_diagram_ids) + len(card_chart_ids)


def retry_diagram_failure(db: Session, lecture_item: ContentItem, failure_id: str) -> dict:
    """重试一条配图失败记录（7c）：成功即清掉该条并同步笔记；再失败则登记新记录。"""
    record = next((r for r in _failures_of(lecture_item) if r.get("id") == failure_id), None)
    if record is None:
        raise ValueError("该失败记录已不存在（可能已经重试成功）")
    course = db.get(Course, lecture_item.course_id)
    node = db.get(OutlineNode, lecture_item.node_id)
    if course is None or node is None:
        raise ValueError("讲义所属课程或知识点不存在，无法重试")

    retry = dict(record.get("retry") or {})
    kind = str(retry.get("kind") or "")
    try:
        if kind == "cards-plan":
            regenerate_cards_for_lecture(db, lecture_item)  # 内部已重建卡片并写回笔记
            _clear_failure(db, lecture_item, failure_id)
            return {"ok": True, "kind": kind, "id": lecture_item.id,
                    "body": lecture_item.body, "meta": lecture_item.meta}
        markdown = str((lecture_item.body or {}).get("markdown") or "")
        diagram_ids = list((lecture_item.meta or {}).get("diagram_ids") or [])
        chart_ids = list((lecture_item.meta or {}).get("chart_ids") or [])
        if kind == "concept":
            # 任务 125：同上 —— 归属信息与讲义引用都覆盖拆出来的每一张，不留孤儿
            for one in generate_diagrams(
                    db, course, node,
                    instruction=str(retry.get("instruction") or node.objective),
                    meta_extra={"from_lecture": lecture_item.id, "role": "concept"}):
                markdown = _inject_diagram_markdown(markdown, one.id)
                diagram_ids.append(one.id)
            db.flush()
        elif kind == "card":
            raw = retry.get("raw")
            if not isinstance(raw, dict):
                raise ValueError("失败记录缺少卡片原始数据，无法单张重试，请点「重建概念卡」")
            caption = str(retry.get("caption") or "概念卡")
            chart_type = str(retry.get("chart_type") or "concept")
            anchor_text = str(retry.get("anchor") or "")
            meta_base = {"from_lecture": lecture_item.id, "role": "card",
                         "card_anchor": anchor_text, "card_seq": retry.get("card_seq"),
                         "chart_type": chart_type}
            if chart_type == "concept":
                marker, fig_ids = _persist_card_concept(db, course, node, caption, raw, meta_base)
                diagram_ids += fig_ids
            else:
                marker, fig_ids = _persist_card_chart(
                    db, course, node, chart_type, caption, raw, meta_base)
                chart_ids += fig_ids
            if not fig_ids or not marker:
                raise ValueError("卡片数据仍无法渲染，请点「重建概念卡」重来")
            markdown, placed = _insert_after_anchor(markdown, anchor_text, marker)
            if not placed:
                markdown = _inject_before_summary(markdown, marker)
        else:
            raise ValueError(f"不支持的失败类型：{kind or '未知'}")
    except Exception as exc:
        # 重试仍失败：把旧记录换成一条新的（带同样的重试信息），状态在网页端持续可见
        stage, reason = _failure_reason(exc)
        _clear_failure(db, lecture_item, failure_id)
        _record_failure(lecture_item, str(record.get("slot") or kind), stage, reason, retry=retry)
        db.commit()
        db.refresh(lecture_item)
        raise

    lecture_item.body = {**(lecture_item.body or {}), "markdown": markdown}
    lecture_item.meta = {**dict(lecture_item.meta or {}),
                         "diagram_ids": diagram_ids, "chart_ids": chart_ids}
    db.commit()
    db.refresh(lecture_item)
    _clear_failure(db, lecture_item, failure_id)
    flush_scheduled_removals(db)
    try:
        attach_vault_write(db, lecture_item, "lecture")
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for lecture %s", lecture_item.id)
    return {"ok": True, "kind": kind, "id": lecture_item.id,
            "body": lecture_item.body, "meta": lecture_item.meta}


def _owned_card_ids(db: Session, lecture_item: ContentItem) -> tuple[list[str], list[str]]:
    """返回 (diagram 概念卡 id, chart 数据卡 id)（meta.role=card 且归属本讲义）。"""
    rows = (
        db.query(ContentItem)
        .filter(ContentItem.course_id == lecture_item.course_id)
        .all()
    )
    diagram_cards: list[str] = []
    chart_cards: list[str] = []
    for r in rows:
        meta = r.meta or {}
        if meta.get("from_lecture") != lecture_item.id or meta.get("role") != "card":
            continue
        if r.kind == "diagram":
            diagram_cards.append(r.id)
        elif r.kind.startswith("chart_"):
            chart_cards.append(r.id)
    return diagram_cards, chart_cards


def _owned_chart_card_ids(db: Session, lecture_item: ContentItem) -> list[str]:
    """本讲义自动创建的数据卡 chart id（供 _drop_owned 清理）。"""
    _, chart_cards = _owned_card_ids(db, lecture_item)
    return chart_cards


def _owned_concept_ids(db: Session, lecture_item: ContentItem) -> list[str]:
    rows = (
        db.query(ContentItem)
        .filter(ContentItem.course_id == lecture_item.course_id,
                ContentItem.kind == "diagram")
        .all()
    )
    return [r.id for r in rows
            if (r.meta or {}).get("from_lecture") == lecture_item.id
            and (r.meta or {}).get("role") != "card"]


def _owned_figure_ids(db: Session, lecture_item: ContentItem) -> tuple[set[str], set[str]]:
    """本讲义创建的全部图条目 id：``(diagram, chart)``（任务 125：拆图后不止一张）。

    正本是 ``meta.from_lecture``（``meta.diagram_ids`` / ``chart_ids`` 只记着生成时返回的
    第 1 张）；老条目没有 from_lecture 时由 :func:`_drop_owned` 用 id 清单兜底。
    """
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == lecture_item.course_id,
                    or_(ContentItem.kind == "diagram", ContentItem.kind.like("chart_%")))
            .all())
    diagrams: set[str] = set()
    charts: set[str] = set()
    for row in rows:
        if (row.meta or {}).get("from_lecture") != lecture_item.id:
            continue
        (diagrams if row.kind == "diagram" else charts).add(row.id)
    return diagrams, charts


def _strip_card_refs(markdown: str, card_ids: list[str]) -> str:
    drop = set(card_ids or [])

    def _sub(match: re.Match) -> str:
        return "" if match.group(1) in drop else match.group(0)

    cleaned = _RE_ANY_CARD_REF.sub(_sub, markdown)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _norm_ws(text: str) -> str:
    return re.sub(r"[\s\u3000\u00a0]+", "", text)


def _norm_core(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", _norm_ws(text))


def _anchor_hit(line: str, anchor: str) -> bool:
    """anchor 命中判定：先逐字匹配，再宽松到「去空白」与「去标点核心词」双向包含。"""
    if anchor in line:
        return True
    if len(anchor) < 4:
        return False
    line_ws, anchor_ws = _norm_ws(line), _norm_ws(anchor)
    if anchor_ws and anchor_ws in line_ws:
        return True
    anchor_core = _norm_core(anchor)
    if len(anchor_core) < 4:
        return False
    line_core = _norm_core(line)
    if not line_core:
        return False
    return anchor_core in line_core or line_core in anchor_core


def _insert_after_anchor(markdown: str, anchor: str, marker: str) -> tuple[str, bool]:
    """把 marker 插到 anchor 所在段落之后（anchor 命中标题时插到该小节结尾）。"""
    anchor = (anchor or "").strip()
    if not anchor:
        return markdown, False
    lines = markdown.split("\n")
    hit = -1
    for idx, line in enumerate(lines):
        if _anchor_hit(line, anchor):
            hit = idx
            break
    if hit < 0:
        return markdown, False
    is_heading = lines[hit].lstrip().startswith("#")
    j = hit + 1 if is_heading else hit
    if is_heading:
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or lines[j].lstrip().startswith("#"):
            insert_at = hit + 1
        else:
            while j + 1 < len(lines) and lines[j + 1].strip() \
                    and not lines[j + 1].lstrip().startswith("#"):
                j += 1
            insert_at = j + 1
    else:
        while j + 1 < len(lines) and lines[j + 1].strip() \
                and not lines[j + 1].lstrip().startswith("#"):
            j += 1
        insert_at = j + 1
    out = lines[:]
    out.insert(insert_at, "")
    out.insert(insert_at + 1, marker.lstrip("\n"))
    out.insert(insert_at + 2, "")
    return "\n".join(out), True


def _inject_before_summary(markdown: str, marker: str) -> str:
    """兜底插入：放在最近的小结/随堂练习标题之前；都没有则追加到末尾。"""
    target = _RE_SUMMARY_HEAD.search(markdown) or _RE_PRACTICE_HEAD.search(markdown)
    if target is not None:
        head = markdown[: target.start()]
        if not head.endswith("\n\n"):
            head = head.rstrip("\n") + "\n\n"
        return head + marker.strip("\n") + "\n\n" + markdown[target.start():]
    return markdown.rstrip() + "\n" + marker.lstrip("\n") + "\n"


def _inject_diagram_markdown(markdown: str, diagram_id: str) -> str:
    """把一图看懂的 diagram:// 引用插到小结标题之前；没有小结则放到随堂练习前或末尾。"""
    ref = f"\n![一图看懂](diagram://{diagram_id})\n"
    target = _RE_SUMMARY_HEAD.search(markdown) or _RE_PRACTICE_HEAD.search(markdown)
    if target is not None:
        head = markdown[: target.start()]
        if not head.endswith("\n\n"):
            head = head.rstrip("\n") + "\n\n"
        return head + ref.strip("\n") + "\n\n" + markdown[target.start():]
    return markdown.rstrip() + "\n\n" + ref.strip() + "\n"


def _relocate_concept_diagram_refs(markdown: str) -> str:
    """把「一图看懂」引用统一放到小结/随堂练习标题之前（兼容真实 LLM 的小结标题层级）。"""
    spans = [(m.start(), m.end(), m.group(1))
             for m in _RE_CONCEPT_DIAGRAM.finditer(markdown)]
    if not spans:
        return markdown
    target = _RE_SUMMARY_HEAD.search(markdown) or _RE_PRACTICE_HEAD.search(markdown)
    if target is None:
        return markdown
    insert_at = target.start()
    if not any(s >= insert_at for s, _, _ in spans):
        return markdown
    removed_before = sum(e - s for s, e, _ in spans if e <= insert_at)
    stripped = markdown[: spans[0][0]] + "".join(
        markdown[spans[i][1]: spans[i + 1][0]] for i in range(len(spans) - 1)
    ) + markdown[spans[-1][1]:]
    block = "\n\n".join(f"![一图看懂](diagram://{rid})" for _, _, rid in spans)
    at = insert_at - removed_before
    head = stripped[:at]
    if not head.endswith("\n\n"):
        head = head.rstrip("\n") + "\n\n"
    return head + block + "\n\n" + stripped[at:]


# ---------------------------------------------------------------------------
# 任务 48b：0-LLM 结构块兜底（Markdown 表格 / Mermaid 流程图）
# ---------------------------------------------------------------------------
# 任务 48 的实测回退：讲义提示词里本来有 4 处要求表格/mermaid，但 47b/47c 的来源标注
# 纪律让「表格」变贵（规则 4-A 曾要求每一行都就地标注）⇒ 模型系统性改用纯列表，
# 27 篇里有 16 篇既没有表格也没有 mermaid。提示词已按 48b 口径改（规则 4-E / 规则 7，
# lecture-v7），但实测提示词只「一半有效」，所以这里再加一层确定性兜底：
# 正文两样都没有时，用该节点已有的 chart 数据回填一张 Markdown 表格
# （表下统一写一行来源，沿用正文里就近的真实来源，没有就写「无直接依据」）；
# 没有可用 chart 就按大纲要点画一张简单 flowchart TD。
# 纯本地渲染、幂等、不调 LLM；失败不阻断主流程，结论记 meta.structured_block。
STRUCTURED_KEY = "structured_block"
_STRUCTURED_MAX_ROWS = 6
_STRUCTURED_CELL_MAX = 24
_CHART_KINDS = tuple(f"chart_{chart_type}" for chart_type in CHART_TYPES)


def chart_option(item: ContentItem) -> dict:
    """把一条 chart_* 条目压成回填候选（只留渲染要用的字段，单测可直接喂 dict）。"""
    body = dict(item.body or {})
    return {
        "id": item.id,
        "kind": str(item.kind or ""),
        "title": str(item.title or ""),
        "data": dict(body.get("data") or {}),
        "markdown_table": str(body.get("markdown_table") or ""),
    }


def _node_chart_options(db: Session, node: OutlineNode) -> list[dict]:
    """该节点已有的图表条目（chart_table / chart_bar / …），按创建顺序。"""
    rows = (db.query(ContentItem)
            .filter(ContentItem.node_id == node.id, ContentItem.kind.in_(_CHART_KINDS))
            .order_by(ContentItem.created_at).all())
    return [chart_option(row) for row in rows]


def _cell(value, limit: int = _STRUCTURED_CELL_MAX) -> str:
    """表格单元格：去换行、超长截断（回填表只做对照，不塞长句）。"""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _md_table(headers: list, rows: list) -> str:
    """渲染 Markdown 表格；表头为空、或无行时返回空串。"""
    heads = [_cell(header) for header in headers if str(header).strip()]
    if not heads or not rows:
        return ""
    lines = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
    for row in list(rows)[:_STRUCTURED_MAX_ROWS]:
        cells = [_cell(cell) for cell in list(row)[: len(heads)]]
        cells += [""] * (len(heads) - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _has_nonzero(values: list) -> bool:
    """至少有一个非 0 数值（真机里存在全 0 的空数据图，不能拿来回填）。"""
    for value in values:
        try:
            if float(str(value).strip()) != 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _chart_markdown_table(option: dict) -> str:
    """把一条 chart 数据渲染成 Markdown 表格；渲染不出来（空图 / 全 0）返回空串。"""
    kind = str(option.get("kind") or "")
    data = dict(option.get("data") or {})
    if kind == "chart_table":
        stored = str(option.get("markdown_table") or "").strip()
        if stored:
            return stored
        rows = [list(row) for row in (data.get("rows") or [])
                if isinstance(row, (list, tuple))]
        return _md_table(list(data.get("headers") or []), rows)
    if kind in ("chart_bar", "chart_line", "chart_pie"):
        labels = [str(label) for label in (data.get("labels") or []) if str(label).strip()]
        datasets = [d for d in (data.get("datasets") or []) if isinstance(d, dict)]
        if len(labels) < 2 or not datasets:
            return ""
        series = [list(dataset.get("data") or []) for dataset in datasets[:2]]
        if not any(_has_nonzero(values) for values in series):
            return ""
        heads: list = ["项目"] + labels[: _STRUCTURED_MAX_ROWS]
        rows = []
        for dataset, values in zip(datasets[:2], series):
            row = [dataset.get("label") or "数值"]
            row += [values[index] if index < len(values) else ""
                    for index in range(len(heads) - 1)]
            rows.append(row)
        return _md_table(heads, rows)
    if kind in ("chart_mindmap", "chart_timeline"):
        key = "branches" if kind == "chart_mindmap" else "steps"
        rows = []
        for entry in (data.get(key) or []):
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            subs = [str(sub).strip() for sub in (entry.get("subs") or []) if str(sub).strip()]
            if title or subs:
                rows.append([title or "—", "；".join(subs)])
        if len(rows) < 2:
            return ""
        return _md_table(["一级分支" if kind == "chart_mindmap" else "阶段", "要点"], rows)
    return ""


def _mermaid_label(text, limit: int = 40) -> str:
    """Mermaid 节点标签：单行、去掉会截断语法的引号与方括号、超长截断。"""
    label = re.sub(r"\s+", " ", str(text or "")).strip()
    label = label.replace('"', "「").replace("[", "【").replace("]", "】")
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _outline_flowchart(node: OutlineNode | None) -> str:
    """按大纲要点画一张简单 flowchart TD（只用大纲已有字段，不新增内容）。"""
    if node is None:
        return ""
    steps = [value for value in (
        str(getattr(node, "title", "") or "").strip(),
        str(getattr(node, "objective", "") or "").strip(),
        str(getattr(node, "teaching_strategy", "") or "").strip(),
        str(getattr(node, "assessment", "") or "").strip(),
    ) if value][:5]
    if len(steps) < 2:
        return ""
    names = "ABCDEFG"
    lines = ["```mermaid", "flowchart TD"]
    lines += [f'  {names[i]}["{_mermaid_label(value)}"]' for i, value in enumerate(steps)]
    lines += [f"  {names[i]} --> {names[i + 1]}" for i in range(len(steps) - 1)]
    lines.append("```")
    return "\n".join(lines)


def _structured_insert_at(markdown: str) -> int:
    """兜底块插在哪：优先「小结」标题之前，其次「随堂练习」之前，都没有就文末。"""
    for pattern in (_RE_SUMMARY_HEAD, _RE_PRACTICE_HEAD):
        match = pattern.search(markdown)
        if match is not None:
            return match.start()
    return len(markdown)


def ensure_structured_block(
    markdown: str,
    *,
    node: OutlineNode | None = None,
    charts: list[dict] | None = None,
) -> tuple[str, dict]:
    """正文里既没有表格也没有 Mermaid 时，用确定性数据回填一个结构块（任务 48b）。

    优先级：① 该节点已有的 chart 数据 —— chart_table 优先，其次 bar/line/pie、
    mindmap、timeline，渲染成 Markdown 表格，表下统一写一行来源（沿用正文里就近的
    一条真实来源；找不到就写「无直接依据,建议人工确认」）；② 没有可用 chart 时，
    按大纲要点（课题 / 学习目标 / 教学策略 / 考核方式）生成一张 flowchart TD。

    幂等：正文已有表格或 Mermaid 代码块时原样返回 ``applied=False``，
    重复调用（重生成、存量回填）不会插第二块。返回 ``(markdown, info)``，
    ``info`` 形如 ``{"applied": True, "source": "chart_table", "chart_item_id": …, "reason": …}``；
    本函数不做 IO、不调 LLM —— 失败由调用方兜住并记进 ``meta.structured_block``。
    """
    text = str(markdown or "")
    if has_structured_block(text):
        return text, {"applied": False, "source": "existing",
                      "reason": "正文已有表格或 mermaid，无需兜底"}

    source, chart_item_id, block = "", None, ""
    for option in (charts or []):
        table = _chart_markdown_table(option)
        if not table:
            continue
        title = _cell(option.get("title") or "图表数据", 32)
        source = str(option.get("kind") or "chart")
        chart_item_id = option.get("id")
        citation = last_citation(text, before=_structured_insert_at(text)) or NO_BASIS_MARK
        block = f"**数据对照表：{title}**\n\n{table}\n\n{citation}"
        break
    if not block:
        block = _outline_flowchart(node)
        source = "flowchart"
    if not block:
        return text, {"applied": False, "source": "none",
                      "reason": "该节点没有可用 chart，大纲也没有足够要点画流程图"}

    insert_at = _structured_insert_at(text)
    head = text[:insert_at].rstrip()
    tail = text[insert_at:].lstrip("\n")
    merged = "\n\n".join(part for part in (head, block.strip()) if part)
    if tail:
        merged = f"{merged}\n\n{tail}" if merged else tail
    info = {"applied": True, "source": source}
    if chart_item_id:
        info["chart_item_id"] = str(chart_item_id)
    return merged, info


def _target_lecture(
    db: Session,
    course: Course,
    node: OutlineNode,
    item_id: str | None,
) -> ContentItem | None:
    if item_id:
        item = db.get(ContentItem, item_id)
        if item is None or item.course_id != course.id or item.kind != "lecture":
            raise ValueError(f"内容项不存在或类型不符：{item_id}")
        if node is not None and item.node_id != node.id:
            raise ValueError("讲义所属节点与请求不一致")
        return item
    return (
        db.query(ContentItem)
        .filter(
            ContentItem.course_id == course.id,
            ContentItem.node_id == node.id,
            ContentItem.kind == "lecture",
        )
        .order_by(ContentItem.created_at.desc())
        .first()
    )


def _drop_owned(db: Session, item: ContentItem) -> None:
    """删除旧讲义创建的习题、AI 配图与概念图条目（同一讲义 id 追踪）。"""
    quiz_ids = list((item.meta or {}).get("quiz_ids") or [])
    if quiz_ids:
        db.query(QuizItem).filter(QuizItem.id.in_(quiz_ids)).delete(synchronize_session=False)
    image_ids = list((item.meta or {}).get("image_ids") or [])
    if image_ids:
        delete_matching_items(db, and_(
            ContentItem.course_id == item.course_id,
            ContentItem.kind == "image",
            ContentItem.id.in_(image_ids),
        ))
    # 任务 125：超限拆图后一篇讲义的图条目不止一张，而 meta.diagram_ids / chart_ids 里
    # 只记着返回的第 1 张 ⇒ 归属（meta.from_lecture）为准，id 清单只作老条目的兜底。
    owned_diagrams, owned_charts = _owned_figure_ids(db, item)
    diagram_ids = sorted(owned_diagrams | set((item.meta or {}).get("diagram_ids") or []))
    if diagram_ids:
        delete_matching_items(db, and_(
            ContentItem.course_id == item.course_id,
            ContentItem.kind == "diagram",
            ContentItem.id.in_(diagram_ids),
        ))
    chart_ids = sorted(owned_charts | set((item.meta or {}).get("chart_ids") or []))
    if chart_ids:
        delete_matching_items(db, and_(
            ContentItem.course_id == item.course_id,
            ContentItem.kind.like("chart_%"),
            ContentItem.id.in_(chart_ids),
        ))
    db.flush()


def _strip_gen_refs(markdown: str) -> str:
    """默认零 AI 位图：剥离讲义里残留的 gen:// 图片占位，不生成 PNG。"""
    return _GEN_IMG_REF.sub("", markdown)


def _persist_quizzes(
    db: Session,
    course: Course,
    node: OutlineNode,
    raw_quizzes: list,
    client=None,
) -> tuple[list[QuizItem], list[dict]]:
    """把 LLM 返回的随堂练习落成 QuizItem 行。

    任务 56：取值走 ``quiz_rules``（显式判 None —— ``0`` / ``False`` 不再被 ``or`` 吞掉），
    落库前校验「choice 必须有 ≥2 个选项且 answer 是 0..len-1 的序号 / blank、short 的
    answer 非空」；不合规**就这道题重问一次**，仍不合规就把题面加上
    ``【答案待人工确认】`` 前缀并返回问题记录 —— 绝不静默丢答案，也绝不静默丢题。

    返回 ``(落库行, 问题列表)``；问题列表由调用方写进讲义 ``meta.quiz_issues``。
    """
    rows: list[QuizItem] = []
    difficulties = {"easy", "medium", "hard"}
    issues: list[dict] = []
    for index, raw in enumerate((raw_quizzes or [])[:5], 1):
        fields, problem = resolve_quiz(raw, client=client)
        if problem:
            fields = mark_manual(fields, problem)
            issues.append({
                "index": index,
                "qtype": fields["qtype"],
                "problem": problem,
                "question": fields["question"][:200],
                "answer_raw": fields.get("answer_raw") or "",
            })
            logger.warning("习题答案待人工确认：node=%s 第 %s 题 —— %s（%s）",
                           node.node_key, index, problem, fields["question"][:60])
        difficulty = (fields["difficulty"] if fields["difficulty"] in difficulties
                      else "medium")
        row = QuizItem(
            course_id=course.id,
            node_id=node.id,
            qtype=fields["qtype"],
            difficulty=difficulty,
            question=fields["question"],
            options=fields["options"],
            answer=fields["answer"],
            explanation=fields["explanation"],
            # 任务 55：分层 / 预计用时 / 开放题评分要点（缺层时按难度 0-LLM 兜底）
            meta=row_meta({**fields, "difficulty": difficulty}),
        )
        db.add(row)
        db.flush()
        rows.append(row)
    return rows, issues


def _quiz_section(rows: list[QuizItem], *, limit: dict | None = None) -> str:
    """把随堂练习渲染成讲义末尾的 Markdown 章节（题目 + 答案/评分要点 + 解析）。

    任务 55：按分层（基础 / 提升 / 挑战）分组、每题标预计用时；开放题（作图 / 材料分析 /
    实验探究）显示「评分要点」与作答区；小节底部给出总时长 —— 超过学段建议上限时**显式提示**
    （``limit`` 由调用方按 ``course.meta`` 的学段算好，见 ``quiz_rules.workload_limit``），
    但**不判 fail**、不影响任何质检分数。
    """
    if not rows:
        return ""
    type_label = dict(TYPE_LABEL)
    diff_label = {"easy": "易", "medium": "中", "hard": "难"}
    zone_label = {"draw": "作图区", "material": "作答区", "experiment": "实验记录区"}
    parts = ["", "## 随堂练习", ""]
    index = 0
    for level in LEVELS:
        group = [q for q in rows if quiz_level(q) == level]
        if not group:
            continue
        parts += [f"### {level}", ""]
        for q in group:
            index += 1
            parts.append(f"#### 第 {index} 题 · {type_label.get(q.qtype, '习题')}"
                         f"（难度：{diff_label.get(q.difficulty, '中')}，约 {quiz_minutes(q)} 分钟）")
            parts += ["", q.question, ""]
            # 任务 56：answer 统一按字符串判序号；空答案不再渲染出光秃秃的「**答案**：」空行
            answer = str(q.answer or "").strip()
            answer_index = int(answer) if answer.isdigit() else -1
            rubric = quiz_rubric(q)
            if q.qtype == "choice" and q.options:
                for opt_idx, opt in enumerate(q.options):
                    marker = "✓ " if answer_index == opt_idx else "- "
                    parts.append(f"{marker} {opt}")
                parts.append("")
            if q.qtype in OPEN_TYPES:
                if rubric:
                    parts += [f"**评分要点**：{rubric}", ""]
                parts += [f"> **{zone_label.get(q.qtype, '作答区')}**："
                          "请在此作答，教师按评分要点批改。", ""]
            elif q.options and 0 <= answer_index < len(q.options):
                parts += [f"**答案**：{q.options[answer_index]}", ""]
            elif q.qtype != "choice" and answer:
                parts += [f"**答案**：{answer}", ""]
            if q.explanation:
                parts += [f"**解析**：{q.explanation}", ""]
            parts += ["---", ""]
    total = sum(quiz_minutes(q) for q in rows)
    tally = "、".join(f"{level} {sum(1 for q in rows if quiz_level(q) == level)} 道"
                     for level in LEVELS
                     if any(quiz_level(q) == level for q in rows))
    parts += [f"> ⏱ 本次练习共 {len(rows)} 道题（{tally}），预计用时 **{total} 分钟**。", ""]
    if limit and int(limit.get("limit") or 0) < total:
        parts += [f"> ⚠️ 预计用时 {total} 分钟，超过{limit.get('label')}的建议上限 "
                  f"{limit.get('limit')} 分钟，建议精简题量或拆成两课时。", ""]
    return "\n".join(parts).rstrip() + "\n"


def rerender_quiz_section(markdown: str, rows: list[QuizItem]) -> str:
    """按当前规则重画讲义里的「随堂练习」小节（任务 56：0 LLM、幂等）。

    背景：旧渲染在 ``answer`` 为空时会漏掉「**答案**」行、也不给正确选项打 ✓，
    存量讲义正文因此和 ``quiz_items`` 里的正确答案对不上。这里用 :func:`_quiz_section`
    同一套规则重新生成该小节，**只替换这一段**（从标题到下一个同级/更高级标题或文末，
    连同前面的空行），正文一字不动；找不到小节时原样返回，由调用方显式报告。
    """
    section = _quiz_section(rows)
    if not section:
        return markdown
    lines = markdown.splitlines()
    start = -1
    level = 0
    for idx, line in enumerate(lines):
        if _RE_PRACTICE_HEAD.match(line):
            start = idx
            stripped = line.strip()
            level = len(stripped) - len(stripped.lstrip("#"))
            break
    if start < 0:
        return markdown
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        head = _RE_ANY_HEADING.match(lines[idx])
        if head and len(head.group(1)) <= level:
            end = idx
            break
    head_start = start
    while head_start > 0 and not lines[head_start - 1].strip():
        head_start -= 1
    prefix = lines[:head_start]
    body = section.strip("\n").splitlines()
    tail = lines[end:]
    new_lines = prefix + ([""] if prefix else []) + body + ([""] + tail if tail else [])
    return "\n".join(new_lines).rstrip() + "\n"
