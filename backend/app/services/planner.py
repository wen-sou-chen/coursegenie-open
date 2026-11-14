"""智能规划：调用 LLM 生成结构化大纲，持久化到 Outline / OutlineNode。

课程绑定 Obsidian 知识库素材时，自动把素材注入提示词作为大纲创作依据。
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.course import (
    STATUS_APPROVED,
    STATUS_PLANNED,
    ContentItem,
    Course,
    Outline,
    OutlineNode,
    QuizItem,
)
from app.schemas.plan import PlanRequest
from app.services import k12
from app.services import scenario
from app.services import usage as usage_log
from app.services.material import (
    inject_material,
    list_materials,
    material_text,
    stored_injection,
)
from app.services.llm import build_llm_client

SYSTEM_PROMPT = """OUTLINE_JSON
你是资深课程设计师。根据用户输入生成结构化课程大纲，只输出 JSON（不要代码块），格式：
{
  "title": "课程标题",
  "goal": "课程目标",
  "nodes": [
    {"key": "1", "title": "章节标题", "node_type": "chapter",
     "objective": "学习目标", "teaching_strategy": "教学策略", "assessment": "评估方式",
     "prerequisites": ["前置章节 key"],
     "points": [{"key": "1.1", "title": "知识点", "node_type": "point",
                 "objective": "...", "teaching_strategy": "...", "assessment": "...",
                 "prerequisites": []}]}
  ]
}
章节用 key "1"、"2"...，知识点用 "1.1"、"1.2"（node_type=point）。
prerequisites 是依赖的前置节点 key（知识图谱用）。保持 JSON 合法、中文输出。

三个描述字段的写法（**读者看得懂最重要**；学生/老师不会为了看懂大纲去查资料）：
- objective / teaching_strategy / assessment **优先用大白话**把「学什么、怎么做、怎么考」说清楚；
  例：把「马尔可夫链预测演示」写成「接龙游戏式演示：只看上一个词猜下一个词，体会模型是
  「按概率接词」而不是「想清楚再说」」。
- 确需保留专业术语（模型名、算法名、方法名、缩写）时，**同一行里紧跟一句白话解释**，
  写成「术语（一句话解释）」；解释要用大白话，不要拿另一个术语去解释术语。
  例：「用一张「选项 × 判断标准」打分表互评（决策矩阵 = 把每个选项按多条标准逐项打分后比较）」。
- 禁止出现「读者查不到、课程正文里也不会解释」的词；
  反例："teaching_strategy": "马尔可夫链预测演示"（学生不知道马尔可夫链是什么，全课也没解释）。"""


def generate_outline(db: Session, course: Course, request: PlanRequest) -> Outline:
    """生成大纲并落库，返回 Outline（nodes 已写入）。"""
    client = build_llm_client()
    audience = (request.audience or "").strip() or (course.audience or "").strip()
    user = (
        f"{course.title}\n"
        f"课程目标：{request.goal}\n"
        f"目标受众：{audience}\n"
        f"预计时长：{course.duration_minutes} 分钟\n"
        f"学科类型：{course.subject}\n"
        f"章节数：{request.num_chapters}\n"
        f"风格：{request.style}"
        + k12.outline_prompt_section(course)
        # 任务 85：使用场景的「口气与要求」段（未选场景 = 空串，提示词与基线逐字相同）
        + scenario.scenario_prompt_block(course)
        # 任务 69：按相关性选材 + 分段注入 + 总预算（并把「本次用了哪些素材」落库）。
        # 大纲这一步还没有章节，targets 用课程目标/受众/标题做相关性口径。
        + inject_material(db, course, scope="outline",
                          targets=[request.goal, audience, course.title, course.subject])
    )
    # 大纲这一次调用原先没有采集器 → token/费用被静默丢弃（P3-4）。包上 collect 后
    # 既能写进接口返回，也会经 record_aux 累加进「用量与成本」页签。
    with usage_log.collect() as collector:
        data = usage_log.complete_json(client, SYSTEM_PROMPT, user,
                                       max_tokens=3000, purpose="outline")

    # P3-1：重新生成前先把旧大纲存进历史（参数 + 节点快照 + 用量），
    # 否则同参两次结果差很多时既没法对比、也回不去。
    snapshot_outline(db, course, reason="regenerate")
    course.meta = {**(course.meta or {}), PLAN_PARAMS_KEY: {
        "goal": request.goal, "num_chapters": request.num_chapters,
        "style": request.style, "audience": audience,
    }}

    # 重新生成语义：用新大纲替换旧大纲（旧结构节点与其绑定的章节内容一并移除，避免新旧并存）
    _replace_existing_outline(db, course)

    # 任务 86：大纲也记「生成时的口径」（大纲提示词没有自己的版本常量 ⇒ 只记场景，不硬造版本号）
    content = {**dict(data or {}), **scenario.run_meta(course)}
    outline = Outline(
        course_id=course.id,
        title=data.get("title") or course.title,
        goal=data.get("goal") or request.goal,
        content=content,
    )
    db.add(outline)
    db.flush()

    nodes = data.get("nodes") or []
    order = 0
    for chapter in nodes:
        order = _persist_node(db, outline.id, chapter, parent_key="", order=order)
    if audience and audience != course.audience:
        course.audience = audience
    course.status = "planned"
    # 任务 70 §B1：自检结论随大纲一起落库（原先只作为接口响应返回 ⇒ 刷新 / 换设备就没了）；
    # 这里读的就是刚落库的节点 ⇒ POST /plan/generate 与 GET /plan/{id} 返回**同一份**结论。
    record_outline_check(db, course, outline_check(
        db, course, request, list_nodes(db, course.id),
        semantic=getattr(request, "semantic", None)))
    usage_log.record_aux(course, "outline", collector.summary())
    db.commit()
    db.refresh(outline)
    return outline


def _replace_existing_outline(db: Session, course: Course) -> None:
    """删除课程现有的旧大纲及绑定在旧节点上的章节内容（用于重新生成）。"""
    old_rows = (
        db.query(OutlineNode.id)
        .join(Outline, OutlineNode.outline_id == Outline.id)
        .filter(Outline.course_id == course.id)
        .all()
    )
    old_node_ids = [row[0] for row in old_rows]
    if old_node_ids:
        db.query(ContentItem).filter(ContentItem.node_id.in_(old_node_ids)).delete(synchronize_session=False)
        db.query(QuizItem).filter(QuizItem.node_id.in_(old_node_ids)).delete(synchronize_session=False)
    outlines = db.query(Outline).filter(Outline.course_id == course.id).all()
    for outline in outlines:
        db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).delete(synchronize_session=False)
        db.delete(outline)
    db.flush()


def update_node(db: Session, node_id: str, fields: dict) -> OutlineNode:
    """教师手动修订大纲节点（标题/目标/策略/评估/前置依赖）。"""
    node = db.get(OutlineNode, node_id)
    if node is None:
        raise ValueError("大纲节点不存在")
    for key in ("title", "objective", "teaching_strategy", "assessment", "prerequisites"):
        if key in fields and fields[key] is not None:
            setattr(node, key, fields[key])
    if node.outline is not None:
        course = db.get(Course, node.outline.course_id)
        if course is not None and course.status == STATUS_APPROVED:
            course.status = STATUS_PLANNED  # 大纲内容变更后需重新「大纲通过」
    db.commit()
    db.refresh(node)
    return node


def _persist_node(db: Session, outline_id: str, node: dict, parent_key: str, order: int) -> int:
    key = _resolve_node_key(node.get("key"), parent_key, order)
    full_key = key
    row = OutlineNode(
        outline_id=outline_id,
        node_key=full_key,
        node_type=node.get("node_type", "section"),
        title=node.get("title", ""),
        objective=node.get("objective", ""),
        teaching_strategy=node.get("teaching_strategy", ""),
        assessment=node.get("assessment", ""),
        prerequisites=node.get("prerequisites") or [],
        order_index=order,
    )
    db.add(row)
    db.flush()
    order += 1
    for child in node.get("points") or []:
        order = _persist_node(db, outline_id, child, parent_key=full_key, order=order)
    return order


def _resolve_node_key(raw_key: object, parent_key: str, order: int) -> str:
    """把 LLM 输出的 key 归一化为绝对 key（修复双前缀 bug）。

    约定：顶层章节用整数 key（"1"）；知识点可用绝对 key（"1.1"）或
    父级下的相对序号（"1" → "1.1"）。旧实现无条件拼接父前缀，会把
    绝对 key "1.1" 错写成 "1.1.1"。这里对已带父前缀的 key 原样保留，
    仅对纯数字的相对序号补父前缀；缺 key 时按顺序兜底生成。
    """
    key = str(raw_key or "").strip()
    if key:
        if parent_key and key != parent_key and not key.startswith(f"{parent_key}."):
            if key.isdigit():
                return f"{parent_key}.{key}"
        return key
    return f"{parent_key}.{order + 1}" if parent_key else str(order + 1)


def get_outline(db: Session, course_id: str) -> Outline | None:
    stmt = (
        select(Outline)
        .where(Outline.course_id == course_id)
        .order_by(Outline.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


def list_nodes(db: Session, course_id: str) -> list[OutlineNode]:
    outline = get_outline(db, course_id)
    if outline is None:
        return []
    return list(
        db.execute(
            select(OutlineNode)
            .where(OutlineNode.outline_id == outline.id)
            .order_by(OutlineNode.order_index)
        ).scalars()
    )

# ---------------------------------------------------------------------------
# 大纲生成后的自检（P3-2）：章节数是否兑现 + 素材要点覆盖
# ---------------------------------------------------------------------------

# 素材里「方法 / 步骤 / 编号」类要点的识别：PPTX/PDF 抽出来的正文基本是一行一条。
_RE_METHOD_POINT = re.compile(r"^\s*方法\s*([一二三四五六七八九十\d]{1,3})\s*[:：、.．]?\s*(.{2,40}?)\s*$", re.M)
_RE_STEP_POINT = re.compile(r"^\s*第\s*([一二三四五六七八九十\d]{1,3})\s*(?:步|步骤|步法)\s*[:：、.．]?\s*(.{2,40}?)\s*$", re.M)
_RE_LIST_POINT = re.compile(r"^\s*(?:\d{1,2}\s*[.、)]|[①②③④⑤⑥⑦⑧⑨⑩])\s*(.{4,40}?)\s*$", re.M)
# 行内枚举：「五大成因包括：概率机器本质、训练数据的裂纹、…」
_RE_INLINE_ENUM = re.compile(
    r"(?:包括|分别是|如下|有以下)\s*[:：]?\s*"
    r"([^\s、，,；;。：:]{2,20}(?:、[^\s、，,；;。：:]{2,20}){2,})")
_RE_POINT_LEAD = re.compile(r"^(?:以及|和|与|还有|另外|并|且|同时)\s*")
_RE_POINT_TAIL = re.compile(r"(?:等|等等|之类|之类的)$")
_RE_PARTICLES = re.compile(r"[的之与和及其该]")
_RE_BLANK = re.compile(r"[\s·、，,。．.：:；;（）()【】\[\]「」『』“”\"'’\-—_/|～~!！?？]+")

# ── 任务 70 §B：自检落库 + 覆盖检查扩面 ────────────────────────────────────────
# 自检结论原先只作为 POST /plan/generate 的响应返回（前端存内存 state）⇒ 刷新/换设备就没了。
# 这里把它落进 course.meta（**不新建表**，与任务 69 的 material_inject 同一处）。
OUTLINE_CHECK_KEY = "outline_check"
# 语义等价判定：**可选、默认关闭**；开启也只走本地词面相似度（不调 LLM、不产生用量）。
SEMANTIC_EQUIV_DEFAULT = False
SEMANTIC_MIN_SCORE = 0.34
POINT_KIND_LABELS = {
    "method": "方法", "step": "步骤", "numbered": "编号", "enum": "枚举",
    "definition": "定义", "concept": "概念", "case": "案例",
}
# 任务 70 §B3：除老三类（方法/步骤/编号）+ 行内枚举外，再认**定义 / 概念 / 案例**类要点。
# 判定按**句子粒度**（不依赖行首序号）；每类都要求明确的引导词，宁可不报也不污染清单。
# 术语里不该出现的东西：引号 / 括号 / 表格竖线 / 强调符 —— 真实素材里这些会把「要点」抽成碎片
_RE_TERM_JUNK = re.compile(r"[|｜\[\]{}#*`]")
# 引号 / 方括号允许出现在术语里（真实素材写成「“幻觉治理”是指…」）—— 由 _TERM_TRIM 去掉两端的装饰
_TERM_CHARS = r"[^\s，,。；;：:（）()\[\]|｜#*]"
_RE_DEF_SENT = re.compile(
    r"(?P<term>" + _TERM_CHARS + r"{2,20})(?:是指|指的是|定义为|是一种|是一个|是一类|即为)")
_RE_SO_CALLED = re.compile(r"所谓\s*(?P<term>" + _TERM_CHARS + r"{2,20})")
# 「概念：X / 定义：X」必须带冒号 —— 否则标题「## 1. 概念说明」会被当成要点
_RE_DEF_LEAD = re.compile(r"(?:概念|定义)\s*[:：]\s*(?P<term>" + _TERM_CHARS + r"{2,20})")
_RE_GLOSS_TERM = re.compile(
    r"^(?P<term>" + _TERM_CHARS + r"{2,20})\s*[（(][^）)]{2,60}[）)]\s*[:：]?\s*$")
_SENT_SPLIT = re.compile(r"[。；;！!？?\n]")
# 抽出来的术语要去掉两端的引号 / 括号 / 竖线等装饰
_TERM_TRIM = "“”\"'‘’「」『』【】[]（）()|｜·*-—　 \t"
# 定义类抽出来的「术语」里，这些是代词 / 泛词，不算要点
_TERM_STOP = frozenset({"本课程", "本课", "这门课", "课程", "这种", "这个", "这些", "该", "它", "其"})

# ── 任务 75 §B1：素材要点「去装饰 / 限长 / 去碎句」 ────────────────────────────
# 真素材（PPTX / Markdown 导出）里的要点常带装饰：`**粗体**`、反引号代码、`[[wikilink]]`、
# 行内尾注 `（→ 参见 [[PE_输出控制策略]]）`。这些装饰会**原样进未覆盖清单与建议文案**
# （如「建议新增知识点《**检测**：结合 [[PE_幻觉检测]] 方法识别潜在错误》」），既不可读，
# 也没法照着补知识点；抽出来的应当是可挂靠的**短语标签**，不是整句叙述。
POINT_MAX_LEN = 24
# 未覆盖明细（会生成「建议新增知识点《…》」的那一栏）只留「像知识点」的条目
POINT_DETAIL_MIN_LEN = 4
_RE_POINT_MARK = re.compile(r"[*`]+")                                    # `**粗体**` / 反引号代码的标记符
_RE_WIKILINK_PLAIN = re.compile(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")  # [[笔记|显示名]] ⇒ 显示名
_RE_REF_NOTE = re.compile(r"[（(][^（()）]*[)）]\s*$")                      # 行尾括号注释（按内容判断是不是尾注）
_RE_REF_HINT = re.compile(r"→|参见|结合")                                  # 尾注特征：交叉引用提示
_RE_SENTENCE_POINT = re.compile(r'[，。；！？："“”‘’]')                       # 含句读/引号 ⇒ 是句子，不是标签
# 「末尾带等」的枚举 = 没写完的句子片段（「（包括 A、B、C 等）」）；素材里枚举常带尾随 `）`，
# 老写法 `等$` 会漏判 ⇒ 允许一个尾括号（任务 75：`用户身份 / 数据背景` 这类碎片就是从这里漏出来的）。
_RE_ENUM_TAIL = re.compile(r"(?:等|等等|之类|之类的)[）)]?\s*$")


def _norm_key(text: str) -> str:
    """比对用的归一化：去空白标点 + 去虚词（的/之/与/和/及/其/该）+ 统一小写。

    去虚词是为了让「概率机器本质」能命中标题「概率机器的本质」这类同义措辞
    （P3-2 覆盖比对是字面比对，不做语义判断）。
    """
    return _RE_PARTICLES.sub("", _RE_BLANK.sub("", str(text or ""))).lower()


def material_point_rows(texts: list[str], *, limit: int = 60) -> list[dict]:
    """抽素材要点，带类型标签 ``[{"point": …, "kind": …}]``（覆盖自检的左栏）。

    真实 PPTX 抽出来的正文是一行一条（如「方法一：反思校对」），也有把 5 条并成一行
    的「五大成因包括：概率机器本质、训练数据的裂纹、…」；还有**没有编号的定义 / 概念 /
    案例**（任务 70 §B3 扩面：按句子粒度抽，不靠行首序号）。抽不到就返回空列表，
    覆盖自检会显式说明「没识别到要点」，不报假结论。

    顺序保证：**先跑 P3-2 的老四类（方法 / 步骤 / 编号 / 行内枚举），再跑新三类** ⇒
    前 N 条与老口径逐字一致（扩面只会往后追加，不会挤掉老要点）。
    """
    rows: list[dict] = []
    seen: set[str] = set()

    def add(candidate: str, kind: str) -> None:
        text = _clean_point(candidate)
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        rows.append({"point": text, "kind": kind})

    for text in texts:
        body = str(text or "")
        for match in _RE_METHOD_POINT.finditer(body):
            add(match.group(2), "method")
        for match in _RE_STEP_POINT.finditer(body):
            add(match.group(2), "step")
        for match in _RE_LIST_POINT.finditer(body):
            add(match.group(1), "numbered")
        for match in _RE_INLINE_ENUM.finditer(body):
            raw_items = re.split("、", match.group(1))
            items = [piece for piece in (_clean_point(p) for p in raw_items) if piece]
            if len(items) < 3 or any(_RE_ENUM_TAIL.search(p.strip()) for p in raw_items):
                # 少于 3 条、或末尾带「等」（说明枚举没写完，如「A、B、C 等，并提出…」）
                # 都当句子片段处理：宁可不报，也不污染未覆盖清单
                continue
            for item in items:
                add(item, "enum")
        if len(rows) >= limit:
            break
    # 任务 70 §B3：定义 / 概念 / 案例类（按句子粒度，不依赖行首序号）
    if len(rows) < limit:
        for text in texts:
            for candidate, kind in _sentence_point_rows(str(text or "")):
                add(candidate, kind)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
    return rows[:limit]


def material_points(texts: list[str], *, limit: int = 40) -> list[str]:
    """老口径的便捷包装：只要要点文字（覆盖自检左栏的字符串列表）。"""
    return [row["point"] for row in material_point_rows(texts, limit=limit)]


def _sentence_point_rows(text: str) -> list[tuple[str, str]]:
    """按**句子**抽定义 / 概念类要点（任务 70 §B3 扩面；任务 75 §B2① 取消案例类）。

    两类判定都要求句子里有明确引导词（``X 是指 / 定义为`` / ``所谓`` / ``概念：``）：
    宁可少报，也不把普通叙述句、标题、表格行当成要点。
    行尾带括号注释的术语行（如「决策矩阵（把每个选项按多条标准逐项打分后比较）」）算概念。

    案例类**已取消**（任务 75 §B2①）：真素材里 ``### 案例 1：事实性治理`` 这类 Markdown
    小标题会被抽成「事实性治理 / 教育场景」这种**分类名**，不是可覆盖的知识点；案例本身是
    "讲故事"，抽出来只会污染未覆盖清单、并生成「建议新增知识点《教育场景》」这种无用建议。
    """
    found: list[tuple[str, str]] = []

    def accept(term: str, kind: str) -> None:
        clean = str(term or "").strip(_TERM_TRIM)
        if len(clean) < 2 or clean in _TERM_STOP or _RE_TERM_JUNK.search(clean):
            return
        found.append((clean, kind))

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        gloss = _RE_GLOSS_TERM.match(line)
        if gloss:
            accept(gloss.group("term"), "concept")
        for sentence in _SENT_SPLIT.split(line):
            piece = sentence.strip()
            if len(piece) < 6:
                continue
            for pattern, kind in ((_RE_DEF_SENT, "definition"), (_RE_SO_CALLED, "definition"),
                                  (_RE_DEF_LEAD, "concept")):
                hit = pattern.search(piece)
                if hit:
                    accept(hit.group("term"), kind)
                    break
    return found


def _balanced_brackets(text: str) -> bool:
    """括号是否成对（全角 / 半角 / 方括号各算各的）。

    不许把「尾部括号不成对」的碎片当要点（任务 75 §B1）：真素材里
    「（包括用户身份、…、任务目标等）」这类行内枚举被截成 `任务目标等）`，
    单看像要点、其实是半句话。
    """
    pairs = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"))
    return all(text.count(left) == text.count(right) for left, right in pairs)


def _looks_like_point(point: str) -> bool:
    """未覆盖明细只留「像知识点」的条目（任务 75 §B3）。

    过滤掉的是**句子与碎片**：过长 / 过短、仍带装饰、括号不成对、含句读或引号，
    以及被截断的那类（结尾带省略号 ⇒ 原本是长句，不是标签）。被过滤的条数由
    :func:`outline_check` 以 ``filtered_out_count`` **如实返回**，不静默丢弃
    （原文仍在 ``material_points`` / ``material_uncovered`` 里可查）。
    """
    text = str(point or "")
    if not (POINT_DETAIL_MIN_LEN <= len(text) <= POINT_MAX_LEN):
        return False
    if _RE_POINT_MARK.search(text) or "[[" in text or "]]" in text:
        return False
    if not _balanced_brackets(text) or text.endswith("…"):
        return False
    return not _RE_SENTENCE_POINT.search(text)


def _clean_point(raw: str) -> str:
    """把一条候选取干净：去 Markdown 装饰、去行内尾注、去引导虚词、限长（任务 75 §B1）。

    - ``**粗体**`` / 反引号代码去掉标记符；``[[笔记]]`` 只保留可见文字；
    - 行尾的交叉引用注释（``（→ 参见 [[PE_xxx]]）`` / ``（结合 [[PE_xxx]]）``）整段去掉；
    - 超过 :data:`POINT_MAX_LEN` 字的截断（带省略号，总长仍不超上限）；
    - 尾部括号不成对的碎片（如 ``任务目标等）``）整条丢弃。
    """
    text = re.sub(r"\s+", " ", str(raw or ""))
    text = _RE_WIKILINK_PLAIN.sub(lambda m: (m.group(2) or m.group(1) or ""), text)
    text = _RE_POINT_MARK.sub("", text)
    text = re.sub(r" {2,}", " ", text).strip()
    text = _RE_POINT_LEAD.sub("", text)
    text = _RE_POINT_TAIL.sub("", text).strip(" ·—-:：、，,。.")
    # 尾注判定放在去尾标点之后：素材里写成「…（结合 [[PE_迭代优化]]）。」，先判会漏
    tail_note = _RE_REF_NOTE.search(text)
    if tail_note and _RE_REF_HINT.search(tail_note.group(0)):
        text = text[: tail_note.start()].strip(" ·—-:：、，,。.")
    if len(text) > POINT_MAX_LEN:
        text = text[: POINT_MAX_LEN - 1].rstrip(" ·—-:：、，,。.") + "…"
    if len(text) < 2 or len(text) > POINT_MAX_LEN:
        return ""
    if not _balanced_brackets(text):
        return ""
    return text


def _bigram_set(text: str) -> set[str]:
    """词面相似度用的二元组集合（任务 70 §B3：语义等价判定走本地计算，不调 LLM）。"""
    key = _norm_key(text)
    if len(key) < 2:
        return {key} if key else set()
    return {key[index:index + 2] for index in range(len(key) - 1)}


def _bigram_dice(left: str, right: str) -> float:
    """Dice 相似度（0~1）。两边任一侧为空时算 0，避免「都空 ⇒ 全相似」的假命中。"""
    a, b = _bigram_set(left), _bigram_set(right)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def material_coverage(points: list, titles: list[str], *, semantic: bool = False) -> dict:
    """素材要点 × 大纲标题的覆盖比对：谁被覆盖、谁没被覆盖（P3-2；任务 70 §B3 扩面）。

    命中规则：归一化后双向包含（要点「反思校对」命中标题「方法一：反思校对」；
    标题较短时只允许「要点被标题包含」，避免「方法」这类泛词命中一切）。

    ``points`` 既接受字符串列表（老口径），也接受 :func:`material_point_rows` 的
    ``[{"point", "kind"}]`` —— 后者会把 ``kind`` / ``kind_label`` 一起带进结果，
    界面就能按类型展示「方法 / 定义 / 案例」各有多少条。

    ``semantic`` **默认关闭**；开启也只走本地词面相似度（:func:`_bigram_dice`），
    **不调 LLM、不产生用量**。
    """
    normalized = [(_strip_blank_title(title), _norm_key(title)) for title in titles]
    rows: list[dict] = []
    for raw_point in points:
        kind = ""
        if isinstance(raw_point, dict):
            kind = str(raw_point.get("kind") or "")
            point = str(raw_point.get("point") or "")
        else:
            point = str(raw_point or "")
        key = _norm_key(point)
        matched = ""
        if len(key) >= 2:
            for raw, norm in normalized:
                if not norm:
                    continue
                if key in norm or (len(norm) >= 3 and norm in key):
                    matched = raw
                    break
            if not matched and semantic:
                best, score = "", 0.0
                for raw, norm in normalized:
                    if not norm:
                        continue
                    ratio = _bigram_dice(key, norm)
                    if ratio > score:
                        best, score = raw, ratio
                if score >= SEMANTIC_MIN_SCORE:
                    matched = best
        row = {"point": point, "covered": bool(matched), "matched_by": matched}
        if kind:
            row["kind"] = kind
            row["kind_label"] = POINT_KIND_LABELS.get(kind, kind)
        rows.append(row)
    uncovered = [row["point"] for row in rows if not row["covered"]]
    return {"points": rows, "uncovered": uncovered, "total": len(rows),
            "covered_count": len(rows) - len(uncovered)}


def _strip_blank_title(title: str) -> str:
    return str(title or "").strip()


def _suggest_for_point(point: str, chapters: list[tuple[str, str]]) -> str:
    """未覆盖要点的可操作建议（任务 70 §B3）。

    能找到词面最接近的章 ⇒「建议并入第 N 章「章名」」；否则 ⇒「建议新增知识点《X》」。
    相似度仍是**本地词面**计算（:func:`_bigram_dice`），不调 LLM。
    """
    best_key, best_title, best_score = "", "", 0.0
    for key, title in chapters:
        score = _bigram_dice(point, title)
        if score > best_score:
            best_key, best_title, best_score = key, title, score
    if best_title and best_score >= 0.25:
        return f"建议并入第 {best_key} 章「{best_title}」"
    return f"建议新增知识点《{point}》"


def outline_check(db: Session, course: Course, request: PlanRequest,
                  nodes: list[OutlineNode], *, semantic: bool | None = None) -> dict:
    """大纲生成后的自检：① 章节数是否兑现请求；② 素材要点覆盖（P3-2 / 任务 70 §B3）。

    `num_chapters` 只进提示词、模型可能只给一半（实测要 8 章给 5 章），原先没有任何
    回报；这里把「请求 vs 实际」「素材里有、大纲没覆盖」都显式算出来，交给界面提示，
    并给出「按此补生成」的建议值（只提示，不自动改大纲）。

    任务 70：要点抽取扩到**定义 / 概念 / 案例**（按句子粒度），未覆盖项各带一条**可操作
    建议**；``semantic`` 控制语义等价判定（**默认关**，开启也只走本地词面相似度、不调 LLM）。
    """
    semantic_on = SEMANTIC_EQUIV_DEFAULT if semantic is None else bool(semantic)
    chapters = [node for node in nodes if "." not in str(node.node_key or "")]
    requested = int(getattr(request, "num_chapters", 0) or 0)
    actual = len(chapters)
    point_rows = material_point_rows([str(row.text or "") for row in list_materials(db, course.id)])
    coverage = material_coverage(point_rows, [str(node.title or "") for node in nodes],
                                 semantic=semantic_on)
    chapter_titles = [(str(node.node_key or ""), str(node.title or "")) for node in chapters]
    by_kind: dict[str, dict] = {}
    uncovered_detail: list[dict] = []
    # 任务 75 §B3：被「像不像知识点」挡在明细之外的条目（只计条数 + 留样例，原文不丢）
    filtered_out: list[str] = []
    for row in coverage["points"]:
        kind = str(row.get("kind") or "other")
        label = str(row.get("kind_label") or POINT_KIND_LABELS.get(kind, kind))
        bucket = by_kind.setdefault(kind, {"kind": kind, "label": label,
                                           "total": 0, "covered_count": 0})
        bucket["total"] += 1
        if row["covered"]:
            bucket["covered_count"] += 1
            continue
        # 任务 75 §B3：明细只留「像知识点」的条目；过滤掉多少条**如实给 filtered_out_count**
        if not _looks_like_point(row["point"]):
            filtered_out.append(row["point"])
            continue
        row["suggestion"] = _suggest_for_point(row["point"], chapter_titles)
        uncovered_detail.append({"point": row["point"], "kind": kind, "kind_label": label,
                                 "suggestion": row["suggestion"]})
    kind_summary = "、".join(f"{item['label']} {item['total']}" for item in by_kind.values())

    suggested = min(12, max(requested, actual + len(coverage["uncovered"]))) or actual
    if actual != requested:
        chapter_notice = (f"请求 {requested} 章，实际生成 {actual} 章（模型没有按请求补足）；"
                          f"章节数是上限建议，可调到 {suggested} 章后重新生成。")
    else:
        chapter_notice = f"请求 {requested} 章，实际生成 {actual} 章，一致。"
    if not point_rows:
        material_notice = ("素材里没识别到要点（方法 / 步骤 / 编号 / 枚举 / 定义 / 概念 / 案例），"
                           "覆盖自检跳过（不报假结论）；可人工核对素材目录章节是否都在大纲里。")
    elif coverage["uncovered"]:
        shown_points = [row["point"] for row in uncovered_detail[:8]]
        shown = "、".join(shown_points) if shown_points else "（都是句子碎片，已过滤）"
        filtered_note = (f"；另有 {len(filtered_out)} 条句子碎片已过滤（不作为可挂靠的知识点）"
                         if filtered_out else "")
        semantic_note = ("（已开启语义等价判定：本地词面相似度，未调模型）" if semantic_on else
                         "（字面比对；语义等价判定默认关 —— 措辞不同但意思相同的可能仍算未覆盖，"
                         "可在重新生成时勾选「语义等价判定」或人工确认）")
        material_notice = (f"素材里有 {coverage['total']} 个要点（{kind_summary}），"
                           f"大纲覆盖 {coverage['covered_count']} 个；没覆盖：{shown}"
                           f"{filtered_note}{semantic_note}")
    else:
        material_notice = f"素材里 {coverage['total']} 个要点（{kind_summary}），大纲全部覆盖。"
    return {
        "requested_chapters": requested,
        "actual_chapters": actual,
        "chapter_match": actual == requested,
        "chapter_notice": chapter_notice,
        "suggested_chapters": suggested,
        # 任务 70 §B3：语义等价判定的开关状态跟着结论一起走（界面上要能看出这结论是哪种口径算的）
        "semantic_enabled": semantic_on,
        "material_points_total": coverage["total"],
        "material_covered_count": coverage["covered_count"],
        "material_uncovered": coverage["uncovered"],
        "material_uncovered_count": len(coverage["uncovered"]),
        "material_points": coverage["points"],
        # 按类型汇总（方法/步骤/编号/枚举/定义/概念/案例）—— 界面上直接看出扩面识别到了什么
        "material_points_by_kind": list(by_kind.values()),
        # 未覆盖清单 + 每条的**可操作建议**（建议并入第 N 章「章名」/ 建议新增知识点《X》）
        "material_uncovered_detail": uncovered_detail,
        # 任务 75 §B3：明细过滤掉的条数 + 样例（不静默丢；原文仍在 material_uncovered 里可查）
        "filtered_out_count": len(filtered_out),
        "filtered_out_samples": filtered_out[:5],
        "material_uncovered_shown_count": len(uncovered_detail),
        "material_notice": material_notice,
        # 任务 69 §B1.3：本次素材注入情况（用了 N/M 份、注入/原始字数、未参与清单）——
        # 与 chapter_notice / material_notice 同一处返回、同一处展示（原先超限完全静默）。
        "material_inject_notice": (stored_injection(course) or {}).get("notice", ""),
        "material_inject": stored_injection(course),
        "action_hint": "按此补生成：把「章节数」调到建议值后重新生成大纲（会替换当前大纲）",
    }


def materials_fingerprint(db: Session, course: Course) -> list[dict]:
    """本次自检所用素材的指纹（id + 标题 + 正文字数）。

    用途：下一次打开大纲页时，能看出「这份结论是不是基于当前这批素材」算的
    （用户口径：AI 补充的内容要有来处 —— 结论也要能对回它依据的素材）。
    """
    return [{
        "id": row.id,
        "title": str(row.title or ""),
        "chars": len(material_text(row) or ""),
        "source": str(getattr(row, "source", "") or ""),
    } for row in list_materials(db, course.id)]


def record_outline_check(db: Session, course: Course, check: dict) -> dict:
    """把自检结论落进 ``course.meta[OUTLINE_CHECK_KEY]``（**不 commit**，由调用方提交）。

    任务 70 §B1：原先结论只作为 ``POST /plan/generate`` 的响应返回、前端存内存 state ⇒
    刷新 / 换设备就看不到了。这里连同 ``generated_at`` 与**所用素材指纹**一起落库；
    与任务 69 的 ``material_inject`` 同一处（``course.meta``），**不新建表**。
    """
    fingerprint = materials_fingerprint(db, course)
    digest = hashlib.sha1(
        "|".join(f"{item['id']}:{item['chars']}" for item in fingerprint).encode("utf-8")
    ).hexdigest()[:12]
    record = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "semantic_enabled": bool(check.get("semantic_enabled")),
        "materials_fingerprint": fingerprint,
        "materials_fingerprint_hash": digest,
        "check": check,
    }
    course.meta = {**(course.meta or {}), OUTLINE_CHECK_KEY: record}
    return record


def stored_outline_check(course: Course) -> dict | None:
    """上一次自检的落库记录（没有则 ``None``）。"""
    value = (course.meta or {}).get(OUTLINE_CHECK_KEY)
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# 大纲节点增 / 删 / 移（P3-3）：node_key 自动重排，且不破坏内容与笔记的绑定
# ---------------------------------------------------------------------------

CHAPTER_TYPE = "chapter"
POINT_TYPE = "point"


class NodeConstraintError(ValueError):
    """操作被已生成的内容/笔记挡住（需要二次确认），由路由转成 409。"""

    def __init__(self, message: str, impact: dict | None = None) -> None:
        super().__init__(message)
        self.impact = impact or {}


def is_chapter(node: OutlineNode) -> bool:
    """章节点：key 里没有「.」（与 kb / audit 的判定口径一致）。"""
    return "." not in str(node.node_key or "")


def _grouped(nodes: list[OutlineNode]) -> list[list[OutlineNode]]:
    """把有序节点切成「章 + 它的知识点」块（顺序即 order_index 顺序）。

    层级只由 key 表达（"1" 是章、"1.2" 是第 1 章的知识点），所以移动章时
    必须整块移，否则它的知识点会被顺位挂到别的章下。
    """
    groups: list[list[OutlineNode]] = []
    index_by_chapter: dict[str, int] = {}
    for row in sorted(nodes, key=lambda item: item.order_index):
        key = str(row.node_key or "")
        if is_chapter(row):
            index_by_chapter[key] = len(groups)
            groups.append([row])
            continue
        at = index_by_chapter.get(key.split(".")[0])
        if at is None:
            groups.append([row])  # 孤儿知识点（父章已不在）：自成一块，不猜归属
        else:
            groups[at].append(row)
    return groups


def plan_keys(nodes: list[OutlineNode]) -> list[tuple[OutlineNode, str]]:
    """按 order_index 计算重排后的 key（纯计算，不写库）。

    先按「章 + 它的知识点」切块（:func:`_grouped`），再按块内位置编号：
    章依次 1/2/3…，其下知识点依次 {章号}.1/{章号}.2…，保证连续
    （删掉 2.2 后 2.3 会补位成 2.2）。
    """
    return plan_keys_for_groups(_grouped(nodes))


def plan_keys_for_groups(groups: list[list[OutlineNode]]) -> list[tuple[OutlineNode, str]]:
    """按「块内位置」编号：块首是章、其余是它的知识点（新增/移动后的顺序用这个）。

    认位置而不是认旧 key：新节点还没有 key，旧 key 也不该决定它落在哪一层。
    """
    pairs: list[tuple[OutlineNode, str]] = []
    for chapter_no, group in enumerate(groups, 1):
        for index, row in enumerate(group):
            pairs.append((row, str(chapter_no) if index == 0 else f"{chapter_no}.{index}"))
    return pairs


def apply_plan_keys(db: Session, course: Course,
                    pairs: list[tuple[OutlineNode, str]]) -> dict[str, str]:
    """把重排结果写进节点，并同步前置依赖与内容行里的 node_key 缓存。

    返回 {旧 key: 新 key}。真正的绑定点是 ``ContentItem.node_id``（不动）；
    ``meta.node_key`` / ``meta.chapter_key`` 只是展示与文件名的缓存，这里一并改掉。
    笔记文件名由节点 key 派生，导出时会按新 key 重写（界面需提前说明）。
    """
    mapping = _mapping_of(pairs)
    for index, (node, new_key) in enumerate(pairs):
        node.node_key = new_key
        node.order_index = index
    if not mapping:
        for index, (node, _new_key) in enumerate(pairs):
            node.order_index = index
        return {}
    for node, _new_key in pairs:
        prerequisites = [mapping.get(str(p), str(p)) for p in (node.prerequisites or [])]
        if prerequisites != list(node.prerequisites or []):
            node.prerequisites = prerequisites
    by_id = {node.id: node for node, _new_key in pairs}
    for item in db.query(ContentItem).filter(ContentItem.course_id == course.id).all():
        meta = dict(item.meta or {})
        changed = False
        node = by_id.get(item.node_id)
        if node is not None and str(meta.get("node_key") or "") != node.node_key:
            meta["node_key"] = node.node_key
            changed = True
        chapter_key = str(meta.get("chapter_key") or "")
        if chapter_key and chapter_key in mapping:
            meta["chapter_key"] = mapping[chapter_key]
            changed = True
        if changed:
            item.meta = meta
    return mapping


def _flatten(groups: list[list[OutlineNode]]) -> list[OutlineNode]:
    return [row for group in groups for row in group]


def node_members(nodes: list[OutlineNode], node: OutlineNode) -> list[OutlineNode]:
    """自身 + 其下子节点（章 → 该章的知识点；知识点 → 只有自己）。"""
    if not is_chapter(node):
        return [row for row in nodes if row.id == node.id]
    prefix = f"{node.node_key}."
    return [row for row in nodes
            if row.id == node.id or str(row.node_key or "").startswith(prefix)]


def _note_files_of(course: Course, keys: list[str]) -> dict:
    """该节点在笔记库里的现有文件（按文件名里的节点 key 匹配；只读，不建目录）。"""
    from app.services import kb

    root = kb._configured_vault_root(course)
    if root is None:
        return {"bound": False, "files": [], "count": 0, "out_dir": "",
                "note": "尚未绑定笔记库（Vault）"}
    rel = str(kb.course_kb(course).get("output_rel") or f"课程/{kb._slug(course.title, 40)}")
    out_dir = Path(str(root)).expanduser() / rel
    if not out_dir.is_dir():
        return {"bound": True, "files": [], "count": 0, "out_dir": rel,
                "note": "笔记库里还没有导出过这门课"}
    wanted = [key for key in keys if key]
    files: list[str] = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        if any(name.startswith(f"{key}-") or f"-{key}-" in name for key in wanted):
            files.append(path.relative_to(out_dir).as_posix())
    return {"bound": True, "files": files, "count": len(files), "out_dir": rel, "note": ""}


def node_impact(db: Session, course: Course, node_id: str, *,
                pairs: list[tuple[OutlineNode, str]] | None = None) -> dict:
    """节点影响面：绑定的内容/习题/锁定讲义 + 笔记库文件 + 编号变化。

    ``pairs`` 传「操作之后的顺序」时额外算出旧键 → 新键，界面据此在二次确认里
    写清「改编号会重排导出文件名」。
    """
    outline = get_outline(db, course.id)
    node = db.get(OutlineNode, node_id)
    if node is None or outline is None or node.outline_id != outline.id:
        raise ValueError("大纲节点不存在")
    nodes = list_nodes(db, course.id)
    members = node_members(nodes, node)
    member_ids = [row.id for row in members]
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id, ContentItem.node_id.in_(member_ids))
             .order_by(ContentItem.created_at).all())
    quizzes = (db.query(QuizItem)
               .filter(QuizItem.course_id == course.id, QuizItem.node_id.in_(member_ids))
               .count())
    counts: dict[str, int] = {}
    locked = 0
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
        if (item.meta or {}).get("locked"):
            locked += 1
    notes = _note_files_of(course, [str(row.node_key or "") for row in members])
    key_changes: list[dict] = []
    if pairs is not None:
        for row, new_key in pairs:
            old_key = str(row.node_key or "")
            if old_key != new_key:
                key_changes.append({"id": row.id, "title": row.title,
                                    "from": old_key, "to": new_key})
    return {
        "node": {"id": node.id, "key": node.node_key, "title": node.title,
                 "type": node.node_type},
        "nodes": [{"id": row.id, "key": row.node_key, "title": row.title,
                   "type": row.node_type} for row in members],
        "node_count": len(members),
        "content_total": len(items),
        "content_counts": counts,
        "content_items": [{"id": item.id, "kind": item.kind, "title": item.title}
                          for item in items],
        "quizzes": int(quizzes),
        "locked": locked,
        "notes": notes,
        "key_changes": key_changes,
        "key_change_count": len(key_changes),
    }


def _mark_planned(course: Course) -> None:
    """大纲结构变了：已通过状态退回「待通过」，否则界面会以为锁定还在生效。"""
    if course.status == STATUS_APPROVED:
        course.status = STATUS_PLANNED


def add_node(db: Session, course: Course, *, title: str, parent_id: str | None = None,
             position: int | None = None, objective: str = "",
             teaching_strategy: str = "", assessment: str = "",
             node_type: str = "") -> dict:
    """新增一章或一条知识点，并把整份大纲的编号重排成连续。"""
    outline = get_outline(db, course.id)
    if outline is None:
        raise ValueError("尚无大纲：请先生成大纲，再手工增删节点")
    nodes = list_nodes(db, course.id)
    parent = None
    if parent_id:
        parent = db.get(OutlineNode, parent_id)
        if parent is None or parent.outline_id != outline.id:
            raise ValueError("父章节不存在")
        if not is_chapter(parent):
            raise ValueError("只能在章节点下新增知识点（当前选中的是知识点）")
    title = str(title or "").strip()
    if not title:
        raise ValueError("请填写节点标题")
    kind = str(node_type or "").strip() or (POINT_TYPE if parent is not None else CHAPTER_TYPE)
    if kind not in (CHAPTER_TYPE, POINT_TYPE):
        raise ValueError("节点类型只能是 chapter 或 point")
    if parent is not None and kind != POINT_TYPE:
        raise ValueError("章节点下的新节点只能是知识点")
    if parent is None and kind != CHAPTER_TYPE:
        raise ValueError("顶层只能新增章节点（知识点请指定父章）")
    row = OutlineNode(outline_id=outline.id, node_key="", node_type=kind,
                      title=title[:255], objective=objective or "",
                      teaching_strategy=teaching_strategy or "",
                      assessment=assessment or "", prerequisites=[], order_index=0)
    db.add(row)
    db.flush()
    groups = _grouped(nodes)
    if parent is None:
        at = len(groups)
        if position and 1 <= position <= len(groups):
            at = position - 1
        groups.insert(at, [row])
    else:
        at = next(idx for idx, group in enumerate(groups) if group[0].id == parent.id)
        members = groups[at][1:]
        index = len(members)
        if position and 1 <= position <= len(members):
            index = position - 1
        groups[at].insert(1 + index, row)
    mapping = apply_plan_keys(db, course, plan_keys_for_groups(groups))
    _mark_planned(course)
    db.commit()
    db.refresh(row)
    return {"node": row, "renumbered": mapping}


def delete_node(db: Session, course: Course, node_id: str, *, cascade: bool = False) -> dict:
    """删除节点（含子节点）并重排编号；绑定内容/习题默认不删，需显式 cascade。"""
    outline = get_outline(db, course.id)
    node = db.get(OutlineNode, node_id)
    if node is None or outline is None or node.outline_id != outline.id:
        raise ValueError("大纲节点不存在")
    nodes = list_nodes(db, course.id)
    members = node_members(nodes, node)
    member_ids = {row.id for row in members}
    keep = [row for row in nodes if row.id not in member_ids]
    impact = node_impact(db, course, node_id, pairs=plan_keys(keep))
    if (impact["content_total"] or impact["quizzes"]) and not cascade:
        raise NodeConstraintError(
            f"「{node.node_key} {node.title}」上还挂着 {impact['content_total']} 个内容条目 / "
            f"{impact['quizzes']} 道习题：确认要连带删除后再试（影响面见 impact）", impact)
    db.query(ContentItem).filter(ContentItem.course_id == course.id,
                                 ContentItem.node_id.in_(member_ids)).delete(synchronize_session=False)
    db.query(QuizItem).filter(QuizItem.course_id == course.id,
                              QuizItem.node_id.in_(member_ids)).delete(synchronize_session=False)
    for row in members:
        db.delete(row)
    mapping = apply_plan_keys(db, course, plan_keys(keep))
    _mark_planned(course)
    db.commit()
    return {
        "deleted": [{"id": row.id, "key": row.node_key, "title": row.title} for row in members],
        "removed_content": impact["content_total"],
        "removed_quizzes": impact["quizzes"],
        "renumbered": mapping,
        "impact": impact,
    }


def move_node(db: Session, course: Course, node_id: str, *, direction: str | None = None,
              reparent: bool = False, to_parent_id: str | None = None,
              position: int | None = None, preview: bool = False) -> dict:
    """移动节点：同组上/下移（direction）或跨章移动 / 升降层级（reparent）。

    preview=True 只算「移动后会变成什么样」（编号变化 + 影响面）然后回滚，供二次确认。
    """
    outline = get_outline(db, course.id)
    node = db.get(OutlineNode, node_id)
    if node is None or outline is None or node.outline_id != outline.id:
        raise ValueError("大纲节点不存在")
    nodes = list_nodes(db, course.id)
    groups = _grouped(nodes)
    at = next((idx for idx, group in enumerate(groups) if any(r.id == node.id for r in group)), None)
    if at is None:
        raise ValueError("大纲节点不存在")
    group = groups[at]
    index = next(idx for idx, row in enumerate(group) if row.id == node.id)

    if direction:
        if direction not in ("up", "down"):
            raise ValueError("direction 只能是 up 或 down")
        target = index - 1 if direction == "up" else index + 1
        if target < 0 or target >= len(group):
            raise NodeConstraintError(
                f"「{node.node_key} {node.title}」已经在"
                f"{'最前面' if direction == 'up' else '最后面'}了，无法继续移动")
        group[index], group[target] = group[target], group[index]
        return _commit_move(db, course, node, groups, preview=preview)

    if not reparent:
        raise ValueError("请指定 direction（同章上/下移）或 reparent=true（跨章移动）")
    if is_chapter(node) and len(group) > 1:
        raise NodeConstraintError(
            f"「{node.node_key} {node.title}」下面还有 {len(group) - 1} 个知识点："
            "先把它们移走或删除，才能把这章降级 / 跨章移动")
    target_group = None
    if to_parent_id:
        parent = db.get(OutlineNode, to_parent_id)
        if parent is None or parent.outline_id != outline.id:
            raise ValueError("目标章不存在")
        if not is_chapter(parent):
            raise ValueError("目标必须是章节点")
        target_group = next(group for group in groups if group[0].id == parent.id)
    group.pop(index)
    groups = [row for row in groups if row]
    if target_group is None:
        # 升到顶层：变成一章（position 是插到第几章之前，缺省追加到末尾）
        node.node_type = CHAPTER_TYPE
        insert_at = len(groups)
        if position and 1 <= position <= len(groups):
            insert_at = position - 1
        groups.insert(insert_at, [node])
    else:
        node.node_type = POINT_TYPE
        members = target_group[1:]
        insert_at = len(members)
        if position and 1 <= position <= len(members):
            insert_at = position - 1
        target_group.insert(1 + insert_at, node)
    return _commit_move(db, course, node, groups, preview=preview)


def _mapping_of(pairs: list[tuple[OutlineNode, str]]) -> dict[str, str]:
    """{旧 key: 新 key}（只收真正变了的）。"""
    out: dict[str, str] = {}
    for row, new_key in pairs:
        old_key = str(row.node_key or "")
        if old_key and old_key != new_key:
            out[old_key] = new_key
    return out


def _commit_move(db: Session, course: Course, node: OutlineNode,
                 groups: list[list[OutlineNode]], *, preview: bool = False) -> dict:
    pairs = plan_keys_for_groups(groups)
    impact = node_impact(db, course, node.id, pairs=pairs)
    if preview:
        # 只预览：把内存里的编号改动丢掉（没有落库，rollback 足够）
        db.rollback()
        return {"node": node, "renumbered": _mapping_of(pairs), "impact": impact,
                "preview": True}
    mapping = apply_plan_keys(db, course, pairs)
    _mark_planned(course)
    db.commit()
    db.refresh(node)
    return {"node": node, "renumbered": mapping, "impact": impact}


# ---------------------------------------------------------------------------
# P3-1：大纲历史（快照 / diff / 回滚）
# ---------------------------------------------------------------------------

PLAN_HISTORY_KEY = "plan_history"
PLAN_PARAMS_KEY = "plan_params"
PLAN_HISTORY_MAX = 10

_SNAPSHOT_FIELDS = ("title", "node_type", "objective", "teaching_strategy",
                    "assessment", "prerequisites")


def plan_params(course: Course) -> dict:
    """当前大纲是用什么参数生成的（同参才能判断“这版比上版好”）。"""
    return dict((course.meta or {}).get(PLAN_PARAMS_KEY) or {})


def plan_history(course: Course) -> list[dict]:
    """历史版本（新→旧）。"""
    return list((course.meta or {}).get(PLAN_HISTORY_KEY) or [])


def find_plan_version(course: Course, version_id: str) -> dict | None:
    for row in plan_history(course):
        if str(row.get("id")) == str(version_id):
            return row
    return None


def outline_snapshot(db: Session, course: Course) -> dict | None:
    """当前大纲的节点快照（没大纲时返回 None）。"""
    nodes = list_nodes(db, course.id)
    if not nodes:
        return None
    return {"nodes": [
        {"key": str(n.node_key or ""), "title": n.title,
         **{field: getattr(n, field) for field in _SNAPSHOT_FIELDS[1:]},
         "prerequisites": list(n.prerequisites or [])}
        for n in nodes
    ]}


def snapshot_outline(db: Session, course: Course, *, reason: str = "regenerate") -> dict | None:
    """把当前大纲存进 ``course.meta['plan_history']``；没大纲时不写入。

    摘要错误不该阻断生成，所以这里只读现有数据。
    """
    outline = get_outline(db, course.id)
    snap = outline_snapshot(db, course)
    if outline is None or snap is None:
        return None
    meta = dict(course.meta or {})
    entry = {
        "id": uuid.uuid4().hex[:12],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "title": outline.title,
        "goal": outline.goal,
        "params": plan_params(course),
        "usage": ((meta.get("aux_usage") or {}).get("outline")),
        "nodes": snap["nodes"],
    }
    meta[PLAN_HISTORY_KEY] = [entry] + plan_history(course)
    meta[PLAN_HISTORY_KEY] = meta[PLAN_HISTORY_KEY][:PLAN_HISTORY_MAX]
    course.meta = meta
    return entry


def plan_diff(old_nodes: list[dict], new_nodes: list[dict]) -> dict:
    """节点级 diff：按 node_key 对齐算出 新增 / 删除 / 改名（只看标题）。"""
    old_map = {str(row.get("key")): row for row in old_nodes or []}
    new_map = {str(row.get("key")): row for row in new_nodes or []}
    renamed = [
        {"key": key, "from": str(old_map[key].get("title") or ""),
         "to": str(new_map[key].get("title") or "")}
        for key in old_map
        if key in new_map and (old_map[key].get("title") or "") != (new_map[key].get("title") or "")
    ]
    return {
        "added": [{"key": key, "title": str(new_map[key].get("title") or "")}
                  for key in new_map if key not in old_map],
        "removed": [{"key": key, "title": str(old_map[key].get("title") or "")}
                    for key in old_map if key not in new_map],
        "renamed": renamed,
        "unchanged": sum(1 for key in old_map if key in new_map and key not in
                         {row["key"] for row in renamed}),
    }


def restore_impact(db: Session, course: Course) -> dict:
    """回滚会动掉什么：现在这版大纲上绑定的内容/习题/笔记文件。"""
    nodes = list_nodes(db, course.id)
    node_ids = [n.id for n in nodes]
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id, ContentItem.node_id.in_(node_ids))
             .all()) if node_ids else []
    quizzes = (db.query(QuizItem)
               .filter(QuizItem.course_id == course.id, QuizItem.node_id.in_(node_ids))
               .all()) if node_ids else []
    notes = _note_files_of(course, [str(n.node_key or "") for n in nodes]) if nodes else {
        "bound": False, "files": [], "count": 0, "out_dir": "", "note": "尚无大纲"}
    kinds: dict[str, int] = {}
    for row in items:
        kinds[row.kind] = kinds.get(row.kind, 0) + 1
    return {
        "nodes": len(nodes),
        "content_total": len(items),
        "content_by_kind": kinds,
        "quizzes": len(quizzes),
        "notes": {"count": int(notes.get("count") or 0),
                  "sample": list(notes.get("files") or [])[:8],
                  "note": notes.get("note", "")},
        "notice": "回滚会删掉现在这版大纲上绑定的讲义与习题（笔记文件下次导出时重写）；"
                  "当前版本会先存进历史，随时可再回滚。",
    }


def _tree_of(flat: list[dict]) -> list[dict]:
    """扁平快照（key/title/…）→ ``_persist_node`` 需要的树（章 → points）。"""
    chapters: list[dict] = []
    by_key: dict[str, dict] = {}
    for row in flat or []:
        key = str(row.get("key") or "")
        item = {"key": key, "points": [],
                **{field: row.get(field) for field in _SNAPSHOT_FIELDS if row.get(field)}}
        item.setdefault("node_type", CHAPTER_TYPE if "." not in key else POINT_TYPE)
        if "." not in key:
            chapters.append(item)
            by_key[key] = item
            continue
        parent = by_key.get(key.split(".")[0])
        if parent is None:
            chapters.append(item)  # 孤儿知识点：不猜归属，自成一块
            by_key[key] = item
            continue
        parent["points"].append(item)
    return chapters


def restore_outline(db: Session, course: Course, version_id: str) -> dict:
    """回滚到某个历史版本：现版先存历史，再用快照重建大纲。

    回滚后课程状态回到 ``planned``（需重新「大纲通过」）：不能拿旧大纲凭空换回已通过状态。
    """
    version = find_plan_version(course, version_id)
    if version is None:
        raise ValueError("历史版本不存在")
    snapshot_outline(db, course, reason="before_restore")
    _replace_existing_outline(db, course)
    outline = Outline(course_id=course.id, title=version.get("title") or course.title,
                      goal=version.get("goal") or "", content={"nodes": version.get("nodes") or []})
    db.add(outline)
    db.flush()
    order = 0
    for chapter in _tree_of(version.get("nodes") or []):
        order = _persist_node(db, outline.id, chapter, parent_key="", order=order)
    course.status = STATUS_PLANNED
    course.meta = {**(course.meta or {}), PLAN_PARAMS_KEY: dict(version.get("params") or {})}
    db.commit()
    db.refresh(outline)
    return {"ok": True, "restored": str(version_id), "outline_id": outline.id,
            "nodes": len(version.get("nodes") or []), "status": course.status}
