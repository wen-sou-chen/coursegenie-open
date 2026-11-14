"""笔记系统生成服务：把「课程 + 教师原始资料」整理成按知识点成页的 Obsidian 笔记。

设计（用户 2026-09-02 确认；2026-09-05 升级为「完整讲义优先、按知识点成页」）：
1) 网页端是唯一编辑源；导出是单向成果快照。每个文件带 frontmatter + 说明横幅；
   导出记录校验和，教师手改过的文件再次导出时跳过不覆盖（force 才覆盖）。
2) 目录结构约定：
   {out_rel}/
     00-课程概览.md             首页：课程信息 + 分章目录（知识点链接）+ 素材库
     01-{第一章名}/             每章一个目录
       1.1-{知识点名}.md        每个知识点一页（优先整篇讲义；无讲义回退分类型拼装）
       1.2-{知识点名}.md
       assets/                  本章图片附件（Markdown 内用相对路径 assets/xxx.png）
     02-{第二章名}/
       2.1-{知识点名}.md
       assets/
     习题集/{节点key}-{名}-习题.md  每个节点的全部习题（题目 + 答案 + 解析）
     图表库/                    Excalidraw 可编辑图（流程图转换）+ 课程级配图
     素材库/                    每份原始资料一页
3) 讲义 / 正文里的 ```mermaid 流程图与 diagram:// 概念图在导出时渲染成可见的
   .svg 图片内嵌在讲义页（Obsidian 直接显示成图，不再只是链接），同时把
   可编辑的 .excalidraw 原件写入 图表库/ 并以「可编辑原件」双链露出；
   讲义内嵌配图复制到本章 assets/ 并改写为相对路径。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import posixpath
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import ContentItem, Course, OutlineNode, QuizItem, SourceMaterial
from app.services.concept import (CONCEPTS_DIR, chapter_concept_map, concept_pages,
                                   concept_slug_map, normalize_key)
from app.services.excalidraw_chart import chart_to_excalidraw_doc
from app.services.excalidraw_flow import mermaid_to_excalidraw_text
from app.services.excalidraw_format import normalize_document
from app.services.deck_label import chapter_label, strip_chapter_prefix
from app.services.material import (
    ANCHOR_STAMP,
    anchor_kind,
    asr_page_note,
    cleaning_info,
    detection_info,
    grounding_of,
    list_materials,
    material_text,
    must_clean,
    numbered_paragraphs,
    ocr_page_note,
    origin_label,
    pages_of,
)
from app.services import md_tables
from app.services import scenario
from app.services.quiz_rules import OPEN_TYPES, quiz_level, quiz_minutes, quiz_rubric

logger = logging.getLogger(__name__)

KB_KEY = "kb"
FINGERPRINT_KEY = "cg_fingerprint"
# 二进制位图无法内嵌指纹：按「机器所有」处理
MACHINE_OWNED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# 图表库资产（svg/excalidraw/位图）是纯机器产出：图形每次按网页端内容重建，
# 「没有指纹」只能说明它是旧版导出的文件，不能当成人手修改的证据（7-补-b）
MANAGED_ASSET_SUFFIXES = MACHINE_OWNED_SUFFIXES | {".svg", ".excalidraw"}
CHARTS_DIR = "图表库"
QUIZ_DIR = "习题集"
MATERIALS_DIR = "素材库"
LESSON_DIR = "教案"
SHEET_DIR = "学案"
PPT_DIR = "课件"
PPT_SUFFIXES = {".pptx"}
ASSETS_DIR = "assets"
ARCHIVE_DIR = "99_归档"
TIDY_PREFIX = "_待整理_"

CHART_LABELS = {
    "bar": "柱状图", "line": "折线图", "pie": "饼图",
    "mindmap": "思维导图", "timeline": "时间轴", "table": "表格",
}
COURSE_LEVEL_FILE = "00-课程级内容.md"
# 课程体检报告（任务 24-C 由 audit 服务生成）：它是「描述其余产出的那一份」，
# 链接体检 / 导出健康统计都要把它排除，否则报告会把自己算进去（文件数、笔记数、
# 链接数随它自己是否已存在而变 → 首次生成与再次生成字节不一致，破坏幂等）。
AUDIT_REPORT_NAME = "00-课程体检报告.md"
# 课程索引页（任务 48s）：次级课程页顶部回链到它，索引本身才有入链、不成孤立页
COURSE_INDEX_FILE = "00-课程概览.md"
COURSE_INDEX_BACKLINK = f"> 返回课程索引：[[{COURSE_INDEX_FILE[:-3]}|课程概览]]"

_RE_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
# 「第N章」前缀剥离与展示标签统一走 deck_label（唯一口径，前端 chapterLabel.ts 对齐）；
# 这里留一个等价别名给旧的调用点（`_chapter_rel`），行为完全一致。
_RE_CHAPTER_PREFIX = re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百]+\s*章[\s:：、.．-]*")
_RE_MERMAID = re.compile(r"```mermaid[ \t]*\r?\n(.*?)```", re.S)
_RE_MD_IMAGE = re.compile(r"\]\(/images/([^)]+)\)")
_RE_DIAGRAM_REF = re.compile(r"!\[[^\]]*\]\(\s*diagram://([A-Za-z0-9_-]+)\s*\)")
_RE_CHART_REF = re.compile(r"!\[[^\]]*\]\(\s*chart://([A-Za-z0-9_-]+)\s*\)")
_RE_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)
_RE_FP_LINE = re.compile(r"^cg_fingerprint:.*\r?\n?", re.M)
_RE_IMG_LINE = re.compile(r"^[ \t]*!\[[^\]]*\]\([^)]*\)[ \t]*$", re.M)
_RE_SVG_FP = re.compile(r'<!--\s*' + FINGERPRINT_KEY + r':\s*"?([0-9a-f]{6,})"?\s*-->(?:\r?\n)?')
_RE_ALL_IMG = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# 阅读图例（任务 57）：笔记页与交付讲义顶部**共用同一份文案**，避免两处漂移。
# 起因：用户读笔记时问「那个 #33 #35 表示的是什么」——来源写法不自解释、紫色双链没人说明。
READ_LEGEND = (
    "**怎么读**：`（来源:《素材标题》第 N 段）` = **文本素材**的出处，可在素材页按 `[N]` 定位；"
    "音视频素材写 `（来源:《录像名》06:47）`（时间戳，按素材页段首的 `[mm:ss]` 定位）；"
    "紫色文字 = 可点击的**内部链接**（概念页 / 素材页 / 习题页 / 图片原件），"
    "悬停可预览、点击会打开；不想要预览可在 Obsidian 设置里关掉。"
)

BANNER = [
    "> ⚠️ 本文件由 CourseGenie 自动生成，网页端是唯一编辑源。",
    "> 修改课程请回到 CourseGenie 网页端操作后重新导出；若你手动改过本文件，再次导出会跳过覆盖（不会弄乱你的修改）。",
    ">",
    "> " + READ_LEGEND,
]


# 任务 133：客户第一次拿到 ``笔记库/`` —— 只说「放进自己的 vault」不够：``图表库/`` 里的
# ``.excalidraw`` 不装 Obsidian 社区插件 Excalidraw 就是一串看不懂的 JSON。这段话在**两处**
# 出现且**同一口径**：笔记库首页 ``00-课程概览.md``（``_index_page``）与交付包
# ``00-交付说明.md`` §2（``delivery._readme_lines`` 直接复用 ``vault_plugin_note()``）。
# 「打开本地仓库」与「Excalidraw」两个字样由
# ``tests/test_delivery.py::test_package_docs_explain_how_to_open_the_vault`` 钉住（删掉就红）。
# 任务 134：同一口径里补上 ``_风格库.excalidrawlib``（Excalidraw 插件专属**素材库**，与
# ``.excalidraw`` 同归「装了插件才有用」）、插件安装路径与**英文界面**按钮名；仍然**只此一处**，
# 两处复用同一个 helper 所以不会飘。由
# ``tests/test_delivery.py::test_package_docs_explain_the_style_library`` 钉住（删掉就红）。
# 任务 135：复测 134 抓到**引向不存在文件**的话 —— 风格库那句原来收尾在「不装的话看 `.svg` 那一份
# 就行」，可 `图表库/` 里除 `.excalidraw` / `.svg` 就只有 `_风格库.excalidrawlib` 这**一个**文件
# （**它没有 svg 版**）⇒ 改成如实说明；并补上装插件的**第一步「先关掉受限模式」**（官方要求，
# 漏掉首次用户会卡住）与对应英文界面名。由
# ``tests/test_delivery.py::test_package_docs_style_library_has_no_svg_and_plugin_first_step`` 钉住。
# 任务 136：复测 135 指出这里的中文按钮名与 **Obsidian 官方简体中文**不符 —— 官方翻译
# （``obsidianmd/obsidian-translations`` → ``translations/zh.txt``）里
# ``[interface.start-screen.option-open-folder-as-vault]`` 的 ``translation=`` 是 **打开本地仓库**，
# 「打开文件夹作为仓库」是字面直译。⇒ 改成官方译名；英文 ``Open folder as vault`` **保留**
# （中英都给，客户照哪个都能找到）。由
# ``tests/test_delivery.py::test_package_docs_explain_how_to_open_the_vault`` 钉住（改错就红）。
VAULT_OPEN_FOLDER = "打开本地仓库"
VAULT_OPEN_FOLDER_EN = "Open folder as vault"
VAULT_PLUGIN_MENU = "设置 → 第三方插件"


def vault_plugin_note(prefix: str = "") -> str:
    """「图怎么看」：两处共用**同一句话**（任务 133；任务 134 补风格库与装插件路径）。

    ``prefix`` 供交付说明加 ``笔记库/``。哪些要插件、哪些不要**只有这一处**说明：
    ``.excalidraw`` 与 ``_风格库.excalidrawlib`` 要 Excalidraw 插件，``.svg`` / Mermaid / Canvas 不要。
    """
    return (
        f"`{prefix}图表库/` 里每张图有**两份**：`.svg` **不装任何插件就能直接看**；"
        f"`.excalidraw` 是**可编辑原件**，要装 Obsidian 社区插件 **Excalidraw**（第三方、免费）"
        f"才能打开与编辑 —— **不装也不影响看图**（看 `.svg` 那份即可）；"
        f"装插件：Obsidian 里打开「{VAULT_PLUGIN_MENU}」（英文界面："
        f"`Settings` → `Community plugins`），**先关掉受限模式**（把 `Restricted mode` 关掉，"
        f"或点 `Turn on community plugins`）—— 这是官方要求的第一步，不关掉的话「浏览」里什么都装不了；"
        f"再点「浏览」搜 **Excalidraw**（`Browse`），安装后启用；"
        f"同一层还有 `{prefix}图表库/_风格库.excalidrawlib`，那是给 Excalidraw 插件用的**素材库**"
        f"（角色、卡片、连线、色卡这些可拖拽素材）—— **它没有 `.svg` 预览版**（只有这一个文件），"
        f"**同样是「装了插件才有用」**：不装插件就用不上它，**但不影响其它任何内容**"
        f"（每张图的 `.svg` 都照常能看）；"
        f"知识图谱（`{prefix}概念库/_图谱.md` 里的 Mermaid 图）与 `{prefix}概念库/_图谱.canvas`"
        f"（画布）**都是 Obsidian 自带能力，不需要任何插件**。"
    )


# 任务 139（用户 2026-10-06 提的路子）：别让客户自己啃 Obsidian 的安装步骤 —— 客户装一个 AI 办公助手
# （他举例 = WorkBuddy），让它替客户把 Obsidian 装好、把收到的 `笔记库/` 导入进去；不用了、卸掉也不影响
# 笔记库。**同时一条硬规矩**：笔记目录不能放在那个助手的工作目录里 —— 那是它自己的地盘（140 只读实测：
# 真机那份 `C:\Users\wen\WorkBuddy` 下按时间戳命名的子目录从 3 月累积到现在、一个都没少 ⇒ **「建」可证、
# 「删」不可证**，所以文案只说「建」），放进去迟早被搅乱。
# 同一口径出现在**两处**：笔记库首页 `00-课程概览.md`（`vault_getting_started()`，放在 ①②③ **之前**）
# 与交付包 `00-交付说明.md` §2 第 7 条（`delivery._readme_lines`）—— 复用同一个 helper 所以不会飘。
# 由 `tests/test_delivery.py::test_package_docs_offer_the_ai_helper_shortcut_and_the_workdir_warning`
# 钉住：两份文本里都要有 `WorkBuddy` 与「工作目录」（删掉任一处就红）；140 起这条用例还把两处那段
# **直接逐字比对** ⇒ 只改一处那段的后半句也红（139 时只钉了首句，那正是复测 m3 的漏洞）。
#
# ⚠️ 只能写「**可以让它帮你**」，**不许**写「一定能装好 / 保证能导入 / 它自动就会」—— 我们没端到端验过，
# 不替第三方打包票（「以它自己的说法为准」这句对冲不许删，用例钉着）。⚠️ 也**不提**「把课程塞进助手自己
# 的知识库」（用户明确说不要）。⚠️ **不替第三方软件的行为打包票**：只说站得住的（工作目录里按时间戳
# **建**子目录 —— 只读实测可证），不提没证过、也没验过的行为（例如「删除」）。
# ⚠️ 工作目录路径写成 `C:\Users\<你的用户名>\WorkBuddy`：包是**给客户看**的、客户用户名各不相同 ⇒ 要写清
# **把 `<你的用户名>` 换成你自己的用户名**（140）；macOS 只写「位置类似、一般在你的个人文件夹里」，
# **不编具体路径**（140）。真机实测值 `C:\Users\wen\WorkBuddy` **不写进包内文案**。
def vault_ai_helper_note() -> str:
    """「不想自己装？」：AI 办公助手可代劳 ＋ 「别放它工作目录里」的硬提醒（任务 139 / 140）。

    两处（笔记库首页 / 交付说明 §2 第 7 条）**逐字同一段**：都调这个 helper；140 起由那条用例
    **直接比对两处文本** ⇒ 只改一处就红。

    140 收口：① 只说站得住的话 —— 那个目录里按时间戳命名的子目录**只增不减**（只读实测），所以只说
    「建」、不提「删」；② 路径写清「把 `<你的用户名>` 换成你自己的用户名」，再补一句 macOS 的位置
    （不编具体路径）；③ 保留「以它自己的说法为准」这层对冲（用例钉着，删了就红）。
    """
    return (
        "**不想自己装？** 如果你已经在用 **AI 办公助手**（例如 **WorkBuddy**），可以直接让它**帮你**"
        "把 **Obsidian 装好**、把这个 `笔记库/` **导入进去**（把解压出来的目录指给它就行；"
        "**具体能不能装好、怎么导入，以它自己的说法为准**）。**它不是必需的**：以后不用它了、"
        "**卸掉也不影响笔记库**（笔记库就是本地一堆文件，Obsidian 自己能打开）。"
        r"⚠️ **一条硬提醒：笔记库、以及任何课程文件，都不要放进 AI 助手的工作目录里** —— "
        r"以 **WorkBuddy** 为例，它在 Windows 上的工作目录是 `C:\Users\<你的用户名>\WorkBuddy` —— "
        r"**把 `<你的用户名>` 换成你自己的用户名**；**macOS 上位置类似，一般在你的个人文件夹里**。"
        "**它干活时会在里面按时间戳建一堆子目录**（那是它自己的地盘，"
        "**我们不建议把任何交付内容放进去**）；"
        r"**请放在独立目录**（例如 `D:\我的课程\`），或直接放进你已有的 vault 里。"
    )

def vault_getting_started() -> list[str]:
    """``00-课程概览.md`` 顶部那段之后新增的「怎么用这个笔记库」一节（任务 133 / 134）。

    三步顺序**以包内实际为准**：① 装 Obsidian → ② 打开本地仓库（「先看本页」并进这一步）
    → ③ 图怎么看。「怎么打开」与「图怎么看」两步由 ``VAULT_OPEN_FOLDER`` 与 ``vault_plugin_note()``
    提供字面串，交付说明第 7 条复用同一出处（任务 134 复测：133 的提交/文档把第 3 步描述成
    「先看本页」，与包内实际不符 ⇒ 以包内为准，别再把「先看本页」当第 3 步）。

    任务 135：三步**真的编号 ①②③**（原来用 markdown 的 ``1.`` / ``2.`` / ``3.``，交付说明那边
    第 ③ 步还退成了挂尾从句）⇒ 两处统一成 ①②③，由
    ``tests/test_delivery.py::test_package_docs_pin_the_three_steps`` 钉住（去掉 ③ 会红）。

    任务 136：② 的中文按钮名改成官方简体中文「打开本地仓库」（原来「打开文件夹作为仓库」是直译）；
    并由 ``tests/test_delivery.py::test_package_docs_explain_how_to_open_the_vault`` 钉住。

    任务 139：③ 之前先插一小段「**不想自己装？**」（AI 助手可代劳 ＋ 别放它工作目录里的硬提醒），
    两处复用 ``vault_ai_helper_note()``。
    """
    return [
        "## 怎么用这个笔记库（3 步）", "",
        # 任务 139：先让人看到有省事的路子（AI 助手代劳），再给 ①②③ 三步
        vault_ai_helper_note(), "",
        "① **装 Obsidian**：到官网 **obsidian.md**（在浏览器里搜「Obsidian」也能找到）"
        "下载安装（免费，Windows / macOS 都有）。",
        f"② **打开它 → 选「{VAULT_OPEN_FOLDER}」**（英文界面里这个按钮叫 "
        f"`{VAULT_OPEN_FOLDER_EN}`），指向解压出来的 `笔记库/` 这个文件夹"
        "（或把它整个拷进你已经有的 vault 里也行）——先看**本页**，下面目录里带方括号的都能点着跳转。",
        "③ **图怎么看**：" + vault_plugin_note(), "",
    ]


def course_kb(course: Course) -> dict:
    return dict((course.meta or {}).get(KB_KEY) or {})


def bind_course(course: Course, vault_root: str | None = None,
                output_rel: str | None = None) -> None:
    """设置课程成果笔记的目标根目录（vault）与导出子目录。"""
    meta = dict(course.meta or {})
    kb = course_kb(course)
    if vault_root:
        kb["vault_root"] = str(Path(vault_root).expanduser())
    if output_rel:
        kb["output_rel"] = output_rel
    meta[KB_KEY] = kb
    course.meta = meta


def export_root_for(course: Course | None = None) -> str:
    """导出目标根：课程绑定 > 全局 OBSIDIAN_VAULT_ROOT > 应用默认 storage/kb。"""
    if course is not None:
        root = course_kb(course).get("vault_root")
        if root:
            return root
    settings = get_settings()
    if settings.obsidian_vault_root:
        return settings.obsidian_vault_root
    return str((settings.storage_dir / "kb").resolve())


def _safe(root: str, rel: str) -> Path:
    base = Path(root).expanduser().resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"路径越界：{rel}")
    return target


def _slug(name: str, limit: int = 60) -> str:
    cleaned = _RE_ILLEGAL.sub("_", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:limit] or "untitled"


_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]|]+)(?:\|([^\[\]]+))?\]\]")


def plain_wikilinks(text) -> str:
    """把**源文档自带**的 Obsidian 双链降级成纯文本（保留可见文字）。

    真机现场（任务 48g 验收）：这门课的素材之一是教师自己的 Obsidian 笔记，原文里带
    跨笔记双链（如「→ 参见 [[PE_输出控制策略]]」）。「来源」小节与素材页**照抄**这些
    文字后，双链在课程笔记库/交付包里指向**不存在的文件** ⇒ ① 一致性自检拒绝对交付包
    （`verify_course_notes` 报 `link` 不一致）、② 读者点开是空的、③ 链接体检报 27 处坏链。
    双链是**源文档内部**的引用，在课程库里没有意义 ⇒ 只保留显示文字（`[[A|B]]` → `B`）。
    """
    return _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), str(text or ""))


def _hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _now_iso() -> str:
    """UTC 时间戳（7e：前端据此显示「已导出/已同步 时间」）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _body_fingerprint(md: str) -> str:
    """正文（frontmatter 之后部分）的 sha1：写入文件、用于判断文件是否被手改。"""
    match = _RE_FRONTMATTER.match(md)
    body = md[match.end():] if match else md
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def _without_fp_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if not ln.lstrip().startswith(f"{FINGERPRINT_KEY}:")]


def stamp_markdown(text: str) -> str:
    """给 markdown 盖正文指纹（只对以 frontmatter 开头的文件生效）；可重复调用。"""
    lines = text.rstrip("\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return text.rstrip("\n") + "\n"
    fences = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(fences) < 2:
        return text.rstrip("\n") + "\n"
    at = fences[1]
    head, tail = _without_fp_lines(lines[:at]), lines[at:]
    fp = _body_fingerprint("\n".join(head + tail).rstrip() + "\n")
    return "\n".join([*head, f'{FINGERPRINT_KEY}: "{fp}"', *tail]).rstrip() + "\n"


def stamp_fingerprint(parts: list[str]) -> str:
    """把 parts 拼成 markdown 后盖章（保留旧调用方式）。"""
    return stamp_markdown("\n".join(parts))


def _svg_fp_marker(fp: str) -> str:
    return f'<!-- {FINGERPRINT_KEY}: "{fp}" -->'


def stamp_svg(text: str) -> str:
    """给 SVG 盖指纹（末尾 XML 注释，浏览器与 Obsidian 直接忽略）；可重复调用。"""
    body = _RE_SVG_FP.sub("", text.rstrip("\n"), count=1).rstrip("\n") + "\n"
    return f"{body}{_svg_fp_marker(hashlib.sha1(body.encode('utf-8')).hexdigest())}\n"


def _json_body(data: dict) -> str:
    """JSON 载体（.excalidraw）的正文：去掉指纹字段后的规范化序列化。"""
    payload = {k: v for k, v in data.items() if k != FINGERPRINT_KEY}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stamp_json(text: str) -> str:
    """给 .excalidraw 这类 JSON 盖指纹（顶层字段）；非 JSON 对象原样返回。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(data, dict):
        return text
    data = {k: v for k, v in data.items() if k != FINGERPRINT_KEY}
    data[FINGERPRINT_KEY] = hashlib.sha1(_json_body(data).encode("utf-8")).hexdigest()
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def stamp_output(rel_path: str, text: str) -> str:
    """按扩展名给文本产出盖指纹：md（讲义/习题/概览/素材）、svg、excalidraw。"""
    suffix = Path(rel_path).suffix.lower()
    if suffix == ".md":
        return stamp_markdown(text)
    if suffix == ".svg":
        return stamp_svg(text)
    if suffix in {".excalidraw", ".json"}:
        return stamp_json(text)
    return text


def file_fingerprint(text: str) -> tuple[str | None, str]:
    """返回（文件里记录的指纹, 按当前正文重算的指纹）；识别 md / svg / json 三种载体。"""
    match = _RE_FRONTMATTER.match(text)
    if match:
        front, body = match.group(0), text[match.end():]
        found = _RE_FP_LINE.search(front)
        recorded = found.group(0).split(":", 1)[1].strip().strip('"') if found else None
        return recorded, hashlib.sha1(body.encode("utf-8")).hexdigest()

    found = _RE_SVG_FP.search(text)
    if found:
        body = _RE_SVG_FP.sub("", text, count=1)
        return found.group(1), hashlib.sha1(body.encode("utf-8")).hexdigest()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, hashlib.sha1(text.encode("utf-8")).hexdigest()
    if isinstance(data, dict) and FINGERPRINT_KEY in data:
        return str(data[FINGERPRINT_KEY]), hashlib.sha1(_json_body(data).encode("utf-8")).hexdigest()
    return None, hashlib.sha1(text.encode("utf-8")).hexdigest()


def _is_managed_asset(rel_path: str) -> bool:
    """受管资产判定：位于 `图表库/` 下，或是 svg / excalidraw / 位图。

    这些文件一律由网页端内容重建、不参与人手编辑，所以「没有指纹」只说明它是
    旧版导出的文件，不能当作被手改的证据（否则旧资产会被永久跳过、越用越旧）。
    """
    rel = Path((rel_path or "").replace("\\", "/"))
    return CHARTS_DIR in rel.parts or rel.suffix.lower() in MANAGED_ASSET_SUFFIXES


def _is_ppt_asset(rel_path: str) -> bool:
    """课件资产（.pptx）：二进制、无内嵌指纹，只有 manifest 记录一致才敢覆盖。"""
    rel = Path((rel_path or "").replace("\\", "/"))
    return rel.suffix.lower() in PPT_SUFFIXES or PPT_DIR in rel.parts


def _skip_reason(old_bytes: bytes, recorded: str | None, rel_path: str = "") -> str | None:
    """文件与 manifest 记录不一致时，判断该不该覆盖。

    - None            → 可以覆盖：①文件内容与 manifest 记录一致（说明没被手改）；
                        ②正文与自带指纹自洽；③受管资产（图表库 svg/excalidraw/位图）
                        缺指纹（旧版文件，覆盖时会补盖指纹）
    - "manual-edit"   → 正文指纹对不上，教师手改过 → 跳过覆盖（no-clobber）
    - "unknown-drift" → 没有指纹、又不是受管资产（多为旧版 markdown）→ 保守跳过
    """
    if recorded == _hash(old_bytes):
        return None
    try:
        text = old_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None:
        stored, actual = file_fingerprint(text)
        if stored is not None:
            return None if stored == actual else "manual-edit"
    if _is_ppt_asset(rel_path):
        # 课件是二进制、没有内嵌指纹：与 manifest 对不上即视为被手改，保守保留
        return "ppt-edited" if recorded else "unknown-drift"
    if _is_managed_asset(rel_path):
        return None
    return "unknown-drift"


PRESERVED_REASONS = {
    "manual-edit": "检测到手动修改，已跳过覆盖",
    "unknown-drift": "与记录不一致且无版本指纹，保守跳过",
    "ppt-edited": "课件与上次导出记录不一致（可能被手动修改），已跳过覆盖",
}


def preserved_entries(preserved) -> list[dict]:
    """把 preserved 统一成 [{"file": 相对路径, "reason": 原因}]，兼容旧版字符串记录。"""
    out: list[dict] = []
    for entry in preserved or []:
        if isinstance(entry, dict):
            out.append({"file": str(entry.get("file") or ""),
                        "reason": str(entry.get("reason") or "unknown-drift")})
        else:
            out.append({"file": str(entry), "reason": "unknown-drift"})
    return out


def _tidy_root(root: Path, course: Course) -> Path:
    """清理目标目录：`{vault}/99_归档/_待整理_YYYYMMDD/孤儿文件-{课名}-{时间}/`。"""
    today = datetime.now().strftime("%Y%m%d")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / ARCHIVE_DIR / f"{TIDY_PREFIX}{today}" / f"孤儿文件-{_slug(course.title, 30)}-{stamp}"


def _is_machine_note(path: Path) -> bool:
    """markdown 页是否仍是「机器那份」：自带 cg_fingerprint 且与内容自洽。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    stored, actual = file_fingerprint(text)
    return stored is not None and stored == actual


def prune_stale_files(out_dir: Path, root: Path, course: Course, written: list[str],
                      preserved: list, *, keep: Iterable[str] = (),
                      dry_run: bool = False) -> list[dict]:
    """清理增量写入留在课程目录里的旧文件（孤儿图表、已消失的页）。

    增量写入只覆盖同名文件，换布局/重建卡片/删条目后旧图会变成无人引用的孤儿，
    越攒越多（2026-09-08 起登记的问题）。这里在每次导出末尾收一次：
    只动同时满足三条件的文件——①机器产物（`图表库/` 与章节 `assets/` 的
    svg/excalidraw/位图，或带自洽指纹的 markdown 页）；②本次导出没有产出它；
    ③不在 no-clobber 保留名单里（教师手改过的文件指纹对不上，会进 preserved
    而被保留）。清理方式是**移动到 vault 的 `99_归档/_待整理_YYYYMMDD/`**，
    不是删除，教师可随时拖回。

    ``keep`` 是「本次导出稍后还会重写、但此刻尚未落盘」的 vault 相对路径：导出
    收尾的体检报告就走这条（报告挪到最后生成，见 :func:`export_course`）。这类
    文件按定义不是孤儿，只看 ``written`` 会被误收进 99_归档，所以显式排除。
    """
    written_set = set(written or [])
    written_set.update(keep or ())
    keep_set = {entry.get("file") for entry in preserved_entries(preserved)}
    archive_root = _tidy_root(root, course)
    moved: list[dict] = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in written_set or rel in keep_set:
            continue
        inside = path.relative_to(out_dir)
        machine = (path.suffix.lower() in MANAGED_ASSET_SUFFIXES
                   or CHARTS_DIR in inside.parts
                   or PPT_DIR in inside.parts
                   or (path.suffix.lower() == ".md" and _is_machine_note(path)))
        if not machine:
            continue
        if dry_run:
            moved.append({"file": rel, "archived_to": (archive_root / inside).as_posix()})
            continue
        dest = archive_root / inside
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(dest)
        except OSError as exc:  # noqa: BLE001 —— 单个文件失败不影响整课导出
            logger.warning("清理旧文件失败 %s: %s", rel, exc)
            continue
        moved.append({"file": rel, "archived_to": dest.as_posix()})
    if not dry_run:
        for path in sorted((d for d in out_dir.rglob("*") if d.is_dir()), reverse=True):
            if path.name in (CHARTS_DIR, QUIZ_DIR, MATERIALS_DIR, CONCEPTS_DIR,
                            ASSETS_DIR, PPT_DIR):
                continue
            try:
                path.rmdir()
            except OSError:
                pass
    return moved


def _write_safe(out_dir: Path, rel_path: str, content: str, manifest: dict,
                force: bool, root: Path, preserved: list, written: list,
                raw: bytes | None = None) -> None:
    """no-clobber 写入：上次导出后未被手动改动的文件才会被覆盖。

    文本产出在写入前统一盖版本指纹（md 进 frontmatter、svg 进 XML 注释、
    excalidraw 进顶层字段），这样「机器写的」与「教师手改的」可区分。
    跳过的文件记入 preserved，形如 {"file": 相对路径, "reason": 原因}，
    便于任务结果与网页端显式展示「哪些文件没更新、为什么」。
    """
    target = out_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    data = raw if raw is not None else stamp_output(rel_path, content).encode("utf-8")
    new_hash = _hash(data)
    rel = target.relative_to(root).as_posix()
    if target.exists():
        old_bytes = target.read_bytes()
        old_hash = _hash(old_bytes)
        if old_hash == new_hash:
            # 内容与本次要写的一模一样 → 就是机器那份；顺手对齐 manifest，
            # 免得旧导出留下的过期记录让这份文件之后被误判成 unknown-drift
            manifest[rel] = new_hash
            written.append(rel)
            return
        reason = None if force else _skip_reason(old_bytes, manifest.get(rel), rel)
        if reason:
            preserved.append({"file": rel, "reason": reason})
            return
    target.write_bytes(data)
    manifest[rel] = new_hash
    written.append(rel)


def _frontmatter(entries: list[tuple[str, str]]) -> list[str]:
    return ["---", *[f"{k}: \"{v}\"" for k, v in entries], "---", ""]


def _image_src(item: ContentItem) -> Path | None:
    rel = (item.body or {}).get("path")
    if not rel:
        return None
    src = get_settings().storage_dir / str(rel)
    return src if src.exists() else None


def _storage_image(rel_url: str) -> Path | None:
    """/images/xxx.png 中的文件名 → storage/images/xxx.png。"""
    src = get_settings().storage_dir / "images" / Path(rel_url).name
    return src if src.exists() else None


# ---------------------------------------------------------------------------
# 数据整理
# ---------------------------------------------------------------------------

def _nodes(db: Session, course_id: str) -> list[OutlineNode]:
    from app.services.planner import list_nodes

    return list_nodes(db, course_id)


def _chapter_list(nodes: list[OutlineNode]) -> list[OutlineNode]:
    top = [n for n in nodes if "." not in n.node_key]
    return top or nodes


def _members_of(chapter: OutlineNode, nodes: list[OutlineNode]) -> list[OutlineNode]:
    return [n for n in nodes if n.node_key == chapter.node_key
            or n.node_key.startswith(f"{chapter.node_key}.")]


def _content_items(db: Session, course_id: str, node_id: str | None) -> list[ContentItem]:
    q = db.query(ContentItem).filter(ContentItem.course_id == course_id)
    if node_id is not None:
        q = q.filter(ContentItem.node_id == node_id)
    else:
        q = q.filter(ContentItem.node_id.is_(None))
    return list(q.order_by(ContentItem.created_at).all())


def _quizzes(db: Session, course_id: str, node_id: str | None) -> list[QuizItem]:
    q = db.query(QuizItem).filter(QuizItem.course_id == course_id)
    if node_id is not None:
        q = q.filter(QuizItem.node_id == node_id)
    else:
        q = q.filter(QuizItem.node_id.is_(None))
    return list(q.order_by(QuizItem.created_at).all())


def _node_stem(node: OutlineNode) -> str:
    return f"{node.node_key}-{_slug(node.title, 60)}"


def _node_md_rel(chapter_dir: str, node: OutlineNode) -> str:
    return f"{chapter_dir}/{_node_stem(node)}.md"


def _quiz_rel(node: OutlineNode) -> str:
    return f"{QUIZ_DIR}/{_node_stem(node)}-习题.md"


def _chapter_dir(idx: int, chapter: OutlineNode) -> str:
    return f"{idx:02d}-{_slug(chapter.title, 40)}"


def _deck_items(db: Session, course: Course,
                nodes: list[OutlineNode]) -> list[tuple[str, Path, OutlineNode | None]]:
    """[(课件在 Vault 的相对路径, 源 pptx, 所属章 None=整课)]。

    只收录源文件真实存在的课件；整课课件命名 `00-整课课件.pptx`，
    章课件命名 `第{N}章-{章名}课件.pptx`（与任务 20 规格一致）。
    """
    from app.services.ppt import SCOPE_CHAPTER, SCOPE_COURSE, list_decks

    def _chapter_rel(chapter: OutlineNode) -> str:
        # 章名自带「第N章」前缀时先去掉，避免出现「第1章-第1章-…」
        title = strip_chapter_prefix(chapter.title)
        return f"{PPT_DIR}/第{chapter.node_key}章-{_slug(title or chapter.title, 40)}课件.pptx"

    by_id = {node.id: node for node in nodes}
    by_key = {str(node.node_key or ""): node for node in nodes}
    items: list[tuple[str, Path, OutlineNode | None]] = []
    for deck in list_decks(db, course.id):
        meta = dict(deck.meta or {})
        src = Path(str((deck.body or {}).get("pptx") or ""))
        if not src.exists():
            continue
        if str(meta.get("scope") or SCOPE_COURSE) == SCOPE_CHAPTER:
            # 老数据 node_id 为 NULL（章号只在 meta.chapter_key）→ 按章号匹配当前大纲节点兜底
            chapter = by_id.get(str(meta.get("chapter_node_id") or "")) \
                or by_key.get(str(meta.get("chapter_key") or ""))
            if chapter is None:
                continue
            items.append((_chapter_rel(chapter), src, chapter))
        else:
            items.append((f"{PPT_DIR}/00-整课课件.pptx", src, None))
    items.sort(key=lambda item: item[0])
    return items


def _deck_names(db: Session, course: Course,
                nodes: list[OutlineNode]) -> tuple[str | None, dict[str, str]]:
    """(整课课件文件名, {章 node_key: 章课件文件名})——供概览页/章节页生成双链。"""
    course_name: str | None = None
    chapters: dict[str, str] = {}
    for rel, _src, chapter in _deck_items(db, course, nodes):
        if chapter is None:
            course_name = Path(rel).name
        else:
            chapters[chapter.node_key] = Path(rel).name
    return course_name, chapters


def _node_has_content(db: Session, course_id: str, node: OutlineNode) -> bool:
    return bool(_content_items(db, course_id, node.id) or _quizzes(db, course_id, node.id))


# ---------------------------------------------------------------------------
# Markdown 片段
# ---------------------------------------------------------------------------

def _material_page(course: Course, m: SourceMaterial) -> list[str]:
    # 任务 69 §B1.5.1：来源缺省值改用**来源类型**标签（人工上传 / 粘贴文本(疑似 AI 产物) /
    # AI 补充）—— 老口径写死「粘贴文本 / 教师素材」，AI 补充的会显示成「粘贴文本」。
    source = str(m.meta.get("original") or origin_label(m))
    # 任务 57：每段前加 [N]，口径与提示词 material_prompt_section(numbered=True) **同一函数**
    # （序号 = 素材解析顺序）—— 正文里的「（来源:《X》第 N 段）」才能在本页按 [N] 定位。
    # 48g 紧急修复：源文档自带的 [[双链]] 降级成纯文本（见 plain_wikilinks），否则整页坏链。
    # 任务 69：正文与注入侧共用 material_text（入库卫生口径），[N] 才能与提示词逐段一致；
    # 老数据在这里就地剥离（**不动库里的原文**），并在页首写明「已清理 N 行」。
    text = material_text(m)
    body = "\n".join(numbered_paragraphs(plain_wikilinks(text), max_chars=None))
    # 任务 54：OCR 素材在页面顶部写明「由 OCR 识别（引擎/版本/平均置信度）」或跳过原因
    ocr_note = ocr_page_note(m)
    # 任务 102：音视频素材写明「由本地语音转写（引擎/模型/时长/段数/平均置信度）」或
    # 转写中 / 跳过 / 失败的原因 —— 读者才知道这页为什么没有正文
    asr_note = asr_page_note(m)
    # 任务 69 §B1.5：页数落库后写进页眉（原先靠每次读原件现算，原件丢了就显示「—」）
    pages = pages_of(m)
    cleaned = cleaning_info(m)
    detected = detection_info(m)
    grounding = grounding_of(m)
    # 任务 72 §B1：三种状态的说法必须不一样 ——
    #   ① ``shell_stripped`` ⇒ 库里的正文真改过（入库即剥 / 点过「一键清理」）；
    #   ② 必须干净但没落库（老数据）⇒ 只在**出库**（注入 / 本页）时剥，库内保留原文；
    #   ③ 上传原件 ⇒ 「检测到 N 行…（按口径保持原样）」，见下面的 detected 分支。
    # 修前这里只有一句「已清理 N 行（入库时自动剥离；库里的原文保持原样）」——自相矛盾：
    # 素材 6 实测库内正文 2926 字里外壳还在，说「已清理」会让人以为库里已经改过。
    if cleaned["lines"] and (m.meta or {}).get("shell_stripped"):
        strip_note = f"> 已清理 {cleaned['lines']} 行对话外壳 / 页眉（库内正文已更新）"
    elif cleaned["lines"]:
        strip_note = (f"> 出库时会剥离 {cleaned['lines']} 行对话外壳 / 页眉（库内保留原文；"
                      "要一并落库可在网页端「原始资料」里点「一键清理」）")
    else:
        strip_note = ""
    meta_bits = [f"字数：{len(text)}"]
    if pages:
        meta_bits.append(f"页数：{pages}")
    # 任务 69 §B1.5：来源类型写进页首（人工上传 / 粘贴文本(疑似 AI 产物) / AI 补充）——
    # 本课素材本身就是 AI 生成的，「来源」二字必须在交付物里说清（用户 2026-09-21 口径）。
    meta_bits.append(f"来源类型：{origin_label(m)}")
    return [
        *_frontmatter([("title", m.title), ("course", course.title), ("source", source),
                       ("tags", "coursegenie 素材")]),
        *BANNER, "",
        f"# {m.title}", "",
        f"> 来源：{source} ｜ {' ｜ '.join(meta_bits)}", "",
        *([strip_note, ""] if strip_note else []),
        # 任务 69 §B1.5.1：上传原件按用户口径**保持原样**，只提示检测到的外壳（清理是可选动作）
        # 清过之后不能再写「保持原样」（两句话会互相打脸：正文里的壳已经被剥掉了）
        *([f"> 检测到 {detected['lines']} 行疑似页眉页脚 / 对话外壳（按口径**保持原样**未改；"
           "要清可在网页端「原始资料」里点「一键清理」）", ""]
          if not must_clean(m) and detected["lines"] and not cleaned["lines"] else []),
        *([f"> AI 补充参考 ｜ 有依据 {grounding['grounded_count']} 条 ｜ "
           f"无依据 {grounding['ungrounded_count']} 条（无依据的表述**不能当事实依据**，"
           "引用它们的句子在讲义里会标「（无直接依据,建议人工确认）」）", ""]
          if str((m.meta or {}).get("source") or "") == "ai" else []),
        *([ocr_note, ""] if ocr_note else []),
        *([asr_note, ""] if asr_note else []),
        # 任务 105：音视频素材的引用是**时间戳**（正文本来就以 [mm:ss] 开头）；`[N]` 照旧保留，
        # 供旧讲义里的「第 N 段」在本页回查（历史数据一个字不用改）。
        ("> 段落号 = 按素材解析顺序编号；本素材是音视频 ⇒ 引用写段首的时间戳"
         "（如（来源: … 06:47）），本页 `[N]` 供旧讲义里的「第 N 段」回查。"
         if anchor_kind(m) == ANCHOR_STAMP else
         "> 段落号 = 按素材解析顺序编号；正文里的（来源: … 第 N 段）就是指本页 `[N]`。"), "",
        body, "",
    ]


def _material_stem(index: int, m: SourceMaterial) -> str:
    """素材页文件名（不含 .md）：导出 / 校验 / 「来源」双链共用同一口径。"""
    return f"{index:02d}-{_slug(m.title)}"


def material_link_map(db: Session, course: Course) -> dict:
    """素材 → 素材页文件名：``{material_id: stem, 归一化标题: stem}``（来源双链用）。"""
    out: dict = {}
    for index, m in enumerate(list_materials(db, course.id), 1):
        stem = _material_stem(index, m)
        out[str(m.id)] = stem
        if m.title:
            out[normalize_key(m.title)] = stem
    return out


def _quiz_block(q: QuizItem, level: str = "") -> list[str]:
    options = q.options or []
    title = f"{level}**随堂练习**" if level else "**随堂练习**"
    lines = [title, ""]
    if q.qtype == "choice" and options:
        for i, opt in enumerate(options):
            marker = "✓ " if q.answer == str(i) else "- "
            lines.append(f"{marker} {opt}")
    else:
        lines.append(f"- 问题：{q.question}")
        lines.append(f"- 分层：{quiz_level(q)}（约 {quiz_minutes(q)} 分钟）")
        rubric = quiz_rubric(q)
        if q.qtype in OPEN_TYPES and rubric:
            # 任务 55：开放题没有唯一答案 —— 给评分要点与作答区（不假装有标准答案）
            lines.append(f"- 评分要点：{rubric}")
            lines.append("- 作答区：（请在此作答，教师按评分要点批改）")
        else:
            lines.append(f"- 答案：{q.answer}")
    lines.append("")
    if q.explanation:
        lines += [f"解析：{q.explanation}", ""]
    return lines


def _quiz_file_lines(course: Course, node: OutlineNode, quizzes: list[QuizItem]) -> list[str]:
    lines = [
        *_frontmatter([("title", f"{node.node_key} {node.title} · 习题集"),
                       ("course", course.title), ("tags", "coursegenie 习题")]),
        *BANNER, "",
        f"# {node.node_key} {node.title} · 习题集", "",
        "> 本页为该知识点的全部随堂练习（题目 + 答案 + 解析），来源为 CourseGenie 讲义生成或分步出题。",
        "",
    ]
    for idx, q in enumerate(quizzes, 1):
        lines += [f"## 第 {idx} 题 · {quiz_level(q)}（约 {quiz_minutes(q)} 分钟）", ""]
        lines.append(f"{q.question}")
        lines.append("")
        rubric = quiz_rubric(q)
        if q.qtype == "choice" and (q.options or []):
            for opt_idx, opt in enumerate(q.options):
                marker = "✓ " if q.answer == str(opt_idx) else "- "
                lines.append(f"{marker} {opt}")
        elif q.qtype in OPEN_TYPES:
            # 任务 55：开放题给评分要点 + 作答区
            if rubric:
                lines += [f"- 评分要点：{rubric}", ""]
            lines += ["- 作答区：（请在此作答，教师按评分要点批改）", ""]
        elif q.qtype == "blank":
            lines += [f"- 答案：{q.answer}", ""]
        elif q.qtype == "short":
            lines += [f"- 参考要点：{q.answer}", ""]
        else:
            lines += [f"- 答案：{q.answer}", ""]
        lines.append("")
        if q.explanation:
            lines += [f"**解析**：{q.explanation}", ""]
        lines += ["---", ""]
    return lines


def _chart_note_lines(c: ContentItem) -> list[str]:
    """单个图表条目转 Markdown：表格用表格、mindmap/timeline 用 mermaid、其余存 JSON 副本。"""
    sub = c.kind[len("chart_"):]
    body = c.body or {}
    lines: list[str] = []
    if sub in ("mindmap", "timeline"):
        mermaid = str(body.get("mermaid") or "")
        if mermaid:
            lines += ["```mermaid", mermaid, "```", ""]
    elif sub == "table":
        data = body.get("data") or {}
        headers = data.get("headers") or []
        rows_data = data.get("rows") or []
        if headers:
            lines += _md_table(headers, rows_data)
    else:
        data = body.get("data")
        if data:
            import json
            lines += ["（网页端以 Chart.js 渲染，此处保存数据副本）", "",
                      "```json", json.dumps(data, ensure_ascii=False, indent=2), "```", ""]
    return lines


def _md_table(headers: list[str], rows: list) -> list[str]:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "---|" * len(headers)]
    for row in rows:
        cells = [str(c).replace("|", "\\|") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _chart_stems(charts: list) -> dict:
    """同节点所有 chart_* 条目按类型排统一序号，防止卡片图与手工图同名覆盖。"""
    counts: dict[str, int] = {}
    out: dict = {}
    for c in sorted(charts, key=lambda i: i.created_at or 0):
        sub = c.kind[len("chart_"):]
        label = CHART_LABELS.get(sub, sub)
        n = counts.get(sub, 0) + 1
        counts[sub] = n
        out[c.id] = (label, n)
    return out


def _chart_visual_block(c: ContentItem, key_name: str, stems: dict,
                        payload: NodePayload, heading: str = "") -> str:
    """单个 chart_* 条目转笔记可见块：表格=原生 md 表格；其余=Excalidraw 小图
    （可见 .svg + 图表库可编辑原件），mindmap/timeline 无图时退回 mermaid。"""
    sub = c.kind[len("chart_"):]
    label = CHART_LABELS.get(sub, sub)
    body = c.body or {}
    title = str(body.get("title") or c.title or label)[:40]
    head = f"{heading}{label}：{title}" if heading else f"**📊 {label}：{title}**"
    if sub == "table":
        text = str(body.get("markdown_table") or "").strip()
        if not text:
            data = body.get("data") or {}
            text = "\n".join(_md_table(data.get("headers") or [],
                                       data.get("rows") or [])).rstrip("\n")
        if not text:
            return head
        return f"{head}\n\n{text}"
    data = body.get("data") or {}
    doc = None
    try:
        doc = chart_to_excalidraw_doc(sub, data, title)
    except Exception:  # noqa: BLE001 —— 数据异常则退回 mermaid/纯标题
        doc = None
    if doc:
        stem_label, seq = stems.get(c.id) or (label, 1)
        stem = f"图-{_slug(stem_label, 8)}-{_slug(str(key_name), 20)}-{seq:02d}"
        xname = f"{stem}.excalidraw"
        payload.excalidraw.append((f"{CHARTS_DIR}/{xname}",
                                   json.dumps(doc, ensure_ascii=False, indent=1)))
        svg = _scene_svg(doc)
        if svg:
            sname = f"{stem}.svg"
            payload.svgs.append((f"{CHARTS_DIR}/{sname}", svg))
            return f"{head}\n\n![[{sname}]]\n\n> {label}｜可编辑原件：[[{xname}]]"
        return f"{head}\n\n> 可编辑图表：[[{xname}]]"
    if sub in ("mindmap", "timeline"):
        mermaid = str(body.get("mermaid") or (data or {}).get("mermaid") or "").strip()
        if mermaid:
            return f"{head}\n\n```mermaid\n{mermaid}\n```"
    if isinstance(data, dict) and data:
        import json as _json
        return head + "\n\n（网页端以交互图表显示，此处保存数据副本）\n```json\n" \
            + _json.dumps(data, ensure_ascii=False, indent=2) + "\n```"
    return head


def _rewrite_chart_refs(md: str, charts: list, key_name: str, stems: dict,
                        payload: NodePayload) -> tuple[str, set[str]]:
    """把讲义里「数据卡」的 chart:// 引用替换为可见 SVG 内嵌 + 可编辑原件。

    返回 (替换后的 md, **已处理**的 chart id) —— 含「就地嵌入」与「正文已有同数据表格
    而跳过」两种；两者都不该再在随堂练习前重复挂载。
    """
    from app.services import delivery as delivery_mod  # 局部导入：delivery 反向依赖本模块

    by_id = {c.id: c for c in charts}
    placed: set[str] = set()
    # 任务 65：笔记正文里已有同数据表格时，数据卡不再渲染第二遍 ——
    # 判定（md_tables）与取数（交付/发布同一份 chart_table_markdowns）都复用，
    # 四处渲染路径共用一套口径，不另写判定。
    md, skipped = md_tables.strip_duplicate_chart_refs(
        md, delivery_mod.chart_table_markdowns(charts))
    if skipped:
        logger.info("笔记正文已有同数据表格，跳过数据卡 %s（与交付/发布同一口径）",
                    "、".join(f"chart://{i}" for i in skipped))

    def _replace(match: re.Match) -> str:
        chart = by_id.get(match.group(1))
        if chart is None:
            return ""  # 数据卡已被网页端删除：不留脏引用
        placed.add(chart.id)
        return _chart_visual_block(chart, key_name, stems, payload)

    return _RE_CHART_REF.sub(_replace, md), placed | set(skipped)


def _attach_chart_blocks(md: str, charts: list, key_name: str, stems: dict,
                         payload: NodePayload) -> str:
    """把「未就地嵌入」的 chart_* 条目集中插到讲义「随堂练习」之前。"""
    blocks = [_chart_visual_block(c, key_name, stems, payload)
              for c in charts if c.id in stems]
    blocks = [b for b in blocks if b]
    if not blocks:
        return md
    chunk = "\n\n".join(blocks) + "\n"
    marker = "\n## 随堂练习"
    if marker in md:
        return md.replace(marker, f"\n{chunk}{marker}", 1)
    return md.rstrip() + "\n\n" + chunk.rstrip() + "\n"


# ---------------------------------------------------------------------------
# 节点/课程级文件生成
# ---------------------------------------------------------------------------

class NodePayload:
    """单个节点一次导出所需的全部文件载荷。"""

    def __init__(self) -> None:
        self.md_rel: str | None = None
        self.md: str = ""
        self.assets: list[tuple[Path, str]] = []     # (源文件, 目标 rel)
        self.excalidraw: list[tuple[str, str]] = []  # (目标 rel, 内容)
        self.svgs: list[tuple[str, str]] = []        # (目标 rel, SVG 文本) 供笔记直接显示
        self.quiz_rel: str | None = None
        self.quiz: str = ""
        self.plan_md: str = ""                       # 教案 markdown（任务 49）
        self.plan_docx: bytes = b""                  # 教案 Word（任务 49）
        self.sheet_md: str = ""                      # 学案 markdown（任务 50）
        self.sheet_docx: bytes = b""                 # 学案 Word（任务 50）


def _plan_rel(chapter_dir: str, node: OutlineNode, suffix: str = "教案.md") -> str:
    """教案在课程目录里的相对路径：``教案/{章目录}/{节点页名}-教案.md``。

    单独放 ``教案/`` 子树（与交付包顶层目录同名同构），知识点页目录保持干净；
    也避免插件的页链接把教案页混进讲义导航。``.docx`` 只进单节点导出 ZIP 与交付包
    —— 二进制没有内嵌指纹，放进共享 Vault 会被 no-clobber 当成「手改文件」而拒绝覆盖。
    """
    return f"{LESSON_DIR}/{chapter_dir}/{_node_stem(node)}-{suffix}"


def _lesson_plan_payload(db: Session, course: Course, node: OutlineNode,
                         lecture: ContentItem | None) -> tuple[str, bytes]:
    """(教案 markdown, .docx 字节)；没有讲义时返回空（教案从讲义派生）。"""
    if lecture is None:
        return "", b""
    from app.services import docx_export
    from app.services import lesson_plan
    from app.services import lecture_qc

    try:
        body = lesson_plan.plan_markdown(
            db, course, node, lecture_qc.lecture_markdown(lecture))
        data = docx_export.markdown_to_docx(body, title=f"《{node.title}》教案")
    except Exception as exc:  # noqa: BLE001 —— 教案失败不影响讲义等主体导出
        logger.warning("教案载荷生成失败 node=%s: %s", node.node_key, exc)
        return "", b""
    # 笔记页带 frontmatter：① 与其它笔记页同构；② stamp_markdown 才能盖指纹，
    # 换课程/重导出时按「机器产物」正常覆盖，而不是每次都被判成手改文件。
    # 任务 86：教案从讲义派生 ⇒ 口径跟着**讲义条目**走（不跟课程当前设置走，否则重导
    # 笔记库就会把老讲义标成新口径）。
    note = "\n".join([*_frontmatter([("title", f"{node.node_key} {node.title} 教案"),
                                     ("course", course.title),
                                     ("tags", "coursegenie 教案"),
                                     *scenario.frontmatter_fields(lecture.meta)]), body])
    return note, data


def _sheet_rel(chapter_dir: str, node: OutlineNode, suffix: str = "学案.md") -> str:
    """学案在课程目录里的相对路径（与教案同构，单独 ``学案/`` 子树）。"""
    return f"{SHEET_DIR}/{chapter_dir}/{_node_stem(node)}-{suffix}"


def _doc_links(node: OutlineNode, payload: "NodePayload") -> list[str]:
    """教案 / 学案页进课程索引的目录链接（任务 48s）。

    这两类页面此前只落在 ``教案/`` ``学案/`` 子树里、**索引里没有入链** ⇒ 在 Obsidian
    里是孤立页（真库实测 66 篇：AI 课 27+27、包子课 6+6），老师从课程索引点不到。
    链接目标写文件名主干（Obsidian 全库按文件名解析），与讲义页同一口径。
    """
    out: list[str] = []
    if payload.plan_md:
        out.append(f"[[{_node_stem(node)}-教案|{node.node_key} 教案]]")
    if payload.sheet_md:
        out.append(f"[[{_node_stem(node)}-学案|{node.node_key} 学案]]")
    return out


def _course_page_links(has_level: bool, has_graph: bool) -> list[str]:
    """课程索引的「课程级页面」链接（任务 48s：把这三类原来无入链的页接进索引）。

    只链**确实会写出**的页：索引里的坏链会顶到交付自检的「坏链必须为 0」门禁。
    ``00-课程体检报告.md`` 在整课导出与体检接口里都会写，所以恒链。
    """
    links: list[str] = []
    if has_level:
        links.append(f"[[{COURSE_LEVEL_FILE[:-3]}|课程级内容]]")
    links.append(f"[[{AUDIT_REPORT_NAME[:-3]}|课程体检报告]]")
    if has_graph:
        from app.services import concept_graph

        links.append(f"[[{concept_graph.GRAPH_MD_REL[:-3]}|概念知识图谱]]")
    return links


def _student_sheet_payload(db: Session, course: Course, node: OutlineNode,
                           lecture: ContentItem | None) -> tuple[str, bytes]:
    """(学案 markdown, .docx 字节)；没有讲义时返回空。"""
    if lecture is None:
        return "", b""
    from app.services import docx_export
    from app.services import lecture_qc
    from app.services import student_sheet

    try:
        body = student_sheet.sheet_markdown(
            db, course, node, lecture_qc.lecture_markdown(lecture))
        data = docx_export.markdown_to_docx(body, title=f"《{node.title}》学案",
                                            page_numbers=True,
                                            footer_text=_slug(course.title, 30))
    except Exception as exc:  # noqa: BLE001 —— 学案失败不影响讲义等主体导出
        logger.warning("学案载荷生成失败 node=%s: %s", node.node_key, exc)
        return "", b""
    # 任务 86：学案同样从讲义派生 ⇒ 口径跟讲义条目走。
    note = "\n".join([*_frontmatter([("title", f"{node.node_key} {node.title} 学案"),
                                     ("course", course.title),
                                     ("tags", "coursegenie 学案"),
                                     *scenario.frontmatter_fields(lecture.meta)]), body])
    return note, data


def _scene_svg(doc: dict) -> str | None:
    """把 Excalidraw scene dict 渲染成简洁 SVG（Obsidian 笔记里直接显示的预览图）。"""
    try:
        from app.services.diagram import diagram_to_svg

        elements = doc.get("elements")
        if not isinstance(elements, list) or not elements:
            return None
        svg = diagram_to_svg(elements)
        return svg or None
    except Exception:  # noqa: BLE001 —— 渲染失败不影响导出主体
        return None


def _chapter_content(db: Session, course: Course,
                     nodes: list[OutlineNode]) -> list[tuple[OutlineNode, list]]:
    """章节 → [(知识点, 该知识点的导出载荷)]，只含真正有内容的节点。

    导出与一致性校验共用同一口径，避免两边算法漂移。
    """
    chapter_concepts = chapter_concept_map(db, course.id)
    concept_links = concept_slug_map(db, course.id)
    material_links = material_link_map(db, course)
    _course_deck, chapter_decks = _deck_names(db, course, nodes)
    out: list[tuple[OutlineNode, list]] = []
    for chapter in _chapter_list(nodes):
        deck_name = chapter_decks.get(chapter.node_key)
        members = [(node, _build_node_payload(db, course, node,
                                              chapter_concepts=chapter_concepts,
                                              concept_links=concept_links,
                                              material_links=material_links,
                                              chapter_deck=deck_name
                                              if node.node_key == chapter.node_key else None))
                   for node in _members_of(chapter, nodes)
                   if _node_has_content(db, course.id, node)]
        if members:
            out.append((chapter, members))
    return out


def figure_names_by_node(chapter_content: list) -> dict[str, list[str]]:
    """{知识点 key: [本次导出到图表库的 SVG 文件名]}——概念库「图示」直接复用现有图。"""
    out: dict[str, list[str]] = {}
    for _chapter, members in chapter_content:
        for node, payload in members:
            names = [Path(rel).name for rel, _text in payload.svgs]
            if names:
                out[node.node_key] = names
    return out


def _terminology_issues_of(lecture: ContentItem) -> list[dict]:
    """讲义质检里的「用别名」明细（任务 22-C 的「术语」小节据此生成）。"""
    qc = (lecture.meta or {}).get("qc") or {}
    for check in qc.get("checks") or []:
        if check.get("key") == "terminology":
            return [row for row in (check.get("issues") or []) if isinstance(row, dict)]
    return []


def _source_section(sources: list[dict], material_links: dict,
                    markdown: str | None = None) -> list[str]:
    """页末「来源」小节：素材双链 + 段落摘要（任务 22-B，来源可追溯）。

    任务 48d：讲义 **0 条引用**时不再让整段消失（交付包里读者会以为漏了文件），
    改为渲染一句说明 + 正文里「无直接依据」的处数（0 LLM）。文案与交付讲义
    共用 ``lecture_qc.empty_sources_note`` —— 不写第二套。
    旧调用方（不传 ``markdown``）保持原样：没正文可数就不硬造小节。
    """
    if not sources:
        if markdown is None:
            return []
        from app.services.lecture_qc import empty_sources_note

        return ["", "## 来源", "", empty_sources_note(markdown), ""]
    lines = ["", "## 来源", ""]
    for src in sources:
        name = str(src.get("material_name") or "").strip() or "未标注素材"
        stem = material_links.get(str(src.get("material_id") or "")) or \
            material_links.get(normalize_key(name))
        label = f"[[{MATERIALS_DIR}/{stem}|{name}]]" if stem else name
        bits = [label]
        if src.get("section"):
            bits.append(str(src["section"]))
        if src.get("paragraph"):
            bits.append(f"第 {src['paragraph']} 段")
        summary = plain_wikilinks(str(src.get("material_quote") or src.get("quote") or "").strip())
        lines.append(f"- {' · '.join(bits)}：{summary}" if summary else f"- {' · '.join(bits)}")
    lines.append("")
    return lines


MAX_TERMS_IN_NOTE = 12   # 术语小节最多列几行，避免把笔记撑爆


def _node_term_lines(db: Session, course: Course, node: OutlineNode,
                     lecture: ContentItem | None, concept_links: dict) -> list[str]:
    """本节真正出现过的术语 → 「标准名 + 一句话定义 + 双链」（任务 58 补齐定义）。

    来源：课程概念库（有页面 → 双链）+ 课程术语表（``course.meta.terminology``，
    只出现一次的术语，没页面就不加链接）。命中范围包含**大纲字段**
    （objective / teaching_strategy / assessment）—— 「教学策略：马尔可夫链预测演示」
    这种只在策略里出现的词，读者也要能在笔记里查到解释。
    """
    from app.services.concept import concept_rows, terminology_only
    from app.services.lecture_qc import concept_definition

    markdown = str((lecture.body or {}).get("markdown") or "") if lecture is not None else ""
    blob = "\n".join([markdown, str(node.objective or ""),
                      str(node.teaching_strategy or ""), str(node.assessment or "")])
    lines: list[str] = []
    seen: set[str] = set()
    for concept in concept_rows(db, course.id):
        name = str(concept.get("name") or "").strip()
        if not name or name in seen or name not in blob:
            continue
        seen.add(name)
        definition = concept_definition(concept) or "（定义待补）"
        lines.append(f"- [[{concept_links.get(name, name)}|{name}]]：{definition}")
    for row in terminology_only(course):
        name = str(row.get("name") or "").strip()
        if not name or name in seen or name not in blob:
            continue
        seen.add(name)
        definition = (str(row.get("definition") or "").strip()
                      or "（定义待补：该词只出现在教学策略/评估方式里，建议补齐一句话解释）")
        lines.append(f"- {name}：{definition}（术语表，尚未建概念页）")
    return lines[:MAX_TERMS_IN_NOTE]


def _terminology_section(issues: list[dict], concept_links: dict,
                         terms: list[str] | None = None) -> list[str]:
    """页末「术语」小节：标准名 + 一句话定义 + 双链（v1 只列别名建议，不改写已定稿正文）。"""
    lines = ["", "## 术语", ""]
    lines += list(terms or [])
    for issue in issues:
        alias = str(issue.get("alias") or "").strip()
        standard = str(issue.get("standard") or "").strip()
        if not standard:
            continue
        target = concept_links.get(standard, standard)
        lines.append(f"- 正文用了别名「{alias}」→ 建议改用标准名 [[{target}]]")
    if len(lines) <= 3:
        return []
    lines.append("")
    return lines


def _build_node_payload(db: Session, course: Course, node: OutlineNode,
                        chapter_concepts: dict | None = None,
                        concept_links: dict | None = None,
                        material_links: dict | None = None,
                        chapter_deck: str | None = None) -> NodePayload:
    payload = NodePayload()
    items = _content_items(db, course.id, node.id)
    quizzes = _quizzes(db, course.id, node.id)
    lectures = [i for i in items if i.kind == "lecture"]
    lecture = lectures[-1] if lectures else None
    texts = [i for i in items if i.kind == "text"]
    flows = [i for i in items if i.kind == "flowchart"]
    charts = sorted((i for i in items if i.kind.startswith("chart_")),
                    key=lambda i: i.created_at)
    stems = _chart_stems(charts)
    images = [i for i in items if i.kind == "image"]
    diagrams = [i for i in items if i.kind == "diagram"]

    title = f"{node.node_key} {node.title}"
    chapter_dir = ""
    parts = [
        # 任务 86：frontmatter 带上「生成时的口径」（scenario / prompt_version）—— 取自该知识点
        # 的**讲义条目 meta**；老内容没有标记 ⇒ 写「通用 / 未记录」（**不许猜**成某个具体场景）。
        *_frontmatter([("title", title), ("course", course.title), ("tags", "coursegenie 知识点"),
                       *scenario.frontmatter_fields(lecture.meta if lecture is not None else {})]),
        *BANNER, "",
        f"# {title}", "",
    ]
    if chapter_deck:
        parts += [f"> 本章课件:[[{chapter_deck}]]", ""]
    if node.objective:
        parts += ["**学习目标**：" + node.objective, ""]
    if node.teaching_strategy or node.assessment:
        parts += [f"- 教学策略：{node.teaching_strategy or '—'}", f"- 评估方式：{node.assessment or '—'}", ""]

    if lecture is not None:
        md = str((lecture.body or {}).get("markdown") or "").strip()
        if md:
            owned_images = {i.id for i in images if (i.meta or {}).get("from_lecture") == lecture.id}
            md = _rewrite_md_images(md, images, owned_images, payload)
            md = _attach_excalidraw_links(md, f"图-流程图-{node.node_key}", payload)
            md = _rewrite_diagram_refs(md, diagrams, node.node_key, payload)
            md, handled_chart_ids = _rewrite_chart_refs(md, charts, node.node_key, stems, payload)
            rest_charts = [c for c in charts if c.id not in handled_chart_ids]
            md = _attach_chart_blocks(md, rest_charts, node.node_key, stems, payload)
            parts += [md, ""]
    else:
        for t in texts:
            md = str((t.body or {}).get("markdown") or "").strip()
            if md:
                parts += [md, ""]
        for fc in flows:
            mermaid = str((fc.body or {}).get("mermaid") or "").strip()
            if not mermaid:
                continue
            desc = (fc.body or {}).get("description") or ""
            parts += [f"## 流程图：{fc.title or '流程图'}", ""]
            if desc:
                parts += [str(desc), ""]
            code = f"```mermaid\n{mermaid}\n```"
            code = _attach_excalidraw_link(code, mermaid, f"图-流程图-{node.node_key}", payload)
            parts += [code, ""]
        for c in charts:
            parts += [_chart_visual_block(c, node.node_key, stems, payload, heading="## "), ""]
        for im in images:
            src = _image_src(im)
            if src is None:
                continue
            target_rel = f"{ASSETS_DIR}/{src.name}"
            payload.assets.append((src, target_rel))
            parts += [f"## 配图：{im.title or ''}", "", f"![{im.title or src.name}]({target_rel})", ""]
        if quizzes:
            parts += ["## 随堂练习", ""]
            for idx, q in enumerate(quizzes, 1):
                parts += [f"### 第 {idx} 题 · {quiz_level(q)}", ""] + _quiz_block(q, "")

    if lecture is not None:
        payload.plan_md, payload.plan_docx = _lesson_plan_payload(db, course, node, lecture)
        payload.sheet_md, payload.sheet_docx = _student_sheet_payload(db, course, node, lecture)

    has_md = len(parts) > 10
    if quizzes:
        parts += ["", f"> 📖 本节习题见 [[{_node_stem(node)}-习题]]", ""]
        has_md = True
    # 本章概念索引（只列双链、不复制正文；概念库是单一真相源）
    if chapter_concepts is None:
        chapter_concepts = chapter_concept_map(db, course.id)
    if concept_links is None:
        concept_links = concept_slug_map(db, course.id)
    concept_names = chapter_concepts.get(str(node.node_key).split(".")[0]) or []
    if concept_names:
        parts += ["", "## 本章概念索引", "",
                  *[f"- [[{concept_links.get(name, name)}]]" for name in concept_names], ""]
        has_md = True
    # 任务 22-B/C：页末「来源」（素材双链 + 段落摘要）与「术语」（标准名 + 双链）
    if lecture is not None:
        if material_links is None:
            material_links = material_link_map(db, course)
        from app.services.lecture_qc import SOURCES_KEY

        parts += _source_section([s for s in ((lecture.meta or {}).get(SOURCES_KEY) or [])
                                  if isinstance(s, dict)], material_links,
                                 str((lecture.body or {}).get("markdown") or ""))
        parts += _terminology_section(
            _terminology_issues_of(lecture), concept_links,
            _node_term_lines(db, course, node, lecture, concept_links))
    if has_md:
        payload.md = "\n".join(parts).rstrip() + "\n"
    if quizzes:
        payload.quiz = "\n".join(_quiz_file_lines(course, node, quizzes)).rstrip() + "\n"
    return payload


def _rewrite_md_images(md: str, images: list[ContentItem], owned_ids: set,
                       payload: NodePayload) -> str:
    """把讲义里 /images/xxx.png 的配图复制到本章 assets/，并改写为相对路径。"""
    by_rel: dict[str, ContentItem] = {}
    for im in images:
        rel = str((im.body or {}).get("path") or "")
        by_rel.setdefault(rel, im)

    def _replace(match: re.Match) -> str:
        rel_url = match.group(1).strip()
        src = _storage_image(rel_url)
        if src is None:
            return match.group(0)
        target_rel = f"{ASSETS_DIR}/{src.name}"
        payload.assets.append((src, target_rel))
        return f"]({target_rel})"

    return _RE_MD_IMAGE.sub(_replace, md)


def _attach_excalidraw_links(md: str, base_name: str, payload: NodePayload) -> str:
    """讲义里每个 mermaid 流程图：导出为可见 SVG 内嵌 + 可编辑 .excalidraw 原件。"""
    blocks = list(_RE_MERMAID.finditer(md))
    if not blocks:
        return md
    out: list[str] = []
    pos = 0
    for idx, m in enumerate(blocks):
        out.append(md[pos:m.start()])
        block = m.group(0)
        code = m.group(1).strip()
        stem = f"{_slug(base_name, 40)}-{idx + 1:02d}"
        xname = f"{stem}.excalidraw"
        xtext = None
        doc = None
        try:
            xtext = mermaid_to_excalidraw_text(code)
            doc = json.loads(xtext)
        except Exception:  # noqa: BLE001 —— 转换/渲染失败则退回 mermaid 源码
            xtext = None
        if xtext is not None and doc is not None:
            payload.excalidraw.append((f"{CHARTS_DIR}/{xname}", xtext))
            svg = _scene_svg(doc)
            if svg:
                sname = f"{stem}.svg"
                payload.svgs.append((f"{CHARTS_DIR}/{sname}", svg))
                out.append(f"![[{sname}]]\n\n> ✏️ 流程图｜可编辑原件：[[{xname}]]\n")
            else:
                out.append(block)
                out.append(f"\n> ✏️ 可编辑版本：[[{xname}]]\n")
        else:
            out.append(block)
        pos = m.end()
    out.append(md[pos:])
    return "".join(out)


def _diagram_text(item: ContentItem) -> str | None:
    """把 diagram 条目的 excalidraw_json 序列化为 .excalidraw 文件文本。

    出口统一走 `normalize_document`：库里存的历史产物（M2 之前生成的）也补齐插件
    规范字段并建立箭头/连线绑定，保证旧课程重导出的图同样可拖动编辑。
    """
    doc = (item.body or {}).get("excalidraw_json")
    if not isinstance(doc, dict):
        return None
    return json.dumps(normalize_document(copy.deepcopy(doc)),
                      ensure_ascii=False, indent=1)


def _rewrite_diagram_refs(md: str, diagrams: list[ContentItem],
                          node_key: str, payload: NodePayload) -> str:
    """把讲义里的 diagram:// 引用换成可见 SVG 内嵌，并把 .excalidraw 原件加入载荷。

    文件命名约定：图-{layout}-{node_key}-{序号}.excalidraw（同布局按顺序编号），
    同名 .svg 为笔记里直接显示的预览图。
    """
    by_layout: dict[str, int] = {}
    item_by_id: dict[str, tuple[str | None, str]] = {}
    for diagram in diagrams:
        doc = (diagram.body or {}).get("excalidraw_json")
        text = _diagram_text(diagram)
        if text is None or not isinstance(doc, dict):
            continue
        layout = str((diagram.body or {}).get("layout") or "radial")
        role = "card" if (diagram.meta or {}).get("role") == "card" else "concept"
        seq = by_layout.get(layout, 0) + 1
        by_layout[layout] = seq
        stem = f"图-{_slug(layout, 16)}-{_slug(node_key, 24)}-{seq:02d}"
        xname = f"{stem}.excalidraw"
        payload.excalidraw.append((f"{CHARTS_DIR}/{xname}", text))
        svg_name: str | None = None
        svg = _scene_svg(doc)
        if svg:
            svg_name = f"{stem}.svg"
            payload.svgs.append((f"{CHARTS_DIR}/{svg_name}", svg))
        item_by_id[diagram.id] = (svg_name, xname, role)

    if not item_by_id:
        return md

    def _replace(match: re.Match) -> str:
        pair = item_by_id.get(match.group(1))
        if pair is None:
            return match.group(0)
        svg_name, xname, role = pair
        tag = "概念卡" if role == "card" else "一图看懂（矢量图）"
        if svg_name:
            return f"![[{svg_name}]]\n\n> {tag}｜可编辑原件：[[{xname}]]"
        return f"![[{xname}]]"

    return _RE_DIAGRAM_REF.sub(_replace, md)


def _build_course_level_payload(db: Session, course: Course) -> NodePayload:
    """课程级（未绑定节点）内容单独成页；配图归 图表库/。"""
    payload = NodePayload()
    items = _content_items(db, course.id, None)
    texts = [i for i in items if i.kind == "text"]
    lectures = [i for i in items if i.kind == "lecture"]
    flows = [i for i in items if i.kind == "flowchart"]
    charts = sorted((i for i in items if i.kind.startswith("chart_")),
                    key=lambda i: i.created_at)
    stems = _chart_stems(charts)
    images = [i for i in items if i.kind == "image"]
    diagrams = [i for i in items if i.kind == "diagram"]
    quizzes = _quizzes(db, course.id, None)
    parts = [
        *_frontmatter([("title", f"{course.title} · 课程级内容"), ("course", course.title),
                       ("tags", "coursegenie 课程")]),
        *BANNER, "",
        f"# {course.title} · 课程级内容", "",
        COURSE_INDEX_BACKLINK, "",
    ]
    handled_chart_ids: set[str] = set()
    for lecture in lectures:
        md = str((lecture.body or {}).get("markdown") or "").strip()
        if md:
            md = _rewrite_md_images(md, images, set(), payload)
            md = _rewrite_diagram_refs(md, diagrams, "course", payload)
            md, handled_ids = _rewrite_chart_refs(md, charts, "course", stems, payload)
            handled_chart_ids |= handled_ids
            parts += [md, ""]
    for t in texts:
        md = str((t.body or {}).get("markdown") or "").strip()
        if md:
            parts += [md, ""]
    for fc in flows:
        mermaid = str((fc.body or {}).get("mermaid") or "").strip()
        if not mermaid:
            continue
        parts += [f"## 流程图：{fc.title or '流程图'}", ""]
        code = f"```mermaid\n{mermaid}\n```"
        code = _attach_excalidraw_link(code, mermaid, "图-流程图-整课", payload)
        parts += [code, ""]
    for c in charts:
        if c.id in handled_chart_ids:
            continue
        parts += [_chart_visual_block(c, "course", stems, payload, heading="## "), ""]
    for im in images:
        src = _image_src(im)
        if src is None:
            continue
        target_rel = f"{CHARTS_DIR}/{src.name}"
        payload.assets.append((src, target_rel))
        parts += [f"## 配图：{im.title or ''}", "", f"![{im.title or src.name}]({CHARTS_DIR}/{src.name})", ""]
    for idx, q in enumerate(quizzes, 1):
        parts += [f"### 第 {idx} 题 · {quiz_level(q)}", ""] + _quiz_block(q, "")
    payload.md = "\n".join(parts).rstrip() + "\n"
    return payload


def _attach_excalidraw_link(block: str, mermaid: str, base_name: str,
                            payload: NodePayload) -> str:
    stem = _slug(base_name, 40)
    xname = f"{stem}.excalidraw"
    xtext = None
    doc = None
    try:
        xtext = mermaid_to_excalidraw_text(mermaid)
        doc = json.loads(xtext)
    except Exception:  # noqa: BLE001 —— 转换/渲染失败则退回 mermaid 源码
        xtext = None
    if xtext is not None and doc is not None:
        payload.excalidraw.append((f"{CHARTS_DIR}/{xname}", xtext))
        svg = _scene_svg(doc)
        if svg:
            sname = f"{stem}.svg"
            payload.svgs.append((f"{CHARTS_DIR}/{sname}", svg))
            return f"![[{sname}]]\n\n> ✏️ 流程图｜可编辑原件：[[{xname}]]"
        return f"{block}\n\n> ✏️ 可编辑版本：[[{xname}]]"
    return block


# ---------------------------------------------------------------------------
# 整课导出
# ---------------------------------------------------------------------------

def export_course(db: Session, course: Course, output_rel: str | None = None,
                  force: bool = False, prune: bool = True) -> dict:
    """整课导出为「按章成目录、按知识点成页」的 Obsidian 笔记系统。"""
    root = Path(_safe(export_root_for(course), ""))
    rel = output_rel or course_kb(course).get("output_rel") or f"课程/{_slug(course.title, 40)}"
    out_dir = _safe(str(root), rel)
    out_dir.mkdir(parents=True, exist_ok=True)
    for sub in (CHARTS_DIR, QUIZ_DIR, MATERIALS_DIR):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    nodes = _nodes(db, course.id)
    warnings: list[str] = []
    if not nodes:
        warnings.append("尚无大纲：请先到「大纲规划」生成并通过大纲后重新导出；本次先导出素材等已有内容")

    kb = course_kb(course)
    manifest = dict(kb.get("manifest") or {})
    preserved: list[dict] = []
    written: list[str] = []
    chart_names: list[str] = []

    def _emit(rel_path: str, content: str, *, raw: bytes | None = None,
              track_chart: bool = True) -> None:
        _write_safe(out_dir, rel_path, content, manifest, force, root, preserved, written, raw=raw)
        if track_chart and (raw is not None or rel_path.endswith((".png", ".excalidraw"))):
            chart_names.append(rel_path)

    materials = list_materials(db, course.id)
    for idx, m in enumerate(materials, 1):
        name = f"{_material_stem(idx, m)}.md"
        _emit(f"{MATERIALS_DIR}/{name}", "\n".join(_material_page(course, m)))

    # 课程级内容（未绑定节点的正文/讲义/流程图/配图）
    course_payload = _build_course_level_payload(db, course)
    _emit(COURSE_LEVEL_FILE, course_payload.md)
    for src, target_rel in course_payload.assets:
        _emit(target_rel, "", raw=src.read_bytes())
    for xrel, xtext in course_payload.excalidraw:
        _emit(xrel, xtext)
    for srel, stext in course_payload.svgs:
        _emit(srel, stext)

    # 分章目录 + 知识点页（与 _chapter_content 同口径，verify 复用同一份载荷）
    chapter_links: list[tuple[OutlineNode, list[str]]] = []
    chapter_content = _chapter_content(db, course, nodes)
    for idx, (chapter, members) in enumerate(chapter_content, 1):
        chapter_dir = _chapter_dir(idx, chapter)
        links: list[str] = []
        for node, payload in members:
            stem = _node_stem(node)
            if payload.md:
                _emit(f"{chapter_dir}/{stem}.md", payload.md)
            for src, target_rel in payload.assets:
                _emit(f"{chapter_dir}/{target_rel}", "", raw=src.read_bytes())
            for xrel, xtext in payload.excalidraw:
                _emit(xrel, xtext)
            for srel, stext in payload.svgs:
                _emit(srel, stext)
            if payload.quiz:
                _emit(_quiz_rel(node), payload.quiz)
            if payload.plan_md:
                _emit(_plan_rel(chapter_dir, node), payload.plan_md)
            if payload.sheet_md:
                _emit(_sheet_rel(chapter_dir, node), payload.sheet_md)
            if payload.md or payload.quiz or payload.plan_md or payload.sheet_md:
                links.append(f"[[{stem}|{node.node_key} {node.title}]]")
                links += _doc_links(node, payload)
        if links:
            chapter_links.append((chapter, links))

    # 概念库（原子笔记 v1）：一概念一篇 + 双链织网 + 来源可追溯
    pages = concept_pages(db, course, figures=figure_names_by_node(chapter_content))
    if pages:
        (out_dir / CONCEPTS_DIR).mkdir(parents=True, exist_ok=True)
    for rel_path, text in pages:
        _emit(rel_path, text)
    # 概念知识图谱：与概念页同批产出（任务 24 三件套），索引要按「本次是否产出」链它
    from app.services import concept_graph

    graph_items = concept_graph.graph_payloads(db, course)

    # 课件落地：整课 00-整课课件.pptx + 每章 第N章-…课件.pptx（受管区，手改则 preserved）
    deck_items = _deck_items(db, course, nodes)
    if deck_items:
        (out_dir / PPT_DIR).mkdir(parents=True, exist_ok=True)
    for deck_rel, deck_src, _deck_chapter in deck_items:
        _emit(deck_rel, "", raw=deck_src.read_bytes(), track_chart=False)
    course_deck = next((Path(rel).name for rel, _s, ch in deck_items if ch is None), None)
    chapter_decks = {ch.node_key: Path(rel).name for rel, _s, ch in deck_items if ch is not None}

    _emit("00-课程概览.md", "\n".join(
        _index_page(course, chapter_links, materials, nodes,
                    course_deck=course_deck, chapter_decks=chapter_decks,
                    course_pages=_course_page_links(bool(course_payload.md),
                                                    bool(graph_items)))))

    # 任务 24 三件套：知识图谱 / 风格库 / 课程体检报告
    # （概念为空、配置关闭时各自自动跳过；顺序放在最后，报告读到的是本次导出后的文件）
    from app.services import audit as audit_service
    from app.services import style_library

    for graph_rel, graph_text in graph_items:
        _emit(graph_rel, graph_text)
    if get_settings().export_style_library:
        (out_dir / CHARTS_DIR).mkdir(parents=True, exist_ok=True)
        _emit(style_library.STYLE_LIB_REL, style_library.library_text(course=course))
    # 收尾顺序（P9-1）：①清理孤儿 → ②写回本次清单 → ③最后才生成体检报告。
    # 报告的「导出健康」读的是 course.meta 里的清单（course_asset_health），若在
    # 写回前生成，刚写出的 .pptx（二进制、无内嵌指纹）会被上一版清单判成
    # 「疑似手改」、旧孤儿也还没清理 → 老师打开文件看到的数字与网页端对不上
    # （实测磁盘 65 分 / 孤儿 6 / 保留 13，重新体检 70 分 / 孤儿 0 / 保留 0）。
    report_rel = (Path(rel) / audit_service.REPORT_NAME).as_posix()
    pruned = prune_stale_files(out_dir, root, course, written, preserved,
                               keep=(report_rel,)) if prune else []
    kb["manifest"] = manifest
    kb["exported_at"] = _now_iso()
    meta = dict(course.meta or {})
    meta[KB_KEY] = kb
    course.meta = meta

    # 报告在清单写回之后生成：此刻磁盘无孤儿、清单是本次结果，报告里的数字与
    # 紧接着重新体检逐项一致。
    # 报告是最后一个落盘的文件，而链接体检在报告落盘前就跑（结果写进报告正文）
    # ⇒ 显式声明「报告马上就会写出」，否则索引 → 报告的合法链接会被记成坏链
    # （任务 60：首次导出必现「坏链 1 条 / 有风险」，重导才自愈）。
    _emit(audit_service.REPORT_NAME, audit_service.course_report(
        db, course, extra_link_targets=(audit_service.REPORT_NAME,))["markdown"])
    # 二次写回：报告自己也登记进清单（manifest 与 kb["manifest"] 是同一个 dict，
    # _write_safe 已就地写入），显式再赋一次，让下面的 db.commit() 一并落库。
    kb["manifest"] = manifest
    db.commit()
    return {
        "vault_root": str(root),
        "output_rel": rel,
        "files": written,
        "preserved": preserved,
        "pruned": pruned,
        "charts": sorted(set(chart_names)),
        "warnings": warnings,
    }


def _index_page(course: Course, chapter_links: list[tuple[OutlineNode, list[str]]],
                materials: list[SourceMaterial], nodes: list[OutlineNode],
                course_deck: str | None = None,
                chapter_decks: dict[str, str] | None = None,
                course_pages: list[str] | None = None) -> list[str]:
    parts = [
        *_frontmatter([("title", course.title), ("subject", course.subject),
                       ("audience", course.audience), ("tags", "coursegenie 课程")]),
        *BANNER, "",
        f"# {course.title}", "",
    ]
    if course_deck:
        parts += [f"> 课件:[[{course_deck}]]", ""]
    if course.subject or course.audience:
        parts += [f"- 学科：{course.subject or '—'} ｜ 受众：{course.audience or '—'} ｜ 时长：{course.duration_minutes} 分钟", ""]
    # 任务 133：顶部那段（自动生成声明 + 怎么读图例）之后，先说「怎么打开这个笔记库」再给目录
    parts += vault_getting_started()
    parts += ["## 目录（按知识点成页；配图归章节 assets，流程图与概念图为 图表库/）", ""]
    if not nodes:
        parts += ["- 暂无章节：请回到网页端「大纲规划」生成并通过大纲后重新同步。", ""]
    else:
        links_by_key = {chapter.node_key: links for chapter, links in chapter_links}
        decks = chapter_decks or {}
        emitted = False
        for chapter in _chapter_list(nodes):
            links = links_by_key.get(chapter.node_key, [])
            deck = decks.get(chapter.node_key)
            if not links and not deck:
                continue
            emitted = True
            parts += [f"### 第{chapter.node_key}章 {chapter.title}", ""]
            if deck:
                parts += [f"> 本章课件:[[{deck}]]", ""]
            parts += [f"- {link}" for link in links]
            parts += [""]
        if not emitted:
            parts += ["- （章节内容为空：请回到网页端逐节生成正文/讲义后重新导出）", ""]
    if course_pages:
        parts += ["## 课程级页面", ""]
        parts += [f"- {link}" for link in course_pages]
        parts += [""]
    if materials:
        parts += [f"## 素材库（{len(materials)} 份原始资料）", ""]
        for idx, m in enumerate(materials, 1):
            m_name = _material_stem(idx, m)
            parts.append(f"- 素材：[[素材库/{m_name}|{m.title}]]")
        parts += [""]
    parts += [f"> 笔记结构：每个知识点一页（01-第1章/1.1-xxx.md 等）；图片在对应章的 assets/；",
              f"> 习题在 {QUIZ_DIR}/；教师教案在 {LESSON_DIR}/；学生学案在 {SHEET_DIR}/；",
              f"> 流程图与概念图（可编辑）为 {CHARTS_DIR}/*.excalidraw；",
              f"> 课件在 {PPT_DIR}/（.pptx，Obsidian 里点上面的双链直接打开）。", ""]
    parts += ["---", ""]
    return parts


# ---------------------------------------------------------------------------
# 增量写入：单条内容生成/导入成功后自动同步到 Vault（幂等、失败不影响主流程）
# ---------------------------------------------------------------------------

def _configured_vault_root(course: Course) -> Path | None:
    """课程绑定优先，其次全局 OBSIDIAN_VAULT_ROOT；两者皆无视为未配置。"""
    kb = course_kb(course)
    if kb.get("vault_root"):
        return Path(str(kb["vault_root"])).expanduser().resolve()
    settings = get_settings()
    if settings.obsidian_vault_root:
        return Path(settings.obsidian_vault_root).expanduser().resolve()
    return None


def _emit_node_payload(db: Session, course: Course, node: OutlineNode | None,
                       out_dir: Path, nodes: list[OutlineNode], *,
                       root: Path, manifest: dict, force: bool = False) -> dict:
    """写单个节点（或课程级）的新结构文件；增量写与整课导出同用 no-clobber。

    manifest 与整课导出共用（同一套校验和记录）；教师手改过的文件跳过，
    记入 preserved，绝不静默覆盖（P1-3）。
    返回 {note_path, chart_path, files(本次实际写入), preserved}。
    """
    written: list[str] = []
    preserved: list[dict] = []
    files: list[str] = []
    note_path: str | None = None
    chart_path: str | None = None

    def _emit(rel_path: str, content: str = "", *, raw: bytes | None = None) -> Path:
        nonlocal note_path, chart_path
        before = len(written)
        _write_safe(out_dir, rel_path, content, manifest, force=force, root=root,
                    preserved=preserved, written=written, raw=raw)
        target = out_dir / rel_path
        if len(written) > before:
            files.append(str(target.resolve()))
        if note_path is None and rel_path.endswith(".md") and not rel_path.startswith(QUIZ_DIR):
            note_path = str(target.resolve())
        if chart_path is None and target.suffix.lower() in (".excalidraw", ".png"):
            chart_path = str(target.resolve())
        return target

    if node is None:
        payload = _build_course_level_payload(db, course)
        _emit(COURSE_LEVEL_FILE, payload.md)
        for src, target_rel in payload.assets:
            _emit(target_rel, raw=src.read_bytes())
        for xrel, xtext in payload.excalidraw:
            _emit(xrel, xtext)
        for srel, stext in payload.svgs:
            _emit(srel, stext)
        return {"note_path": note_path, "chart_path": chart_path,
                "files": files, "preserved": preserved}

    chapters = _chapter_list(nodes)
    _course_deck, chapter_decks = _deck_names(db, course, nodes)
    for idx, chapter in enumerate(chapters, 1):
        if node.node_key == chapter.node_key or node.node_key.startswith(f"{chapter.node_key}."):
            chapter_dir = _chapter_dir(idx, chapter)
            payload = _build_node_payload(
                db, course, node,
                chapter_deck=(chapter_decks.get(chapter.node_key)
                              if node.node_key == chapter.node_key else None))
            stem = _node_stem(node)
            if payload.md:
                _emit(f"{chapter_dir}/{stem}.md", payload.md)
            for src, target_rel in payload.assets:
                _emit(f"{chapter_dir}/{target_rel}", raw=src.read_bytes())
            for xrel, xtext in payload.excalidraw:
                _emit(xrel, xtext)
            for srel, stext in payload.svgs:
                _emit(srel, stext)
            if payload.quiz:
                _emit(_quiz_rel(node), payload.quiz)
            if payload.plan_md:
                _emit(_plan_rel(chapter_dir, node), payload.plan_md)
            if payload.sheet_md:
                _emit(_sheet_rel(chapter_dir, node), payload.sheet_md)
            return {"note_path": note_path, "chart_path": chart_path,
                    "files": files, "preserved": preserved}
    return {"note_path": None, "chart_path": None, "files": files,
            "preserved": preserved}


def _node_note_rel(nodes: list[OutlineNode], node: OutlineNode) -> str | None:
    """知识点讲义页在 Vault 里的相对路径（按所属章节归档）。"""
    for idx, chapter in enumerate(_chapter_list(nodes), 1):
        if node.node_key == chapter.node_key or node.node_key.startswith(f"{chapter.node_key}."):
            return f"{_chapter_dir(idx, chapter)}/{_node_stem(node)}.md"
    return None


def _plain_body(text: str) -> str:
    """去掉 frontmatter 与整行图片后的正文：用于「网页 vs 笔记」文字比对。"""
    match = _RE_FRONTMATTER.match(text)
    body = text[match.end():] if match else text
    body = _RE_IMG_LINE.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def ref_counts(text: str) -> dict:
    """正文里的图片/概念图/数据图/流程图引用数量（7b 差异摘要用）。"""
    refs = _RE_ALL_IMG.findall(text)
    return {
        "images": len(refs),
        "diagrams": len(_RE_DIAGRAM_REF.findall(text)),
        "charts": len(_RE_CHART_REF.findall(text)),
        "mermaid": len(_RE_MERMAID.findall(text)),
    }


def force_write_course_files(db: Session, course: Course) -> dict:
    """以网页端为准强制覆盖课程级页面：素材页、课程级内容、00-课程概览、概念图谱与体检报告。"""
    root, out_dir, rel = _course_out_dir(course)
    kb = course_kb(course)
    manifest = dict(kb.get("manifest") or {})
    nodes = _nodes(db, course.id)
    written: list[str] = []
    preserved: list[dict] = []

    def _emit(rel_path: str, content: str = "", *, raw: bytes | None = None,
              force: bool = True) -> None:
        _write_safe(out_dir, rel_path, content, manifest, force=force, root=root,
                    preserved=preserved, written=written, raw=raw)

    materials = list_materials(db, course.id)
    for idx, material in enumerate(materials, 1):
        _emit(f"{MATERIALS_DIR}/{_material_stem(idx, material)}.md",
              "\n".join(_material_page(course, material)))

    payload = _build_course_level_payload(db, course)
    if payload.md:
        _emit(COURSE_LEVEL_FILE, payload.md)
    for src, target_rel in payload.assets:
        _emit(target_rel, "", raw=src.read_bytes())
    for xrel, xtext in payload.excalidraw:
        _emit(xrel, xtext)
    for srel, stext in payload.svgs:
        _emit(srel, stext)

    chapter_links: list[tuple[OutlineNode, list[str]]] = []
    chapter_content = _chapter_content(db, course, nodes)
    for chapter, members in chapter_content:
        links: list[str] = []
        for node, item in members:
            if item.md or item.quiz or item.plan_md or item.sheet_md:
                links.append(f"[[{_node_stem(node)}|{node.node_key} {node.title}]]")
                links += _doc_links(node, item)
        if links:
            chapter_links.append((chapter, links))
    pages = concept_pages(db, course, figures=figure_names_by_node(chapter_content))
    if pages:
        (out_dir / CONCEPTS_DIR).mkdir(parents=True, exist_ok=True)
    for rel_path, text in pages:
        _emit(rel_path, text)
    from app.services import concept_graph

    graph_items = concept_graph.graph_payloads(db, course)
    for graph_rel, graph_text in graph_items:
        _emit(graph_rel, graph_text)
    deck_items = _deck_items(db, course, nodes)
    if deck_items:
        (out_dir / PPT_DIR).mkdir(parents=True, exist_ok=True)
    for deck_rel, deck_src, _deck_chapter in deck_items:
        _emit(deck_rel, "", raw=deck_src.read_bytes(), force=False)
    course_deck = next((Path(rel).name for rel, _s, ch in deck_items if ch is None), None)
    chapter_decks = {ch.node_key: Path(rel).name for rel, _s, ch in deck_items if ch is not None}
    _emit("00-课程概览.md", "\n".join(
        _index_page(course, chapter_links, materials, nodes,
                    course_deck=course_deck, chapter_decks=chapter_decks,
                    course_pages=_course_page_links(bool(payload.md), bool(graph_items)))))

    kb["manifest"] = manifest
    kb["exported_at"] = _now_iso()
    meta = dict(course.meta or {})
    meta[KB_KEY] = kb
    course.meta = meta
    # 体检报告同样是「课程级页面」且被索引链着 —— 这次刷新一并重写，索引不会留坏链
    from app.services import audit as audit_service

    _emit(audit_service.REPORT_NAME, audit_service.course_report(
        db, course, extra_link_targets=(audit_service.REPORT_NAME,))["markdown"])
    kb["manifest"] = manifest
    db.commit()
    return {"ok": True, "vault_path": str(root), "output_rel": rel,
            "files": written, "preserved": preserved}


def _course_out_dir(course: Course) -> tuple[Path, Path, str]:
    """课程绑定的 (vault 根, 导出目录, 导出相对路径)。"""
    root = _configured_vault_root(course)
    if root is None:
        raise ValueError("尚未绑定笔记库目录，请先在第 5 步绑定 Vault 后再操作")
    rel = course_kb(course).get("output_rel") or f"课程/{_slug(course.title, 40)}"
    out_dir = _safe(str(root), rel)
    out_dir.mkdir(parents=True, exist_ok=True)
    for sub in (CHARTS_DIR, QUIZ_DIR, MATERIALS_DIR):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    return root, out_dir, rel


def _verify_file(out_dir: Path, rel_path: str, expected_text, base: dict,
                 required: bool = True) -> dict | None:
    """比对「磁盘上的笔记文件」与「网页端会写出的内容」，返回一行校验结果。

    required=False 的页面（概览/课程级/素材）只在已经存在时才校验：它们由整课导出
    统一生成，增量写不产生它们。
    """
    entry = {**base, "note": rel_path}
    path = out_dir / rel_path
    if not path.exists():
        if not required:
            return None
        return {**entry, "status": "missing", "fingerprint": "absent",
                "diff": ["笔记里还没有这个文件（尚未导出）"]}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {**entry, "status": "mismatch", "fingerprint": "unreadable",
                "diff": [f"笔记文件无法读取：{exc}"]}

    stored, actual = file_fingerprint(text)
    fingerprint = "ok" if stored and stored == actual else ("absent" if not stored else "broken")
    if text == stamp_output(rel_path, expected_text):
        return {**entry, "status": "consistent", "fingerprint": fingerprint, "diff": []}

    if Path(rel_path).suffix.lower() == ".md":
        note_body, exp_body = _plain_body(text), _plain_body(expected_text)
        note_refs, exp_refs = ref_counts(text), ref_counts(expected_text)
        diff: list[str] = []
        if note_body != exp_body:
            diff.append(f"正文文本不一致（笔记 {len(note_body)} 字 / 网页端 {len(exp_body)} 字）")
        for key, label in (("images", "图片引用"), ("diagrams", "概念图引用"),
                           ("charts", "数据图引用"), ("mermaid", "流程图")):
            if note_refs[key] != exp_refs[key]:
                diff.append(f"{label} {note_refs[key]} 处 / 网页端 {exp_refs[key]} 处")
        return {**entry, "status": "mismatch", "fingerprint": fingerprint,
                "refs": {"note": note_refs, "expected": exp_refs},
                "diff": diff or ["笔记与网页端内容不一致"]}

    return {**entry, "status": "mismatch", "fingerprint": fingerprint,
            "diff": ["图表库资产与网页端不一致（重新导出会按网页端重建）"]}


def _verify_assets(out_dir: Path, files: list[tuple[str, str]], base: dict) -> dict | None:
    """一组图表库资产合成一行结果（不比对内容：图形每次生成带随机 id）。

    只看文件自带的版本指纹是否完好：完好=就是机器写的那份；缺失=旧版资产需重新导出补齐；
    对不上=文件在笔记里被改动过。
    """
    missing: list[str] = []
    legacy: list[str] = []
    edited: list[str] = []
    for rel, _text in files:
        path = out_dir / rel
        if not path.exists():
            missing.append(rel)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            edited.append(rel)
            continue
        stored, actual = file_fingerprint(text)
        if stored is None:
            legacy.append(rel)
        elif stored != actual:
            edited.append(rel)
    diff: list[str] = []
    if missing:
        diff.append(f"{len(missing)} 个资产文件缺失")
    if legacy:
        diff.append(f"{len(legacy)} 个旧版资产没有版本指纹（重新导出即可补齐）")
    if edited:
        diff.append(f"{len(edited)} 个资产在笔记里被改动过")
    if not diff:
        return None
    return {**base, "note": (missing + legacy + edited)[0], "status": "mismatch",
            "fingerprint": "broken" if edited else "absent", "diff": diff}


def _verify_deck_file(out_dir: Path, rel_path: str, src: Path, base: dict) -> dict:
    """课件文件校验：缺失 / 与网页端源不一致（被手改）/ 一致。"""
    entry = {**base, "note": rel_path}
    path = out_dir / rel_path
    if not path.exists():
        return {**entry, "status": "missing", "fingerprint": "absent",
                "diff": ["笔记里还没有这份课件（重新导出即可落地）"]}
    try:
        if src.exists() and path.read_bytes() != src.read_bytes():
            return {**entry, "status": "mismatch", "fingerprint": "broken",
                    "diff": ["课件与网页端生成的文件不一致（可能被手动修改）"]}
    except OSError as exc:
        return {**entry, "status": "mismatch", "fingerprint": "unreadable",
                "diff": [f"课件无法读取：{exc}"]}
    return {**entry, "status": "consistent", "fingerprint": "ok", "diff": []}


_RE_PPT_LINK = re.compile(r"\[\[([^\]|]+\.pptx)(?:\|[^\]]*)?\]\]", re.I)


def _verify_ppt_links(out_dir: Path, base: dict) -> dict | None:
    """概览页里的课件双链是否都能解析到真实文件（坏链 0）。"""
    overview = out_dir / "00-课程概览.md"
    if not overview.exists():
        return None
    try:
        text = overview.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    broken: list[str] = []
    for target in _RE_PPT_LINK.findall(text):
        target = target.strip().replace("\\", "/")
        candidate = (out_dir / target) if "/" in target else (out_dir / PPT_DIR / target)
        if not candidate.exists():
            broken.append(target)
    if not broken:
        return None
    return {**base, "note": "00-课程概览.md", "status": "mismatch",
            "fingerprint": "broken",
            "diff": [f"{len(broken)} 处课件双链无法解析：{'、'.join(broken[:3])}"]}


_RE_NOTE_SECTION = re.compile(r"^##\s+(来源|术语)\s*$")
_RE_WIKI_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _section_links(md: str) -> list[str]:
    """「来源」「术语」小节里的双链目标（坏链检查只针对这两个小节）。"""
    out: list[str] = []
    in_section = False
    for line in str(md or "").splitlines():
        if line.startswith("## "):
            in_section = bool(_RE_NOTE_SECTION.match(line))
            continue
        if in_section:
            out += [target.strip() for target in _RE_WIKI_LINK.findall(line)]
    return out


def _vault_link_index(out_dir: Path) -> tuple[set, set]:
    """(相对路径集合（含去后缀）, 文件名集合) —— 双链目标解析用。

    文件名集合同时收「主干」与「带后缀全名」：笔记里的配图用 ``![[图-x.svg]]``
    这种最短写法（图落在 ``图表库/``），Obsidian 按文件名全库唯一解析，这里必须同口径，
    否则合法的资源双链会被误判成坏链。
    """
    rels: set = set()
    names: set = set()
    if not out_dir.exists():
        return rels, names
    for path in out_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(out_dir).as_posix()
        rels.add(rel)
        if path.suffix:
            rels.add(rel[: -len(path.suffix)])
        names.add(path.stem)
        names.add(path.name)
    return rels, names


def _note_link_exists(rels: set, stems: set, target: str) -> bool:
    rel = str(target or "").strip().replace("\\", "/").strip("/")
    if not rel:
        return True
    if rel in rels or f"{rel}.md" in rels:
        return True
    if rel.split("/")[-1] in stems:  # Obsidian 同名解析：概念库/xx.md 可以只写 xx
        return True
    return False


def _declare_pending_targets(rels: set, stems: set, targets: Iterable[str]) -> None:
    """把「本次操作紧接着就会落盘」的目标登记进链接索引（任务 60）。

    体检报告正文里写着链接体检结果，而它自己是**最后一个**落盘的文件（任务 24
    起排在清理孤儿、写回清单之后）⇒ 检查那一刻报告还不存在，索引里
    ``[[00-课程体检报告]]`` 这条合法链接会被记成坏链、写死进报告正文
    （首次导出必现，且报告只在生成时算一次）。调用方用 ``extra_targets``
    声明「这些目标本次操作紧接着就会写出」；判定规则不动：目标仍要解析到索引里
    的名字才算存在，只是把这一个即将存在的名字提前算进来。
    """
    for raw in targets:
        rel = str(raw or "").strip().replace("\\", "/").strip("/")
        if not rel:
            continue
        rels.add(rel)
        suffix = Path(rel).suffix
        if suffix:  # 与 _vault_link_index 同口径：同时收「带后缀全名」与「主干」
            rels.add(rel[: -len(suffix)])
        stems.add(Path(rel).stem)
        stems.add(Path(rel).name)


def _verify_note_links(out_dir: Path, rel_path: str, expected_text, base: dict) -> dict | None:
    """「来源」「术语」小节的双链是否都能解析到真实文件（任务 22 的坏链检查）。"""
    links = _section_links(expected_text)
    if not links:
        return None
    rels, stems = _vault_link_index(out_dir)
    broken = [target for target in links if not _note_link_exists(rels, stems, target)]
    if not broken:
        return None
    return {**base, "note": rel_path, "status": "mismatch", "fingerprint": "broken",
            "diff": [f"{len(broken)} 处来源/术语双链无法解析：{'、'.join(broken[:3])}"]}


def verify_course_notes(db: Session, course: Course,
                        node_ids: list[str] | None = None) -> dict:
    """逐产出校验「网页端内容」与「笔记文件」是否一致，返回状态与差异摘要。

    覆盖范围与整课导出同一口径：讲义页、习题页、课程级内容、00-课程概览、素材页，
    以及图表库的 svg / excalidraw 资产（同一节点的资产合并成一行）。
    """
    try:
        root, out_dir, rel = _course_out_dir(course)
    except ValueError as exc:
        return {"course_id": course.id, "ok": False, "reason": str(exc),
                "vault_root": None, "output_rel": None, "checked": 0,
                "consistent": 0, "inconsistent": 0, "nodes": []}

    scoped = bool(node_ids)
    wanted = set(node_ids or [])
    nodes = _nodes(db, course.id)
    materials = list_materials(db, course.id)
    rows: list[dict] = []
    chapter_links: list[tuple[OutlineNode, list[str]]] = []

    chapter_content = _chapter_content(db, course, nodes)
    for idx, (chapter, members) in enumerate(chapter_content, 1):
        chapter_dir = _chapter_dir(idx, chapter)
        links: list[str] = []
        for node, payload in members:
            stem = _node_stem(node)
            if payload.md or payload.quiz or payload.plan_md or payload.sheet_md:
                links.append(f"[[{stem}|{node.node_key} {node.title}]]")
                links += _doc_links(node, payload)
            if scoped and node.id not in wanted:
                continue
            base = {"node_id": node.id, "node_key": node.node_key, "title": node.title}
            if payload.md:
                rows.append(_verify_file(out_dir, f"{chapter_dir}/{stem}.md", payload.md,
                                         {**base, "part": "lecture"}))
                link_row = _verify_note_links(out_dir, f"{chapter_dir}/{stem}.md",
                                              payload.md, {**base, "part": "link"})
                if link_row:
                    rows.append(link_row)
            if payload.quiz:
                rows.append(_verify_file(out_dir, _quiz_rel(node), payload.quiz,
                                         {**base, "part": "quiz"}))
            if payload.plan_md:
                rows.append(_verify_file(out_dir, _plan_rel(chapter_dir, node), payload.plan_md,
                                         {**base, "part": "lesson-plan"}))
            if payload.sheet_md:
                rows.append(_verify_file(out_dir, _sheet_rel(chapter_dir, node), payload.sheet_md,
                                         {**base, "part": "student-sheet"}))
            assets = [*payload.excalidraw, *payload.svgs]
            if assets:
                row = _verify_assets(out_dir, assets, {**base, "part": "asset"})
                if row:
                    rows.append(row)
        if links:
            chapter_links.append((chapter, links))

    if not scoped:
        course_payload = _build_course_level_payload(db, course)
        if course_payload.md:
            row = _verify_file(out_dir, COURSE_LEVEL_FILE, course_payload.md,
                               {"node_id": None, "node_key": "00",
                                "title": "课程级内容", "part": "course-level"},
                               required=False)
            if row:
                rows.append(row)
        course_assets = [*course_payload.excalidraw, *course_payload.svgs]
        if course_assets:
            row = _verify_assets(out_dir, course_assets,
                                 {"node_id": None, "node_key": "00",
                                  "title": "课程级图形", "part": "asset"})
            if row:
                rows.append(row)
        deck_items = _deck_items(db, course, nodes)
        course_deck = next((Path(rel).name for rel, _s, ch in deck_items if ch is None), None)
        chapter_decks = {ch.node_key: Path(rel).name for rel, _s, ch in deck_items
                         if ch is not None}
        from app.services import concept_graph

        graph_items = concept_graph.graph_payloads(db, course)
        row = _verify_file(out_dir, "00-课程概览.md",
                           "\n".join(_index_page(course, chapter_links, materials, nodes,
                                                course_deck=course_deck,
                                                chapter_decks=chapter_decks,
                                                course_pages=_course_page_links(
                                                    bool(course_payload.md),
                                                    bool(graph_items)))),
                           {"node_id": None, "node_key": "00",
                            "title": "课程概览（目录）", "part": "index"},
                           required=False)
        if row:
            rows.append(row)
        for deck_rel, deck_src, deck_chapter in deck_items:
            rows.append(_verify_deck_file(out_dir, deck_rel, deck_src, {
                "node_id": deck_chapter.id if deck_chapter is not None else None,
                "node_key": deck_chapter.node_key if deck_chapter is not None else "00",
                "title": chapter_label(deck_chapter) if deck_chapter is not None else "整课课件",
                "part": "ppt",
            }))
        link_row = _verify_ppt_links(out_dir, {"node_id": None, "node_key": "00",
                                               "title": "课件双链", "part": "ppt-link"})
        if link_row:
            rows.append(link_row)
        for c_rel, c_text in concept_pages(db, course,
                                           figures=figure_names_by_node(chapter_content)):
            row = _verify_file(out_dir, c_rel, c_text,
                               {"node_id": None, "node_key": "00",
                                "title": Path(c_rel).stem, "part": "concept"},
                               required=False)
            if row:
                rows.append(row)
        for m_idx, material in enumerate(materials, 1):
            m_rel = f"{MATERIALS_DIR}/{_material_stem(m_idx, material)}.md"
            row = _verify_file(out_dir, m_rel, "\n".join(_material_page(course, material)),
                               {"node_id": None, "node_key": "00",
                                "title": material.title, "part": "material"},
                               required=False)
            if row:
                rows.append(row)

    bad = [r for r in rows if r["status"] != "consistent"]
    return {"course_id": course.id, "ok": True, "reason": None,
            "vault_root": str(root), "output_rel": rel,
            "checked": len(rows), "consistent": len(rows) - len(bad),
            "inconsistent": len(bad), "nodes": rows}


# ---------------------------------------------------------------------------
# 资产体检数据源（任务 24-C）：链接健康 + 导出健康
# 报告只读这里的结果，不另算一套口径。
# ---------------------------------------------------------------------------

_RE_FENCE_ANY = re.compile(r"```.*?```", re.S)
_RE_WIKI_ANY = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_RE_MD_INLINE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
_EXTERNAL_PREFIX = ("http://", "https://", "mailto:", "tel:", "obsidian://", "data:", "/")


def _note_text(path: Path) -> str:
    """去掉 frontmatter 的笔记正文（读不到返回空串，不抛）。"""
    try:
        return _RE_FRONTMATTER.sub("", path.read_text(encoding="utf-8"), count=1)
    except (OSError, UnicodeDecodeError):
        return ""


def _link_targets(text: str) -> tuple[list[str], list[str]]:
    """``(双链目标, 相对链接目标)``；围栏代码块里的示例不算链接（Obsidian 也不解析）。"""
    body = _RE_FENCE_ANY.sub("", text)
    wiki = [raw.strip() for raw in _RE_WIKI_ANY.findall(body) if raw.strip()]
    inline: list[str] = []
    for raw in _RE_MD_INLINE.findall(body):
        target = raw.strip().split("#")[0].strip().replace("\\", "/")
        if not target or target.startswith("#") or target.lower().startswith(_EXTERNAL_PREFIX):
            continue
        inline.append(unquote(target))
    return wiki, inline


def _resolve_link(note_dir: str, target: str) -> str:
    """相对链接按「笔记所在目录」解析成导出目录下的相对路径。"""
    base = "" if note_dir == "." else note_dir
    return posixpath.normpath(posixpath.join(base, target)).lstrip("./")


def course_link_health(db: Session, course: Course, *,
                       extra_targets: Iterable[str] = ()) -> dict:
    """课程目录里所有 md 的链接体检（双链 + 站内相对链接）；坏链要求为 0。

    ``extra_targets``：「本次操作紧接着就会落盘」的目标（任务 60）。导出流程在写
    体检报告**之前**算链接体检，而报告是最后一个落盘的文件、又被课程索引链着，
    不声明就会自报 1 条假坏链。只影响这些显式声明的名字，判定规则不变。
    """
    empty = {"ok": False, "output_rel": None, "links": 0, "wiki_links": 0, "file_links": 0,
             "broken": 0, "broken_links": []}
    try:
        _root, out_dir, rel = _course_out_dir(course)
    except ValueError as exc:
        return {**empty, "reason": str(exc)}
    if not out_dir.exists():
        return {**empty, "reason": "课程目录还不存在：先导出一次笔记"}

    rels, stems = _vault_link_index(out_dir)
    _declare_pending_targets(rels, stems, extra_targets)
    wiki_total = file_total = 0
    broken: list[dict] = []
    for path in sorted(out_dir.rglob("*.md")):
        if path.name == AUDIT_REPORT_NAME:
            continue  # 报告只描述别的产出，不算进自己的链接体检
        note = path.relative_to(out_dir)
        wiki, inline = _link_targets(_note_text(path))
        wiki_total += len(wiki)
        file_total += len(inline)
        for target in wiki:
            if not _note_link_exists(rels, stems, target):
                broken.append({"file": note.as_posix(), "target": target, "kind": "wiki"})
        for target in inline:
            if _resolve_link(note.parent.as_posix(), target) not in rels:
                broken.append({"file": note.as_posix(), "target": target, "kind": "file"})
    return {"ok": True, "reason": None, "output_rel": rel, "links": wiki_total + file_total,
            "wiki_links": wiki_total, "file_links": file_total,
            "broken": len(broken), "broken_links": broken[:20]}


def course_asset_health(db: Session, course: Course) -> dict:
    """导出健康：文件清单、孤儿资产、下次导出会保留（手改）的文件、课件落地情况。

    - **孤儿**：不在任何 md 引用里的机器资产（`图表库/`、章节 ``assets/``、可编辑原件）；
      导出末尾的清理会把它们移进 ``99_归档``，所以数量应趋近 0。以 ``_`` 开头的是
      课程级产出（`_图谱` / `_风格库` 等），不按孤儿算。
    - **保留**：与 manifest 记录对不上、会被 no-clobber 跳过的文件（教师手改的证据）。
    """
    empty = {"ok": False, "output_rel": None, "files": 0, "notes": 0, "charts": 0,
             "assets": 0, "concept_pages": 0, "ppt": 0, "orphan_count": 0, "orphans": [],
             "preserved_count": 0, "preserved": [], "decks": 0, "deck_missing": []}
    try:
        root, out_dir, rel = _course_out_dir(course)
    except ValueError as exc:
        return {**empty, "reason": str(exc)}
    if not out_dir.exists():
        return {**empty, "reason": "课程目录还不存在：先导出一次笔记"}

    manifest = dict(course_kb(course).get("manifest") or {})
    referenced: set[str] = set()
    notes: list[Path] = []
    for path in sorted(out_dir.rglob("*.md")):
        if path.name == AUDIT_REPORT_NAME:
            continue  # 报告是「描述其余产出的那一份」，不计入自己的统计
        notes.append(path)
        wiki, inline = _link_targets(_note_text(path))
        for target in wiki:
            referenced.update({target.strip("/"), Path(target).name,
                               Path(Path(target).name).stem})
        for target in inline:
            referenced.update({target.strip("/"), Path(target).name,
                               Path(Path(target).name).stem})

    files = [path for path in sorted(out_dir.rglob("*"))
             if path.is_file() and path.name != AUDIT_REPORT_NAME]
    counts = {"charts": 0, "assets": 0, "concept_pages": 0, "ppt": 0}
    orphans: list[str] = []
    preserved: list[dict] = []
    for path in files:
        inside = path.relative_to(out_dir)
        rel_path = inside.as_posix()
        suffix = path.suffix.lower()
        if CHARTS_DIR in inside.parts:
            counts["charts"] += 1
        if ASSETS_DIR in inside.parts:
            counts["assets"] += 1
        if CONCEPTS_DIR in inside.parts and suffix == ".md" and not path.name.startswith("_"):
            counts["concept_pages"] += 1
        if suffix in PPT_SUFFIXES:
            counts["ppt"] += 1
        try:
            old_bytes = path.read_bytes()
        except OSError:
            continue
        # manifest 的键是「vault 相对路径」，这里必须用同一口径查，否则永远查不到记录，
        # 没有内嵌指纹的文件（.canvas / .pptx）会被误判成 unknown-drift「疑似手改」
        vault_rel = f"{rel}/{rel_path}"
        reason = _skip_reason(old_bytes, manifest.get(vault_rel) or manifest.get(rel_path), rel_path)
        if reason:
            preserved.append({"file": rel_path, "reason": reason})
        if suffix == ".md" or suffix in PPT_SUFFIXES or path.name.startswith("_"):
            continue
        names = {rel_path, path.name, path.stem, f"{path.stem}{suffix}"}
        if not (names & referenced):
            orphans.append(rel_path)

    decks = _deck_items(db, course, _nodes(db, course.id))
    deck_missing = [deck_rel for deck_rel, _src, _chapter in decks
                    if not (out_dir / deck_rel).exists()]
    return {"ok": True, "reason": None, "output_rel": rel, "root": str(root),
            "files": len(files), "notes": len(notes), **counts,
            "orphan_count": len(orphans), "orphans": orphans[:20],
            "preserved_count": len(preserved), "preserved": preserved[:20],
            "decks": len(decks), "deck_missing": deck_missing}


def force_write_node(db: Session, course: Course, node: OutlineNode) -> dict:
    """以网页端数据为准强制覆盖该知识点的笔记文件（7b 的「修复不一致」）。"""
    root, out_dir, rel = _course_out_dir(course)
    kb = course_kb(course)
    manifest = dict(kb.get("manifest") or {})
    nodes = _nodes(db, course.id)
    result = _emit_node_payload(db, course, node, out_dir, nodes, root=root,
                                manifest=manifest, force=True)
    kb["manifest"] = manifest
    kb["exported_at"] = _now_iso()
    meta = dict(course.meta or {})
    meta[KB_KEY] = kb
    course.meta = meta
    db.commit()
    result.update({"ok": True, "vault_path": str(root), "output_rel": rel,
                   "node_key": node.node_key})
    return result


def collect_node_notes(db: Session, course: Course, node_ids: list[str]) -> list[tuple[str, bytes]]:
    """收集指定节点的导出文件（不含落盘）：(vault 相对路径, 内容字节)。

    供「导出 ZIP（单节点/多节点）」使用：只打包所选知识点页、本章 assets 配图、
    该节点习题页与对应的可编辑 Excalidraw，便于单独分发。
    """
    nodes = _nodes(db, course.id)
    by_id = {n.id: n for n in nodes}
    chapters = _chapter_list(nodes)
    entries: list[tuple[str, bytes]] = []
    for node_id in node_ids:
        node = by_id.get(node_id)
        if node is None:
            continue
        for idx, chapter in enumerate(chapters, 1):
            if not (node.node_key == chapter.node_key
                    or node.node_key.startswith(f"{chapter.node_key}.")):
                continue
            chapter_dir = _chapter_dir(idx, chapter)
            payload = _build_node_payload(db, course, node)
            if payload.md:
                entries.append((f"{chapter_dir}/{_node_stem(node)}.md",
                                stamp_markdown(payload.md).encode("utf-8")))
            for src, target_rel in payload.assets:
                entries.append((f"{chapter_dir}/{target_rel}", src.read_bytes()))
            for xrel, xtext in payload.excalidraw:
                entries.append((xrel, stamp_json(xtext).encode("utf-8")))
            for srel, stext in payload.svgs:
                entries.append((srel, stamp_svg(stext).encode("utf-8")))
            if payload.quiz:
                entries.append((_quiz_rel(node),
                                stamp_markdown(payload.quiz).encode("utf-8")))
            if payload.plan_md:
                entries.append((_plan_rel(chapter_dir, node),
                                stamp_markdown(payload.plan_md).encode("utf-8")))
            if payload.plan_docx:
                entries.append((_plan_rel(chapter_dir, node, "教案.docx"), payload.plan_docx))
            if payload.sheet_md:
                entries.append((_sheet_rel(chapter_dir, node),
                                stamp_markdown(payload.sheet_md).encode("utf-8")))
            if payload.sheet_docx:
                entries.append((_sheet_rel(chapter_dir, node, "学案.docx"), payload.sheet_docx))
            break
    return entries


def incremental_write_content(
    db: Session,
    course_id: str,
    node_id: str | None,
    kind: str,
    payload: dict | None = None,
) -> dict:
    """单条内容增量写入 Vault（新结构：知识点页 + 本章 assets + 习题集 + 图表库）。"""
    course = db.get(Course, course_id)
    if course is None:
        return {"vault_path": None, "note_path": None, "chart_path": None,
                "ok": False, "reason": "course-not-found"}
    root = _configured_vault_root(course)
    if root is None:
        return {"vault_path": None, "note_path": None, "chart_path": None,
                "ok": False, "reason": "vault-not-configured"}
    node = None
    if node_id:
        node = db.get(OutlineNode, node_id)
        if node is None or node.outline is None or node.outline.course_id != course.id:
            return {"vault_path": str(root), "note_path": None, "chart_path": None,
                    "ok": False, "reason": "node-not-found"}
    try:
        rel = course_kb(course).get("output_rel") or f"课程/{_slug(course.title, 40)}"
        out_dir = _safe(str(root), rel)
        nodes = _nodes(db, course.id)
        kb = course_kb(course)
        manifest = dict(kb.get("manifest") or {})
        result = _emit_node_payload(db, course, node, out_dir, nodes,
                                    root=root, manifest=manifest)
    except ValueError as exc:
        return {"vault_path": str(root), "note_path": None, "chart_path": None,
                "ok": False, "reason": "invalid-path", "error": str(exc)}
    # 与整课导出共用同一份 manifest：教师手改过的文件会被跳过且不被标记为已写
    meta = dict(course.meta or {})
    kb_meta = dict(meta.get(KB_KEY) or {})
    kb_meta["manifest"] = manifest
    kb_meta["synced_at"] = _now_iso()
    meta[KB_KEY] = kb_meta
    course.meta = meta
    return {
        "vault_path": str(root),
        "note_path": result["note_path"],
        "chart_path": result["chart_path"],
        "ok": True,
        "files": result["files"],
        "preserved": result["preserved"],
    }


def sync_content_vault(
    db: Session,
    course_id: str,
    node_id: str | None,
    kind: str,
    payload: dict | None = None,
    *,
    item: ContentItem | None = None,
) -> dict:
    """安全包装：增量写入失败只记日志，不影响主流程；结果记入 course.meta.kb.incr。"""
    try:
        result = incremental_write_content(db, course_id, node_id, kind, payload)
    except Exception as exc:  # noqa: BLE001 —— 主流程不因 Vault 写入失败而中断
        logger.warning("incremental vault write failed course=%s node=%s kind=%s: %s",
                       course_id, node_id, kind, exc)
        result = {"vault_path": None, "note_path": None, "chart_path": None,
                  "ok": False, "reason": "failed", "error": str(exc)}

    if result.get("ok"):
        result["vault_write"] = "saved"
    elif result.get("reason") == "vault-not-configured":
        result["vault_write"] = "skipped"
    else:
        result["vault_write"] = "failed"

    course = db.get(Course, course_id)
    if course is not None:
        meta = dict(course.meta or {})
        kb = dict(meta.get(KB_KEY) or {})
        incr = dict(kb.get("incr") or {})
        if item is not None:
            incr[f"item:{item.id}"] = result
        elif node_id:
            incr[f"{kind}:{node_id}"] = result
        else:
            incr[f"{kind}:course"] = result
        kb["incr"] = incr
        meta[KB_KEY] = kb
        course.meta = meta
    if item is not None:
        item.meta = {**(dict(item.meta or {})), "vault": result}
    db.commit()
    return result


def attach_vault_write(
    db: Session,
    item: ContentItem,
    kind: str,
    payload: dict | None = None,
) -> dict:
    """内容条目生成/导入成功后调用：增量写 Vault，并把状态记到 item.meta。"""
    return sync_content_vault(db, item.course_id, item.node_id, kind, payload, item=item)


def vault_status_map(db: Session, course_id: str) -> dict:
    """供列表接口回显每个条目最后一次的增量写入状态。"""
    course = db.get(Course, course_id)
    if course is None:
        return {}
    return dict(course_kb(course).get("incr") or {})
