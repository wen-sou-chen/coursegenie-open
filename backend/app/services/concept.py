# -*- coding: utf-8 -*-
"""原子笔记 v1「概念库」：一个基础概念一篇、双链织网、来源可追溯。

设计要点（HANDOFF 任务 15 / 规划文档 §6）：

1. 独立 ``概念库/`` 目录，与 00-概览、章节、习题集、图表库、素材库平级；属受管区，
   完全沿用 manifest + ``cg_fingerprint`` + preserved 语义（手改过的概念页不会被覆盖）。
2. **一个概念一篇**（单一真相源）：跨章复用的概念只追加 ``chapters``，不重复建篇；
   同名与 ``aliases`` 归一后合并。
3. 概念之间用 ``[[双链]]`` 织网：只对「确实存在于概念库」的名字发链接，未收录的关系词
   以纯文本列出——保证导出后双链 0 坏链。
4. 来源可追溯：每条来源都指向真实存在的讲义页 / 大纲节点 / 素材页。
5. 稳定业务 id（``cpt-<sha1>``）：为 M5 的「意见挂在某个概念上」与版本 diff 做前向兼容。
6. 抽取失败**显式上报**（:class:`ConceptExtractionError`），绝不静默产出空概念库。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, OutlineNode
from app.services import usage as usage_log
from app.services.llm import build_llm_client
from app.services.material import list_materials
from app.services.planner import list_nodes

CONCEPT_KIND = "concept"
CONCEPTS_DIR = "概念库"

MAX_CONCEPTS = 40
MAX_POINTS = 5
MAX_RELATIONS = 8
MAX_FIGURES = 2
QUOTE_LIMIT = 140

# 关系分类（顺序即渲染顺序）
RELATION_SECTIONS = (("broader", "上位"), ("narrower", "下位"), ("related", "相关"))

SOURCE_LABELS = {
    "lecture": "讲义",
    "outline": "大纲",
    "material": "素材",
}

# 概念质量约束：与章节/知识点标题同名、像句子、结构停用词都不是概念（噪声来源）。
STOPWORDS = {
    "概念", "要点", "知识点", "章节", "内容", "目标", "学习目标", "小结", "总结",
    "练习", "作业", "例题", "引言", "介绍", "概述", "目录", "注意事项", "常见误区",
}
MAX_NAME_CHARS = 8
MAX_ASCII_WORDS = 3
# 任务 41 准入（借鉴仓颉.skill 的「跨域复现」）：只在 ≥2 个知识点出现过的名字
# 才升级为概念页；只出现一次的术语记进 ``course.meta["terminology"]``，不建页。
MIN_CONCEPT_POINTS = 2
TERMINOLOGY_KEY = "terminology"

SYSTEM = """CONCEPT_JSON 你是课程设计专家，负责把一门课里「最原始的基本概念」整理成原子笔记。

要求：
1. 只抽取基础概念（术语、原理、要素），不要抽取章节标题或操作步骤；
2. 概念之间要织网：broader 上位概念 / narrower 下位概念 / related 相关概念；
3. **每个概念都必须给 definition（一句话定义，用读者能懂的白话）**，再给 3~5 条要点、1~3 条常见误区；
4. chapters 填这个概念出现在哪些知识点 key（如 "1.1"），跨章复用就都列上；
5. sources 填依据来源：kind 取 lecture（讲义）/ outline（大纲）/ material（素材），
   ref 填知识点 key 或素材标题，quote 填依据的原文片段（可截断）；
6. 关系里出现的名字，尽量也在 concepts 里给出定义，避免悬空链接。

7. 提取优先级（重要）：优先抽「素材里才有、通识资料里查不到」的东西 ——
   ① 素材里的一手案例（谁做过、在什么场景）；② 失败教训与返工（踩过的坑）；
   ③ 能力边界（什么情况不适用）；④ 内在矛盾（两种做法都有人坚持）。
   definitions / points 里的通用观点必须能追到素材出处（写进 sources.quote），
   追不到的通用观点不要写。
8. pitfalls 只写素材里真实出现过的误区 / 返工 / 误判；素材没有就给空数组，
   不要写「常见误区」这类放之四海皆准的话。
9. 准入：只有在本课 **≥2 个知识点**都出现过的名字才作为概念输出；
   只出现一次的术语不要输出（系统会单独记进课程 meta.terminology，不建概念页）。
10. 抽取范围包含**大纲的 teaching_strategy / assessment / objective 字段**：
    这些字段里出现的专有名词（模型名、算法名、方法名、缩写）同样是学生要理解的东西，
    一律要抽出来并给 definition —— 它们常常只出现在一个知识点里（系统会自动记进
    课程 meta.terminology，不建概念页）。**没有 definition 的候选视为不合格**：
    宁可不输出，也不要给空定义或「待补充」。

只输出 JSON，结构：
{"concepts": [{"name": "乳酸菌", "aliases": ["LAB"], "definition": "一句话定义",
  "points": ["要点1", "要点2", "要点3"], "broader": ["发酵"], "narrower": ["酵母菌"],
  "related": ["面筋网络"], "pitfalls": ["常见误区"], "chapters": ["1.1"],
  "sources": [{"kind": "lecture", "ref": "1.1", "quote": "原文片段"}]}]}
"""


SUPPLEMENT_SYSTEM = """CONCEPT_JSON 你是课程设计专家。老师已经确认，下面这些词是本课必须入库的基础概念，
请为它们补齐定义与关系。

要求：
1. 每个词给一句话定义、3~5 条要点、1~3 条常见误区；
2. broader / narrower / related 里**优先引用「已有概念」里的名字**（这些名字已确定存在），让概念之间真正织成网；
3. chapters 填它出现在哪些知识点 key（参考「知识点清单」）；
4. 只输出 JSON，结构：
{"concepts": [{"name": "关键词", "aliases": [], "definition": "一句话定义",
  "points": ["要点1", "要点2"], "broader": [], "narrower": [], "related": [],
  "pitfalls": [], "chapters": ["1.1"], "sources": []}]}
"""


class ConceptExtractionError(RuntimeError):
    """概念抽取失败（LLM 报错 / 返回空 / 结构不可用）——必须显式上报给用户。"""


# ---------------------------------------------------------------------------
# 名字归一与稳定 id
# ---------------------------------------------------------------------------

_PUNCT = set("_-—–·。，,.;；:：、!！?？()（）[]【】<>《》“”\"'’‘`~*#|/\\+=")


def normalize_key(text: str) -> str:
    """概念名归一：全角转半角、去空白与标点、统一小写（同名/别名合并的依据）。"""
    out: list[str] = []
    for ch in str(text or "").strip().lower():
        code = ord(ch)
        if code == 0x3000:
            ch = " "
        elif 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        if ch.isspace() or ch in _PUNCT:
            continue
        out.append(ch)
    return "".join(out)


def concept_id_for(name: str) -> str:
    """稳定业务 id：同一个概念名永远得到同一个 id（M5 意见挂载 / 版本 diff 用）。"""
    digest = hashlib.sha1(normalize_key(name).encode("utf-8")).hexdigest()
    return f"cpt-{digest[:12]}"


def chapter_key_of(node_key: str) -> str:
    """知识点 key → 所属章 key（"1.2" → "1"；章节点 "2" → "2"）。"""
    return str(node_key or "").split(".")[0]


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SENTENCE_RE = re.compile(r"[，。！？；、,.!?;…—]")
# 章号前缀：1- / 1.2- / 1、/ 第 3 章 —— 双链目标要按「去掉前缀后的真实文件名」解析
_CHAPTER_PREFIX_RE = re.compile(
    r"^\s*(?:第\s*)?[0-9０-９]+(?:\s*[.．、\-—–]\s*[0-9０-９]+)*\s*[-—–.．、)）:：]\s*(?=\S)"
)
_CHAPTER_WORD_RE = re.compile(r"^\s*第\s*[0-9０-９]+\s*[章节节]\s*(?=\S)")
_WRAPPER_RE = re.compile(r"^\s*[《【\[（(](.+?)[》】\]）)]\s*$")


def strip_chapter_prefix(text: str) -> str:
    """去掉「章号前缀」（``1-`` / ``1.2-`` / ``第 3 章``）；没有前缀就原样返回。"""
    out = str(text or "").strip()
    for pattern in (_CHAPTER_PREFIX_RE, _CHAPTER_WORD_RE):
        match = pattern.match(out)
        if match and out[match.end():].strip():
            out = out[match.end():].strip()
    return out


def _strip_wrapper(text: str) -> str:
    match = _WRAPPER_RE.match(str(text or ""))
    return match.group(1).strip() if match else str(text or "").strip()


def name_variants(text: str) -> list[str]:
    """一个双链目标的候选写法：原样 → 去章号前缀 → 去书名号包裹。"""
    out: list[str] = []
    for candidate in (text, strip_chapter_prefix(text),
                      _strip_wrapper(strip_chapter_prefix(text)), _strip_wrapper(text)):
        cand = str(candidate or "").strip()
        if cand and cand not in out:
            out.append(cand)
    return out


def resolve_target(name: str, index: dict, valid: set | None = None) -> str | None:
    """把关系/来源里的名字解析成真实存在的笔记文件名；解析不到返回 None（不写双链）。"""
    for candidate in name_variants(name):
        target = (index or {}).get(normalize_key(candidate))
        if target and (valid is None or target in valid):
            return target
    return None


def name_reject_reason(name: str, blocked: set, *, strict_terms: bool = True) -> str | None:
    """概念候选不达标的原因（None = 通过）：排除标题同名、句子、停用词与非名词性术语。

    ``strict_terms=False`` 用于「补抽」（老师点名要的概念）：跳过「目标/属性短语」这条
    自动判据 —— 人工确认优先于自动准入，不被自动规则挡回去。
    """
    raw = str(name or "").strip()
    norm = normalize_key(raw)
    if len(norm) < 2:
        return "太短（不足 2 字）"
    if norm in blocked:
        return "与章节/知识点标题同名"
    if norm in STOPWORDS:
        return "停用词（结构词，不是概念）"
    if _SENTENCE_RE.search(raw):
        return "像句子而不是术语（含句读符号）"
    if strict_terms:
        goal = goal_phrase_reason(raw)
        if goal:
            return goal
    if _CJK_RE.search(raw):
        if len(norm) > MAX_NAME_CHARS:
            return f"过长（超过 {MAX_NAME_CHARS} 字）"
        return None
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]*", raw)
    if not words or len(words) > MAX_ASCII_WORDS or sum(len(w) for w in words) > 24:
        return "不是名词性术语（词数或长度异常）"
    return None


def concept_name_ok(name: str, blocked: set) -> bool:
    """概念名是否满足名词性术语约束（标题同名 / 句子 / 停用词一律不是概念）。"""
    return name_reject_reason(name, blocked) is None


# 目标/属性短语 vs 术语名词（任务 52，DSH 走查缺陷 2）
# 真机现场：概念库把「字音准确」建成概念页并配别名 [翘舌音, 平舌音]，
# 于是质检建议老师把「注意『霜』是翘舌音」改写成「注意『霜』是字音准确」——
# **照做会把内容改坏**。根因是「字音准确」是目标/属性短语，根本不是术语名词。
_ATTRIBUTE_SUFFIXES = ("准确", "流利", "规范", "正确", "熟练", "清晰", "到位", "达标",
                       "美观", "工整", "顺畅", "得体", "严谨", "扎实", "牢固", "提高")
_GOAL_HEADS = ("掌握", "学会", "能够", "能", "会", "提高", "培养", "认识", "体会",
               "感受", "了解", "理解", "熟悉", "运用", "应用", "灵活")
ALIAS_MIN_OVERLAP = 2      # 同义判定：与标准名至少共享 2 个汉字（或共享词头/词尾）


def goal_phrase_reason(name: str) -> str | None:
    """是不是「教学目标/属性」短语（而不是名词性术语）——是则返回原因。"""
    raw = str(name or "").strip()
    if not _CJK_RE.search(raw):
        return None
    if any(raw.endswith(suffix) for suffix in _ATTRIBUTE_SUFFIXES):
        return "是目标/属性短语（形容词性），不是术语名词"
    if any(raw.startswith(head) for head in _GOAL_HEADS):
        return "是目标短语（以「掌握/学会/能够…」开头），不是术语名词"
    return None


def alias_reject_reason(name: str, alias: str) -> str | None:
    """别名是不是「同义的不同写法」——不是则返回原因（None = 收下）。

    别名会被质检用来建议老师改写正文，所以**宁可少收，不可收错**：
    上下位/相关关系（翘舌音 vs 字音准确）一旦当同义，质检就会建议把内容改坏。
    判据：① 别名本身要是名词性术语；② 与标准名共享词头或词尾，或共享 ≥2 个汉字
    （中文同义词几乎都共享汉字：五言诗/五言、换词法/替换法）；③ 双方都是 ASCII 缩写时放过。
    **一中文一英文**（叶绿体 / chloroplast、发酵 / fermentation）只能放过：字符重叠
    判不出跨语言同义，硬挡会把既有概念库的正确别名打掉。
    """
    raw_name, raw_alias = str(name or "").strip(), str(alias or "").strip()
    if not raw_name or not raw_alias:
        return "别名为空"
    if normalize_key(raw_name) == normalize_key(raw_alias):
        return "别名与标准名相同"
    reason = name_reject_reason(raw_alias, set())
    if reason:
        return f"别名不是名词性术语：{reason}"
    if not (_CJK_RE.search(raw_name) or _CJK_RE.search(raw_alias)):
        return None
    name_chars, alias_chars = normalize_key(raw_name), normalize_key(raw_alias)
    if not (_CJK_RE.search(raw_name) and _CJK_RE.search(raw_alias)):
        # 一中文一英文：字符重叠判不出跨语言同义（chloroplast vs 叶绿体 就是翻译），放过
        return None
    head, tail = name_chars[:2], name_chars[-2:]
    overlap = len(set(alias_chars) & set(name_chars))
    if alias_chars.startswith(head) or alias_chars.endswith(tail):
        return None
    if overlap >= ALIAS_MIN_OVERLAP:
        return None
    return (f"与标准名「{raw_name}」看不出同义（只共享 {overlap} 个字，"
            "更像上下位/相关关系）")


# ---------------------------------------------------------------------------
# 语料：讲义 + 大纲 + 素材
# ---------------------------------------------------------------------------

def _trim_lines(md: str, *, limit: int = 30, width: int = 160) -> tuple[list[str], list[str]]:
    """讲义 markdown → (小标题, 要点行)；丢掉代码块与长段落（抽取只吃结构化信息）。"""
    headings: list[str] = []
    points: list[str] = []
    in_fence = False
    for raw in str(md or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title and len(headings) < limit:
                headings.append(title[:width])
            continue
        if stripped.startswith(("- ", "* ", "> ")):
            text = stripped[2:].strip()
            if text and len(points) < limit:
                points.append(text[:width])
    return headings, points


def _excerpt(text: str, limit: int = 240) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[:limit]


def collect_corpus(db: Session, course: Course) -> dict:
    """抽取用语料：每个知识点的标题/目标/**策略/评估** + 讲义结构 + 素材摘要（同时可作来源）。

    任务 58：``teaching_strategy`` / ``assessment`` 以前完全不进语料，导致「教学策略:
    马尔可夫链预测演示」这种**大纲里自造的术语必然漏网**（正文里根本不会出现）。
    """
    nodes = list_nodes(db, course.id)
    lectures: dict[str, ContentItem] = {}
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())
    for row in rows:
        if row.node_id:
            lectures[row.node_id] = row

    units: list[dict] = []
    for node in nodes:
        lecture = lectures.get(node.id)
        md = str((lecture.body or {}).get("markdown") or "") if lecture else ""
        headings, points = _trim_lines(md)
        strategy = str(node.teaching_strategy or "")[:200]
        assessment = str(node.assessment or "")[:200]
        units.append({
            "key": node.node_key,
            "title": node.title,
            "objective": str(node.objective or "")[:200],
            "strategy": strategy,
            "assessment": assessment,
            "chapter": chapter_key_of(node.node_key),
            "headings": headings,
            "points": points,
            "has_lecture": lecture is not None,
            "lecture_item_id": lecture.id if lecture is not None else None,
        })
    materials = [{"title": m.title, "excerpt": _excerpt(m.text)} for m in list_materials(db, course.id)]
    return {"nodes": units, "materials": materials}


def target_concept_count(corpus: dict) -> int:
    """抽取规模自适应（P6-1：27 节只抽出 9 个概念，整块内容在图上是孤点）。

    口径：按「已写讲义的知识点数」算（1 节 ≈ 1 个概念，再加固定基数），
    下限 12、上限仍受 ``MAX_CONCEPTS`` 约束。这个数只是「约多少」的提示，
    不是硬指标：曾出现「为了凑数编造概念」的风险更大。
    """
    lectures = sum(1 for unit in corpus.get("nodes") or [] if unit.get("has_lecture"))
    base = round(1.2 * lectures) + 4
    return max(12, min(MAX_CONCEPTS, base))


def _prompt(course: Course, corpus: dict) -> str:
    lines = [
        f"课程：《{course.title}》（受众：{course.audience or '未指定'}；课时 {course.duration_minutes} 分钟）",
        "",
        "【知识点清单】",
    ]
    for unit in corpus["nodes"]:
        lines.append(
            f"- key={unit['key']} | title={unit['title']} | objective={unit['objective'] or '—'}"
            f" | teaching_strategy={unit['strategy'] or '—'} | assessment={unit['assessment'] or '—'}")
    lines += ["", "【讲义结构与要点】"]
    for unit in corpus["nodes"]:
        lines.append(f"## {unit['key']} {unit['title']}")
        for head in unit["headings"]:
            lines.append(f"- {head}")
        for point in unit["points"]:
            lines.append(f"  · {point}")
    if corpus["materials"]:
        lines += ["", "【教师素材】"]
        for material in corpus["materials"]:
            lines.append(f"## {material['title']}")
            if material["excerpt"]:
                lines.append(material["excerpt"])
    lectures = sum(1 for unit in corpus["nodes"] if unit["has_lecture"])
    lines += [
        "",
        f"本课有 {len(corpus['nodes'])} 个知识点（其中 {lectures} 个已写讲义）。"
        f"请抽出约 {target_concept_count(corpus)} 个基础概念：覆盖率优先，"
        "宁可多列，也不要把整块内容漏掉；但不要为了凑数编造概念。",
        "",
        "请按系统提示的 JSON 结构输出概念清单（只输出 JSON）。",
    ]
    return "\n".join(lines)


def _supplement_prompt(course: Course, corpus: dict, wanted: list[str],
                       existing: dict[str, str]) -> str:
    """补抽提示词：把「库外关系目标 / 孤点」名单交给模型。"""
    lines = [
        f"课程：《{course.title}》（受众：{course.audience or '未指定'}）",
        "",
        "【需要补齐的概念】",
    ]
    lines += [f"- {name}" for name in wanted]
    if existing:
        lines += ["", "【已有概念（关系里请优先引用这些名字）】",
                  "、".join(existing.values())]
    lines += ["", "【知识点清单】"]
    lines += [f"- key={unit['key']} | title={unit['title']}" for unit in corpus["nodes"]]
    lines += ["", "请按系统提示的 JSON 结构输出这些概念（只输出 JSON）。"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 抽取与合并
# ---------------------------------------------------------------------------

def _str_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _blank_concept(name: str) -> dict:
    return {
        "name": name,
        "aliases": [],
        "definition": "",
        "points": [],
        "relations": {"broader": [], "narrower": [], "related": []},
        "pitfalls": [],
        "chapters": [],
        "sources": [],
        "concept_id": concept_id_for(name),
    }


def _merge_relations(concept: dict, item: dict) -> None:
    for key, _label in RELATION_SECTIONS:
        bucket = concept["relations"][key]
        seen = {normalize_key(x) for x in bucket}
        for name in _str_list(item.get(key)):
            norm = normalize_key(name)
            if norm and norm not in seen:
                bucket.append(name)
                seen.add(norm)
        del bucket[MAX_RELATIONS:]


def _node_texts(corpus: dict) -> dict[str, str]:
    """每个知识点的比对文本（标题 + 目标 + 策略/评估 + 讲义小节 + 要点），用于「出现过」判定。"""
    out: dict[str, str] = {}
    for unit in corpus.get("nodes") or []:
        key = str(unit.get("key") or "")
        if not key:
            continue
        parts = [str(unit.get("title") or ""), str(unit.get("objective") or ""),
                 str(unit.get("strategy") or ""), str(unit.get("assessment") or "")]
        parts += [str(x) for x in (unit.get("headings") or [])]
        parts += [str(x) for x in (unit.get("points") or [])]
        out[key] = normalize_key(" ".join(parts))
    return out


def concept_appearances(name: str, aliases: list[str], node_texts: dict[str, str]) -> list[str]:
    """名字（含别名）在哪些知识点的标题/要点里真的出现过（任务 41 准入依据）。"""
    words = [normalize_key(name)] + [normalize_key(a) for a in aliases or []]
    words = [w for w in words if len(w) >= 2]
    if not words:
        return []
    hits = [key for key, text in node_texts.items()
            if text and any(word in text for word in words)]
    return sorted(hits)


_PAREN_OPEN = {"（": "）", "(": ")"}
DEFINITION_MAX = 160


def _usable_definition(text: str) -> bool:
    """够长、且真的含中文说明（挡掉「见上文」「略」这类占位，别把占位当解释）。"""
    if not (6 <= len(text) <= DEFINITION_MAX):
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def definition_from_context(term: str, *texts: str) -> str:
    """从大纲字段里给术语捞一句现成的白话解释（``术语（解释）`` 的括号内容）。

    任务 58：大纲提示词要求「确需专业术语时同一行紧跟一句白话解释」⇒ 策略/评估字段
    常常自带解释。命中就把它当 ``definition``（0 LLM、可追溯到原文），命中不了返回空串，
    由调用方显式上报「缺定义」——不编定义。
    """
    name = str(term or "").strip()
    if not name:
        return ""
    for text in texts:
        blob = str(text or "")
        start = 0
        while True:
            at = blob.find(name, start)
            if at < 0:
                break
            tail = blob[at + len(name):].lstrip()
            if tail[:1] in _PAREN_OPEN:
                end = tail.find(_PAREN_OPEN[tail[0]], 1)
                inner = tail[1:end].strip() if end > 0 else ""
                if _usable_definition(inner):
                    return inner
            # 「术语 + 演示（解释）」这种写法：解释在同一行的括号里（提示词要求同一行紧跟）
            line_start = blob.rfind("\n", 0, at) + 1
            line_end = blob.find("\n", at)
            line = blob[line_start:line_end if line_end >= 0 else len(blob)]
            match = re.search(r"[（(]([^）)]{6,})[）)]", line)
            if match and _usable_definition(match.group(1).strip()):
                return match.group(1).strip()
            start = at + len(name)
    return ""


def outline_context(corpus: dict) -> str:
    """大纲描述字段拼成的上下文（给术语找解释用）：标题 / 目标 / 策略 / 评估。"""
    parts: list[str] = []
    for unit in corpus.get("nodes") or []:
        parts += [str(unit.get(key) or "")
                  for key in ("title", "objective", "strategy", "assessment")]
    return "\n".join(parts)


def terminology_entries(dropped: list[dict] | None) -> list[dict]:
    """从 dropped 里挑出「只出现过一次」的术语（不建概念页，记 meta.terminology）。"""
    out: list[dict] = []
    for item in dropped or []:
        if not isinstance(item, dict) or item.get("kind") != TERMINOLOGY_KEY:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "aliases": _str_list(item.get("aliases")),
            "chapters": _str_list(item.get("chapters")),
            "definition": str(item.get("definition") or "").strip(),
        })
    return out


def terminology_only(course: Course) -> list[dict]:
    """课程级「只出现一次、未升级为概念页」的术语清单（``course.meta.terminology``）。"""
    rows = (course.meta or {}).get(TERMINOLOGY_KEY)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def record_terminology(db: Session, course: Course, entries: list[dict],
                       *, promoted: list[str] | None = None) -> list[dict]:
    """把「只出现一次」的术语记进 ``course.meta.terminology``（同名合并，可追溯）。

    ``promoted`` 是这次升级成概念页的名字：命中的旧术语条目会被清掉，避免
    「已经建页了还挂在术语表里」。
    """
    if not entries and not promoted:
        return terminology_only(course)
    merged: "OrderedDict[str, dict]" = OrderedDict()
    for row in terminology_only(course):
        key = normalize_key(str(row.get("name") or ""))
        if key:
            merged[key] = dict(row)
    for name in promoted or []:
        merged.pop(normalize_key(name), None)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for entry in entries:
        key = normalize_key(entry["name"])
        if not key:
            continue
        old = merged.get(key) or {}
        chapters = set(_str_list(entry.get("chapters"))) | set(_str_list(old.get("chapters")))
        merged[key] = {
            "name": entry["name"],
            "aliases": _str_list(entry.get("aliases")) or _str_list(old.get("aliases")),
            "chapters": sorted(chapters),
            "definition": str(entry.get("definition") or old.get("definition") or ""),
            "first_seen": old.get("first_seen") or at,
            "at": at,
        }
    rows = list(merged.values())
    course.meta = {**(course.meta or {}), TERMINOLOGY_KEY: rows}
    db.commit()
    return rows


def merge_raw(raw: list, corpus: dict, dropped: list | None = None,
              *, enforce_admission: bool = True) -> list[dict]:
    """把 LLM 输出规整成「一概念一篇」：同名/别名合并，只保留合法的章节与来源。

    ``dropped``（可选）收集被质量约束过滤掉的候选，供调用方显式上报，不静默丢弃。
    ``enforce_admission=False`` 用于「补抽」：老师点名要的概念即便只出现在 1 个
    知识点也照建（人工确认优先于自动准入）。
    """
    valid = {unit["key"] for unit in corpus["nodes"]}
    materials = {str(m["title"]) for m in corpus["materials"]}
    context = outline_context(corpus)
    blocked = {normalize_key(unit["title"]) for unit in corpus["nodes"]}
    by_key: "OrderedDict[str, dict]" = OrderedDict()
    alias_index: dict[str, str] = {}

    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        reason = name_reject_reason(name, blocked)
        if reason and not enforce_admission:
            # 补抽：老师点名的概念只做「像不像句子/太长/同名」这类硬校验
            reason = name_reject_reason(name, blocked, strict_terms=False)
        if reason:
            if dropped is not None and all(d.get("name") != name for d in dropped):
                dropped.append({"name": name, "reason": reason})
            continue
        aliases: list[str] = []
        for candidate in _str_list(item.get("aliases")):
            if normalize_key(candidate) == normalize_key(name):
                continue
            # 任务 52：别名必须是**同义的不同写法**；上下位/相关关系当同义会让质检
            # 给出「把翘舌音改成字音准确」这种把内容改坏的建议（DSH 走查缺陷 2）。
            alias_reason = alias_reject_reason(name, candidate)
            if alias_reason:
                if dropped is not None:
                    dropped.append({"name": candidate, "kind": "alias", "of": name,
                                    "reason": alias_reason})
                continue
            if candidate not in aliases:
                aliases.append(candidate)
        keys = [normalize_key(name)] + [normalize_key(a) for a in aliases]
        keys = [k for k in keys if k]
        slot = next((alias_index[k] for k in keys if k in alias_index), None)
        if slot is None:
            slot = normalize_key(name)
            by_key[slot] = _blank_concept(name)
        concept = by_key[slot]
        for alias in aliases:
            if alias not in concept["aliases"]:
                concept["aliases"].append(alias)
        for k in keys:
            alias_index.setdefault(k, slot)

        definition = re.sub(r"\s+", " ", str(item.get("definition") or "")).strip()
        if definition and not concept["definition"]:
            concept["definition"] = definition
        if not concept["points"]:
            concept["points"] = _str_list(item.get("points"))[:MAX_POINTS]
        seen_pitfalls = {normalize_key(p) for p in concept["pitfalls"]}
        for pit in _str_list(item.get("pitfalls"))[:3]:
            if normalize_key(pit) not in seen_pitfalls:
                concept["pitfalls"].append(pit)
                seen_pitfalls.add(normalize_key(pit))
        _merge_relations(concept, item)

        for key in _str_list(item.get("chapters")):
            if key in valid and key not in concept["chapters"]:
                concept["chapters"].append(key)
        for src in item.get("sources") or []:
            if not isinstance(src, dict):
                continue
            kind = str(src.get("kind") or "").strip()
            ref = str(src.get("ref") or "").strip()
            quote = _excerpt(src.get("quote"), QUOTE_LIMIT)
            if kind not in SOURCE_LABELS:
                continue
            if kind == "material" and ref not in materials:
                continue
            if kind in ("lecture", "outline") and ref not in valid:
                continue
            entry = {"kind": kind, "ref": ref, "quote": quote}
            if entry not in concept["sources"]:
                concept["sources"].append(entry)

    node_texts = _node_texts(corpus)
    concepts: list[dict] = []
    for concept in by_key.values():
        # 任务 41 准入：LLM 声明的 chapters ∪ 语料里真的出现过的知识点，
        # 不足 MIN_CONCEPT_POINTS 个 → 记进 meta.terminology，不建概念页。
        evidence = sorted(set(concept["chapters"])
                          | set(concept_appearances(concept["name"], concept["aliases"], node_texts)))
        if enforce_admission and len(evidence) < MIN_CONCEPT_POINTS:
            if dropped is not None and all(d.get("name") != concept["name"] for d in dropped):
                dropped.append({
                    "name": concept["name"],
                    "kind": TERMINOLOGY_KEY,
                    "reason": (f"只出现在 {len(evidence)} 个知识点"
                               f"（不到 {MIN_CONCEPT_POINTS} 个）：记入 meta.terminology，不建概念页"),
                    "aliases": list(concept["aliases"]),
                    "chapters": evidence,
                    # 任务 58：术语条目**必须带 definition** —— 模型没写就从大纲字段里
                    # 捞现成的括号解释（0 LLM）；仍捞不到就留空，由调用方显式上报。
                    "definition": concept["definition"] or definition_from_context(
                        concept["name"], context),
                })
            continue
        concept["chapters"] = evidence or list(concept["chapters"])
        concept["chapters"].sort()
        if not concept["definition"]:
            concept["definition"] = (definition_from_context(concept["name"], context)
                                     or "（网页端待补充定义）")
        if not concept["points"]:
            concept["points"] = ["（网页端待补充要点）"]
        # 来源兜底：至少要能追到讲义/大纲，保证「可追溯」
        have = {(s["kind"], s["ref"]) for s in concept["sources"]}
        for key in concept["chapters"]:
            if ("lecture", key) not in have and ("outline", key) not in have:
                concept["sources"].append({"kind": "outline", "ref": key, "quote": ""})
        concept["sources"].sort(key=lambda s: (s["kind"], s["ref"]))
        concepts.append(concept)

    concepts.sort(key=lambda c: (c["chapters"][0] if c["chapters"] else "zz", c["name"]))
    return concepts[:MAX_CONCEPTS]


def extract(db: Session, course: Course, llm=None) -> tuple[list[dict], str, list[dict]]:
    """抽取概念（LLM）。任何失败都抛 ConceptExtractionError，调用方显式上报。"""
    corpus = collect_corpus(db, course)
    if not corpus["nodes"]:
        raise ConceptExtractionError("尚无大纲节点：请先生成并通过大纲，再抽取概念")
    try:
        client = llm or build_llm_client()
        data = usage_log.complete_json(client, SYSTEM, _prompt(course, corpus),
                                       max_tokens=8000, purpose="concept")
    except json.JSONDecodeError as exc:
        # 任务 63：坏 JSON 的失败原因直接用它自己的消息（含位置与现场片段），
        # 不再套「LLM 调用失败：」，免得前端看到的还是「Expecting ',' delimiter」。
        raise ConceptExtractionError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 统一转成显式失败
        raise ConceptExtractionError(f"LLM 调用失败：{exc}") from exc
    raw = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ConceptExtractionError("LLM 未返回任何概念（concepts 为空），本次不写入概念库")
    dropped: list[dict] = []
    concepts = merge_raw(raw, corpus, dropped)
    # 任务 41：没升级成概念页的术语也要有落点（course.meta.terminology），
    # 「抽不到概念」时同样先记下来再报错，不白跑一次 LLM。
    record_terminology(db, course, terminology_entries(dropped),
                       promoted=[c["name"] for c in concepts])
    if not concepts:
        raise ConceptExtractionError(
            f"LLM 返回的 {len(raw)} 个候选全部不达标"
            "（与章节/知识点标题同名、像句子、是停用词，或只在 1 个知识点出现过"
            "——后者已记入课程 meta.terminology），本次不写入概念库"
        )
    return concepts, str(getattr(client, "name", "") or ""), dropped


# ---------------------------------------------------------------------------
# 落库（幂等）
# ---------------------------------------------------------------------------

def _row_body(concept: dict) -> dict:
    return {
        "definition": concept["definition"],
        "points": concept["points"],
        "relations": concept["relations"],
        "pitfalls": concept["pitfalls"],
        "sources": concept["sources"],
    }


def persist(db: Session, course: Course, concepts: list[dict], *, prune: bool = True) -> dict:
    """按稳定 concept_id 原地更新：重跑不重复建篇；教师手改过的条目保留不覆盖。

    ``prune=False`` 用于「补抽」：只动这次提到的概念，绝不能把概念库其余条目当作“本次未用到”而删掉。
    """
    existing: dict[str, ContentItem] = {}
    for row in list_concepts(db, course.id):
        cid = str((row.meta or {}).get("concept_id") or "")
        if cid:
            existing[cid] = row
    created = updated = kept = removed = 0
    used: set[str] = set()
    for concept in concepts:
        cid = str(concept["concept_id"])
        used.add(cid)
        body = _row_body(concept)
        meta = {
            "concept_id": cid,
            "aliases": concept["aliases"],
            "chapters": concept["chapters"],
            "edited": False,
        }
        row = existing.get(cid)
        if row is None:
            db.add(ContentItem(course_id=course.id, node_id=None, kind=CONCEPT_KIND,
                               title=concept["name"], body=body, meta=meta))
            created += 1
            continue
        if (row.meta or {}).get("edited"):
            kept += 1
            continue
        row.title = concept["name"]
        row.body = body
        row.meta = {**meta, "first_seen": (row.meta or {}).get("first_seen")}
        updated += 1
    for cid, row in existing.items():
        if cid not in used and prune:
            db.delete(row)
            removed += 1
    db.commit()
    return {"created": created, "updated": updated, "kept_edited": kept, "removed": removed}


def generate_concepts(db: Session, course: Course, llm=None) -> dict:
    """抽取 → 落库；返回可上报的结果（含失败时抛出的显式异常）。"""
    # 概念抽取这一次调用原先没有采集器 → token/费用被静默丢弃（P6-2 / P9-3）
    with usage_log.collect() as collector:
        concepts, model, dropped = extract(db, course, llm)
    report = persist(db, course, concepts)
    summary = collector.summary()
    usage_log.record_aux(course, "concept", summary)
    db.commit()
    return {
        "ok": True,
        "model": model,
        "concepts": len(concepts),
        "names": [c["name"] for c in concepts],
        "filtered": dropped,
        "filtered_count": len(dropped),
        # 任务 41：只出现一次、未升级为概念页的术语（course.meta.terminology）
        "terminology": [row["name"] for row in terminology_only(course)],
        "terminology_count": len(terminology_only(course)),
        "usage": summary,
        **report,
    }


def supplement_concepts(db: Session, course: Course, names: list[str], llm=None) -> dict:
    """补抽指定概念（P6-1）：库外关系目标、孤点都可以用这一步补上。

    与 ``generate_concepts`` 的区别：只针对传入的名字，且 ``persist(prune=False)``，
    **不会**把概念库里其余条目当成「本次未抽到」而删掉。
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw or "").strip()
        key = normalize_key(name)
        if not name or not key or key in seen:
            continue
        seen.add(key)
        wanted.append(name)
    if not wanted:
        raise ConceptExtractionError("没有需要补抽的概念名")

    corpus = collect_corpus(db, course)
    if not corpus["nodes"]:
        raise ConceptExtractionError("尚无大纲节点：请先生成并通过大纲，再补抽概念")
    existing = {normalize_key(row.title): row.title for row in list_concepts(db, course.id)}

    with usage_log.collect() as collector:
        try:
            client = llm or build_llm_client()
            data = usage_log.complete_json(
                client, SUPPLEMENT_SYSTEM,
                _supplement_prompt(course, corpus, wanted, existing),
                max_tokens=8000, purpose="concept",
            )
        except json.JSONDecodeError as exc:  # 任务 63：同 extract，失败原因带位置与现场
            raise ConceptExtractionError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise ConceptExtractionError(f"LLM 调用失败：{exc}") from exc
    raw = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ConceptExtractionError("LLM 未返回任何概念，本次不写入概念库")
    dropped: list[dict] = []
    # 补抽是老师点名要的概念 → 不走「跨 ≥2 个知识点」的自动准入
    concepts = merge_raw(raw, corpus, dropped, enforce_admission=False)
    # 升级成概念页的术语从 meta.terminology 里摘掉，避免两处重复登记
    record_terminology(db, course, [], promoted=[c["name"] for c in concepts])
    if not concepts:
        raise ConceptExtractionError(
            f"LLM 返回的 {len(raw)} 个候选全部不达标（与章节/知识点标题同名、像句子、或是停用词），本次不写入概念库"
        )
    report = persist(db, course, concepts, prune=False)
    summary = collector.summary()
    usage_log.record_aux(course, "concept", summary)
    db.commit()
    return {
        "ok": True,
        "model": str(getattr(client, "name", "") or ""),
        "requested": wanted,
        "concepts": len(concepts),
        "names": [c["name"] for c in concepts],
        "missing": [name for name in wanted
                    if normalize_key(name) not in {normalize_key(c["name"]) for c in concepts}],
        "filtered": dropped,
        "filtered_count": len(dropped),
        "usage": summary,
        **report,
    }


# ---------------------------------------------------------------------------
# 读取：列表 / 章节索引 / 导出页
# ---------------------------------------------------------------------------

def list_concepts(db: Session, course_id: str) -> list[ContentItem]:
    return list(db.query(ContentItem)
                .filter(ContentItem.course_id == course_id, ContentItem.kind == CONCEPT_KIND)
                .order_by(ContentItem.created_at).all())


def concept_rows(db: Session, course_id: str) -> list[dict]:
    """API 友好结构（列表 / 编辑器用）。"""
    return [{
        "id": row.id,
        "concept_id": str((row.meta or {}).get("concept_id") or ""),
        "name": row.title,
        "aliases": list((row.meta or {}).get("aliases") or []),
        "chapters": list((row.meta or {}).get("chapters") or []),
        "edited": bool((row.meta or {}).get("edited")),
        "body": row.body or {},
    } for row in list_concepts(db, course_id)]


def chapter_concept_map(db: Session, course_id: str) -> dict[str, list[str]]:
    """章 key → 该章要索引的概念名（保序去重；章节页「本章概念索引」用）。"""
    out: dict[str, list[str]] = {}
    for row in list_concepts(db, course_id):
        for key in (row.meta or {}).get("chapters") or []:
            bucket = out.setdefault(chapter_key_of(key), [])
            if row.title not in bucket:
                bucket.append(row.title)
    return out


def concept_slug_map(db: Session, course_id: str,
                     rows: list[ContentItem] | None = None) -> dict[str, str]:
    """概念标题 → 导出文件名（不含 .md）：概念页与章节索引共用同一口径。

    双链目标必须与文件名一致，否则 Obsidian 里就是坏链（标题里的空格会被 _slug
    换成横线）。这里与 concept_pages 的命名规则保持单一实现。
    """
    from app.services.kb import _slug  # 局部导入，避免 kb ↔ concept 循环引用

    used: dict[str, int] = {}
    out: dict[str, str] = {}
    for row in rows if rows is not None else list_concepts(db, course_id):
        slug = _slug(row.title, 60) or row.id
        if slug in used:
            used[slug] += 1
            slug = f"{slug}-{used[slug]}"
        else:
            used[slug] = 1
        out.setdefault(row.title, slug)
    return out


RELATION_FIELDS = ("broader", "narrower", "related")


def update_concept(db: Session, row: ContentItem, patch: dict) -> ContentItem:
    """网页端编辑概念（定义/要点/误区/别名/关系）；标记 edited，重抽时不再覆盖。"""
    body = dict(row.body or {})
    for key in ("definition", "points", "pitfalls"):
        if key in patch and patch[key] is not None:
            body[key] = patch[key]
    if patch.get("relations") is not None:
        raw = patch["relations"] or {}
        relations = dict(body.get("relations") or {})
        for field in RELATION_FIELDS:
            if field not in raw:
                continue
            values = [str(item).strip() for item in (raw.get(field) or []) if str(item).strip()]
            # 去重保序；空掉某一类就是「删掉这类关系」，所以显式写空列表也要生效
            seen: list[str] = []
            for value in values:
                if value not in seen:
                    seen.append(value)
            relations[field] = seen
        body["relations"] = relations
    row.body = body
    meta = dict(row.meta or {})
    if patch.get("aliases") is not None:
        meta["aliases"] = [str(a).strip() for a in patch["aliases"] if str(a).strip()]
    meta["edited"] = True
    row.meta = meta
    db.commit()
    db.refresh(row)
    return row

# ---------------------------------------------------------------------------
# 渲染概念页（导出到 概念库/）
# ---------------------------------------------------------------------------

def _figure_rank(name: str) -> int:
    """图示排序：思维导图/时间轴 > 数据图 > 概念图（同一概念最多取 2 张）。"""
    if name.startswith(("图-思维导图-", "图-时间轴-")):
        return 0
    if name.startswith(("图-柱状图-", "图-折线图-", "图-饼图-", "图-表格-")):
        return 1
    return 2


def concept_figures(meta: dict, figures: dict) -> list[str]:
    """该概念的图示：从它出现的知识点里取图表库 SVG（复用现有图，不重复造图）。"""
    names: list[str] = []
    for key in (meta or {}).get("chapters") or []:
        for name in (figures or {}).get(key) or []:
            if name not in names:
                names.append(name)
    names.sort(key=_figure_rank)
    return names[:MAX_FIGURES]


def _quoted(value) -> str:
    return '"' + str(value).replace('"', "'") + '"'


def _flow_list(values) -> str:
    return "[" + ", ".join(_quoted(v) for v in values) + "]"


def _frontmatter_lines(course: Course, row: ContentItem, meta: dict) -> list[str]:
    """概念页 frontmatter（type/aliases/chapters 供 Obsidian 与 Dataview 使用）。"""
    return [
        "---",
        f"title: {_quoted(row.title)}",
        'type: "concept"',
        f"aliases: {_flow_list(meta.get('aliases') or [])}",
        f"course: {_quoted(course.title)}",
        f"chapters: {_flow_list(meta.get('chapters') or [])}",
        f"concept_id: {_quoted(meta.get('concept_id') or '')}",
        f"tags: {_flow_list(['概念', 'coursegenie'])}",
        "---",
        "",
    ]


def _link(name: str, resolve) -> str:
    """能落到概念库的名字才发双链，否则纯文本——保证双链 0 坏链。"""
    target = resolve(name)
    return f"[[{target}]]" if target else str(name)


def _bullets(items: list[str], fallback: str, *, ordered: bool = False) -> list[str]:
    if not items:
        return [fallback]
    if ordered:
        return [f"{i}. {text}" for i, text in enumerate(items, 1)]
    return [f"- {text}" for text in items]


def _relation_lines(relations: dict, resolve) -> list[str]:
    lines: list[str] = []
    for key, label in RELATION_SECTIONS:
        names = [str(n) for n in (relations or {}).get(key) or [] if str(n).strip()]
        if names:
            lines.append(f"{label}：" + "、".join(_link(n, resolve) for n in names))
    return lines


def _source_lines(sources: list, node_stems: dict, material_pages: dict) -> list[str]:
    lines: list[str] = []
    for src in sources or []:
        src = src if isinstance(src, dict) else {}
        kind = str(src.get("kind") or "")
        ref = str(src.get("ref") or "")
        quote = str(src.get("quote") or "")
        label = SOURCE_LABELS.get(kind, kind or "来源")
        if kind == "material":
            page = material_pages.get(ref)
            where = f"[[{page}]]" if page else ref
        else:
            stem = node_stems.get(ref)
            where = f"[[{stem}]]" if stem else ref
        line = f"{label}：{where}"
        if quote:
            line += f" —— “{quote}”"
        lines.append(line)
    return lines


def render_concept(course: Course, row: ContentItem, *, node_stems: dict,
                   material_pages: dict, figures: dict, resolve) -> str:
    """一篇原子笔记的完整 markdown（结构见规划文档 §6.2）。"""
    from app.services.kb import BANNER

    meta = dict(row.meta or {})
    body = dict(row.body or {})
    points = [str(p) for p in body.get("points") or [] if str(p).strip()]
    pitfalls = [str(p) for p in body.get("pitfalls") or [] if str(p).strip()]
    figs = concept_figures(meta, figures)

    lines = [*_frontmatter_lines(course, row, meta), *BANNER, "", f"# {row.title}", ""]
    lines += ["## 一句话定义", "", str(body.get("definition") or "（待补充）"), ""]
    lines += ["## 要点", "", *_bullets(points, "1. （待补充）", ordered=True), ""]
    lines += ["## 概念关系", "",
              *_bullets(_relation_lines(body.get("relations") or {}, resolve),
                        "- （暂未建立关系，可在网页端补充）"), ""]
    lines += ["## 常见误区", "", *_bullets(pitfalls, "- （暂无）"), ""]
    lines += ["## 图示", "", *_bullets([f"![[{n}]]" for n in figs], "- （本章暂无图表）"), ""]
    lines += ["## 来源", "",
              *_bullets(_source_lines(body.get("sources") or [], node_stems, material_pages),
                        "- （尚未登记来源）"), ""]
    return "\n".join(lines).rstrip() + "\n"


def link_index(db: Session, course: Course) -> tuple[dict[str, str], set[str]]:
    """{归一化名字: 导出后真实存在的笔记文件名} —— 双链目标的唯一事实来源。

    可解析目标 = 概念页 + 有内容的知识点页 + 素材页；章节节点不单独成页，不参与解析。
    解析不到的目标一律退化为纯文本，保证导出后双链 0 坏链。
    """
    from app.services.kb import _node_has_content, _node_stem, _slug

    rows = list_concepts(db, course.id)
    slug_of = concept_slug_map(db, course.id, rows)
    index: dict[str, str] = {}
    for row in rows:
        target = slug_of.get(row.title) or row.title
        index.setdefault(normalize_key(row.title), target)
        for alias in (row.meta or {}).get("aliases") or []:
            index.setdefault(normalize_key(alias), target)
    for node in list_nodes(db, course.id):
        if not _node_has_content(db, course.id, node):
            continue
        stem = _node_stem(node)
        for name in (node.title, node.node_key, f"{node.node_key}-{node.title}"):
            index.setdefault(normalize_key(name), stem)
    for idx, material in enumerate(list_materials(db, course.id), 1):
        index.setdefault(normalize_key(material.title), f"{idx:02d}-{_slug(material.title)}")
    return index, set(index.values())


def concept_link_report(db: Session, course: Course) -> dict:
    """概念总数 + 双链体检。

    - ``broken_links``：**实际写入**的双链里指向不存在文件的条数（导出后必须为 0）；
    - ``unresolved_relations``：关系里解析不到真实文件的目标（退化为纯文本，不写双链）。
    """
    rows = list_concepts(db, course.id)
    index, valid = link_index(db, course)

    def resolve(name: str) -> str | None:
        return resolve_target(name, index, valid)

    broken: list[dict] = []
    unresolved: list[dict] = []
    linked = 0
    for row in rows:
        relations = (row.body or {}).get("relations") or {}
        for key, label in RELATION_SECTIONS:
            for name in relations.get(key) or []:
                text = str(name or "").strip()
                if not text:
                    continue
                if resolve(text) is None:
                    unresolved.append({"concept": row.title, "section": label, "target": text})
        for line in _relation_lines(relations, resolve):
            for target in re.findall(r"\[\[([^\]|#]+)\]\]", line):
                linked += 1
                if target not in valid:
                    broken.append({"concept": row.title, "target": target})
    return {
        "count": len(rows),
        "linked_relations": linked,
        "broken_links": len(broken),
        "unresolved_relations": len(unresolved),
        "broken": broken[:20],
        "unresolved": unresolved[:20],
    }


def concept_pages(db: Session, course: Course, *, figures: dict | None = None) -> list[tuple[str, str]]:
    """概念库文件载荷：[(vault 相对路径, markdown)]；没有概念时返回空列表。"""
    from app.services.kb import _node_has_content, _node_stem, _slug

    rows = list_concepts(db, course.id)
    if not rows:
        return []
    slug_of = concept_slug_map(db, course.id, rows)
    index, valid = link_index(db, course)

    def resolve(name: str) -> str | None:
        return resolve_target(name, index, valid)

    # 来源双链只指向「确实有正文、导出后会成页」的知识点，避免导出后坏链
    node_stems = {n.node_key: _node_stem(n) for n in list_nodes(db, course.id)
                  if _node_has_content(db, course.id, n)}
    material_pages = {m.title: f"{idx:02d}-{_slug(m.title)}"
                      for idx, m in enumerate(list_materials(db, course.id), 1)}

    pages: list[tuple[str, str]] = []
    for row in rows:
        slug = slug_of.get(row.title) or row.id
        text = render_concept(course, row, node_stems=node_stems,
                              material_pages=material_pages, figures=figures or {},
                              resolve=resolve)
        pages.append((f"{CONCEPTS_DIR}/{slug}.md", text))
    return pages
