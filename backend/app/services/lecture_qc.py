"""讲义质检器（任务 22 / v1）：质检报告 + 来源锚定解析 + 术语与概念库比对。

三块能力：

1. **质检**（``compute_qc``）：11 项检查（structure/fluff/length/objective/
   terminology/figures/quiz/sources/numbers/substance/actionable），每项 ``ok|warn|fail``，
   总分 ``100 - 20*fail - 6*warn``（按已执行项归一）；结果挂 ``item.meta["qc"]``。
   其中 ``numbers``（引用数字对账：数字必须在被引段里）是任务 106 新增的第 11 项 ——
   它**只出 ok / warn**（不加 fail、不动权重），并复用既有的「按质检建议重生成」注入机制。
   其中 ``substance``（含金量：一手案例 / 失败教训 / 边界声明）与 ``actionable``
   （步骤能否被另一个人照着独立执行）是任务 41 借鉴「仓颉.skill」新增的两项：
   前者治「通篇都是训练数据里就有的通用观点」，后者治「只有抽象描述、照做不了」。
2. **来源锚定**（``parse_sources``）：解析正文里的 ``(来源:《素材名》第 N 段)``
   （**兼容历史写法** ``(来源: 素材名 #N)`` —— 任务 57 换措辞后老讲义不能变成「没标来源」；
   任务 105 起音视频素材另写 **时间戳** ``(来源:《录像名》06:47)``，同样兼容），
   产出 ``item.meta["sources"]``；既无来源、又没写「无直接依据」的关键点会被
   ``sources`` 项标出来（不编造依据）。
3. **术语对齐**（``terminology_issues`` / ``terminology_prompt_section``）：
   生成前把概念库标准名 + 别名注入提示词；生成后比对，用别名记入 ``terminology``
   项（给出建议标准名）。v1 不改写已定稿正文。

本模块刻意保持「纯计算 + 少量读库」，方便单测直接喂 Markdown 断言检查项。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, OutlineNode, QuizItem
from app.services.concept import normalize_key
from app.services.material import (ANCHOR_PARA, ANCHOR_STAMP, material_catalog, ocr_notices,
                                   paragraph_stamp, stamp_seconds)
from app.services import qc_profile
from app.services.quiz_rules import OPEN_TYPES, text_of

logger = logging.getLogger(__name__)

QC_KEY = "qc"
SOURCES_KEY = "sources"
# 质检口径版本：v1 存的 checks 里没有 sentences（P5-2 之后才有具体句子），
# 所以读取时按版本号判定「落后」并就地把老数据补齐（纯本地解析，零 LLM）。
# v3（任务 41）：新增 substance / actionable 两项 → 老数据读到时自动补算。
QC_VERSION = 3

# 无依据段落的显式标注（规范写法见任务 22-B）
NO_BASIS_MARK = "(无直接依据,建议人工确认)"

_NO_BASIS_RE = re.compile(r"[(（]\s*(?:无直接依据|无依据|建议人工确认)[^)）]*[)）]")
# 来源标注（任务 57）：**新旧两种写法都必须认**，否则历史讲义一换措辞就被判成「没标来源」：
#   新（提示词侧只产这种）：（来源:《破解AI幻觉》第 33 段）
#   旧（历史 27 篇）：      （来源: 破解AI幻觉 #33）
# 任务 105：音视频素材写**时间戳**（（来源:《录像名》06:47），兼容 1:02:03）—— 与段落号
#   同一条正则、同一个 name 组，用 `para` / `stamp` 两个具名组区分；旧讲义与文本素材的
#   「第 N 段」照旧解析（历史数据一个字不用改）。
#   时间戳两侧的环视是**区间 / 并列的护栏**：写「06:47-08:12」「06:47、07:20」一律解析不了
#   （与「第 42-45 段」同款），否则会悄悄取到区间尾巴当成一个有效锚点。
# 段落号只取一个数字；`段` 可省（旧写法没有）。标题里的 `#`/`）` 仍不允许（老口径不变）。
_CITE_RE = re.compile(
    r"[(（]\s*来源\s*[:：]\s*(?P<name>[^#)）]+?)\s*"
    r"(?:(?:#\s*|第\s*)(?P<para>\d+)\s*(?:段)?"
    r"|(?<![-\d、,，:：~～–])(?P<stamp>\d{1,3}:\d{2}(?::\d{2})?)(?![-\d、,，:：~～–]))\s*[)）]")
_RE_HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*$", re.M)
_RE_FENCE = re.compile(r"```([A-Za-z0-9_-]*)\n(.*?)```", re.S)
_RE_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$", re.M)
# 任务 48d：表格数据行（含表头行，不含 `| --- |` 分隔行）——判断信号也要认表格
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RE_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$", re.M)
# --- 任务 47b：来源覆盖要按「缩进」区分分组标签行与子项 -------------------------
# 带缩进的列表行（key_points / unsourced_* / unsourced_groups 共用同一套条目集合）
_RE_LIST_ROW = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])[ \t]+(?P<text>.+?)[ \t]*$")
_RE_EMPHASIS = re.compile(r"\*\*|__|\*|_|`")
_GROUP_LABEL_MAX = 24
_RE_LABEL_PUNCT = re.compile(r"[。！？；，]")
# 标签里出现这些词 = 有主谓的断言（如「成本会翻三倍」），不能当分组标题豁免
_GROUP_LABEL_ASSERT = ("会", "能", "可以", "必须", "应该", "需要", "导致", "造成", "使得",
                       "让", "是", "有", "没有", "不", "无", "提高", "降低", "增加", "减少",
                       "翻", "达到", "超过", "低于", "高于", "等于", "成为", "变得",
                       "决定", "取决于",
                       # 变化动词也算断言：真机 9.1 的「上下文窗口占用率提升30%」「成本线性增长」
                       # 与「成本会翻三倍」同类，不能因为用词不同就当成分组标签豁免掉。
                       "提升", "增长", "下降", "上升", "上涨", "下滑", "翻倍", "倍增", "减半")
_RE_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_RE_CARD_REF = re.compile(r"!\[[^\]]*\]\(\s*(?:diagram|chart)://([A-Za-z0-9_-]+)\s*\)")
_RE_DIAGRAM_REF = re.compile(r"!\[[^\]]*\]\(\s*diagram://([A-Za-z0-9_-]+)\s*\)")
_RE_CHART_REF = re.compile(r"!\[[^\]]*\]\(\s*chart://([A-Za-z0-9_-]+)\s*\)")
_RE_IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_RE_PRACTICE_HEAD = re.compile(r"^[ \t]{0,3}#{2,4}[ \t]+[^#\n]*随堂练习[ \t]*$", re.M)

# --- 任务 106：引用数字对账（「标了来源」⇒「数字真的在被引段里」）的取数与排除口径 ---
# 只查**带来源标注**的同一句 / 同一条列表项 / 同一行表格里的阿拉伯数字：
#   在被引段里 ⇒ 过；不在被引段、但出自**同一素材**的别的段 ⇒ 提示级（「疑似出自…」）；
#   整个素材里都没有 ⇒ 本项 warn（**不加 fail、不动权重、不动任何版本号**）。
# 中文数字（「三百个」「两三百」）v1 **不做** —— 与量词 / 约数的组合太活，硬拆会把
#   「三个要点」这类表达性说法一起拉进来，误报成本高于收益（HANDOFF 规格 §A4 同口径）。
# 必须排除（规格 §A3；DSH 实测 naive 实现 16 条里报 13 条，绝大多数是这些噪声）：
#   ① 图卡引用 ID / 路径：`chart://…` / `diagram://…` / `![](...)`（连 alt 里的数字一起丢）；
#   ② 行首的列表 / 步骤序号（`1.` / `1、` / `1）`）与表格**首列**的纯序号；
#   ③ 「编号词 + 数字」：`案例1` / `第 2 题` / `图 3` / `步骤5` / `方案6` …
#      —— 真实语料里 1.1 的「案例1 / 案例2」正是这类编号，不是内容数字；
#   ④ `## 随堂练习` 之后的一切（题号 / 难度与用时 / 评分要点 / 答案 / 解析）；
#   ⑤ 代码块 / `mermaid` 块；URL 与文件名里的数字。
_RE_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%?")
_RE_LABEL_NUM = re.compile(
    r"(?:案例|例|第|图|表|步骤|方案|方式|阶段|模块|版本|序号|编号|题|要点|原则|类别|章|节|列|栏)"
    r"\s*\d[\d,]*")
_RE_LINE_ORDINAL = re.compile(r"^\s*[(（]?\d{1,3}\s*[)）.、：:]+\s*")
_RE_URL = re.compile(r"(?:https?://|www\.)\S+")
_RE_FILE_NAME = re.compile(
    r"[^\s()（）【】,，。;；:：\"'|]+\.(?:md|markdown|txt|docx?|pptx?|xlsx?|csv|json|pdf|"
    r"png|jpe?g|gif|svg|mp4|mkv|mov|wav|mp3|srt|zip|py|ts|tsx|db)\b", re.I)
_RE_CELL_ORDINAL = re.compile(r"^\s*\d{1,3}\s*$")
_RE_SENTENCE_CUT = re.compile(r"[。！？；;]")
_NUM_UNIT_MAX = 120          # 明细里带出去的原文长度上限（detail 要能一眼读完）

CHECK_KEYS = ("structure", "fluff", "length", "objective", "terminology",
              "figures", "quiz", "sources", "numbers", "substance", "actionable")
# 检查项中文名（体检报告 / 前端徽标共用同一份，避免各写一套）
CHECK_LABELS = {
    "structure": "结构完整", "fluff": "空话套话", "length": "篇幅匹配",
    "objective": "目标对齐", "terminology": "术语一致", "figures": "图与表",
    "quiz": "随堂练习", "sources": "来源标注",
    "numbers": "数字与来源不符",
    "substance": "含金量", "actionable": "可操作",
}


def rules_label() -> str:
    """现行判据的「口径标签」：``qc-v2 · 11 项``（交付单 / 体检报告标注分数口径用）。

    任务 112：验收单与课程体检报告的分数都要能一眼看出「按哪套判据算的」。
    ``QC_RULES_VERSION`` 的正本在 :mod:`app.services.lecture`（那一层才管提示词与输入指纹），
    所以这里按需取、不做模块级导入 —— ``lecture`` 反过来依赖本模块。
    """
    from app.services.lecture import QC_RULES_VERSION

    return f"{QC_RULES_VERSION} · {len(CHECK_KEYS)} 项"


_WEIGHT = {"warn": 6, "fail": 20}
_LEVEL_ORDER = {"ok": 0, "warn": 1, "fail": 2}
# 第三种状态：该项这次**没有执行**（例如概念库为空时的术语比对）。
# 它既不算合格也不算不合格，且不计入总分分母——否则「少考一门」会变成白送分（P5-1）。
SKIPPED_LEVEL = "skipped"

# 章节齐备性：points、summary 缺失直接 fail，其余缺失 warn
_STRUCTURE_SLOTS = (
    ("introduce", "引入", ("引入", "导入", "开场", "情境", "场景", "背景")),
    ("points", "关键点", ("关键", "核心", "讲解", "要点", "定义", "原理", "步骤", "方法", "机制")),
    ("examples", "例子或类比", ("例", "类比", "案例", "实例", "示例")),
    ("summary", "小结", ("小结", "总结", "回顾", "归纳")),
    ("practice", "随堂练习", ("随堂练习", "练习", "思考题", "作业", "自测")),
)
_CORE_SLOTS = {"points", "summary"}

_FLUFF_WORDS = ("总之", "综上", "总而言之", "值得注意的是", "众所周知", "不言而喻",
                "显而易见", "非常重要", "至关重要", "意义重大", "不言自明",
                "举足轻重", "不可或缺", "一言以蔽之")

_OBJECTIVE_STOP = ("掌握", "理解", "了解", "学会", "能够", "可以", "熟悉", "运用",
                   "应用", "目标", "学习", "通过", "本节", "本章", "重点", "基本",
                   "初步", "知道", "认识", "教学", "会做", "记住")

# 任务 48h：学习目标句用「2 字滑窗」切词，窗口会**跨词边界**切出「地的 / 的严 / 重影 /
# 为锚 / 备知」这类虚词碎片 —— 没有任何读者会把它当成「目标关键词」，却全部进了分母，
# 于是比例天然偏低、把「对齐度低」判成过报。**整条**含虚词 / 结构字的一律丢出分母。
# ⚠️ 只放虚词与结构字：实义字（如上 / 下 / 关 / 系 / 问 / 题 / 上 / 文）**不许**进这张表 ——
# 48h 规格里的反面教训：第一版把「上 / 下」当虚词后，5.1 直接从 warn 掉成 fail。
_OBJECTIVE_FUNC = frozenset("的了着是在和与或对如何为把被这那有也都很就会能要个们呢吗啊之其")

# --- 任务 41：含金量（substance）词表 ---------------------------------------
# 口径来自「从素材里优先提取模型训练数据里不可能有的东西」：一手案例 / 失败教训 /
# 边界声明。通用观点（「XX 很重要」）不算，金句也不算。
_SUBSTANCE_FAILURE = ("踩过的坑", "踩坑", "失败", "返工", "翻车", "走弯路", "教训",
                      "误判", "试错", "出错", "踩过", "做砸", "亏了")
_SUBSTANCE_CASE = ("一手", "案例", "实例", "实战", "现场", "复盘", "亲历", "亲测",
                   "实测", "真实项目", "某个学员", "有位学员", "客户")
# 注意：不把「无直接依据 / 建议人工确认」算作边界声明 —— 那是来源标注口径
# （每篇讲义本来就要求写），拿它当边界会让「能力边界」这一项白送分。
_SUBSTANCE_BOUNDARY = ("不覆盖", "不涉及", "不适用", "能力边界", "边界", "超出本节",
                       "本节之外", "找专家", "请教专家", "咨询专业人士", "局限性",
                       "前提是")

# --- 任务 41：可操作（actionable）词表 --------------------------------------
_ACTION_VERB = ("打开", "检查", "设置", "选择", "记录", "测量", "配置", "运行",
                "填写", "调整", "确认", "导出", "备份", "计算", "观察", "对比",
                "替换", "安装", "输入", "输出", "保存", "提交", "测试", "验证",
                "统计", "标注", "整理", "准备", "控制", "取样", "检测", "上传",
                "下载", "点击", "称取", "搅拌", "预热", "启动", "关闭", "打印",
                "拍摄", "拆解", "演练", "示范", "锁定", "排序", "分组", "复盘")
# 判断信号 / 参数：带量纲的数字，或条件句式
_STEP_SIGNAL = ("当", "如果", "一旦", "超过", "低于", "达到", "大于", "小于",
                "≥", "≤", "不少于", "不超过", "不少于")
_RE_STEP_NUMBER = re.compile(
    r"\d+\s*(?:分钟|小时|秒|天|周|次|度|℃|%|倍|克|千克|kg|g|ml|mL|L|元|人|张|页|条|档)")
_STEP_DELIVERABLE = ("输出", "产出", "得到", "生成", "保存", "写入", "记录成",
                     "文件", "表格", "报告", "清单", "结论", "方案", "画布",
                     "模板", "截图", "文档")
# 任务 48d：术语定义型条目（「**温度参数**：控制模型采样随机性的超参数」）不算步骤。
# 去掉 markdown 强调后形如「X：…」，X 是短名词短语 —— 判据沿用 47b `_is_group_label`：
# 冒号前是 ≤ `_GROUP_LABEL_MAX` 字、无句读、无断言词（会/是/导致…）的标签；
# 冒号前不允许出现括号（否则会把「（来源:《X》第 3 段）」里的冒号当成定义分隔符）。
_RE_DEF_LABEL = re.compile(r"^(?P<label>[^:：（()）]{1,40})[:：]")

_MUDDY_WORDS = ("各有优劣", "各有利弊", "因情况而异", "视情况而定", "具体情况具体分析",
                "两者都有道理", "没有绝对的对错")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Markdown 拆解小工具
# ---------------------------------------------------------------------------

def split_quiz(markdown: str) -> tuple[str, str]:
    """正文 / 随堂练习两段（质检关键点只看正文，选项列表不算关键点）。"""
    match = _RE_PRACTICE_HEAD.search(str(markdown or ""))
    if match is None:
        return str(markdown or ""), ""
    return str(markdown or "")[:match.start()], str(markdown or "")[match.start():]


def prose_of(markdown: str) -> str:
    """去掉代码块后的「人读正文」（字数、套话统计都基于它）。"""
    return _RE_FENCE.sub("", str(markdown or "")).strip()


def headings_of(markdown: str) -> list[str]:
    return [m.group(2).strip() for m in _RE_HEADING.finditer(str(markdown or ""))]


def key_points(markdown: str) -> list[str]:
    """关键点候选 = 正文里的列表行（篇幅这类「复杂度」统计按它走）。

    任务 47b 起，**来源覆盖**另有 ``source_key_points``（会把「分组标签行」剔除）；
    本函数保持「所有列表行」的原义，避免连带改动 length / metrics 的口径。
    """
    return [row["text"] for row in _list_rows(markdown)]


def _list_rows(markdown: str) -> list[dict]:
    """正文里的列表行（含缩进与小节归属）——来源覆盖三处共用同一套条目集合。

    每条 ``{indent, text, bullet, line, section, section_index}``：

    - ``indent``：展开制表符后的缩进宽度（4 空格制表位）；
    - ``text``：去掉列表标记后的条目原文；
    - ``bullet``：True = 无序列表项（``-/*/+``），False = 编号项；
    - ``line``：原始 markdown 里的 1 基行号（前端据此跳转）；
    - ``section`` / ``section_index``：所属小标题与其出现序号（同名标题出现两次算两节）。

    代码块内、以及「随堂练习」之后的内容不算正文关键点。
    """
    text = str(markdown or "")
    if not text:
        return []
    rows: list[dict] = []
    in_fence = False
    heading = ""
    section_index = 0
    for index, line in enumerate(text.splitlines()):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _RE_PRACTICE_HEAD.match(line):
            break
        head = _RE_HEADING.match(line.strip())
        if head:
            heading = head.group(2).strip()
            section_index += 1
            continue
        match = _RE_LIST_ROW.match(line)
        if match is None:
            continue
        body = match.group("text").strip()
        if not body:
            continue
        rows.append({
            "indent": len(match.group("indent").expandtabs(4)),
            "text": body,
            "bullet": not match.group("marker")[0].isdigit(),
            "line": index + 1,
            "section": heading,
            "section_index": section_index,
        })
    return rows


def _is_group_label(rows: list[dict], index: int) -> bool:
    """第 ``index`` 行是不是「分组标签行」（任务 47b）——这种行不计入来源覆盖。

    两个条件同时成立才算：① 去掉 markdown 强调后只剩一个短标签（≤ ``_GROUP_LABEL_MAX``
    字、不含句末标点、不含断言词），可带或不带尾随冒号；② 其后紧跟至少一个**缩进更深**
    的子列表项。

    护栏：悬空标签（后面没有子项）、含断言的标签（如「成本会翻三倍」）、以及所有子项，
    都照旧算关键点 —— 整组不豁免。

    护栏 (d) 的口径：普通句子、顶层无缩进条目、以及**有内容的编号步骤**（如
    ``1. 打开配置文件``）一律照旧；编号**父标签**（如真机 5.1 的 ``3. **动态调整**：``
    后面挂着带来源的子项）按「列表项」的定义同样豁免 —— 它和 ``- **行为阈值**：`` 只是
    列表标记不同。若要把编号项一律排除在豁免之外，只需在此处加回::

        if not row["bullet"]:
            return False
    """
    row = rows[index]
    label = _RE_EMPHASIS.sub("", row["text"]).strip().rstrip("：:").strip()
    if not label or len(label) > _GROUP_LABEL_MAX:
        return False
    if _RE_LABEL_PUNCT.search(label):
        return False
    if any(word in label for word in _GROUP_LABEL_ASSERT):
        return False
    child = rows[index + 1] if index + 1 < len(rows) else None
    return child is not None and child["indent"] > row["indent"]


def source_key_points(markdown: str) -> list[str]:
    """来源覆盖口径下的关键点 = 列表行剔除「分组标签行」后的条目。"""
    rows = _list_rows(markdown)
    return [row["text"] for index, row in enumerate(rows)
            if not _is_group_label(rows, index)]


def _count_fences(markdown: str) -> tuple[int, int]:
    """(代码块总数, mermaid 块数)。"""
    fences = _RE_FENCE.findall(str(markdown or ""))
    return len(fences), sum(1 for lang, _body in fences if (lang or "").lower() == "mermaid")


def _table_count(markdown: str) -> int:
    return len(_RE_TABLE_SEP.findall(str(markdown or "")))


def _table_rows(markdown: str) -> list[str]:
    """表格里的行（任务 48d）——判断信号也要认表格。

    只认**真正的表格**：一整块 ``| … |`` 行里必须出现 ``| --- |`` 分隔行，否则
    只是正文里的散装竖线。``| --- |`` 分隔行本身不算数据行；代码块内的竖线不算。
    """
    body = _RE_FENCE.sub("", str(markdown or ""))
    rows: list[str] = []
    block: list[str] = []
    for line in body.splitlines() + [""]:
        if _RE_TABLE_ROW.match(line):
            block.append(line.strip())
            continue
        if block:
            if any(_RE_TABLE_SEP.match(item) for item in block):
                rows.extend(item for item in block if not _RE_TABLE_SEP.match(item))
            block = []
    return rows


def last_citation(markdown: str, *, before: int | None = None) -> str:
    """正文里最后一条来源标注的**原文**（如「（来源:《破解AI幻觉》第 12 段）」）。

    任务 48b：0-LLM 兜底回填表格时，表下那一行来源要**沿用就近的真实来源**，
    而不是编造一个新出处（找不到就由调用方写「无直接依据」）。

    ``before``：只在文中该字符位置**之前**查找 —— 兜底块插在「### 小结」前，
    只看插入点以前的正文，避免借用到后面小节的来源。返回空串表示没找到。
    """
    text = str(markdown or "")
    if before is not None:
        text = text[:max(0, int(before))]
    matches = list(_CITE_RE.finditer(text))
    return matches[-1].group(0) if matches else ""


def has_structured_block(markdown: str) -> bool:
    """正文里有没有「结构块」：Markdown 表格（``| --- |`` 分隔行）或 ```mermaid 代码块。

    任务 48b 的唯一判据来源 —— ``figures`` 检查与讲义落库后的 0-LLM 兜底
    （``lecture.ensure_structured_block``）共用它，避免两处判定走偏。
    """
    text = str(markdown or "")
    return bool(_RE_TABLE_SEP.search(text)) or _count_fences(text)[1] > 0


# ---------------------------------------------------------------------------
# B. 来源锚定：解析 (来源:《素材名》第 N 段)（兼容旧写法 (来源: 素材名 #N)）
#    任务 105：音视频素材的锚点是时间戳 (来源:《录像名》06:47)（兼容 1:02:03）
# ---------------------------------------------------------------------------

def _resolve_material(name: str, catalog: list[dict]) -> dict | None:
    """把模型写的素材名解析回素材：支持标题、``素材N``、以及互相包含的写法。"""
    key = normalize_key(name)
    if not key:
        return None
    by_title = {normalize_key(item["title"]): item for item in catalog}
    by_index = {normalize_key(f"素材{item['index']}"): item for item in catalog}
    by_index.update({normalize_key(str(item["index"])): item for item in catalog})
    hit = by_title.get(key) or by_index.get(key)
    if hit is not None:
        return hit
    for item in catalog:
        title_key = normalize_key(item["title"])
        if title_key and (title_key in key or key in title_key):
            return item
    return None


def _locate_anchor(material: dict | None, para_raw: str | None,
                   stamp_raw: str) -> tuple[int, str]:
    """锚点 → ``(段落号, 该段摘要)``；对不上返回 ``(0, "")``（与「段号越界」同款）。

    段落号锚点：越界时**照旧把数字留着**、摘要为空（任务 57 起的口径，一个字没改）。
    时间戳锚点（任务 105）：按该素材**每段段首的时间戳表**核对 —— 只有正好落在某一段
    起点上才算对上（段中 / 素材里没有的时刻都不算，锚点必须能跳到确定的一段）。
    """
    paragraphs = list((material or {}).get("paragraphs") or [])
    if para_raw is not None:
        paragraph = int(para_raw)
        quote = paragraphs[paragraph - 1][:200] if 1 <= paragraph <= len(paragraphs) else ""
        return paragraph, quote
    if material is None:                     # 素材没解析出来 ⇒ 时间戳无处可对
        return 0, ""
    seconds = stamp_seconds(stamp_raw)
    if seconds is None:
        return 0, ""
    for index, para in enumerate(paragraphs, 1):
        value = paragraph_stamp(para)
        if value and stamp_seconds(value) == seconds:
            return index, para[:200]
    return 0, ""


def parse_sources(markdown: str, node_key: str, catalog: list[dict]) -> list[dict]:
    """解析正文里的来源标注 → ``meta.sources`` 结构。

    每条：``{node_key, section, material_id, material_name, quote, paragraph, anchor_kind,
    anchor, material_quote}``。``section`` 是标注所在的小节标题，``quote`` 是讲义原文，
    ``anchor`` 是**原文锚点**（段落号 / 音视频的时间戳原文），``material_quote`` 是素材里
    对应段落的摘要（供笔记页「来源」小节展示）。

    任务 105：音视频素材的引用写时间戳 ``（来源:《录像名》06:47）``（兼容 ``1:02:03``）——
    按该素材**段落的时间戳表**核对，只有正好落在某一段起点上才算对上；对不上与
    「第 N 段越界」**同款处理**（``paragraph=0`` + 摘要为空，来源条目照旧产出）。
    **旧写法「第 N 段」继续接受**（旧讲义 / 文本素材 / 转写未完成的音视频）。
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    section = ""
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        heading = _RE_HEADING.match(line)
        if heading:
            section = heading.group(2).strip()
            continue
        if not line:
            continue
        for match in _CITE_RE.finditer(line):
            name = match.group("name").strip().strip("《》【】\"'")
            para_raw = match.group("para")
            stamp_raw = "" if para_raw is not None else (match.group("stamp") or "")
            key = int(para_raw) if para_raw is not None else stamp_raw
            dedup = (normalize_key(name), key, section)
            if dedup in seen:
                continue
            seen.add(dedup)
            material = _resolve_material(name, catalog)
            paragraph, material_quote = _locate_anchor(material, para_raw, stamp_raw)
            quote = _CITE_RE.sub("", line).strip("　 ，,。;；")[:200]
            out.append({
                "node_key": str(node_key or ""),
                "section": section,
                "material_id": material["id"] if material else None,
                "material_name": material["title"] if material else name,
                "quote": quote,
                "paragraph": paragraph,
                "anchor_kind": ANCHOR_STAMP if stamp_raw else ANCHOR_PARA,
                "anchor": stamp_raw or f"第 {paragraph} 段",
                "material_quote": material_quote,
            })
    return out


def unsourced_points(markdown: str) -> list[str]:
    """既没有来源标注、也没写「无直接依据」的关键点（不编造依据的硬要求）。

    任务 47b：**分组标签行**（如 ``- **行为阈值**：`` 后面挂着子项）本身没有断言、
    来源都在子项里，所以不算关键点、不计入缺标注；子项与其余条目照旧 ——
    真缺来源的照样出现在这里（真缺口不会被这层豁免盖住）。
    """
    rows = _list_rows(markdown)
    return [row["text"] for index, row in enumerate(rows)
            if not _is_group_label(rows, index)
            and not _CITE_RE.search(row["text"])
            and not _NO_BASIS_RE.search(row["text"])]


def unsourced_sentences(markdown: str, *, context: int = 20) -> list[dict]:
    """缺来源标注的具体句子（P5-2：原先只给条数，老师得在 1500 字里逐句找）。

    每条返回 ``{text, context, line, section}``：

    - ``text``：缺标注的那一条关键点原文；
    - ``context``：前后各 ``context`` 字的上下文（方便老师定位）；
    - ``line``：markdown 里的 1 基行号（前端可据此滚动到正文）；
    - ``section``：它所属的最近一个小标题（没有则为空）。

    任务 47b：与 ``unsourced_points`` 共用同一条目集合（都走 ``_list_rows`` +
    ``_is_group_label``），保证「缺多少条」与「缺哪几句」永远对得上。
    """
    text = str(markdown or "")
    if not text:
        return []
    rows = _list_rows(text)
    if not rows:
        return []
    raw_lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in raw_lines:
        offsets.append(cursor)
        cursor += len(line)
    out: list[dict] = []
    for index, row in enumerate(rows):
        if _is_group_label(rows, index):
            continue
        if _CITE_RE.search(row["text"]) or _NO_BASIS_RE.search(row["text"]):
            continue
        line_no = row["line"]
        start = offsets[line_no - 1]
        end = start + len(raw_lines[line_no - 1].rstrip("\n"))
        out.append({
            "text": row["text"],
            "context": text[max(0, start - context):min(len(text), end + context)].strip(),
            "line": line_no,
            "section": row["section"],
        })
    return out


def unsourced_groups(markdown: str) -> list[dict]:
    """把「缺来源标注的关键点」按小节归组（任务 47：只为 detail 定位，不改判定口径）。

    每组 ``{section, indexes, total, all_missing}``：

    - ``section``：它所属的最近一个小标题（没有则为空）；
    - ``indexes``：该小节里缺标注条目的序号（1 基，按正文顺序），如 ``[2, 3]``；
    - ``total``：该小节关键点总条数（用来判断「整节未标」）；
    - ``all_missing``：该小节的关键点全都没标（且至少 1 条）。

    条目集合与 ``unsourced_points`` 严格一致（同一套 ``_list_rows`` + 同一套
    ``_is_group_label``，分组标签行同样剔除），否则 detail 点名的条号会错位。
    """
    rows = _list_rows(markdown)
    groups: list[dict] = []
    current: dict | None = None
    for index, row in enumerate(rows):
        if _is_group_label(rows, index):
            continue
        if current is None or current["section_index"] != row["section_index"]:
            current = {"section": row["section"], "section_index": row["section_index"],
                       "indexes": [], "total": 0}
            groups.append(current)
        current["total"] += 1
        if _CITE_RE.search(row["text"]) or _NO_BASIS_RE.search(row["text"]):
            continue
        current["indexes"].append(current["total"])
    out: list[dict] = []
    for group in groups:
        if not group["indexes"]:
            continue
        out.append({"section": group["section"], "indexes": group["indexes"],
                    "total": group["total"],
                    "all_missing": len(group["indexes"]) == group["total"]})
    return out


def _sources_detail(groups: list[dict], *, limit: int = 3) -> str:
    """把缺标注的小节点名成一句可定位的文案（任务 47）。

    例：``### 执行步骤 第 2/3 条（共 4 条）；### 踩过的坑 整节未标来源（3 条）``。
    小节太多时只列前 ``limit`` 个，其余折算成「等 N 个小节」，避免 detail 过长。
    """
    parts: list[str] = []
    for group in groups[:limit]:
        name = f"### {group['section']}" if group["section"] else "正文开头"
        if group["all_missing"]:
            parts.append(f"{name} 整节未标来源（{group['total']} 条）")
        else:
            nums = "/".join(str(index) for index in group["indexes"])
            parts.append(f"{name} 第 {nums} 条（共 {group['total']} 条）")
    if len(groups) > limit:
        parts.append(f"等 {len(groups)} 个小节")
    return "；".join(parts)


def no_basis_count(markdown: str) -> int:
    """显式标注「无直接依据,建议人工确认」的处数。"""
    return len(_NO_BASIS_RE.findall(str(markdown or "")))


def empty_sources_note(markdown: str) -> str:
    """0 条引用时「## 来源」小节里的说明（任务 48d）——笔记与交付讲义共用这一份文案。

    起因：48c 重生成后 10.1 变成 0 条引用 + 13 处「无直接依据」⇒ 交付包里
    「## 来源」小节整段消失，读者会以为交付漏了文件。小节必须留着，并把
    「正文里还有 N 处已标注无直接依据」说清楚（N 走现成的 ``no_basis_count``，0 LLM）。
    """
    return (f"本讲没有可溯源的素材段落：正文中 {no_basis_count(markdown)} 处已标注"
            f"「{NO_BASIS_MARK}」，请人工核对后再用于对外交付。")


# ---------------------------------------------------------------------------
# C. 术语与概念库对齐
# ---------------------------------------------------------------------------

def terminology_issues(markdown: str, concepts: list[dict]) -> list[dict]:
    """正文里用了概念库别名的地方 → ``[{alias, standard, context}]``。

    「标准名（别名）」这种首次出现的括号注明不算问题（窗口内出现标准名即跳过）。
    """
    text = str(markdown or "")
    issues: list[dict] = []
    for concept in concepts or []:
        name = str(concept.get("name") or "").strip()
        if not name:
            continue
        for alias in concept.get("aliases") or []:
            word = str(alias or "").strip()
            if len(word) < 2 or word == name:
                continue
            match = re.search(re.escape(word), text)
            if match is None:
                continue
            window = text[max(0, match.start() - 12): match.end() + 12]
            if name in window:
                continue
            issues.append({
                "alias": word,
                "standard": name,
                "context": text[max(0, match.start() - 20): match.end() + 20].replace("\n", " "),
            })
    return issues


def concept_definition(concept: dict) -> str:
    """概念条目的一句话定义。

    ``concept_rows`` 把定义放在 ``body.definition``；测试与「补抽」路径直接给顶层
    ``definition``。两处都认，避免渲染侧各写一套。
    """
    body = concept.get("body") if isinstance(concept.get("body"), dict) else {}
    return str(concept.get("definition") or body.get("definition") or "").strip()


# ---------------------------------------------------------------------------
# 任务 58：大纲「教学策略 / 评估方式」里的**术语嫌疑词**（没人解释 → warn）
# ---------------------------------------------------------------------------

# 依据 DSH 真机扫描：马尔可夫链 / 参数梯度 / 决策矩阵 / 成本效益计算模型 /
# 双模型验证工作流 / 三模型验证流程 —— 都是「专业词缀」形态，另有拉丁缩写（RAG、MCMC）。
JARGON_SUFFIXES = ("链", "模型", "算法", "定理", "矩阵", "熵", "向量", "梯度", "分布", "范式")
_COMMON_ABBREV = {"AI", "PPT", "PDF", "API", "URL", "JSON", "HTML", "CPU", "GPU"}
_JARGON_LEAD = set("用把和的与在做了对从向以并再很更最这那等及")
# 前缀里常见的「动作/修饰」词：不是术语的一部分，按最长匹配剥掉（「设计一个三模型」→「三模型」）
_JARGON_STOP_HEADS = ("设计", "预测", "配置", "使用", "选择", "构建", "搭建", "实现", "完成",
                      "分析", "讲解", "演示", "展示", "练习", "实验", "对比", "评估", "制作",
                      "处理", "根据", "针对", "一个", "两个", "三个", "特定", "典型", "常见",
                      "不同", "多个", "以及", "同时", "并且", "可以",
                      # 真机采样补的动词：「体会模型是按概率接词」里的「模型」是日常词，不是术语
                      "体会", "理解", "了解", "掌握", "学会", "观察", "尝试", "感受",
                      "认识", "明白", "知道", "思考", "发现", "看看", "查看")
_SUBSCRIPT = set("₀₁₂₃₄₅₆₇₈₉")
_JARGON_SKIP = ("链接", "链条")
_RE_ABBREV = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")
_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
# 括号里的内容本身就是「解释」——不再拿它当术语嫌疑词（例如
# 「马尔可夫链预测演示（只根据当前状态预测下一步的概率模型）」里的「概率模型」）
_RE_PAREN = re.compile(r"[（(][^）)]*[）)]")
JARGON_HEAD_MAX = 8      # 词缀前最多回看几个汉字（「成本效益计算模型」要能整词捞出来）
JARGON_TERM_MAX = 12     # 再长就不是一个词了，丢掉避免噪声
JARGON_COVER_GRAM = 3    # 覆盖判定：与库内词共享 3 字连续片段就算「同一个东西」
JARGON_SHOWN = 3         # detail 里最多点名几个


def _norm_term(text) -> str:
    """术语归一：去空白/标点、统一小写（与 concept.normalize_key 同思路，避免跨模块依赖）。"""
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _trim_head(head: str) -> str:
    """剥掉候选词前面的动作/修饰词（保留术语本身）。"""
    changed = True
    while changed and head:
        changed = False
        if head[0] in _JARGON_LEAD:
            head = head[1:]
            changed = True
            continue
        for stop in _JARGON_STOP_HEADS:
            if head.startswith(stop):
                head = head[len(stop):]
                changed = True
                break
    return head


def jargon_suspects(text: str) -> list[str]:
    """从一段文字里捞出**术语嫌疑词**（拉丁缩写 + 专业词缀），按出现顺序去重。

    例：「马尔可夫链预测演示」→ ``["马尔可夫链"]``；「用 RAG 检索」→ ``["RAG"]``。
    """
    blob = str(text or "")
    if not blob.strip():
        return []
    blob = _RE_PAREN.sub(" ", blob)
    out: list[str] = []
    for match in _RE_ABBREV.finditer(blob):
        word = match.group(0)
        if word.upper() in _COMMON_ABBREV:
            continue
        nxt = blob[match.end():match.end() + 1]
        if nxt and nxt in _SUBSCRIPT:      # CO₂ 这类化学式下标不是术语
            continue
        out.append(word)
    for run in _RE_CJK_RUN.findall(blob):
        for suffix in JARGON_SUFFIXES:
            start = 0
            while True:
                at = run.find(suffix, start)
                if at < 0:
                    break
                head = run[max(0, at - JARGON_HEAD_MAX):at]
                head = _trim_head(head)
                term = head + suffix
                start = at + len(suffix)
                if not (3 <= len(term) <= JARGON_TERM_MAX):
                    continue
                if any(skip in term for skip in _JARGON_SKIP):
                    continue
                out.append(term)
    seen: list[str] = []
    for term in out:
        if term not in seen:
            seen.append(term)
    return seen


def known_term_names(concepts: list[dict] | None,
                     terminology: list[dict] | None = None) -> set[str]:
    """「查得到解释」的名字集合：概念库全部 + 术语表里**带 definition** 的条目。

    术语表里只有名字、没有一句话定义的条目**不算已解释**（任务 58 的硬要求：
    条目必须带 definition，读者点进去得有东西看）。
    """
    out: set[str] = set()
    for concept in concepts or []:
        if not isinstance(concept, dict):
            continue
        for name in [concept.get("name")] + list(concept.get("aliases") or []):
            norm = _norm_term(name)
            if norm:
                out.add(norm)
    for row in terminology or []:
        if not isinstance(row, dict) or not str(row.get("definition") or "").strip():
            continue
        for name in [row.get("name")] + list(row.get("aliases") or []):
            norm = _norm_term(name)
            if norm:
                out.add(norm)
    return out


def _term_covered(term: str, known: set[str]) -> bool:
    """候选词是不是「已经在库里」：同名 / 互相包含 / 与库内词共享 3 字连续片段。"""
    norm = _norm_term(term)
    if not norm:
        return True
    grams = {norm[i:i + JARGON_COVER_GRAM]
             for i in range(max(0, len(norm) - JARGON_COVER_GRAM + 1))}
    for name in known:
        if norm == name or norm in name or name in norm:
            return True
        if any(gram and gram in name for gram in grams):
            return True
    return False


def unexplained_jargon(text: str, concepts: list[dict] | None,
                       terminology: list[dict] | None = None) -> list[str]:
    """一段文字里**既不在概念库、也不在术语表**的术语嫌疑词（保持出现顺序）。"""
    known = known_term_names(concepts, terminology)
    out: list[str] = []
    for term in jargon_suspects(text):
        if term not in out and not _term_covered(term, known):
            out.append(term)
    return out


def terminology_prompt_section(concepts: list[dict]) -> str:
    """生成前注入的术语表（标准名 + 一句话定义 + 别名）。

    任务 58：以前只给标准名与别名，模型不知道定义，正文里也就不会解释术语。
    现在**带定义**，并要求术语**首次出现**时用括号给一句白话解释（之后不再重复解释）。
    """
    if not concepts:
        return ("\n\n=== 课程术语表 ===\n"
                "（本课程概念库为空：尚未抽取概念，术语请自行保持一致，不要混用近义说法。）")
    lines = ["\n\n=== 课程术语表（概念库标准名 + 一句话定义；正文一律用标准名，"
             "别名只在首次出现时括号注明）===",
             "专业术语**第一次出现**时，必须紧跟一个括号给读者一句白话解释"
             "（优先用这里的定义，也可以用自己的话说）；同一术语之后再出现就直接用标准名，"
             "不要反复解释。"]
    for concept in concepts:
        name = str(concept.get("name") or "").strip()
        if not name:
            continue
        aliases = [str(a).strip() for a in (concept.get("aliases") or []) if str(a).strip()]
        definition = concept_definition(concept) or "（定义待补）"
        tail = f"；别名：{'、'.join(aliases)}" if aliases else ""
        lines.append(f"- 标准名「{name}」：{definition}{tail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 11 项检查（任务 106 起含 numbers）
# ---------------------------------------------------------------------------

def _check(key: str, level: str, detail: str, **extra) -> dict:
    return {"key": key, "level": level, "detail": detail, **extra}


def _structure_check(markdown: str) -> dict:
    text = str(markdown or "")
    missing: list[str] = []
    missing_core: list[str] = []
    for slot, label, words in _STRUCTURE_SLOTS:
        if not any(word in text for word in words):
            missing.append(label)
            if slot in _CORE_SLOTS:
                missing_core.append(label)
    if missing_core:
        return _check("structure", "fail", f"缺少关键小节：{'、'.join(missing_core)}")
    if missing:
        return _check("structure", "warn", f"小节不齐，缺少：{'、'.join(missing)}")
    return _check("structure", "ok", "引入 / 关键点 / 例子或类比 / 小结 / 随堂练习 齐备")


def _fluff_check(markdown: str) -> dict:
    prose = prose_of(markdown)
    hits: list[str] = []
    for word in _FLUFF_WORDS:
        hits += [word] * prose.count(word)
    if not hits:
        return _check("fluff", "ok", "未发现空话套话")
    sentences = max(1, len(re.findall(r"[。！？!?；;\n]", prose)))
    density = len(hits) / sentences
    sample = "、".join(dict.fromkeys(hits))[:40]
    if len(hits) >= 10 or density >= 0.6:
        return _check("fluff", "fail",
                      f"空话套话过多（{len(hits)} 处，密度 {density:.0%}）：{sample}")
    if len(hits) >= 3 or density >= 0.4:
        return _check("fluff", "warn",
                      f"套话偏多（{len(hits)} 处，密度 {density:.0%}）：{sample}")
    return _check("fluff", "ok", f"套话极少（{len(hits)} 处）：{sample}")


def _length_check(markdown: str) -> dict:
    prose = prose_of(markdown)
    chars = len(re.sub(r"\s+", "", prose))
    points = max(1, len(key_points(markdown)))
    # 每条关键点至少要有 60 字展开（够一句解释），否则就是「只有骨架没有讲透」；
    # 上限按 500 字/条自适应，超出说明这条知识点写得太散。
    low = max(300, 60 * points)
    high = max(3000, 500 * points)
    if chars < low:
        return _check("length", "warn",
                      f"正文 {chars} 字，低于 {points} 条关键点的建议下限 {low} 字，偏短")
    if chars > high:
        return _check("length", "warn", f"正文 {chars} 字，超过建议上限 {high} 字，偏长")
    return _check("length", "ok",
                  f"正文 {chars} 字，与 {points} 条关键点的复杂度匹配（{low}~{high} 字）")


def _objective_tokens(objective: str) -> list[str]:
    """学习目标的可判关键词（2 字滑窗 + 英文词），供目标对齐检查当分母用。

    任务 48h：滑窗切出的**虚词碎片**（「地的」「的严」「重影」）不进分母 —— 判据见
    ``_OBJECTIVE_FUNC``。阈值与判定口径（``ratio < 0.5 → warn`` / 0 命中 → fail）不变。
    """
    text = str(objective or "")
    for word in _OBJECTIVE_STOP:
        text = text.replace(word, " ")
    tokens: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens += [run] if len(run) <= 3 else [run[i:i + 2] for i in range(len(run) - 1)]
    tokens += re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", text)
    tokens = [token for token in tokens if not (_OBJECTIVE_FUNC & set(token))]
    return list(dict.fromkeys(tokens))[:8]


_OBJECTIVE_VERBS = ("说出", "写出", "读出", "背诵", "默写", "指出", "标出", "圈出", "复述",
                    "讲述", "说明", "解释", "列举", "列出", "举例", "比较", "对比", "概括",
                    "归纳", "分类", "计算", "算出", "画出", "演示", "操作", "测量", "标注",
                    "翻译", "朗读", "辨析", "判断", "评价", "设计", "制定", "完成", "会读",
                    "会写", "会用", "能读", "能写", "能用", "能说")


def _objective_check(markdown: str, objective: str, *, mode: str = "coverage") -> dict:
    """学习目标对齐。

    ``mode="coverage"``（缺省，存量课）：目标关键词在正文里的覆盖度，口径与改造前逐字一致。
    ``mode="lesson"``（K-12 档）：**按课时目标校验** —— 课时目标必须写成可观测的行为
    （「说出 / 写出 / 背诵 / 计算…」），否则先提示老师把目标写具体；覆盖度判据（阈值
    0.5、0 命中 fail）**一个都没改**。
    """
    tokens = _objective_tokens(objective)
    if not tokens:
        # 任务 48h 护栏③：目标句里全是虚词 / 结构字时滤完为空 —— 按「没有可判的目标词」
        # 处理（ok + 说明），不要误报成「没填学习目标」，更不许判 fail、不许崩。
        if str(objective or "").strip():
            return _check("objective", "ok",
                          "学习目标里没有可判定的关键词（虚词已滤除），跳过目标对齐检查")
        return _check("objective", "ok", "该知识点没有填学习目标，跳过目标对齐检查")
    if mode == "lesson" and not any(verb in str(objective) for verb in _OBJECTIVE_VERBS):
        return _check("objective", "warn",
                      f"课时目标「{str(objective)[:40]}」里没有可观测的行为动词"
                      "（说出/写出/背诵/计算/画出…）：老师无法据此判断学生会不会，"
                      "建议改写成「能说出…」「能写出…」")
    prose = prose_of(markdown)
    hit = [token for token in tokens if token in prose]
    ratio = len(hit) / len(tokens)
    shown = "、".join(hit[:5]) + ("…" if len(hit) > 5 else "")
    if not hit:
        return _check("objective", "fail",
                      f"正文与学习目标「{objective}」完全不对齐（命中 0/{len(tokens)}）")
    if ratio < 0.5:
        return _check("objective", "warn",
                      f"与学习目标对齐度偏低（命中 {len(hit)}/{len(tokens)}：{shown}）")
    return _check("objective", "ok",
                  f"贴合学习目标（命中 {len(hit)}/{len(tokens)}：{shown}）")


_TERM_FIELDS = ("教学策略", "评估方式")


def _terminology_check(markdown: str, concepts: list[dict], *,
                       strategy: str = "", assessment: str = "",
                       terminology: list[dict] | None = None) -> dict:
    """术语一致（正文别名）+ **大纲字段里的陌生术语**（任务 58）。

    以前只比对「正文用了概念库别名」，于是「教学策略：马尔可夫链预测演示」这种
    **读者查不到的写法**一路绿灯（正文里根本没出现这个词，比对自然比不出来）。
    现在把 ``teaching_strategy`` / ``assessment`` 里的术语嫌疑词也纳入同一项：
    既不在概念库、也不在术语表（带定义）→ warn，detail 点名「词 + 所在字段」。
    """
    if not concepts:
        # P5-1：原先这里返回 ok + 说明文字，计分时按「合格」算 → 85.5 分是少考一门考出来的。
        return _check("terminology", SKIPPED_LEVEL,
                      "概念库为空，本次未执行术语比对（不计入总分分母；抽完概念后复检即可转成真实结论）")
    issues = terminology_issues(markdown, concepts)
    jargon = [(label, term)
              for label, text in zip(_TERM_FIELDS, (strategy, assessment))
              for term in unexplained_jargon(text, concepts, terminology)]
    if jargon:
        shown = "、".join(f"「{term}」（{field}）" for field, term in jargon[:JARGON_SHOWN])
        more = f" 等 {len(jargon)} 处" if len(jargon) > JARGON_SHOWN else ""
        detail = (f"{shown}{more} 既不在概念库、也不在课程术语表（读者查不到解释）："
                  "建议改用白话，或补一条带一句话定义的概念页/术语条目")
        if issues:
            detail += f"；另有 {len(issues)} 处使用了概念库别名"
        return _check("terminology", "warn", detail, issues=issues,
                      jargon=[{"term": term, "field": field} for field, term in jargon])
    if not issues:
        return _check("terminology", "ok", f"术语与概念库一致（比对 {len(concepts)} 个概念）")
    pairs = "、".join(f"{i['alias']} → 建议「{i['standard']}」" for i in issues[:3])
    return _check("terminology", "warn", f"{len(issues)} 处使用了别名：{pairs}", issues=issues)


def _figures_check(markdown: str, metrics: dict, meta: dict) -> dict:
    """图与表：**结构块（表格 / mermaid）是硬要求**，同时检查图片引用没失效。

    任务 48b：任务 48 重生成后 16 篇讲义既没有 Markdown 表格、也没有 ```mermaid
    （根因是来源标注纪律让表格「变贵」，模型改用纯列表省事）。这里把它变成可测量的
    信号：**正文两样都没有 → warn**。并入现有 figures 项，不新增第 11 项
    （``CHECK_KEYS`` / 评分分母 / 前端徽标口径一律不动）。
    """
    total = metrics["figures"]
    if not has_structured_block(markdown):
        return _check("figures", "warn", "正文里既没有表格也没有 ```mermaid 流程图")
    if total == 0:
        return _check("figures", "warn", "正文没有图/表，学生理解成本偏高")
    diagram_ids = set(meta.get("diagram_ids") or [])
    chart_ids = set(meta.get("chart_ids") or [])
    text = str(markdown or "")
    dangling = ([ref for ref in _RE_DIAGRAM_REF.findall(text)
                 if diagram_ids and ref not in diagram_ids]
                + [ref for ref in _RE_CHART_REF.findall(text)
                   if chart_ids and ref not in chart_ids])
    if dangling:
        return _check("figures", "warn",
                      f"{len(dangling)} 处图片引用没落到本讲义产出的图上（可能已失效）")
    return _check("figures", "ok",
                  f"图/表 {total} 处（图示 {metrics['figures_diagram']}、表格 {metrics['tables']}）")


def _quiz_check(quizzes: list[dict]) -> dict:
    total = len(quizzes or [])
    if total == 0:
        return _check("quiz", "fail", "没有随堂练习")
    # 任务 56：只数数量与难度分布拦不住「答案丢失」（真机 AI 课 27 道选择题里 8 道
    # 没有答案，占 30%）。有题缺答案/解析就 warn，并在 detail 里按题号点名。
    # 任务 55：开放题（draw/material/experiment）没有唯一答案 ⇒ 判「评分要点 rubric」。
    no_answer, no_explanation, no_rubric = _quiz_gaps(quizzes)
    if no_answer or no_explanation or no_rubric:
        gaps: list[str] = []
        if no_answer:
            gaps.append(f"第 {'、'.join(str(i) for i in no_answer)} 题缺答案")
        if no_explanation:
            gaps.append(f"第 {'、'.join(str(i) for i in no_explanation)} 题缺解析")
        if no_rubric:
            gaps.append(f"第 {'、'.join(str(i) for i in no_rubric)} 题缺评分要点")
        return _check("quiz", "warn", f"{total} 道随堂练习：{'；'.join(gaps)}")
    difficulties = [str(q.get("difficulty") or "medium") for q in quizzes]
    if total == 1:
        return _check("quiz", "warn", "只有 1 道随堂练习，建议 ≥2 道")
    if len(set(difficulties)) == 1:
        return _check("quiz", "warn",
                      f"{total} 道随堂练习难度单一（全是 {difficulties[0]}），建议易中难搭配")
    return _check("quiz", "ok", f"{total} 道随堂练习，难度分布 {'/'.join(difficulties)}")


def _quiz_gaps(quizzes: list[dict]) -> tuple[list[int], list[int], list[int]]:
    """缺答案 / 缺解析 / 缺评分要点的题号（1 基），供 :func:`_quiz_check` 点名到具体题目。

    只判**上报了该字段**（键存在）的题目：旧调用方只传 ``qtype``/``difficulty`` 时
    属于「没数据可判」，不能凭空判 warn；讲义路径 ``build_for_item`` 三者都传，
    所以真实课是全覆盖的。

    任务 55：开放题（draw / material / experiment）没有唯一答案 —— 判 ``rubric``
    评分要点，**不判 answer**。
    """
    no_answer: list[int] = []
    no_explanation: list[int] = []
    no_rubric: list[int] = []
    for index, quiz in enumerate(quizzes or [], 1):
        if text_of(quiz.get("qtype")) in OPEN_TYPES:
            if "rubric" in quiz and not text_of(quiz.get("rubric")):
                no_rubric.append(index)
        elif "answer" in quiz and not text_of(quiz.get("answer")):
            no_answer.append(index)
        if "explanation" in quiz and not text_of(quiz.get("explanation")):
            no_explanation.append(index)
    return no_answer, no_explanation, no_rubric


def _sources_check(markdown: str, sources: list[dict], catalog: list[dict]) -> dict:
    missing = unsourced_points(markdown)
    marked = no_basis_count(markdown)
    # P5-2：把「哪几句缺标注」连同上下文一并带出去，前端可跳转到正文对应行
    sentences = unsourced_sentences(markdown) if missing else []
    # 任务 47：老师看 detail 就知道该去哪个小节补标注（哪一节、第几条）
    where = _sources_detail(unsourced_groups(markdown)) if missing else ""
    if not sources:
        detail = f"正文没有来源标注（{len(missing)} 条关键点未标注依据"
        detail += "；本课程暂无素材，请标注「无直接依据」）" if not catalog else "）"
        detail += f" —— 集中在：{where}" if where else ""
        return _check("sources", "warn", detail, sentences=sentences)
    if missing:
        return _check("sources", "warn",
                      f"{len(missing)} 条关键点既没有 (来源: …) 也没有「无直接依据」标注"
                      f" —— 集中在：{where}",
                      sentences=sentences)
    return _check("sources", "ok",
                  f"{len(sources)} 条来源标注，全部关键点有依据（其中 {marked} 处显式标注无直接依据）")


def _strip_number_noise(text: str) -> str:
    """把「不算数字」的东西从一段文本里减掉（图卡 ID / URL / 文件名 / 编号词 + 数字）。

    只做减法、不留位置：本检查只需要「这一句里还有哪几个数字」，给老师看的原文另有
    ``unit`` 字段原样带出去（不被这层减法影响）。
    """
    out = _RE_FENCE.sub(" ", str(text or ""))
    out = _RE_IMG.sub(" ", out)              # ![alt](chart://…) —— 连 alt 里的数字一起丢
    out = _RE_URL.sub(" ", out)
    out = _RE_FILE_NAME.sub(" ", out)
    out = _RE_LABEL_NUM.sub(" ", out)        # 案例1 / 第 2 题 / 步骤5 …
    return out


def numbers_in_unit(unit: str) -> list[str]:
    """一个文本单元里的阿拉伯数字（保序去重；``1,000`` 归一成 ``1000``、``%`` 只留数字）。

    来源标注 / 「无直接依据」括号**先整段剥掉**：锚点本身就是数字（``第 6 段``、``09:20``），
    留着会被当成正文数字（``06:47`` 还会被拆成 ``06`` 与 ``47``）。中文数字不做（口径见
    模块上方任务 106 那一段）。
    """
    text = _NO_BASIS_RE.sub(" ", _CITE_RE.sub(" ", str(unit or "")))
    out: list[str] = []
    for raw in _RE_NUM_TOKEN.findall(_strip_number_noise(text)):
        value = raw.replace(",", "").replace(" ", "").rstrip("%")
        if value and value not in out:
            out.append(value)
    return out


def _numeric_units(markdown: str) -> list[dict]:
    """带来源标注、且**含阿拉伯数字**的文本单元（任务 106 的检查对象）。

    单元 = 一条列表项 / 一行表格（首列纯序号先剥掉）/ 散文里的**一个句子**（按 ``。！？；``
    切句）—— 与规格「只查同一句 / 同一条列表项内带来源的文本单元」同一个口径；
    ``## 随堂练习`` 之后与代码块之内一律不看。每条 ``{unit, line, section, numbers}``。
    """
    body, _quiz = split_quiz(str(markdown or ""))
    out: list[dict] = []
    section = ""
    in_fence = False
    for index, raw in enumerate(body.splitlines()):
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        head = _RE_HEADING.match(line)
        if head:
            section = head.group(2).strip()
            continue
        if _RE_TABLE_ROW.match(line) and not _RE_TABLE_SEP.match(line):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and _RE_CELL_ORDINAL.match(cells[0]):
                cells = cells[1:]                # 表格首列纯序号：不是内容数字
            units = [" | ".join(cells)] if cells else []
        else:
            row = _RE_LIST_ROW.match(line)
            if row is not None:
                units = [row.group("text").strip()]
            else:
                units = [part.strip()
                         for part in _RE_SENTENCE_CUT.split(_RE_LINE_ORDINAL.sub("", line))]
        for unit in units:
            if not unit or not _CITE_RE.search(unit):
                continue
            values = numbers_in_unit(unit)
            if values:
                out.append({"unit": unit[:_NUM_UNIT_MAX], "key": unit, "line": index + 1,
                            "section": section, "numbers": values})
    return out


def _paragraph_body(material: dict, index: int) -> str:
    """素材里第 ``index`` 段（1 基）的正文 —— 段首的 ``[mm:ss]`` 先摘掉。"""
    paragraphs = list((material or {}).get("paragraphs") or [])
    if not 1 <= index <= len(paragraphs):
        return ""
    return re.sub(r"^\s*\[[^\]]*\]\s*", "", str(paragraphs[index - 1]))


def _paragraph_has_number(material: dict, index: int, value: str) -> bool:
    """这一段正文里有没有这个数字（按**数字整体**比对：``1`` 不会被 ``158`` 撞上）。"""
    text = re.sub(r"[,\s]", "", _paragraph_body(material, index))
    if not text or not value:
        return False
    return re.search(rf"(?<!\d){re.escape(value)}(?!\d)", text) is not None


def _find_number(material: dict, value: str) -> int:
    """同一素材里第一个含这个数字的段号（没有则 0）。"""
    total = len((material or {}).get("paragraphs") or [])
    for index in range(1, total + 1):
        if _paragraph_has_number(material, index, value):
            return index
    return 0


def _anchor_text(material: dict, index: int) -> str:
    """段号 → 老师看得懂的锚点：音视频素材给时间戳，其它给「第 N 段」。"""
    paragraphs = list((material or {}).get("paragraphs") or [])
    if not 1 <= index <= len(paragraphs):
        return ""
    return paragraph_stamp(paragraphs[index - 1]) or f"第 {index} 段"


def numbers_audit(markdown: str, catalog: list[dict] | None) -> dict:
    """引用数字对账的明细（任务 106）→ ``{checked, numbers, ok, hints, warns, unverified}``。

    每条明细 ``{value, values, unit, line, section, sources, where, wheres}``（``where``
    给提示级：它其实出自哪一段；多数字合并时 ``wheres`` 逐个给）。去重两遍：**同一个数字
    整篇只出一条**（重复出现时取最强结论：素材里都没有 > 疑似出自别的段 > 在被引段里）、
    **同一个文本单元只出一条**（该单元里对不上的数字一起点名）—— 否则同一个数字 / 同一句会
    被反复点名、条数虚高；``numbers`` 即**报出的条数**（= 文本单元数），与规格的 naive 基线
    「16 个带数字的来源标注 → 报了 13 条」同一个口径。

    锚点解析不出来（素材名对不上 / 段号越界 / 时间戳对不上）的单元进 ``unverified`` ——
    那类「来源根本定位不到」由 ``sources`` 项负责，这里不重复报警（避免制造噪声）。
    """
    units = _numeric_units(markdown)
    hits: list[dict] = []
    unverified: list[dict] = []
    for row in units:
        referenced: list[tuple[dict, int]] = []
        labels: list[str] = []
        for match in _CITE_RE.finditer(row["unit"]):
            name = match.group("name").strip().strip("《》【】\"'")
            para_raw = match.group("para")
            stamp_raw = "" if para_raw is not None else (match.group("stamp") or "")
            material = _resolve_material(name, catalog or [])
            index, _quote = _locate_anchor(material, para_raw, stamp_raw)
            if material is None:
                labels.append(name)
                continue
            labels.append(f"《{material['title']}》{_anchor_text(material, index) or '锚点对不上'}")
            # 段号越界（``_locate_anchor`` 会照旧把数字留着）时「被引段」并不存在 ⇒ 无从对账，
            # 归 ``unverified``：那类「来源根本定位不到」由 ``sources`` 项负责，这里不重复报警。
            if index > 0 and index <= len(material.get("paragraphs") or []):
                referenced.append((material, index))
        if not referenced:
            unverified.append({"unit": row["unit"], "line": row["line"],
                               "numbers": list(row["numbers"]), "sources": "、".join(labels)})
            continue
        for value in row["numbers"]:
            item = {"value": value, "key": row["key"], "unit": row["unit"], "line": row["line"],
                    "section": row["section"], "sources": "、".join(labels)}
            if any(_paragraph_has_number(material, index, value)
                   for material, index in referenced):
                item["verdict"] = "ok"
            else:
                where = ""
                for material, _index in referenced:
                    hit = _find_number(material, value)
                    if hit:
                        where = _anchor_text(material, hit)
                        break
                item["verdict"] = "hint" if where else "warn"
                item["where"] = where
            hits.append(item)
    rank = {"ok": 0, "hint": 1, "warn": 2}
    picked: dict[str, dict] = {}
    for item in hits:
        previous = picked.get(item["value"])
        if previous is None or rank[item["verdict"]] > rank[previous["verdict"]]:
            picked[item["value"]] = item
    # 去重两遍：① 同一个数字整篇只出一条（取最强结论，否则条数虚高）；② 同一个文本单元
    #   只出一条 —— 把该单元里对不上的数字**一起**点名。「条数」按**文本单元**算，与规格的
    #   naive 基线（「16 个带数字的来源标注 → 报了 13 条」）同一个口径；真实语料里
    #   「如156-758个」是**一句双标注**（两个数字都对不上、但同属一句）⇒ 报 1 条。
    merged: dict[str, dict] = {}
    for item in picked.values():
        current = merged.get(item["key"])
        if current is None:
            merged[item["key"]] = dict(item, values=[item["value"]],
                                       wheres={item["value"]: item.get("where", "")})
            continue
        current["values"].append(item["value"])
        current["wheres"].setdefault(item["value"], item.get("where", ""))
        current["value"] = "、".join(current["values"])
        if rank[item["verdict"]] > rank[current["verdict"]]:
            current["verdict"] = item["verdict"]
            current["where"] = item.get("where", "")
    out: dict = {"checked": len(units), "numbers": len(merged),
                 "ok": [], "hints": [], "warns": [], "unverified": unverified}
    for item in merged.values():
        out[{"ok": "ok", "hint": "hints", "warn": "warns"}[item["verdict"]]].append(item)
    return out


def _numbers_brief(items: list[dict], *, where: bool = False, limit: int = 2) -> str:
    """detail 里点名（最多 ``limit`` 条，其余折算成「等 N 处」）。"""
    parts: list[str] = []
    for item in items[:limit]:
        if where:
            wheres = dict(item.get("wheres") or {item["value"]: item.get("where") or "—"})
            parts.append("、".join(f"「{value}」疑似出自 {at or '—'}"
                                   for value, at in wheres.items()))
        else:
            parts.append(f"「{item['value']}」（原句：{item['unit']}）")
    if len(items) > limit:
        parts.append(f"等 {len(items)} 处")
    return "、".join(parts)


def _numbers_check(markdown: str, catalog: list[dict]) -> dict:
    """引用数字对账（任务 106）：带来源的数字必须能在**被引段**里找到。

    只有「整个素材里都没有」才把本项判 **warn**（不加 fail、不动权重）；「不在被引段、
    但出自同一素材的别的段」只在本项 detail 里点名 —— 那多半不是编造，而是**引错了段**
    （真实语料：``758`` 出自 ``09:20`` 却被引到 ``01:32``）。
    """
    audit = numbers_audit(markdown, catalog)
    warns, hints = audit["warns"], audit["hints"]
    items = warns + hints + audit["unverified"]
    if warns:
        detail = (f"{len(warns)} 处数字在本课素材里找不到：{_numbers_brief(warns)}"
                  " —— 请核对素材后改成素材里有的数字，或删掉这句里的数字")
        if hints:
            detail += (f"；另有 {len(hints)} 处不在被引段、疑似出自同一素材的别的段："
                       f"{_numbers_brief(hints, where=True)}")
        return _check("numbers", "warn", detail, numbers=items)
    if hints:
        return _check("numbers", "ok",
                      f"{audit['numbers']} 个带数字的来源单元：数字都在素材里，其中 {len(hints)} 处"
                      f"不在被引段、疑似出自同一素材的别的段（{_numbers_brief(hints, where=True)}）",
                      numbers=items)
    if not audit["numbers"]:
        return _check("numbers", "ok",
                      f"没有带数字的来源标注（本项无可对账；已看 {audit['checked']} 个来源单元）")
    return _check("numbers", "ok",
                  f"{audit['numbers']} 个带数字的来源单元，数字都在被引段里")


def substance_hits(markdown: str) -> dict:
    """含金量命中：一手案例 / 失败教训 / 边界声明各命中几处（任务 41）。"""
    prose = prose_of(markdown)
    return {
        "case": sum(prose.count(word) for word in _SUBSTANCE_CASE),
        "failure": sum(prose.count(word) for word in _SUBSTANCE_FAILURE),
        "boundary": sum(prose.count(word) for word in _SUBSTANCE_BOUNDARY),
    }


def muddy_sentences(markdown: str) -> list[str]:
    """和稀泥句（「各有优劣 / 因情况而异」这类）——任务 41 明确禁止。"""
    prose = prose_of(markdown)
    return [word for word in _MUDDY_WORDS if word in prose]


def _substance_check(markdown: str) -> dict:
    """含金量：通篇都是通用观点（训练数据里就有的话）→ warn。

    至少要有 1 处「一手案例 / 失败教训 / 边界声明」，这三类才是素材里才有、
    模型凭训练数据编不出来的东西。
    """
    hits = substance_hits(markdown)
    total = sum(hits.values())
    muddy = muddy_sentences(markdown)
    extra = {"hits": hits, "muddy": muddy}
    if total == 0:
        return _check("substance", "warn",
                      "正文没有一手案例 / 失败教训 / 边界声明（全是通用观点或抽象描述）："
                      "建议从素材补一个真实案例或失败教训，并加一句「本节不覆盖什么」",
                      **extra)
    parts = []
    if hits["failure"]:
        parts.append(f"失败教训 {hits['failure']} 处")
    if hits["case"]:
        parts.append(f"一手案例 {hits['case']} 处")
    if hits["boundary"]:
        parts.append(f"边界声明 {hits['boundary']} 处")
    detail = "含金量达标（" + "、".join(parts) + "）"
    if muddy:
        return _check("substance", "warn",
                      f"{detail}；但有 {len(muddy)} 处和稀泥句（{'、'.join(muddy[:3])}），"
                      "请写清「什么信号出现时用 A、什么信号出现时用 B」", **extra)
    return _check("substance", "ok", detail, **extra)


def _is_definition_entry(line: str) -> bool:
    """这条列表项是不是「术语定义型条目」（任务 48d）——这种条目不算步骤。

    形如「**温度参数**：控制模型采样随机性的超参数，高温度可能导致更多幻觉」：
    去掉 markdown 强调后是「X：…」，X 是一个短名词短语。三条同时成立才判定为定义条目：
    ① 冒号前是短标签（≤ ``_GROUP_LABEL_MAX`` 字、无句读、无断言词、**本身不含动作动词**
    ——「**模型选择**：选择多个模型」里的「选择」是动作，仍算步骤）；
    ② 整条**不以动作动词开头**（「先注入法案关键条款（占 30% 窗口）」照旧算步骤）；
    ③ 冒号前没有括号（避免把「（来源:《X》第 3 段）」里的冒号当成分隔符）。

    真机证据：48c 后 1.2 的 5 条「候选步骤」全是这种定义条目，把候选集灌满、
    真正的操作说明被稀释 ⇒ 「每篇至少 1 条带信号候选」的二值判定被放大成误报。
    """
    text = _RE_EMPHASIS.sub("", str(line or "")).strip()
    match = _RE_DEF_LABEL.match(text)
    if match is None:
        return False
    label = match.group("label").strip()
    if not label or len(label) > _GROUP_LABEL_MAX:
        return False
    if _RE_LABEL_PUNCT.search(label):
        return False
    if any(word in label for word in _GROUP_LABEL_ASSERT):
        return False
    if any(verb in label for verb in _ACTION_VERB):
        return False
    return not text.startswith(tuple(_ACTION_VERB))


def _step_candidates(markdown: str) -> list[str]:
    """步骤类候选行：列表项里带动作动词、且**不是术语定义型条目**的那些。

    正文与随堂练习分开统计（``split_quiz``）；任务 48d 起把「名词 + 定义」条目
    排除 —— 它们碰巧含动作动词（控制 / 输出 / 验证 之类）却不是给人照做的步骤。
    """
    body, _quiz = split_quiz(markdown)
    body = _RE_FENCE.sub("", body)
    lines = [m.group(1).strip() for m in _RE_LIST_ITEM.finditer(body) if m.group(1).strip()]
    return [line for line in lines
            if any(verb in line for verb in _ACTION_VERB)
            and not _is_definition_entry(line)]


def _has_signal(line: str) -> bool:
    return bool(_RE_STEP_NUMBER.search(line)) or any(word in line for word in _STEP_SIGNAL)


# 任务 74（并入原 73 B6）：actionable 的「抽象描述」例句必须**摘自本篇正文**。
# 修前这里是硬编码的**别课**例句（面点课的温度阈值语境），真机 27 篇里 17 篇都带着它，
# 客户一眼就看出是模板。摘不出（候选为空 / 太短）才退回**课程无关**的中性例子。
_ACTIONABLE_NEUTRAL_EXAMPLE = "只写「检查数据质量」而不写判断标准与产出物"
_RE_CITE_TAIL = re.compile(r"（来源[:：][^）]*）\s*$")
# 表格表头/字段标签这类「X：」候选（真机 5.1 的「输入 Prompt：」）拿出来当例句毫无信息量。
_RE_LABEL_ONLY = re.compile(r"^[^，。；;、（）()]{0,12}[：:]$")


def _abstract_example(candidates: list[str], limit: int = 20) -> str:
    """从本篇的步骤候选里摘出那句被判为抽象的描述（去掉强调与来源尾巴，截断到 ~20 字）。

    跳过两类候选：① 太短（不足 8 字）；② 只是「标签：」（没有内容的表头 / 字段名）。
    摘不出就退回 ``_ACTIONABLE_NEUTRAL_EXAMPLE``（**课程无关**的中性例子）。

    ⚠️ 只影响 ``actionable`` 的**说明文案**：判定分支、``steps`` 计数与分数一个字不改
    （分数由 level 决定，本函数不碰 level）。
    """
    for line in candidates:
        text = _RE_CITE_TAIL.sub("", _RE_EMPHASIS.sub("", str(line or ""))).strip()
        if len(text) < 8 or _RE_LABEL_ONLY.match(text):
            continue
        return text if len(text) <= limit else text[:limit].rstrip() + "…"
    return _ACTIONABLE_NEUTRAL_EXAMPLE


def _actionable_check(markdown: str, *, tier: str = "required") -> dict:
    """可操作：步骤类内容能不能被另一个人照着独立执行（任务 41）。

    判定 = 含动作动词 + 判断信号/参数 + 产出物。只有抽象描述（「要注意做好
    关键环节的把控」这类）→ warn，并说清缺的是哪一样。

    任务 48d：参数/阈值常写在 48b 要求的 Markdown 表格里，所以**判断信号不只扫列表项，
    表格行一起判**（表格行里的量纲数字 /「低于」「超过」这类条件词同样算信号；表格里
    只有纯文字、没有数字与条件词时不算，``_has_signal`` 的判据不变）。两颗护栏：
    ① 候选为 0（术语解释条目已被排除）且表里也找不到参数 → 仍判 warn，不借此放宽；
    ② 候选里一条带信号的都没有 → 仍判 warn。

    任务 52：``tier="hint"``（K-12 文科/低学段档）时**判据一字不改**，只是把 warn
    降级成「提示」呈现（``downgraded=True`` + ``hint`` 保留原文），不扣分也不刷分；
    理科 / 职教 / 缺省档照旧 ``required``。
    """
    def _emit(level: str, detail: str, **extra) -> dict:
        if level == "warn" and tier == "hint":
            return _check("actionable", "ok",
                          f"{detail}；本档位（文科/低学段）按**提示**处理，不计入扣分",
                          downgraded=True, hint=detail, **extra)
        return _check("actionable", level, detail, **extra)

    candidates = _step_candidates(markdown)
    table_rows = _table_rows(markdown)
    with_signal = [line for line in candidates + table_rows if _has_signal(line)]
    if not candidates and not with_signal:
        return _emit("warn",
                     "正文没有可照做的步骤（步骤类内容全是抽象描述或术语解释）："
                     "建议补一条「动作 + 判断信号/参数 + 产出物」的操作说明", steps=0)
    if not with_signal:
        return _emit("warn",
                     f"{len(candidates)} 条步骤只有抽象描述（如「{_abstract_example(candidates)}」），"
                     "缺判断信号/参数：建议补「动作 + 判断信号/参数 + 产出物」，别人才能照着独立执行",
                     steps=len(candidates))
    has_output = any(word in line for line in candidates for word in _STEP_DELIVERABLE)
    if not has_output:
        if not candidates:
            # 表格里只有参数、正文一条可照做的操作都没有 —— detail 别说成「0 条步骤」
            return _emit("warn",
                         "正文没有可照做的步骤（表格里有参数，但没有可照做的操作说明）："
                         "建议补一条「动作 + 判断信号/参数 + 产出物」的操作说明", steps=0)
        return _emit("warn",
                     f"{len(with_signal)} 条步骤有动作与判断信号，但没写产出物"
                     "（做完得到什么文件/表格/结论），建议补上", steps=len(candidates))
    return _check("actionable", "ok",
                  f"{len(with_signal)}/{len(candidates)} 条步骤含动作 + 判断信号 + 产出物，"
                  "可被他人照着独立执行",
                  steps=len(candidates))


def _metrics(markdown: str, sources: list[dict], quizzes: list[dict]) -> dict:
    prose = prose_of(markdown)
    _fences, mermaid = _count_fences(markdown)
    text = str(markdown or "")
    refs = _RE_CARD_REF.findall(text)
    images = [src for src in _RE_IMG.findall(text)
              if not src.startswith(("diagram://", "chart://", "gen://"))]
    tables = _table_count(text)
    return {
        "chars": len(re.sub(r"\s+", "", prose)),
        "sections": len(headings_of(text)),
        "points": len(key_points(text)),
        "examples": len(re.findall(r"例|案例|实例", prose)),
        "figures": len(images) + mermaid + len(refs),
        "figures_diagram": len(refs),
        "tables": tables,
        "mermaid": mermaid,
        "quizzes": len(quizzes or []),
        "sources": len(sources or []),
        "links": len(_RE_WIKI.findall(text)),
    }


def compute_qc(markdown: str, *, objective: str = "", quizzes: list[dict] | None = None,
               sources: list[dict] | None = None, concepts: list[dict] | None = None,
               catalog: list[dict] | None = None, meta: dict | None = None,
               strategy: str = "", assessment: str = "",
               terminology: list[dict] | None = None,
               profile: dict | None = None) -> dict:
    """产出 ``meta.qc``：``{score, checks[{key,level,detail}], metrics, version}``。"""
    quizzes = list(quizzes or [])
    sources = list(sources or [])
    concepts = list(concepts or [])
    catalog = list(catalog or [])
    # 任务 52：档位只决定「这一档要不要这门判据」，权重与阈值一律不动。
    profile = dict(profile or qc_profile.default())
    metrics = _metrics(markdown, sources, quizzes)
    checks = [
        _structure_check(markdown),
        _fluff_check(markdown),
        _length_check(markdown),
        _objective_check(markdown, objective,
                         mode=str(profile.get("objective_mode") or "coverage")),
        _terminology_check(markdown, concepts, strategy=strategy,
                           assessment=assessment, terminology=terminology),
        _figures_check(markdown, metrics, dict(meta or {})),
        _quiz_check(quizzes),
        _sources_check(markdown, sources, catalog),
        _numbers_check(markdown, catalog),
        _substance_check(markdown),
        _actionable_check(markdown, tier=str(profile.get("actionable_tier") or "required")),
    ]
    order = {key: idx for idx, key in enumerate(CHECK_KEYS)}
    checks.sort(key=lambda c: order.get(c["key"], len(order)))
    total = len(checks)
    executed = [c for c in checks if c.get("level") != SKIPPED_LEVEL]
    skipped_keys = [str(c.get("key")) for c in checks if c.get("level") == SKIPPED_LEVEL]
    penalty = float(sum(_WEIGHT.get(c["level"], 0) for c in executed))
    # 未执行的检查项不计入分母：把已执行项的扣分按「全部 / 已执行」等比放大，
    # 使分数与「所有项都执行」同一口径 —— 少考一门不能白拿分，也不能凭空扣分。
    if executed and len(executed) < total:
        penalty = penalty * total / len(executed)
    out = {
        "version": QC_VERSION,
        "profile": profile,
        "score": max(0, round(100 - penalty)),
        "checks": checks,
        "metrics": metrics,
        "executed_checks": len(executed),
        "total_checks": total,
        "skipped_count": len(skipped_keys),
        "skipped_keys": skipped_keys,
        "generated_at": _now_iso(),
    }
    downgraded = [c["key"] for c in executed if c.get("downgraded")]
    if downgraded:
        out["downgraded_keys"] = downgraded
    if skipped_keys:
        out["note"] = (f"{total} 项检查里 {len(skipped_keys)} 项未执行"
                       f"（{'、'.join(CHECK_LABELS.get(k, k) for k in skipped_keys)}）；"
                       "总分按已执行项归一，未执行项不计入分母。")
    return out


# ---------------------------------------------------------------------------
# 读库封装（生成后挂载 / 按需复检 / 课程级汇总）
# ---------------------------------------------------------------------------

def quiz_rows_of(db: Session, item: ContentItem) -> list[QuizItem]:
    """讲义带出的随堂练习（优先 meta.quiz_ids，旧数据回退按节点查）。

    任务 56：显式按 ``meta.quiz_ids`` 的顺序返回 —— 质检 detail 里的「第 N 题」
    必须和讲义正文里的「### 第 N 题」对齐，而 ``in_()`` 的返回顺序由数据库决定。
    """
    ids = [str(qid) for qid in ((item.meta or {}).get("quiz_ids") or [])]
    rows: list[QuizItem] = []
    if ids:
        found = db.query(QuizItem).filter(QuizItem.id.in_(ids)).all()
        order = {qid: idx for idx, qid in enumerate(ids)}
        rows = sorted(found, key=lambda q: order.get(str(q.id), len(order)))
    if not rows and item.node_id:
        rows = (db.query(QuizItem)
                .filter(QuizItem.course_id == item.course_id,
                        QuizItem.node_id == item.node_id)
                .order_by(QuizItem.created_at).all())
    return rows


def concepts_of(db: Session, course: Course) -> list[dict]:
    """课程概念库（标准名 + 别名），供术语表注入与比对。"""
    from app.services.concept import concept_rows

    return concept_rows(db, course.id)


def terminology_of(course: Course) -> list[dict]:
    """课程术语表（只出现一次、未建概念页的术语，带 definition）。

    任务 58：术语表条目和概念页一样算「读者查得到」—— 但必须**带一句话定义**才算数
    （见 :func:`known_term_names`）。
    """
    from app.services.concept import terminology_only

    return terminology_only(course)


def lecture_markdown(item: ContentItem) -> str:
    return str((item.body or {}).get("markdown") or "")


def build_for_item(db: Session, course: Course, node: OutlineNode | None,
                   item: ContentItem, markdown: str) -> dict:
    """给一篇讲义算质检 + 解析来源，返回 ``{"qc": …, "sources": [...]}``。"""
    catalog = material_catalog(db, course)
    node_key = (node.node_key if node is not None
                else str((item.meta or {}).get("node_key") or ""))
    sources = parse_sources(markdown, node_key, catalog)
    # 任务 56：把答案/解析一并交给质检 —— 「每道题都要有答案与解析」这条判据
    # 之前拿不到这两个字段，正是它没拦住真机 30% 缺答案的原因。
    quizzes = [{"difficulty": q.difficulty, "qtype": q.qtype,
                "answer": text_of(q.answer), "explanation": text_of(q.explanation),
                # 任务 55：开放题判「评分要点」而不是答案；带上分层
                "rubric": text_of((q.meta or {}).get("rubric")),
                "level": text_of((q.meta or {}).get("level"))}
               for q in quiz_rows_of(db, item)]
    qc = compute_qc(
        markdown,
        objective=(node.objective if node is not None else ""),
        quizzes=quizzes,
        sources=sources,
        concepts=concepts_of(db, course),
        catalog=catalog,
        meta=dict(item.meta or {}),
        # 任务 58：大纲的「教学策略 / 评估方式」进术语比对（节点缺失时不硬猜，留空）
        strategy=(str(getattr(node, "teaching_strategy", "") or "")
                  if node is not None else ""),
        assessment=(str(getattr(node, "assessment", "") or "")
                    if node is not None else ""),
        terminology=terminology_of(course),
        profile=qc_profile.resolve(course),
    )
    return {"qc": qc, "sources": sources}


def attach(result: dict, item: ContentItem) -> None:
    """把质检/来源写进 ``item.meta``（不提交，由调用方统一 commit）。"""
    item.meta = {**dict(item.meta or {}),
                 QC_KEY: result["qc"],
                 SOURCES_KEY: result["sources"]}


def stored_qc(item: ContentItem) -> dict | None:
    qc = (item.meta or {}).get(QC_KEY)
    return qc if isinstance(qc, dict) and qc.get("checks") else None


def concepts_available(db: Session, course: Course) -> bool:
    """课程概念库里有没有条目（任务 48k：决定 ``terminology=skipped`` 值不值得重算）。

    只查一条、不做 join —— 每读一次质检都会调用它，必须是廉价判断。
    """
    from app.services.concept import CONCEPT_KIND

    return db.query(ContentItem.id).filter(
        ContentItem.course_id == course.id,
        ContentItem.kind == CONCEPT_KIND).first() is not None


def qc_outdated(qc: dict | None, *, profile_key: str = "",
                concepts_ready: bool = False) -> bool:
    """这份质检是不是旧口径：缺 checks / 版本落后 / 该给句子却没给（P5-2 之前的老数据）。

    真机现场：AI 课 27 篇讲义的 ``meta.qc`` 都是 ``version 1`` 的老格式，
    界面能看到「26 篇缺来源标注」，却看不到具体是哪几句。

    任务 52：再加一条「档位变了」—— 同一篇讲义在「小学语文档」与「职业培训档」下的
    ``actionable`` 结论本来就不同，缓存里存的档位对不上就必须现算（缺 profile 的老数据
    按缺省档算，所以存量课不会被这次改动整体判过期）。

    任务 48k：再加一条「**概念库已就绪，但这篇的 terminology 还是 skipped**」——
    5 步流程天然是「先出讲义、后抽概念库」，抽完概念库后老 QC 仍被当成「够新」，
    老师看到的报告因此少一项、分数虚高。护栏：``concepts_ready=False``（概念库仍为空）
    时**不许**判过期，否则每次读质检都要白算一遍。

    任务 112：再加一条「**判据集合变了**」—— ``total_checks`` 是任务 48 起就落库的既有字段，
    库里记的项数与现行 ``CHECK_KEYS`` 对不上（任务 106 新增第 11 项 ``numbers`` 之后的存量数据
    就是「10 项」）⇒ 这份分数是旧口径算的，读出时就得重算。**不改任何判据 / 权重 / 阈值 /
    版本号，只把「什么时候算」补上。** 没记 ``total_checks`` 的更老数据照旧不判过期
    （与「缺 profile 按缺省档算」同口径，避免每次读质检都白跑一遍）。
    """
    if not isinstance(qc, dict) or not qc.get("checks"):
        return True
    if int(qc.get("version") or 0) < QC_VERSION:
        return True
    if profile_key:
        stored = str((qc.get("profile") or {}).get("key")
                     or qc_profile.DEFAULT_KEY)
        if stored != profile_key:
            return True
    if concepts_ready:
        for check in qc["checks"]:
            if check.get("key") == "terminology" and check.get("level") == SKIPPED_LEVEL:
                return True
    total = qc.get("total_checks")
    if total is not None:
        if int(total or 0) != len(CHECK_KEYS):
            return True
        # 项数一样但判据换了（同一位置换了一门检查）也要重算：认的是「检查项集合」本身
        if sorted(str(c.get("key")) for c in qc["checks"]) != sorted(CHECK_KEYS):
            return True
    return any(c.get("key") == "sources" and c.get("level") in ("warn", "fail")
               and "sentences" not in c for c in qc["checks"])


def refresh_item(db: Session, course: Course, item: ContentItem,
                 node: OutlineNode | None = None, *, commit: bool = True) -> dict:
    """按当前正文复检一篇讲义（老讲义没有 meta.qc 时用它补算）。"""
    if node is None and item.node_id:
        node = db.get(OutlineNode, item.node_id)
    result = build_for_item(db, course, node, item, lecture_markdown(item))
    attach(result, item)
    if commit:
        db.commit()
        db.refresh(item)
    return result


def refresh_course(db: Session, course: Course, *, commit: bool = True) -> dict:
    """按当前正文复检该课**全部讲义**的 ``meta.qc``（0 LLM，幂等）。

    任务 48k：概念抽取是后置步骤（先出讲义、后抽概念库），抽完概念库后这些讲义的
    ``terminology`` 还停在 skipped —— 概念任务成功后在这里顺手补一次。
    单篇失败**不阻断**（记 warning 并计数），由调用方把失败篇数写进 job.result。
    """
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id, ContentItem.kind == "lecture")
             .order_by(ContentItem.created_at).all())
    failed = 0
    for item in items:
        try:
            refresh_item(db, course, item, commit=False)
        except Exception as exc:  # noqa: BLE001 —— 单篇失败不影响其它篇与概念任务
            failed += 1
            logger.warning("讲义质检复检失败：item=%s —— %s", item.id, exc)
    if commit:
        db.commit()
    return {"lectures": len(items), "refreshed": len(items) - failed, "failed": failed}


def item_qc(db: Session, course: Course, item: ContentItem, *, refresh: bool = False) -> dict:
    """取一篇讲义的质检：够新就用现成的；缺失 / 版本落后（老数据）/ 显式 refresh 则现算并落库。

    补算只是本地文案解析（零 LLM），所以「读质检明细」时顺手补一次，
    不必等老师点「按当前正文复检」才看得到具体句子。判定见 :func:`qc_current`。
    """
    if not refresh and qc_current(db, course, item):
        return stored_qc(item) or {}
    return refresh_item(db, course, item)["qc"]


def qc_current(db: Session, course: Course, item: ContentItem) -> bool:
    """库里那份质检够不够新（缺 checks / 版本落后 / 档位变了 / 判据集合变了 / 概念库已就绪）。

    任务 112：``item_qc`` 的「用缓存还是现算」与出包前的复检 :func:`refresh_stale_course`
    共用这一套判定 —— 两处各写一份，最容易走偏（这正是本次要治的那条链路）。
    """
    cached = stored_qc(item)
    return cached is not None and not qc_outdated(
        cached, profile_key=qc_profile.resolve(course)["key"],
        concepts_ready=concepts_available(db, course))


def refresh_stale_course(db: Session, course: Course, *, commit: bool = True) -> dict:
    """把该课**旧口径**的质检按现行判据重算并落库（0 LLM，幂等）。

    任务 112：任务 106 给质检加了第 11 项（``numbers``）之后，存量讲义的 QC 还停在旧口径，
    出包时就把旧分数写进了验收单 / 课程体检报告 ⇒ 与「按现行判据现算」必然对不上。
    出包前先过一遍全课讲义：够新（:func:`qc_current`）的一篇都不重算（**幂等**），
    旧口径的走既有 :func:`item_qc` 的 ``refresh=True`` 现算并**写回 ``meta.qc``**
    （与既有落库口径一致，不是只塞进文件里）。单篇失败不阻断出包，如实计数。
    """
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id, ContentItem.kind == "lecture")
             .order_by(ContentItem.created_at).all())
    stale = [item for item in items if not qc_current(db, course, item)]
    failed = 0
    for item in stale:
        try:
            item_qc(db, course, item, refresh=True)
        except Exception as exc:  # noqa: BLE001 —— 单篇失败不影响出包与其它篇
            failed += 1
            logger.warning("出包前质检复检失败：item=%s —— %s", item.id, exc)
    if commit:
        db.commit()
    return {"lectures": len(items), "refreshed": len(stale) - failed,
            "current": len(items) - len(stale), "failed": failed,
            "rules": rules_label()}


def course_report(db: Session, course: Course, *, worst_n: int = 5) -> dict:
    """整课质检汇总：平均分、各检查项失败分布、最差 N 篇。"""
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())
    items: list[dict] = []
    counts = {key: {"ok": 0, "warn": 0, "fail": 0, SKIPPED_LEVEL: 0} for key in CHECK_KEYS}
    skipped_items = 0
    downgraded_items = 0
    for item in rows:
        qc = item_qc(db, course, item)
        checks = {c.get("key"): c for c in qc.get("checks") or []}
        for key in CHECK_KEYS:
            level = (checks.get(key) or {}).get("level", "ok")
            counts[key][level if level in counts[key] else "ok"] += 1
        if int(qc.get("skipped_count") or 0):
            skipped_items += 1
        downgraded = [c for c in (qc.get("checks") or []) if c.get("downgraded")]
        if downgraded:
            downgraded_items += 1
        node = db.get(OutlineNode, item.node_id) if item.node_id else None
        issues = open_issues(qc)
        items.append({
            "item_id": item.id,
            "node_id": item.node_id,
            "node_key": (node.node_key if node is not None
                         else str((item.meta or {}).get("node_key") or "")),
            "title": item.title,
            "score": int(qc.get("score") or 0),
            "metrics": qc.get("metrics") or {},
            "issues": [{"key": c.get("key"), "level": c.get("level"),
                        "detail": c.get("detail")} for c in issues],
            "failed": [{"key": c.get("key"), "detail": c.get("detail")}
                       for c in issues if c.get("level") == "fail"],
            "warned": [{"key": c.get("key"), "detail": c.get("detail")}
                       for c in issues if c.get("level") == "warn"],
            "locked": bool((item.meta or {}).get("locked")),
            # P5-1：这篇有几项没执行（概念库为空时术语比对不参与计分）
            "skipped_count": int(qc.get("skipped_count") or 0),
            "skipped_keys": list(qc.get("skipped_keys") or []),
            "executed_checks": int(qc.get("executed_checks") or len(CHECK_KEYS)),
            "total_checks": int(qc.get("total_checks") or len(CHECK_KEYS)),
            # 任务 52：这一档里「按提示处理、不扣分」的项（判据没放宽，只是不当问题）
            "downgraded": [{"key": c.get("key"), "hint": c.get("hint") or c.get("detail")}
                           for c in downgraded],
        })
    scores = [row["score"] for row in items]
    worst = sorted(items, key=lambda row: (row["score"], row["node_key"]))[:worst_n]
    return {
        "course_id": course.id,
        "course_title": course.title,
        # 任务 112：这份分数是按哪套判据算的（验收单 / 体检报告都标注这一行）
        "rules": rules_label(),
        # 任务 52：当前档位（前端徽标与报告页脚显示「这条为什么不算问题」）
        "profile": qc_profile.resolve(course),
        "downgraded_items": downgraded_items,
        "downgraded_notice": (
            f"{downgraded_items} 篇有按提示处理的项（本档位不强制该判据，未计入扣分）；"
            "判据本身没有放宽。" if downgraded_items else ""),
        "checked": len(items),
        "average_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "min_score": min(scores) if scores else 0,
        "max_score": max(scores) if scores else 0,
        "checks": [{"key": key, **counts[key]} for key in CHECK_KEYS],
        # 课程级：有多少篇存在「未执行」的检查项（分数已按已执行项归一）
        "skipped_items": skipped_items,
        "skipped_items_notice": (
            f"{skipped_items} 篇有未执行项（多为概念库为空时的术语比对），"
            "这些项的分数按已执行项归一，未计入分母。" if skipped_items else ""),
        "failure_distribution": [
            {"key": key, "warn": counts[key]["warn"], "fail": counts[key]["fail"],
             "failed": counts[key]["warn"] + counts[key]["fail"],
             "skipped": counts[key][SKIPPED_LEVEL]}
            for key in CHECK_KEYS
        ],
        "items": items,
        "worst": worst,
        # 任务 54：OCR 的素材级提示（低置信度 / 未装组件 / 识别失败）。
        # 刻意不并入 10 项判据（不动 CHECK_KEYS / 权重 / 分母），由前端单独显示。
        "material_notices": ocr_notices(db, course),
        "generated_at": _now_iso(),
    }


def open_issues(qc: dict | None) -> list[dict]:
    """质检里所有 warn/fail 项（重生成提示词与前端明细共用）。"""
    return [c for c in ((qc or {}).get("checks") or [])
            if c.get("level") in ("warn", "fail")]


def prompt_section(qc: dict | None) -> str:
    """把质检问题注入「按质检建议重生成」的提示词；没有问题返回空串。"""
    issues = open_issues(qc)
    if not issues:
        return ""
    issues.sort(key=lambda c: (-_LEVEL_ORDER.get(c.get("level"), 0),
                               CHECK_KEYS.index(c["key"]) if c.get("key") in CHECK_KEYS else 99))
    lines = ["\n\n=== 上一版讲义质检问题（本次重生成必须逐条修正）==="]
    for check in issues:
        lines.append(f"- [{check.get('key')}/{check.get('level')}] {check.get('detail')}")
    lines.append("要求：逐条修正上述问题；保留原稿中已经合格的部分；不要新增空话套话。")
    return "\n".join(lines)
