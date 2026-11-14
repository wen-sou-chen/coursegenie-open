"""M5c 交付包：把一门课的资产打成「客户能直接收」的文件夹 + zip（任务 31）。

交付包：``00-交付说明.md`` / ``01-验收单.md`` / ``讲义/``（每节 PDF + md 源）/
``教案/``（每课时 Word + PDF，0 LLM 骨架，任务 49）/ ``课件/``（整课 + 各章 pptx）/
``图表/``（svg + png）/ ``笔记库/``（Obsidian 导出）/ ``素材清单.md`` /
``manifest.json``（相对路径 + sha1 + 字节数 + 生成参数与耗时费用）。

硬要求（HANDOFF 任务 31）：
- **0 LLM 调用**：只读 DB / storage / vault，纯本地渲染与打包；
- **不写笔记库**（任务 38 起例外）：``D:\\CourseGenie知识库`` 里的文件默认只读复制；只有出包前
  一致性自检发现「笔记库落后于网页端」时，才会先按网页端口径自动导出一次笔记（同样 0 LLM），
  导出后仍不一致则拒绝出包（``allow_inconsistent=True`` 可强制）；交付包只落在 ``storage/deliveries``；
- **PDF**：讲义 Markdown → HTML → Windows 自带无头 Edge 打印（零新依赖）。缺 Edge 或打印失败
  → 显式降级为 ``讲义/*.html``（浏览器可一键打印），并在 ``00-交付说明.md`` 与 ``manifest.json``
  里写明原因，绝不静默留空；打印完把 PDF 文本层的兼容区码位归一到正字（``repair_pdf_text_layer``，
  任务 95：换成 OFL 字体后 Chromium 的 ToUnicode 反查会挑到康熙部首，客户搜不到「二 / 长 / 门」）；
 - **幂等**：**同一份内容 = 同一次交付** —— 重复构建重建同一个包（任务 126 起构建号跟内容
  指纹走、跨分钟也复用），不产生一堆重复目录；
- 缺哪一项就在说明与 manifest 里写「本次未生成 + 原因」。
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import tempfile
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import (ContentItem, Course, Outline, OutlineNode, QuizItem,
                               SourceMaterial)
from app.services import audience
from app.services import audit as audit_mod
from app.services import deck_html
from app.services import docx_export
from app.services import integrity_check
from app.services import kb as kb_mod
from app.services import lesson_plan
from app.services import lecture_qc
from app.services import md_tables
from app.services import recording_kit
from app.services import scenario
from app.services import student_sheet
from app.services.material import (
    ORIGIN_AI,
    grounding_of,
    list_materials,
    origin_label,
    origin_of,
    pages_of,
)
from app.services.planner import list_nodes

logger = logging.getLogger(__name__)

DELIVERY_DIR_NAME = "deliveries"
BUILD_VERSION = "M5c-1"
# 任务 48p：交付模式。**默认对外** —— 金额 / tokens / LLM 调用次数不进客户包；
# 内部复盘（course.meta.delivery.external = False）保持原样（与今天的包逐字一致）。
DELIVERY_META_KEY = "delivery"
MODE_EXTERNAL = "external"
MODE_INTERNAL = "internal"
CONTENT_VERSION = "v1"
# 任务 61：文案与体检报告对外口径同一份（audit.EXTERNAL_USAGE_NOTE），避免两处走偏。
EXTERNAL_USAGE_NOTE = audit_mod.EXTERNAL_USAGE_NOTE
README_NAME = "00-交付说明.md"
ACCEPT_NAME = "01-验收单.md"
MATERIAL_NAME = "素材清单.md"
MANIFEST_NAME = "manifest.json"
LECTURE_DIR = "讲义"
#: 任务 86：学员版讲义（去答案与解析，题目与作答区保留）。**只在**课程场景含
#: ``self_study`` 或出包页显式勾选时多出这个目录 —— 默认八件套结构一个字不变。
STUDENT_LECTURE_DIR = "学员版讲义"
LECTURE_ASSETS_DIR = "_assets"
LESSON_DIR = lesson_plan.DIR_NAME
SHEET_DIR = student_sheet.DIR_NAME
DECK_DIR = "课件"
FIGURE_DIR = "图表"
NOTES_DIR = "笔记库"

FIGURE_KINDS = ("diagram", "chart_bar", "chart_line", "chart_pie",
                "chart_table", "chart_mindmap", "chart_timeline", "chart_quadrant")

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Noto Sans SC", "思源黑体", "Microsoft YaHei", "微软雅黑", "PingFang SC", "SimSun", sans-serif;
       font-size: 12pt; line-height: 1.75; color: #1f2328; }
h1 { font-size: 19pt; margin: 0 0 8pt; }
h2 { font-size: 15pt; margin: 14pt 0 6pt; padding-bottom: 3pt; border-bottom: 1px solid #d0d7de; }
h3 { font-size: 13pt; margin: 11pt 0 4pt; }
p, li { word-break: break-word; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 10.5pt; }
th, td { border: 1px solid #c8ccd1; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #f2f5f8; }
pre { background: #f6f8fa; border: 1px solid #e2e6ea; border-radius: 4pt; padding: 6pt 8pt;
      white-space: pre-wrap; word-break: break-word;
      font-family: "Cascadia Mono", Consolas, "Courier New", monospace; font-size: 9.5pt; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 10pt; }
blockquote { margin: 6pt 0; padding: 2pt 10pt; border-left: 3pt solid #d0d7de; color: #57606a; }
figure { margin: 10pt 0; text-align: center; page-break-inside: avoid; }
figure svg { max-width: 100%; height: auto; }
figcaption { font-size: 9.5pt; color: #57606a; margin-top: 3pt; }
.mermaid-note { font-size: 9pt; color: #57606a; margin: 0 0 4pt; }
/* 任务 48n：mermaid 默认字体（trebuchet ms / verdana / arial）不含中文 —— 无头打印时
   回退字体的 ToUnicode 会把字形映成康熙部首码位（「用 / 生 / 工」落到 U+2F00 段），
   客户在 PDF 里搜不到、复制出来是怪字。把图内文字显式落到 CJK 字体即可：只动字体族，
   不动字号 / 字重 / 行距。带 bare svg / .mermaid 选择器是为了同时覆盖 foreignObject
   里的 HTML 标签（font-family 可继承）。
   任务 95：首位换成 SIL OFL 的 Noto Sans SC / 思源黑体（免费可商用、可嵌入）——
   微软雅黑是微软授权字体，嵌进要发给客户的 PDF/HTML 有授权风险，降为兜底。 */
svg, svg text, svg tspan, .mermaid, .mermaid text, .mermaid tspan,
.mermaid foreignObject, .mermaid foreignObject * {
  font-family: "Noto Sans SC", "思源黑体", "Microsoft YaHei", "微软雅黑", "PingFang SC", "SimSun", sans-serif !important; }
.course-head { color: #57606a; font-size: 10pt; margin-bottom: 10pt; }
math { font-family: "Cambria Math", "Latin Modern Math", serif; font-size: 1.05em; }
math[display="block"] { display: block; margin: 8pt 0; text-align: center; }
.math-note { background: #fff8e6; border: 1px solid #f0d9a0; border-radius: 4pt;
             padding: 5pt 8pt; font-size: 10pt; margin: 0 0 8pt; }
.math-src { color: #7d4e00; }
li.correct { font-weight: 600; color: #0b6b3a; }
li.correct::before { content: ""; display: inline-block; width: 5pt; height: 9pt;
                     margin-right: 5pt; vertical-align: -2pt;
                     border-right: 2pt solid currentColor; border-bottom: 2pt solid currentColor;
                     transform: rotate(45deg); }
"""


# ---------------------------------------------------------------------------
# 路径与工具
# ---------------------------------------------------------------------------

def deliveries_root() -> Path:
    """交付包根目录（绝对路径）。

    ``settings.storage_dir`` 默认是相对的 ``./storage``；无头 Edge 的 ``--print-to-pdf``
    只认绝对路径（真机实测：相对路径 → 退出码 21 / Failed to write file），
    所以这里统一落成绝对路径。
    """
    return (get_settings().storage_dir / DELIVERY_DIR_NAME).resolve()


def course_slug(course: Course) -> str:
    return kb_mod._slug(course.title or course.id, 40)


def delivery_mode(course: Course) -> dict:
    """当前交付模式（0 LLM、无副作用）：``{"external": bool, "mode": str, "label": str}``。

    缺省**按对外处理**（course.meta 里没写就按对外）—— 宁可少说内部信息：
    金额 / tokens / LLM 调用次数只在自己复盘时才写进客户包。
    """
    external = dict((course.meta or {}).get(DELIVERY_META_KEY) or {}).get("external")
    external = True if external is None else bool(external)
    return {"external": external, "mode": MODE_EXTERNAL if external else MODE_INTERNAL,
            "label": ("对外交付（不含用量与金额）" if external
                      else "内部复盘（含用量与金额）")}


def set_delivery_mode(db: Session, course: Course, *, external: bool) -> dict:
    """把交付模式写回 ``course.meta.delivery.external``（JSON 列要整体替换才会被追踪）。"""
    meta = dict(course.meta or {})
    cfg = dict(meta.get(DELIVERY_META_KEY) or {})
    cfg["external"] = bool(external)
    meta[DELIVERY_META_KEY] = cfg
    course.meta = meta
    db.add(course)
    db.commit()
    return delivery_mode(course)


def build_id_for(course: Course, when: datetime | None = None) -> str:
    """``<课程slug>-<YYYYMMDD-HHMM>``：构建号的**排头**（时间戳只负责「像一次交付」）。

    「同一次交付」的判据不在这里 —— 任务 126 起由 :func:`resolve_build_target` 按**内容
    指纹**决定：同一份内容（+ 同一套出包选项）不管隔几分钟都复用同一个号。任务 126 之前
    这里就是全部口径（「同一分钟内重复构建 = 同一次交付」），跨分钟必换号 ⇒ 交付包幂等
    在跨分钟的慢构建 / 前一分钟撞过名的课上必然红（DSH 2026-10-03 门禁实测）。
    """
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M")
    return f"{course_slug(course)}-{stamp}"


# ── 任务 126：交付包幂等 —— 构建号由**内容指纹**决定 ─────────────────────────────
#: 内容指纹**故意排除**的易变 meta 键：打包流程自己会写它们（出包前的旧口径质检复核 /
#: 笔记导出回写的状态），计进指纹就是「自己改自己」—— 第一次出包把指纹改掉，第二次出包
#: 立刻被判成「内容变了」，幂等当场失效。**契约：谁在出包过程中写库，谁就在这里登记。**
FINGERPRINT_VOLATILE_ITEM_META_KEYS = ("qc", "sources", "vault")
#: ``course.meta`` 整块都**不进**指纹：``delivery`` 由 :func:`set_delivery_mode` 写、
#: ``kb`` 块里的 manifest / synced_at / audit_at 由笔记导出与体检写（出手即变）。只有下面
#: 这两个与「``笔记库/`` 落在包里哪一层」有关的稳定键例外 —— 交付模式另走 options 参数。
FINGERPRINT_STABLE_KB_KEYS = ("vault_root", "output_rel")


def _stable_item_meta(meta: dict | None) -> dict:
    """内容条目的 meta 去掉出包过程自己会写的键（未知键保留：它也可能进包）。"""
    return {key: value for key, value in dict(meta or {}).items()
            if key not in FINGERPRINT_VOLATILE_ITEM_META_KEYS}


def delivery_content_fingerprint(db: Session, course: Course, *, mode: str = "",
                                 pdf_engine: str = "",
                                 student_lectures: bool | None = None) -> str:
    """这次交付的**内容指纹**（sha1 40 位；纯读库、0 LLM，任意时刻重算同一个值）。

    覆盖「会写进交付包正文」的东西 —— 课程标题与笔记库绑定、提纲 / 节点 / 内容条目
    （kind + title + body + 元数据）/ 测验 / 素材（含正文），外加**本次出包选项**
    （交付模式 / PDF 口径 / 学员版：选项不同 = 包里文件集合不同，不该共用一个构建号）。

    **不覆盖**（逐条登记在 :data:`FINGERPRINT_VOLATILE_*` 与下面的 docstring 里）：
    打包过程自己写回的键、构建时刻 / 耗时 / 随机数 / 进程与环境探测结果。理由只有一个：
    指纹一旦能被「重算一次就变」的东西带偏，幂等就当场失效 —— 而这正是任务 126 要修的 bug。
    """
    nodes = (db.query(OutlineNode)
             .join(Outline, OutlineNode.outline_id == Outline.id)
             .filter(Outline.course_id == course.id).all())
    payload = {
        # 出包选项：pdf 口径先归一（"AUTO" / " auto " 与 "auto" 是同一次交付）
        "options": {"mode": str(mode or ""),
                    "pdf_engine": str(pdf_engine or "").strip().lower(),
                    "student_lectures": (None if student_lectures is None
                                         else bool(student_lectures))},
        # 课程本身：标题决定 slug 与各文件标题；受众 / 课时 / 状态都写在 pack 的文件里
        # （`00-交付说明.md` 题头 + manifest），改了它们包就变了 ⇒ 算「内容变了」。
        # course.meta 只认笔记库绑定那两键（其余键交付流程一个字都不读，见上面的常量）。
        "course": {"title": course.title or "", "subject": course.subject or "",
                   "audience": course.audience or "",
                   "duration_minutes": int(course.duration_minutes or 0),
                   "status": course.status or ""},
        "kb": {key: str(kb_mod.course_kb(course).get(key) or "")
               for key in FINGERPRINT_STABLE_KB_KEYS},
        "outlines": sorted(
            ({"id": row.id, "title": row.title or "", "goal": row.goal or "",
              "content": row.content or {}}
             for row in db.query(Outline).filter(Outline.course_id == course.id).all()),
            key=lambda row: row["id"]),
        "nodes": sorted(
            ({"id": row.id, "key": row.node_key or "", "type": row.node_type or "",
              "title": row.title or "", "objective": row.objective or "",
              "strategy": row.teaching_strategy or "", "assessment": row.assessment or "",
              "prerequisites": row.prerequisites or [], "content_md": row.content_md or "",
              "order": row.order_index} for row in nodes),
            key=lambda row: (row["order"], row["key"], row["id"])),
        "items": sorted(
            ({"id": row.id, "node_id": row.node_id or "", "kind": row.kind or "",
              "title": row.title or "", "body": row.body or {},
              "meta": _stable_item_meta(row.meta)}
             for row in db.query(ContentItem)
             .filter(ContentItem.course_id == course.id).all()),
            key=lambda row: row["id"]),
        "quizzes": sorted(
            ({"id": row.id, "node_id": row.node_id or "", "qtype": row.qtype or "",
              "difficulty": row.difficulty or "", "question": row.question or "",
              "options": row.options or [], "answer": row.answer or "",
              "explanation": row.explanation or "", "meta": dict(row.meta or {})}
             for row in db.query(QuizItem).filter(QuizItem.course_id == course.id).all()),
            key=lambda row: row["id"]),
        "materials": sorted(
            ({"id": row.id, "title": row.title or "", "kind": row.kind or "",
              "text": row.text or "", "meta": dict(row.meta or {})}
             for row in list_materials(db, course.id)),
            key=lambda row: row["id"]),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _manifest_of(dir_path: Path) -> dict | None:
    """读某交付目录的 manifest（没有 / 坏掉 / 不是对象 ⇒ None）。"""
    path = dir_path / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _find_same_content_package(root: Path, course: Course,
                               fingerprint: str) -> tuple[str, Path] | None:
    """在同 slug 的既有包里找「这门课 + 这份内容」的那一个（任务 126 的复用口径）。

    用 ``iterdir`` + 前缀比对而不是 ``glob``：``course_slug`` 允许 ``[]`` 之类的字符，
    在 glob 里是元字符，会误匹配到别人的包。manifest 里没有 ``content_fingerprint`` 的
    存量包一律不认（宁可多出一个包，也不拿旧包当同内容）。
    """
    if not root.is_dir():
        return None
    prefix = f"{course_slug(course)}-"
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir() or not candidate.name.startswith(prefix):
            continue
        data = _manifest_of(candidate)
        if not data or str(data.get("content_fingerprint") or "") != fingerprint:
            continue
        if str((data.get("course") or {}).get("id") or "") != course.id:
            continue
        return str(data.get("build_id") or candidate.name), candidate
    return None


def resolve_build_target(root: Path, course: Course, when: datetime,
                         fingerprint: str) -> tuple[str, Path]:
    """这次的构建号与输出目录（**幂等口径的唯一出处**，任务 126）。

    * 已有**同内容**的包 ⇒ 复用它的构建号与目录（跨分钟、跨天都算同一次交付）；
    * 头一次出包 / 内容变了 ⇒ ``<slug>-<YYYYMMDD-HHMM>``；这个名字被**别的内容 / 别的课**
      占着（同一分钟撞名）就追加内容指纹短码 —— 不覆盖别人的包，也不让两门课共用一个目录。

    撞名判据从既有的「课程 id 不同」细化为「**内容指纹不同**」：任务 126 之前，同一门课在
    同一分钟改了正文会**原地覆盖**上一版（内容变了却同名），跨分钟又只认时间戳 ⇒ 幂等
    只剩「恰好同一分钟」这一种运气。
    """
    reused = _find_same_content_package(root, course, fingerprint)
    if reused is not None:
        return reused
    build_id = build_id_for(course, when)
    out_dir = root / build_id
    if out_dir.exists():
        build_id = f"{build_id}-{fingerprint[:6]}"
        out_dir = root / build_id
        if out_dir.exists():
            # 极窄的兜底：两门课同 slug + 同内容（指纹短码也一样）⇒ 再挂课程 id 短码
            build_id = f"{build_id}-{course.id[:6]}"
            out_dir = root / build_id
    return build_id, out_dir


def _sha1(path: Path) -> str:
    import hashlib

    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _write_text(root: Path, rel: str, text: str) -> dict:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return {"path": rel, "bytes": path.stat().st_size, "sha1": _sha1(path)}


def _write_bytes(root: Path, rel: str, data: bytes) -> dict:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": rel, "bytes": path.stat().st_size, "sha1": _sha1(path)}


def _copy_file(root: Path, rel: str, src: Path) -> dict:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, path)
    return {"path": rel, "bytes": path.stat().st_size, "sha1": _sha1(path)}


def find_edge() -> str | None:
    """Windows 自带 Edge（零新依赖的 PDF 打印引擎）。"""
    override = os.environ.get("DELIVERY_EDGE_EXE")
    if override and Path(override).exists():
        return override
    return next((path for path in EDGE_CANDIDATES if Path(path).exists()), None)


def find_mermaid_js() -> Path | None:
    """前端已装好的 mermaid 打包产物（讲义 HTML 里离线渲染流程图，缺了就退化为代码块）。"""
    override = os.environ.get("DELIVERY_MERMAID_JS")
    if override and Path(override).is_file():
        return Path(override)
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "frontend" / "node_modules" / "mermaid" / "dist" / "mermaid.min.js"
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# Markdown → HTML（无第三方依赖：够讲义用即可，表格 / 列表 / 代码块 / mermaid）
# ---------------------------------------------------------------------------

_RE_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_EM = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_RE_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_RE_OL = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_RE_BULLET = re.compile(r"^(\s*)([-*+✓✔√])\s+(.*)$")
_CORRECT_MARKS = frozenset("✓✔√")
_RE_RANGE_TILDE = re.compile(r"(?<=\d)\s*~\s*(?=\d)")


# ---------------------------------------------------------------------------
# 公式：$…$ / $$…$$ → MathML（任务 53）
#
# 交付件 HTML 由 Chromium/Edge 打印 PDF，**原生支持 MathML** ⇒ 服务端换成 MathML 就够，
# 不必引前端那套 KaTeX；换不出来（坏 LaTeX / 引擎缺失）时**不许静默**：把原 LaTeX 放进
# ``<code>`` 并在该篇顶部写一行「本篇有 N 处公式未能渲染」。
# ---------------------------------------------------------------------------
_RE_MATH_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.S)
_RE_MATH_INLINE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)")
_MATH_ENGINE_OK: bool | None = None
_MATH_STATE = threading.local()


class _MathStats:
    """一次渲染里公式的统计（成功几处、哪几处失败）。"""

    def __init__(self) -> None:
        self.rendered = 0
        self.failed: list[str] = []


def _math_stats() -> _MathStats | None:
    return getattr(_MATH_STATE, "current", None)


def math_engine_available() -> bool:
    """``latex2mathml`` 是否可用（探测一次并缓存）。缺了就把公式显式降级，不抛异常。"""
    global _MATH_ENGINE_OK
    if _MATH_ENGINE_OK is None:
        try:
            import latex2mathml.converter  # noqa: F401
        except Exception:  # noqa: BLE001 —— 纯 Python 依赖，正常都装得上
            _MATH_ENGINE_OK = False
        else:
            _MATH_ENGINE_OK = True
    return _MATH_ENGINE_OK


def tex_to_mathml(tex: str, *, display: bool = False) -> str | None:
    """LaTeX → MathML；引擎缺失或 LaTeX 写坏时返回 ``None``（由调用方显式降级）。"""
    source = str(tex or "").strip()
    if not source or not math_engine_available():
        return None
    try:
        import latex2mathml.converter as converter

        out = converter.convert(source)
    except Exception:  # noqa: BLE001 —— 坏 LaTeX 不许把出包搞挂
        return None
    if not out or "<math" not in out:
        return None
    if display:
        out = out.replace("display=" + chr(34) + "inline" + chr(34),
                          "display=" + chr(34) + "block" + chr(34), 1)
    return out


def _math_html(tex: str, *, display: bool) -> str:
    """一处公式 → MathML；失败就退化成 ``<code>`` 里的 LaTeX 原文并计数（不静默）。"""
    html = tex_to_mathml(tex, display=display)
    stats = _math_stats()
    if html is not None:
        if stats is not None:
            stats.rendered += 1
        return html
    if stats is not None:
        stats.failed.append(str(tex).strip())
    marker = f"$${tex}$$" if display else f"${tex}$"
    klass = "math-src math-src-block" if display else "math-src"
    return f"<code class=\"{klass}\">{html_mod.escape(marker)}</code>"


def _mask_math(text: str) -> tuple[str, dict[str, str]]:
    """把 ``$$…$$`` / ``$…$`` 摘出来换成占位符。

    必须先摘公式再做转义与行内替换：公式里的 ``* _ [ ] \\`` 否则会被当成强调 / 链接语法。
    """
    rendered: dict[str, str] = {}

    def _take(match: re.Match[str], *, display: bool) -> str:
        token = f"\x00M{len(rendered)}\x00"
        rendered[token] = _math_html(match.group(1), display=display)
        return token

    masked = _RE_MATH_DISPLAY.sub(lambda m: _take(m, display=True), str(text or ""))
    masked = _RE_MATH_INLINE.sub(lambda m: _take(m, display=False), masked)
    return masked, rendered


def _inline_html(text: str) -> str:
    """行内语法：先摘出公式，再转义，再按「双链 → 链接 → 粗体/斜体 → 行内码」替换。"""
    masked, rendered = _mask_math(text)
    masked = _RE_RANGE_TILDE.sub("–", masked)
    out = html_mod.escape(masked, quote=False)
    out = _RE_WIKI.sub(lambda m: f"<span class=\"wiki\">{m.group(2) or m.group(1)}</span>", out)
    out = _RE_LINK.sub(lambda m: f"<a href=\"{m.group(2)}\">{m.group(1)}</a>", out)
    out = _RE_BOLD.sub(r"<strong>\1</strong>", out)
    out = _RE_EM.sub(r"<em>\1</em>", out)
    out = _RE_CODE.sub(r"<code>\1</code>", out)
    for token, html in rendered.items():
        out = out.replace(token, html)
    return out


def _figure_html(caption: str, inner: str) -> str:
    cap = f"<figcaption>{_inline_html(caption)}</figcaption>" if caption else ""
    return f"<figure>{inner}{cap}</figure>"


def markdown_to_html(md: str, *, resolve_image=None, mermaid: bool = True) -> str:
    """讲义 Markdown → 可打印 HTML（公式换成 MathML，正文见 ``_markdown_to_html``）。

    公式统计挂在线程局部状态上：``resolve_image`` 回调里渲染的 ``chart://`` 数据卡表格
    也计入同一篇的失败数，所以统一在这里托管进出。
    """
    stats = _MathStats()
    previous = getattr(_MATH_STATE, "current", None)
    _MATH_STATE.current = stats
    try:
        body = _markdown_to_html(md, resolve_image=resolve_image, mermaid=mermaid)
    finally:
        _MATH_STATE.current = previous
    if stats.failed:
        # 不许静默：坏 LaTeX / 引擎缺失时在该篇顶部留一行，正文里原样保留源码
        note = (f"<p class=\"math-note\">本篇有 {len(stats.failed)} 处公式未能渲染"
                "（已按原 LaTeX 原样保留，请人工核对后再交付）</p>")
        return note + "\n" + body
    return body


def _markdown_to_html(md: str, *, resolve_image=None, mermaid: bool = True) -> str:
    """讲义 Markdown → 可打印 HTML。

    ``resolve_image(url)`` 把 ``diagram://id`` / ``chart://id`` 换成内嵌 SVG（或 None 表示
    取不到 → 显式留一行「图缺失」说明，不静默丢）。
    """
    lines = str(md or "").split("\n")
    out: list[str] = []
    list_stack: list[str] = []
    i = 0

    def close_lists() -> None:
        while list_stack:
            out.append(f"</{list_stack.pop()}>")

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # 独立公式块 $$…$$（任务 53）：单行或多行，累积到收尾的 $$
        if stripped.startswith("$$"):
            close_lists()
            block = [stripped[2:]]
            i += 1
            while "$$" not in block[-1] and i < len(lines):
                block.append(lines[i].strip())
                i += 1
            if "$$" in block[-1]:
                block[-1] = block[-1].split("$$")[0]
            out.append(_math_html("\n".join(block).strip(), display=True))
            continue

        # 代码块（``` / ```mermaid / ```text）
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            i += 1
            body: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            close_lists()
            code = html_mod.escape("\n".join(body))
            if lang == "mermaid" and mermaid:
                out.append('<p class="mermaid-note">流程图（Mermaid 源码，下面渲染为图）</p>')
                out.append(f'<pre class="mermaid">{code}</pre>')
            else:
                out.append(f"<pre><code>{code}</code></pre>")
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < len(lines) and _RE_TABLE_SEP.match(lines[i + 1].strip()):
            close_lists()
            header = [cell.strip() for cell in stripped.strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([cell.strip() for cell in lines[i].strip().strip("|").split("|")])
                i += 1
            head_html = "".join(f"<th>{_inline_html(cell)}</th>" for cell in header)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline_html(cell)}</td>" for cell in row) + "</tr>"
                for row in rows)
            out.append(f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>")
            continue

        # 标题
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_lists()
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{_inline_html(heading.group(2))}</h{level}>")
            i += 1
            continue

        # 分隔线
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            close_lists()
            out.append("<hr />")
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            close_lists()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{_inline_html(' '.join(quote))}</blockquote>")
            continue

        # 独立图片（图卡）：diagram:// 与 chart:// 内嵌 SVG
        image = re.match(r"^!\[([^\]]*)\]\(([^)\s]+)\)$", stripped)
        if image:
            close_lists()
            caption, url = image.group(1), image.group(2)
            inner = resolve_image(url) if resolve_image else None
            if inner is None:
                out.append(_figure_html(caption, '<div class="figure-missing">'
                                        f'（本图未随包交付：{html_mod.escape(url)}）</div>'))
            else:
                out.append(_figure_html(caption, inner))
            i += 1
            continue

        # 列表
        bullet = _RE_BULLET.match(raw)
        ordered = _RE_OL.match(raw)
        if bullet or ordered:
            depth = len((bullet.group(1) if bullet else raw[: raw.index(ordered.group(1))])) // 2
            tag = "ol" if ordered else "ul"
            text = ordered.group(2) if ordered else bullet.group(3)
            while len(list_stack) > depth + 1:
                out.append(f"</{list_stack.pop()}>")
            if len(list_stack) == depth:
                out.append(f"<{tag}>")
                list_stack.append(tag)
            marker = bullet.group(2) if bullet else ""
            cls = ' class="correct"' if marker in _CORRECT_MARKS else ""
            out.append(f"<li{cls}>{_inline_html(text)}</li>")
            i += 1
            continue

        if not stripped:
            close_lists()
            i += 1
            continue

        close_lists()
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith(("#", "|", ">", "```", "![", "$$"))
                    or _RE_BULLET.match(lines[i]) or _RE_OL.match(lines[i])):
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{_inline_html(' '.join(para))}</p>")

    close_lists()
    return "\n".join(out)


def html_page(title: str, body_html: str, *, subtitle: str = "",
              mermaid_js: str | None = None) -> str:
    """包一层可打印的 HTML（A4 + 中文字体 + 可选离线 mermaid）。"""
    script = ""
    if mermaid_js:
        script = ("\n<script src=\"" + html_mod.escape(mermaid_js) + "\"></script>\n"
                  "<script>mermaid.initialize({startOnLoad:true,theme:'neutral',"
                  "fontFamily:'Noto Sans SC, 思源黑体, Microsoft YaHei, SimSun, sans-serif'"
                  "});</script>")
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\" />\n"
        f"<title>{html_mod.escape(title)}</title>\n<style>{PRINT_CSS}</style>\n</head>\n<body>\n"
        f"<h1>{html_mod.escape(title)}</h1>\n"
        + (f"<p class=\"course-head\">{html_mod.escape(subtitle)}</p>\n" if subtitle else "")
        + body_html + script + "\n</body>\n</html>\n")


# ---------------------------------------------------------------------------
# 讲义 → PDF（无头 Edge）
# ---------------------------------------------------------------------------

def _edge_profile_dir() -> Path:
    """Edge 的临时 profile 目录：必须落在系统临时目录（短 ASCII 路径）。

    实测（2026-09-15 任务 31 真机）：把 profile 放进交付包目录
    （``storage/deliveries/<中文课名：…>-<时间>/讲义/…``）时，无头 Edge 打印
    讲义 PDF 会**退出码 21、不产出文件**；profile 放到系统临时目录后 27 篇全部正常。
    """
    root = Path(tempfile.gettempdir()) / "coursegenie_edge"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"profile-{os.getpid()}-{uuid.uuid4().hex[:8]}"


_COMPAT_RANGES = ((0x2E80, 0x2FDF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F))
# 兼容区码位 → 正字。来源：SIL OFL 交付字体自己的 cmap —— Noto Sans SC / 思源黑体里
# 「U+2F06 ⼆」与「U+4E8C 二」共用同一个字形，Chromium 生成 PDF 的 ToUnicode 按字形反查
# Unicode ⇒ 挑到码位更小的兼容码位，客户搜「二 / 长 / 门」搜不到、复制出来是怪字。
# 每一条都能在字体文件上按「同一字形 + 兼容码位更小 + NFKC 修不了」三步重新推导
# （tests/test_delivery_font_ofl.py::test_compat_fix_table_matches_font_cmaps 会在门禁里复算）。
_COMPAT_FIX = {
    0x2E82: 0x4E5B, 0x2E83: 0x4E5A, 0x2E85: 0x4EBB, 0x2E87: 0x20628, 0x2E89: 0x5202,
    0x2E8B: 0x353E, 0x2E8D: 0x2D544, 0x2E8F: 0x5C23, 0x2E90: 0x5C22, 0x2E92: 0x5DF3,
    0x2E93: 0x5E7A, 0x2E94: 0x5F51, 0x2E95: 0x5F50, 0x2E96: 0x5FC4, 0x2E98: 0x624C,
    0x2E99: 0x6535, 0x2E9B: 0x65E1, 0x2E9E: 0x6B7A, 0x2EA0: 0x6C11, 0x2EA1: 0x6C35,
    0x2EA3: 0x706C, 0x2EA4: 0x722B, 0x2EA6: 0x4E2C, 0x2EA8: 0x72AD, 0x2EA9: 0x248E9,
    0x2EAD: 0x793B, 0x2EAE: 0x25AD7, 0x2EAF: 0x7CF9, 0x2EB0: 0x7E9F, 0x2EB1: 0x7F53,
    0x2EB3: 0x34C1, 0x2EB9: 0x8002, 0x2EBA: 0x8080, 0x2EBD: 0x26951, 0x2EBE: 0x8279,
    0x2EC1: 0x864E, 0x2EC2: 0x8864, 0x2EC5: 0x89C1, 0x2EC6: 0x89D2, 0x2EC7: 0x278B2,
    0x2EC8: 0x8BA0, 0x2EC9: 0x8D1D, 0x2ECA: 0x27FB7, 0x2ECB: 0x8F66, 0x2ECC: 0x8FB6,
    0x2ED0: 0x9485, 0x2ED1: 0x9577, 0x2ED2: 0x9578, 0x2ED3: 0x957F, 0x2ED4: 0x95E8,
    0x2ED5: 0x28E0F, 0x2ED6: 0x961D, 0x2ED8: 0x9752, 0x2ED9: 0x97E6, 0x2EDA: 0x9875,
    0x2EDB: 0x98CE, 0x2EDC: 0x98DE, 0x2EDD: 0x98DF, 0x2EDE: 0x2967F, 0x2EDF: 0x98E0,
    0x2EE0: 0x9963, 0x2EE1: 0x29810, 0x2EE2: 0x9A6C, 0x2EE3: 0x9AA8, 0x2EE4: 0x9B3C,
    0x2EE5: 0x9C7C, 0x2EE6: 0x9E1F, 0x2EE7: 0x5364, 0x2EE8: 0x9EA6, 0x2EE9: 0x9EC4,
    0x2EEA: 0x9EFE, 0x2EEB: 0x6589, 0x2EEC: 0x9F50, 0x2EEE: 0x9F7F, 0x2EEF: 0x7ADC,
    0x2EF0: 0x9F99, 0x2EF1: 0x9F9C, 0x2EF2: 0x4E80, 0xFA23: 0x27EAF,
}
#: 控制符码位 → 正字。思源黑体的 cmap 把 U+0000–U+001F（以及 U+00A0）与 U+0020 **映到同一个
#: 字形**（gid 1 —— Adobe 用这一手避免 .notdef），Chromium 的 ToUnicode 反查挑「码位更小的
#: 那个」⇒ 空格在文本层里被写成 U+0001：客户复制出来是控制符、带空格的短语也搜不到
#: （任务 96 真机实测：18 份 PDF 里 2135 处）。与 `_COMPAT_FIX` 同源同规矩 ——
#: tests/test_delivery_font_ofl.py::test_control_fix_table_matches_font_cmaps 会在门禁里复算。
_CONTROL_FIX = {cp: 0x0020 for cp in range(0x01, 0x20)}
#: 第三类：**异体字对**的反查错位（任务 98 DSH 实测）。
#: 机制：思源黑体里 `U+6236 戶 → gid 13880`、`U+6237 户 → gid 13881`（**两个不同字形** ——
#: 所以既不是"同字形碰撞"、也躲过了 NFKC 与 `_COMPAT_FIX`）；但 Chromium 生成 ToUnicode 时
#: 把 gid 13881（= 0x3639）反查写成了 **U+6236** ⇒ 客户在交付 PDF 里搜「用户」搜不到。
#: 实测证据（2026-09-25）：`AI-…-20260925-1932.zip` 的
#: `讲义/01-AI-幻觉初印象/1.1-AI-也会一本正经地胡说.pdf` 里 `ToUnicode` 有 `<3639> <6236>`，
#: 旗舰课 27 篇合计 130 处「户」→「戶」。
#: 首选修法是**源文本校正**（打印入口手上就有源 HTML：「本篇用到的码位」里只有 户、没有 戶
#: ⇒ 把 戶 改回 户）；这张表只是**拿不到源文本时**的兜底，值由实测 + 字体 cmap 复算
#: （tests/test_delivery_font_ofl.py::test_variant_fix_table_matches_font_cmaps 守门）。
_VARIANT_FIX = {0x6236: 0x6237}
#: 「源文本校正」只认这些**汉字区**：异体字反查错位实测只发生在汉字上，而把「同区近邻」放宽到
#: 标点 / 符号（General Punctuation、Halfwidth 等）会误伤 Chromium 自己画上去的列表圆点「•」
#: —— 它不在源文本里，却和源文本里的「—」同区 ⇒ 会被错改成 —。
_CJK_BLOCKS = ((0x2E80, 0x2EFF), (0x2F00, 0x2FDF), (0x3400, 0x4DBF),
               (0x4E00, 0x9FFF), (0xF900, 0xFAFF))
_CMAP_TOKEN = re.compile(r"<([0-9A-Fa-f]{2,})>")


def _is_compat(ch: str) -> bool:
    """是不是「兼容区码位」（与工具 `PDF码位审计.py` / 任务 48n 同一套范围）。"""
    cp = ord(ch)
    return any(low <= cp <= high for low, high in _COMPAT_RANGES)


def _block_of(cp: int) -> tuple[int, int] | None:
    """这个码位落在哪个「汉字区」（不在 `_CJK_BLOCKS` 里就 None）。"""
    for low, high in _CJK_BLOCKS:
        if low <= cp <= high:
            return low, high
    return None


def _nearest_in_source(cp: int, source_cps: set[int]) -> int | None:
    """源文本里与这个码位「同区」的最近邻（并列取码位小的，保证确定性）。

    「同区」= **同一个汉字区 + 同一个 256 码位窗口**。窗口收窄是刻意的：实测那一对
    （U+6236 / U+6237）只差 1，而"反查挑错"只会挑到相邻码位；窗口放大到整个汉字区
    （U+4E00–U+9FFF 有 2 万个码位）就可能把源文本里没出现过的**正当**汉字改到很远的邻居去。
    """
    block = _block_of(cp)
    if block is None:
        return None
    low, high = block
    best: int | None = None
    for candidate in source_cps:
        if candidate == cp or not low <= candidate <= high:
            continue
        if (candidate >> 8) != (cp >> 8):
            continue
        if best is None or (abs(candidate - cp), candidate) < (abs(best - cp), best):
            best = candidate
    return best


def _variant_to_canonical(ch: str, source_cps: set[int] | None) -> str:
    """第三类（异体字对反查错位）：**源文本 > 显式对照表 > 不动**。

    源文本校正（首选）：这个码位**不在**源文本里、而源文本里有一个同汉字区的近邻
    ⇒ 说明 Chromium 的反查挑错了码位，改回源文本里那个（`戶 U+6236` → `户 U+6237`）。
    这条规则天然覆盖将来在别的字体上冒出来的同类问题，不必逐字维护黑名单。
    """
    cp = ord(ch)
    if source_cps is not None:
        if cp in source_cps:
            return ch                       # 源文本里就是这个码位 ⇒ 不是错位，别碰
        # 交付字体里 U+2012 与 U+2013 共用一个字形，反查会挑码位更小的 U+2012；
        # 源文本只有 U+2013 时改回去（作者真写 U+2012 的正文不动）。
        if cp == 0x2012 and 0x2013 in source_cps:
            return "–"
        near = _nearest_in_source(cp, source_cps)
        if near is not None:
            return chr(near)
    target = _VARIANT_FIX.get(cp)
    return chr(target) if target else ch


def _to_canonical(ch: str, source_cps: set[int] | None = None) -> str:
    """「反查挑错的码位」→ 正字。

    ①②同源（字体里两个码位共用一个字形，Chromium 的 ToUnicode 反查挑了码位更小的那个）：
    ① 兼容区码位 —— 先试 NFKC（康熙部首那一大片），再查 `_COMPAT_FIX`；
    ② 控制符码位 —— 查 `_CONTROL_FIX`（思源黑体把 U+0001 当空格）。
    ③ 异体字对反查错位（任务 98）—— 两个字形**不同**、单纯是反查挑错了码位；优先用源文本校正
    （`source_cps` = 本篇源文本用到的码位集合），拿不到源文本时查 `_VARIANT_FIX`。
    **只有这三类才动**：普通字符一个字节都不许碰 —— 对全体字符跑 NFKC 会把「：」「（」这类
    全角标点换成半角、还会把「Ⅲ」这种字符拆成两个，等于偷偷改了客户的文本层。
    """
    if _is_compat(ch):
        return _compat_to_canonical(ch)
    target = _CONTROL_FIX.get(ord(ch))
    if target:
        return chr(target)
    return _variant_to_canonical(ch, source_cps)


def _compat_to_canonical(ch: str) -> str:
    """兼容区码位 → 正字：NFKC 能修的优先（康熙部首那一大片），余下查字体 cmap 推出的表。"""
    fixed = unicodedata.normalize("NFKC", ch)
    if fixed != ch:
        return fixed
    target = _COMPAT_FIX.get(ord(ch))
    return chr(target) if target else ch


def _fix_cmap_token(token: str, counter: list[int], source_cps: set[int] | None = None) -> str:
    """ToUnicode 里的一个**目标**码位（UTF-16BE 十六进制串）：反查挑错的才动，其余原样返回。"""
    try:
        text = bytes.fromhex(token).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return token
    mapped = [_to_canonical(ch, source_cps) for ch in text]
    if mapped == list(text):
        return token
    counter[0] += sum(1 for ch, new in zip(text, mapped) if ch != new)
    return "".join(mapped).encode("utf-16-be").hex().upper()


def repair_tounicode_cmap(text: str, source_cps: set[int] | None = None) -> tuple[str, int]:
    """一份 ToUnicode CMap → 归一到正字的新文本 + 改动码位数。

    只动**目标**码位：``beginbfchar`` 里每对 ``<src> <dst>`` 的第二个（奇数位），
    ``beginbfrange`` 里第 3 个起（``<lo> <hi> <dst>`` 或 ``<lo> <hi> [<d>…]``）。
    源码位是 CID、跟 Unicode 无关 —— 碰它就会串码（原型阶段实测踩过这个坑）。
    三类目标码位要修：① 兼容区码位（康熙部首 / 兼容汉字）；② 控制符（思源黑体把 U+0001 当空格）
    —— 这两类的病根是「字体里两个码位共用一个字形，Chromium 反查挑了更小的那个」；
    ③ 异体字对反查错位（任务 98：字形不同、纯粹反查错位，见 :data:`_VARIANT_FIX`）
    —— 有 ``source_cps``（本篇源文本的码位集合）时优先用源文本校正。
    """
    counter = [0]
    lines, mode = [], None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.endswith("beginbfchar"):
            mode = "char"
        elif stripped.endswith("beginbfrange"):
            mode = "range"
        elif stripped.endswith(("endbfchar", "endbfrange")):
            mode = None
        elif mode is not None and "<" in line:
            pieces, last = [], 0
            for index, match in enumerate(_CMAP_TOKEN.finditer(line)):
                is_target = index % 2 == 1 if mode == "char" else index >= 2
                if not is_target:
                    continue
                new = _fix_cmap_token(match.group(1), counter, source_cps)
                if new != match.group(1):
                    start, end = match.span(1)
                    pieces.append(line[last:start] + new)
                    last = end
            if pieces:
                pieces.append(line[last:])
                line = "".join(pieces)
        lines.append(line)
    return "".join(lines), counter[0]


def _repair_writer_tounicode(writer, source_cps: set[int] | None) -> tuple[set, int, int, list[str]]:
    """把这份 PDF 里所有字体的 ``/ToUnicode`` 目标码位归一到正字。

    返回 ``(流集合, 改动的流派数, 改动码位数, 失败原因)``。**单个字体流失败不连累其它**
    （出包要降级而不是中断）。
    """
    streams, changed, codepoints, problems = set(), 0, 0, []
    for page in writer.pages:
        resources = page.get("/Resources")
        fonts = resources.get("/Font") if resources is not None else None
        for font in (fonts or {}).values():
            ref = font.get_object().get("/ToUnicode")
            if ref is None:
                continue
            streams.add(getattr(ref, "idnum", None) or id(ref))
            try:
                stream = ref.get_object()
                new_text, count = repair_tounicode_cmap(stream.get_data().decode("latin1"), source_cps)
                if count:
                    stream.set_data(new_text.encode("latin1"))
                    changed += 1
                    codepoints += count
            except Exception as exc:  # noqa: BLE001 —— 单个字体流失败不连累其它
                problems.append(str(exc))
    return streams, changed, codepoints, problems


#: 文本层修复的**最多趟数**：Edge 写出来的 PDF 里有一批 ToUnicode CMap（Type3 那批小 CMap）
#: **要等 pypdf 把文件重写一遍之后才读得出来**（第一趟的 `stream.get_data()` 拿到的还是压缩态
#: 字节）。实测（任务 98）：同一份页面上第一趟改到 3 处、第二趟又改到 2 处（户→户），
#: 第三趟起 0 改动 ⇒ 两趟够。**干净文件第一趟就 0 改动、直接停 ⇒ 不写盘**（不让交付件无谓重写）。
_TEXT_LAYER_ROUNDS = 3


def repair_pdf_text_layer(pdf_path: Path, source_text: str | None = None) -> dict:
    """把一份 PDF 的 /ToUnicode 反查码位（兼容区 + 控制符 + 异体字对）归一到正字。

    只修文本层，字形 / 版面一个字节不动。

    **读→修→写，最多 `_TEXT_LAYER_ROUNDS` 趟**（某趟 0 改动即停；第一趟就干净 ⇒ 一个字节不写）：
    单趟会漏掉那批「要等文件重写一遍才读得出」的 CMap（见 `_TEXT_LAYER_ROUNDS` 的实测记录）。

    ``source_text``（任务 98）＝ 本篇**源文本**（打印入口手上的那份 HTML）：拿它算「本篇用到的
    码位集合」，异体字那一类就能按「源文本 > 显式对照表 > 不动」校正（见 `_variant_to_canonical`）。
    传 None 也行 —— 那一类退回显式对照表。

    失败不抛异常：返回 ``{"ok": False, "reason": …}``，PDF 保持调用前的原样 ⇒ 出包降级而不是中断。
    真打印回归（``tests/test_pdf_cjk_font.py`` 的非标准码位 == 0 断言）是这条修复的守门人。

    返回值里 **``unrepaired``（任务 141）** = 「这份 PDF 的文本层**没修成**」的机器可读判别：
    缺 pypdf / 读不动 / 回写失败都算硬失败，调用方要把它写进 ``degraded``。**单个字体流报错不算**
    （``problems`` ⇒ ``ok`` 为假，但按既有口径只在 ``reason`` 里带出、不进 ``degraded``）。
    临时文件 **无论成败都不留**（``finally`` 里删）—— 它曾跟着 zip 发给客户。
    """
    try:
        from pypdf import PdfWriter
    except ImportError as exc:  # pragma: no cover —— 依赖没装
        return {"ok": False, "reason": f"没有 pypdf：{exc}", "streams": 0, "changed": 0,
                "codepoints": 0, "rounds": 0, "unrepaired": True}
    source_cps = {ord(ch) for ch in source_text} if source_text else None
    streams, changed, codepoints, problems, rounds = set(), 0, 0, [], 0
    for _ in range(_TEXT_LAYER_ROUNDS):
        try:
            writer = PdfWriter(clone_from=str(pdf_path))
        except Exception as exc:  # noqa: BLE001 —— 读不动就原样交付
            return {"ok": False, "reason": f"读 PDF 失败：{exc}", "streams": len(streams),
                    "changed": changed, "codepoints": codepoints, "rounds": rounds,
                    "unrepaired": True}
        round_streams, round_changed, round_codepoints, round_problems = \
            _repair_writer_tounicode(writer, source_cps)
        streams |= round_streams
        problems.extend(round_problems)
        rounds += 1
        if not round_changed:
            break
        changed += round_changed
        codepoints += round_codepoints
        tmp_path = pdf_path.with_name(pdf_path.name + ".textfix")
        try:
            with tmp_path.open("wb") as handle:
                writer.write(handle)
            tmp_path.replace(pdf_path)
        except Exception as exc:  # noqa: BLE001 —— 回写失败：PDF 保持这一趟之前的原样
            return {"ok": False, "reason": f"回写 PDF 失败：{exc}", "streams": len(streams),
                    "changed": changed - round_changed, "codepoints": codepoints - round_codepoints,
                    "rounds": rounds, "unrepaired": True}
        finally:
            # 任务 141：临时文件**无论成败都不许留在交付目录里** —— 成功时它已被 replace 移走，
            # 失败时它还躺在原地；不删就会跟着 zip 发给客户（复测 140 在 AI 包上抓到 5 个）。
            tmp_path.unlink(missing_ok=True)
    return {"ok": not problems, "reason": "；".join(problems[:3]),
            "streams": len(streams), "changed": changed, "codepoints": codepoints, "rounds": rounds,
            "unrepaired": False}


def print_pdf(html_path: Path, pdf_path: Path, *, edge: str, budget_ms: int = 8000,
              source_text: str | None = None) -> dict:
    """用无头 Edge 把 HTML 打成 PDF；返回 ``{"ok":…, "reason":…}``（失败不抛异常）。"""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        pdf_path.unlink()
    profile = _edge_profile_dir()
    cmd = [
        edge, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        # 任务 48n：浏览器自己的页眉页脚（标题 / 日期 / file:// 路径 + 页码）用的是**我们管不到**的
        # 字体栈，实测每页都会带 1 个康熙部首码位（标题里的「一 / 用」），而 CSS 只能管文内文字；
        # 顺带去掉客户不该看到的本地路径（file:///C:/Users/wen/...）。文内文字已由 PRINT_CSS 锁定 CJK。
        "--no-pdf-header-footer",
        "--disable-extensions", "--disable-sync", f"--virtual-time-budget={budget_ms}",
        f"--user-data-dir={profile}", f"--print-to-pdf={pdf_path.resolve()}",
        html_path.resolve().as_uri(),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(profile, ignore_errors=True)
        return {"ok": False, "reason": f"调用 Edge 失败：{exc}"}
    shutil.rmtree(profile, ignore_errors=True)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        tail = detail[-1][:200] if detail else f"退出码 {proc.returncode}"
        return {"ok": False, "reason": f"Edge 没有产出 PDF（{tail}）"}
    with pdf_path.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            return {"ok": False, "reason": "Edge 产出的文件不是合法 PDF"}
    return {"ok": True, "reason": "",
            "text_layer": repair_pdf_text_layer(pdf_path, source_text=source_text)}


# ---------------------------------------------------------------------------
# 任务 96：交付 PDF **真内嵌** OFL 字体（只改「打印用的那份 HTML」）
# ---------------------------------------------------------------------------
#
# 任务 95 把字体族换成 OFL 之后，Chromium 打 PDF 时**嵌不进这批字体**，改用
# ``/Subtype /Type3`` 矢量轮廓画字 —— 字体名只出现在描述符 ``/FontName`` 里，
# **没有 ``/FontFile2`` 字体程序**，同一批 18 份 PDF 还从 5.7 MB 涨到 13.0 MB。
# 根因：本机 ``Noto Sans SC`` 是 Thin 实例（``wght`` 轴退化 100..100）、思源黑体只装了
# Regular 一个面 ⇒ 标题 / ``strong`` / ``th`` 全靠 Chromium **合成粗体**，而合成粗体
# 嵌不进去。解法：给打印件补两个**静态** OFL 面（400 / 700）并把字重钉死。

#: 打印用静态 SIL OFL 字体的两个面（``%WINDIR%\Fonts`` 下的思源黑体）。
PRINT_FONT_FILES = (("sourcehansans.ttf", 400), ("sourcehansansb.ttf", 700))
PRINT_FONT_FAMILY = "CGPrint"
#: 覆盖正文 / 标题的字体族与字重：字重**钉死** 400 / 700 是这一步的关键
#: （绝不让 Chromium 合成粗体）。等宽（代码块）与 ``math`` 显式排除，字体链保持任务 95 的样子。
_PRINT_FONT_OVERRIDE = """
html body, html body * { font-family: "@FAMILY@", "Noto Sans SC", "思源黑体", "Microsoft YaHei", "微软雅黑", "PingFang SC", "SimSun", sans-serif !important; }
html body * { font-weight: 400 !important; }
html body h1, html body h2, html body h3, html body h4, html body h5, html body h6,
html body strong, html body b, html body th { font-weight: 700 !important; }
html body pre, html body pre *, html body code, html body code * {
  font-family: "Cascadia Mono", Consolas, "Courier New", monospace !important; }
html body math, html body math * {
  font-family: "Cambria Math", "Latin Modern Math", serif !important; }
"""


def print_font_paths() -> list[tuple[Path, int]]:
    """打印用静态 OFL 字体（字重 400 / 700 各一个面）。"""
    fonts_dir = Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"
    return [(fonts_dir / name, weight) for name, weight in PRINT_FONT_FILES]


def print_font_style() -> str | None:
    """打印用 ``@font-face`` + 字体族 / 字重覆盖；两个字体面**缺任一** ⇒ ``None``。"""
    faces = print_font_paths()
    if not all(path.is_file() for path, _weight in faces):
        return None
    rules = "".join(
        f'@font-face{{font-family:"{PRINT_FONT_FAMILY}";font-weight:{weight};'
        f'src:url("{path.as_uri()}") format("truetype");}}\n' for path, weight in faces)
    return rules + _PRINT_FONT_OVERRIDE.replace("@FAMILY@", PRINT_FONT_FAMILY)


def inject_print_fonts(html_text: str) -> tuple[str, str]:
    """给**打印用**的 HTML 注入 OFL 字体覆盖，返回 ``(html, warn)``。

    交付给客户的 HTML 一个字节都不许动（本地 ``file:///`` 路径不能写给客户，也会破坏
    「交付 HTML sha256 不变」的判据）⇒ 只在临时打印件上调这个函数。
    缺字体文件 / 没有 ``</head>`` ⇒ 原样返回 + 一句 warn，**绝不让出包失败**。
    """
    style = print_font_style()
    if style is None:
        missing = "、".join(str(path) for path, _weight in print_font_paths())
        return html_text, (f"打印字体未内嵌（{missing} 缺文件）：PDF 退回系统字体，"
                           f"字形仍来自 OFL 字体但没有字体程序")
    if "</head>" not in html_text:
        return html_text, "打印字体未内嵌（文档里没有 </head>）：PDF 退回系统字体"
    return html_text.replace("</head>", f"<style>\n{style}</style>\n</head>", 1), ""


def print_document(html_text: str, pdf_path: Path, *, edge: str,
                   budget_ms: int = 8000, source_text: str | None = None) -> dict:
    """打一份「打印用」的 HTML：落在 PDF **同目录**的临时文件再交给 :func:`print_pdf`。

    必须同目录：讲义 HTML 里 ``../_assets/mermaid.min.js`` 是相对引用，换个目录就找不到
    （流程图会退化成代码块）。临时文件打印完即删，不进 manifest / 不进 zip。

    ``source_text``（任务 98）＝ **注入打印字体之前**的那份 HTML，原样交给
    :func:`print_pdf` 做文本层校正（注入的那段 CSS 不改变「本篇用了哪些码位」）。
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_html = pdf_path.with_name(f".print-{uuid.uuid4().hex[:8]}.html")
    try:
        tmp_html.write_text(html_text, encoding="utf-8", newline="")
        return print_pdf(tmp_html, pdf_path, edge=edge, budget_ms=budget_ms, source_text=source_text)
    finally:
        tmp_html.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 采集：这一门课有哪些东西可交付
# ---------------------------------------------------------------------------

def _figure_kinds_label(item: ContentItem) -> str:
    kind = str(item.kind or "")
    return {"diagram": "概念图/流程图"}.get(kind, f"数据图表（{kind.replace('chart_', '')}）")


def _figure_caption(item: ContentItem) -> str:
    body = dict(item.body or {})
    return str(body.get("caption") or body.get("title") or item.title or "图")


def _svg_source_of(item: ContentItem) -> Path | None:
    """图条目里 ``body.svg_preview`` 指向 storage 下的 SVG（服务端已渲染好）。"""
    rel = str((item.body or {}).get("svg_preview") or "").strip()
    if not rel:
        return None
    path = get_settings().storage_dir / rel
    return path if path.is_file() else None


def figure_rows(items: list[ContentItem]) -> list[dict]:
    """图类内容条目 → `deck_html.build_files(figures=...)` 的入参（**唯一一份口径**）。

    出交付包与网页端「网页版预览」（任务 94）都从这里取 ⇒ 同一门课同一个 deck，
    两边渲染出的 HTML 逐字节一致；调用方传的 `items` 必须是**按 `created_at` 升序**
    的那一份（出包与预览接口同序），否则同一节点下多张图的先后会不同。
    """
    return [{"node_key": str((item.meta or {}).get("node_key") or ""),
             "caption": _figure_caption(item), "kind": str(item.kind or ""),
             "svg": _svg_source_of(item),
             "table": str((item.body or {}).get("markdown_table") or "")}
            for item in items if item.kind in FIGURE_KINDS]


def chart_table_markdowns(items: list[ContentItem]) -> dict[str, str]:
    """数据卡 id → 它的 markdown 表格（任务 64 去重用）。

    与 ``resolve_image`` 取数口径一致：``chart_table`` 只要表格能解析（≥2 行）就会渲染
    成表格，因此都参与「正文已有同数据表格」的判定。
    """
    out: dict[str, str] = {}
    for item in items:
        if item.kind != "chart_table":
            continue
        table = str((item.body or {}).get("markdown_table") or "").strip()
        if len([line for line in table.splitlines() if line.strip()]) >= 2:
            out[item.id] = table
    return out


def _lecture_pairs(db: Session, course: Course) -> list[tuple[object | None, object, ContentItem]]:
    """[(章节点或 None, 知识点节点, 讲义条目)]，按章 / 节顺序。"""
    nodes = list_nodes(db, course.id)
    by_id = {node.id: node for node in nodes}
    lectures = (db.query(ContentItem)
                .filter(ContentItem.course_id == course.id, ContentItem.kind == "lecture")
                .order_by(ContentItem.created_at).all())
    chapter_of: dict[str, object] = {}
    for node in nodes:
        key = str(node.node_key or "")
        if "." not in key:
            chapter_of[node.id] = node
        else:
            parent = next((n for n in nodes
                           if str(n.node_key or "") == key.split(".")[0]), None)
            chapter_of[node.id] = parent
    pairs: list[tuple[object | None, object, ContentItem]] = []
    for item in lectures:
        node = by_id.get(item.node_id or "")
        if node is None:
            continue
        pairs.append((chapter_of.get(node.id), node, item))
    pairs.sort(key=lambda row: _key_order(str(getattr(row[1], "node_key", "") or "")))
    return pairs


def _delivery_lecture_markdown(item: ContentItem) -> str:
    """交付讲义正文 = 顶部「怎么读」图例 + 库里正文 + 页末「来源」小节（任务 57）。

    起因：交付包里 27 篇 ``讲义/*.md`` 全含 ``#N`` 来源标记，却**一篇都没有「## 来源」小节**
    （笔记库那份有）⇒ 客户拿着 PDF 无从解读「#33」。这里直接复用 ``kb._source_section``
    —— **不写第二套渲染**，两处口径才不会漂移。
    交付件是独立文件（不在 Obsidian vault 里），所以来源小节**只写素材名、不写 wiki 双链**。
    任务 48d：0 条引用时小节不再消失，渲染 ``empty_sources_note`` 那句说明
    （md / html / pdf 三处同源：html 由本文 markdown 渲染，pdf 由 html 打印）。
    """
    markdown = lecture_qc.lecture_markdown(item)
    sources = [row for row in ((item.meta or {}).get(lecture_qc.SOURCES_KEY) or [])
               if isinstance(row, dict)]
    parts = [f"> {kb_mod.READ_LEGEND}", "", markdown.strip()]
    section = "\n".join(kb_mod._source_section(sources, {}, markdown)).strip("\n")
    if section:
        parts.append(section)
    return "\n\n".join(part for part in parts if part).rstrip() + "\n"


def _key_order(key: str) -> tuple:
    parts = []
    for chunk in str(key or "").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def _chapter_dirs(pairs) -> dict[str, str]:
    """章 id → 目录名（``01-第1章-…``），保持讲义与笔记库同构。"""
    out: dict[str, str] = {}
    index = 0
    for chapter, _node, _item in pairs:
        cid = getattr(chapter, "id", "") or "00"
        if cid in out:
            continue
        index += 1
        title = getattr(chapter, "title", "") or "未分章"
        out[cid] = f"{index:02d}-{kb_mod._slug(title, 40)}"
    return out


def collect_plan(db: Session, course: Course) -> dict:
    """清单预览（不写盘）：将包含哪些文件、各自数量、哪些缺项及原因。"""
    nodes = list_nodes(db, course.id)
    items = db.query(ContentItem).filter(ContentItem.course_id == course.id).all()
    lectures = [i for i in items if i.kind == "lecture"]
    figures = [i for i in items if i.kind in FIGURE_KINDS]
    decks = [i for i in items if i.kind == "ppt"]
    quizzes = db.query(QuizItem).filter(QuizItem.course_id == course.id).count()
    materials = list_materials(db, course.id)

    missing: list[dict] = []
    if not lectures:
        missing.append({"item": f"{LECTURE_DIR}/", "reason": "这门课还没有讲义，先在网页端生成"})
        missing.append({"item": f"{LESSON_DIR}/",
                        "reason": "教案由讲义派生（0 LLM 骨架）：本课还没有讲义，先生成讲义再重新出包"})
        missing.append({"item": f"{SHEET_DIR}/",
                        "reason": "学案由讲义派生（0 LLM 学生版）：本课还没有讲义，先生成讲义再重新出包"})
    if not decks:
        missing.append({"item": f"{DECK_DIR}/", "reason": "还没有课件，先在「高级工具 → PPT」生成"})
    if not figures:
        missing.append({"item": f"{FIGURE_DIR}/", "reason": "还没有图（概念图 / 数据图表）"})
    if not materials:
        missing.append({"item": MATERIAL_NAME, "reason": "还没有导入素材，来源标注无从核对"})
    vault_root = kb_mod._configured_vault_root(course)
    notes_dir = None
    if vault_root is None:
        missing.append({"item": f"{NOTES_DIR}/", "reason": "课程未绑定笔记库（Vault）"})
    else:
        rel = (kb_mod.course_kb(course).get("output_rel")
               or f"课程/{kb_mod._slug(course.title, 40)}")
        notes_dir = Path(str(vault_root)).expanduser() / rel
        if not notes_dir.is_dir():
            missing.append({"item": f"{NOTES_DIR}/",
                            "reason": "笔记库里还没有导出过这门课，先在网页端「保存到 Obsidian 笔记」"})
    if not find_edge():
        missing.append({"item": "讲义 PDF", "reason": "本机没找到 Microsoft Edge，将降级为 HTML"})
    # 任务 87：按场景会**多出**哪些料（出包页逐条显示；默认八件套仍在最上）。
    # 判定与出包时同一个出处（`recording_kit` / `scenario`），界面不另写一份规则。
    kit_on = recording_kit.kit_enabled(course)
    kit_entries = recording_kit.collect_entries(db, course) if kit_on else None
    kit_stats = recording_kit.summary(kit_entries)
    extra_rows = [
        {"key": "recording_script", "label": f"{recording_kit.DIR_NAME}/{recording_kit.SCRIPT_DIR}/"
         "（每课件一篇逐字稿：拼现有演讲备注，不改写）",
         "present": kit_on, "scene": "录成视频课", "count": kit_stats["scripts"]},
        {"key": "recording_duration", "label": f"{recording_kit.DIR_NAME}/{recording_kit.DURATION_NAME}"
         "（逐小节秒数 + 6 分钟闸门）",
         "present": kit_on, "scene": "录成视频课", "count": 1 if kit_on else 0},
        {"key": "recording_subtitle", "label": f"{recording_kit.DIR_NAME}/{recording_kit.SRT_DIR}/"
         "（SRT 字幕：≤20 字符/秒、单行 ≤42）",
         "present": kit_on, "scene": "录成视频课", "count": kit_stats["subtitles"]},
        {"key": "recording_storyboard", "label": f"{recording_kit.DIR_NAME}/{recording_kit.STORYBOARD_NAME}"
         "（逐页「讲什么 / 画面是什么 / 板书要点」）",
         "present": kit_on, "scene": "录成视频课", "count": 1 if kit_on else 0},
        {"key": "recording_interaction", "label": f"{recording_kit.DIR_NAME}/{recording_kit.INTERACTION_NAME}"
         "（每节 1~2 条提问点）",
         "present": kit_on and recording_kit.wants_interactions(course),
         "scene": "线下课堂面授 / 线上直播", "count": 1 if kit_on and recording_kit.wants_interactions(course) else 0},
        {"key": "student_lectures", "label": f"{STUDENT_LECTURE_DIR}/（去答案与解析的学员版）",
         "present": "self_study" in (scenario.scenario_meta(course).get("selected") or []),
         "scene": "学生自学", "count": len(lectures)},
        {"key": "lms_scorm", "label": "SCORM 1.2 按课时打包（每课时一个 <item>，导出前自检）",
         "present": "lms" in (scenario.scenario_meta(course).get("selected") or []),
         "scene": "平台投放(SCORM)", "count": sum(1 for n in nodes if "." in str(n.node_key or ""))},
    ]
    return {
        "course_id": course.id,
        "course_title": course.title,
        "counts": {
            "chapters": sum(1 for n in nodes if "." not in str(n.node_key or "")),
            "points": sum(1 for n in nodes if "." in str(n.node_key or "")),
            "lectures": len(lectures),
            "lesson_plans": len(lectures),
            "student_sheets": len(lectures),
            "decks": len(decks),
            "figures": len(figures),
            "charts": sum(1 for i in figures if str(i.kind).startswith("chart_")),
            "diagrams": sum(1 for i in figures if i.kind == "diagram"),
            "quizzes": quizzes,
            "concepts": sum(1 for i in items if i.kind == "concept"),
            "materials": len(materials),
            "notes_files": (sum(1 for p in notes_dir.rglob("*") if p.is_file())
                            if notes_dir is not None and notes_dir.is_dir() else 0),
        },
        "items": [
            {"key": "readme", "label": README_NAME, "present": True},
            {"key": "accept", "label": ACCEPT_NAME, "present": True},
            {"key": "lectures", "label": f"{LECTURE_DIR}/（每节 PDF + md 源）", "present": bool(lectures)},
            {"key": "lesson_plans", "label": f"{LESSON_DIR}/（每课时 Word + PDF）",
             "present": bool(lectures)},
            {"key": "student_sheets", "label": f"{SHEET_DIR}/（学生版 Word + PDF）",
             "present": bool(lectures)},
            {"key": "decks",
             "label": f"{DECK_DIR}/（整课 + 各章 pptx）+ {DECK_DIR}/{deck_html.DIR_NAME}/"
                      "（网页版，可投影 / 打印 / 手机看）",
             "present": bool(decks)},
            {"key": "figures", "label": f"{FIGURE_DIR}/（svg + png）", "present": bool(figures)},
            {"key": "notes", "label": f"{NOTES_DIR}/（Obsidian 导出）", "present": notes_dir is not None and notes_dir.is_dir()},
            {"key": "materials", "label": MATERIAL_NAME, "present": bool(materials)},
            {"key": "manifest", "label": MANIFEST_NAME, "present": True},
        ],
        "extras": extra_rows,
        "recording_kit": {"enabled": kit_on, **kit_stats}
        if kit_on else {"enabled": False},
        "missing": missing,
        "pdf": {"available": bool(find_edge()), "engine": find_edge() or ""},
        "mermaid": {"available": bool(find_mermaid_js())},
        "delivery_mode": delivery_mode(course),
    }


# ---------------------------------------------------------------------------
# 渲染各份说明文件
# ---------------------------------------------------------------------------

def _cell(value: object) -> str:
    """客户可见文档里**一个值**的统一渲染（任务 143：防御性渲染）。

    `None` ⇒ `—`；`True` / `False` ⇒ `是` / `否`；其余 `str()` 原样。

    来历：142 修掉的那个 `pdf_ok` 被覆盖成布尔、验收单印出「PDF True 篇」——当时靠的是
    「取值恰好不是布尔」；`_md_table` 那 9 个裸单元格表达式还全在插值清单之外。这里把
    「不许印出 True / False / None」从**巧合**变成**写法**。
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _md_table(headers: list[str], rows: list[list[object]]) -> list[str]:
    """Markdown 表；单元格一律过 `_cell`（任务 143：`str(cell)` 这条路以前不在插值清单里）。"""
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return lines


def _public_summary(summary: str) -> str:
    """对外模式的结论句：抹掉「累计费用 ≈… 元」这一句（金额不写进客户包）。

    ``audit.course_report`` 的 summary 是给我们复盘用的，这里只在**展示层**收敛，
    不动 audit 的口径（内部模式照旧原样输出）。
    """
    return audit_mod.public_summary(summary)


def _readme_lines(db: Session, course: Course, plan: dict, stats: dict,
                  missing: list[dict], degraded: list[dict], build_id: str,
                  started_at: datetime | None = None,
                  fig_files: dict | None = None) -> list[str]:
    counts = plan["counts"]
    fig_files = fig_files or {}      # 任务 130：包内 `图表/` 的文件数（现算，见 _figure_file_counts）
    voice = str(plan.get("voice_tally") or "")
    sync = plan.get("notes_sync") or {}
    now = (started_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    external = delivery_mode(course)["external"]     # 任务 48p：对外模式不写用量与金额
    version_line = (f"- 交付日期：{now}　内容版本：{CONTENT_VERSION}　构建号：{build_id}"
                    if external else
                    f"- 交付日期：{now}　交付包版本：{BUILD_VERSION}　构建号：{build_id}")
    model_line = (f"- {EXTERNAL_USAGE_NOTE}" if external else
                  f"- 内容生成模型：{_cell(stats.get('model') or '（未记录）')}　"
                  f"课程累计 LLM 用量：{_cell(stats.get('llm_calls', 0))} 次 / "
                  f"{_cell(stats.get('total_tokens', 0))} tokens / ≈¥{_cell(stats.get('cost_cny', 0))}")
    lines = [
        "---",
        f'title: "{course.title} · 交付说明"',
        'tags: "coursegenie 交付包"',
        "---",
        "",
        f"# {course.title} · 交付说明",
        "",
        f"- 课程受众：{_cell(course.audience or '未填写')}",
        f"- 预计课时：{_cell(course.duration_minutes)} 分钟",
        f"- 规模：{_cell(counts['chapters'])} 章 / {_cell(counts['points'])} 个知识点 / "
        f"{_cell(counts['lectures'])} 篇讲义 / {_cell(counts['quizzes'])} 道随堂练习",
        # 任务 86：本包各篇的「生成口径」（每篇生成时记在 meta 里；老内容显示「通用(未标记)」）
        *([f"- 本包各篇的口径：{voice}"] if voice else []),
        version_line,
        f"- 构建前已同步笔记：{'是' if (sync.get('synced')) else '否'}"
        f"（不一致 {_cell(sync.get('inconsistent_after', '—'))} 项）　"
        f"数字快照时间：{_cell(sync.get('snapshot_label') or now)}",
        model_line,
        "",
        "## 1. 本包文件清单",
        "",
    ]
    pack_rows: list[list[object]] = [
        ["`00-交付说明.md`", "本文件（打开顺序与注意事项）", "1"],
        ["`01-验收单.md`", "逐项验收数字 + 客户意见空栏", "1"],
        ["`讲义/`", "每节讲义：PDF + 同名 markdown 源（+ 可直接打印的 HTML）；**教师版**"
                    "（含答案与解析）", counts["lectures"]],
        # 任务 86：只有勾选 / 场景含 self_study 时才多这一行（默认八件套结构没变）
        *([["`学员版讲义/`", "同上，但去掉了答案与解析、正确项不再标 ✓（学生可直接读）",
            counts.get("student_lectures", 0)]] if counts.get("student_lectures") else []),
        ["`教案/`", "每课时教案：Word（.docx，可直接改）+ PDF + markdown 源（0 LLM 骨架）",
         counts.get("lesson_plans", 0)],
        ["`学案/`", "每课时学案（学生版）：答案与解析已移除、关键处留空、带页码与作答区",
         counts.get("student_sheets", 0)],
        ["`课件/`", "整课课件 + 各章课件（.pptx，可直接放映/二次编辑）", counts["decks"]],
        # 任务 88：网页版课件（每个 deck 一份单文件 HTML；只在真有 HTML 时多这一行）
        *([["`课件/HTML/`", "网页版课件（单文件 HTML：可投影 / 打印 A4 横版 / 手机看；"
            "图内联、断网可打开）", counts.get("html_decks", 0)]]
           if counts.get("html_decks") else []),
        ["`图表/`", "概念图 / 数据图表：svg（矢量可编辑）+ png（无 Obsidian 也能看）；"
         "表格类数据卡以 Markdown 随包（数据已写进讲义正文的那几张并入正文，不再单独出文件）",
         f"{_cell(fig_files.get('files', 0))} 个文件"
         f"（svg {_cell(fig_files.get('svg', 0))} + png {_cell(fig_files.get('png', 0))}"
         f" + Markdown {_cell(fig_files.get('tables', 0) + fig_files.get('graph', 0))}"
         f"{_figure_others_part(fig_files)}）"
         f"；图条目 {_cell(counts['figures'])} 个"],
        ["`笔记库/`", "Obsidian 导出目录（含图表库、习题集、概念库）", counts["notes_files"]],
        # 任务 87：只有场景含 recorded 时才多这一行（默认八件套结构没变）
        *([["`录课包/`", "按场景「录成视频课」多出：逐字稿（拼现有演讲备注，不改写）+ 时长表"
            "（6 分钟闸门）+ 字幕 SRT + 分镜/板书表"
            + ("+ 课堂互动点" if counts.get("recording_interactions") else "")
            + f"（{_cell(counts.get('recording_decks', 0))} 个课件 / "
              f"{_cell(counts.get('recording_pages', 0))} 页 / "
              f"约 {_cell(counts.get('recording_seconds', 0))} 秒）",
            counts.get("recording_kit_files", 0)]] if counts.get("recording_kit_files") else []),
        ["`素材清单.md`", "本课依据的原始素材与来源标注口径", "1"],
        # 任务 132：客户不敲命令行 —— 双击一下就知道文件有没有坏（只读、不联网、不要管理员）
        ["`核对文件完整性.bat`", "双击即逐条重算每个文件的 sha1，与 `manifest.json` 对账"
         "（全一致 = 文件没被改动也没损坏；有提示就重新解压一次）", "1"],
        ["`manifest.json`", "包内每个文件的相对路径 + sha1 + 字节数 + 生成参数", "1"],
    ]
    lines += _md_table(["目录 / 文件", "内容", "数量"], pack_rows)
    lines += ["", "## 2. 怎么用（建议打开顺序）", "",
              "1. 先读 `01-验收单.md`：一分钟确认数量与质量数字（坏链、一致性、质检均分）。",
              "2. 再读 `00-交付说明.md`（本文件）与 `素材清单.md`：知道内容依据是什么。",
              "3. 备课：看 `讲义/`（PDF 可直接打印）；需要改字就改同名 `.md` 源，"
              "或在网页端编辑后重新导出（**网页端才是唯一编辑源**，改笔记库文件会在下次导出被覆盖）。",
              "4. 交教案：`教案/` 里每课时一份 Word + PDF（课题/目标/重难点/教学过程含时间分配/"
              "板书/作业/反思栏），是在 Word 里直接增删的那一份。",
              "5. 发学案：`学案/` 是学生版（已去答案与解析、关键处留空、带作答区与页码），"
              "直接打印发给学生；**答案版留在教师侧**（本包 `讲义/` 与 `笔记库/习题集/`）。",
              "6. 上课：用 `课件/` 里的 pptx（`00-整课课件.pptx` 是整门课，`第N章…pptx` 按章）。",
              # 任务 133：含糊的「放进 vault」不够 —— 写清三步 + `.excalidraw` 要插件（与
              # `笔记库/00-课程概览.md` 同一口径，那句「图怎么看」直接复用 kb 的唯一出处）
              # 任务 134：三步顺序与包内实际对齐（① 装 → ② 打开，含「先看概览」 → ③ 图怎么看）；
              # 按钮中英文名、插件路径、`.excalidrawlib` 说明同样取自 kb（不在本文件另写一遍）。
              # 任务 135：第 ③ 步原来退成裸的「；**图怎么看**：」（只有 ①②）⇒ 补成显式 ③，
              # 三步编号由 `tests/test_delivery.py::test_package_docs_pin_the_three_steps` 钉住。
              # 任务 139：第 7 条在 ①②③ **之前**插一小段「不想自己装？」（AI 助手可代劳 ＋ 别放它
              # 工作目录里的硬提醒）—— 与 `笔记库/00-课程概览.md` **逐字同一段**（同调 kb 的
              # `vault_ai_helper_note()`），由
              # `tests/test_delivery.py::test_package_docs_offer_the_ai_helper_shortcut_and_
              # the_workdir_warning` 钉住（140 起两处文本**直接逐字比对** ⇒ 只改一处就红）。
              "7. 看 `笔记库/`（Obsidian 版）：" + kb_mod.vault_ai_helper_note()
              + " ① 到官网 **obsidian.md**（搜「Obsidian」也能找到）"
              "装 **Obsidian**（免费，Windows / macOS 都有）；② 打开它 → 选「**"
              + kb_mod.VAULT_OPEN_FOLDER + "**」（英文界面里叫 `" + kb_mod.VAULT_OPEN_FOLDER_EN
              + "`）→ 指向解压出来的 `笔记库/` 这个文件夹（或把它整个拷进你已有的 vault），"
              "先看 `笔记库/00-课程概览.md`（带方括号的都能点着跳转）；③ **图怎么看**："
              + kb_mod.vault_plugin_note("笔记库/"),
              "8. 要替换某张图：用 `图表/svg/` 里的矢量图（Illustrator / Inkscape / Figma 都能开）。",
              *(["9. 录课：`录课包/逐字稿/` 每个课件一篇（按页序拼装演讲备注、**未改写**），"
                 "`时长表.md` 逐小节给秒数（>360 秒的行带 ⚠️ 切分建议），`字幕/` 是可直接上传的 SRT，"
                 "`分镜表.md` 逐页写「讲什么 / 画面是什么 / 板书要点」"
                 + ("；`互动点.md` 是课堂/直播可用的提问点。" if counts.get("recording_interactions")
                    else "。")] if counts.get("recording_kit_files") else []),
              "",
              "## 3. 注意事项", "",
              f"- **AI 生成内容需人工核对**：{EXTERNAL_USAGE_NOTE}",
              "  讲义里的 `（来源:《素材名》第 N 段）` 表示该句依据**文本素材**第 N 段"
              "（可在 `笔记库/素材库/` 对应素材页按 `[N]` 定位）；"
              "音视频素材写 `（来源:《录像名》06:47）`（时间戳，按素材页段首的 `[mm:ss]` 定位），"
              "`（无直接依据,建议人工确认）` 表示没有找到素材依据、由模型补充，上课前请人工确认。",
              "  引用 AI 补充素材的句子写成 `（来源:AI补充——《素材名》第 N 段）`。",
              "- **「来源」的含义**：`素材清单.md` 里的「来源」指**导入本课的素材**"
              "（原件保持原样）；**若素材本身是 AI 生成的**（粘贴的对话稿、模型产出的知识库等），"
              "清单会如实标注「来源类型」—— 它**不等同于人工原始资料**。",
              "- **网页端是唯一编辑源**：`笔记库/` 与 `讲义/` 都是从网页端导出的产物，直接改这些文件会在下次导出被覆盖；",
              "- **交付包里的文件请勿单独删除**：`manifest.json` 记录了每个文件的 sha1，缺文件会导致校验失败；",
              # 任务 143：把「`~` 只删像编辑器备份的」写进**客户看得到**的说明（141 定的「不许静默」）
              "- **打包前会清掉出包过程的临时件**：临时文件与临时目录（`.textfix` / `.tmp` / `.bak` 等后缀）"
              "以及**编辑器备份**（形如 `讲义.md~` —— 文件名 = 已交付文件 + `~`）一律清理、不进包，"
              "清理记录写在 `01-验收单.md` 的「2.2 降级项」里；**其它以 `~` 结尾的文件**"
              "（例如你自己加的 `补充说明~`）**原样保留**；",
              # 任务 132：客户不敲命令行 —— 双击一下就知道文件有没有坏
              "- **想确认文件有没有在传输中损坏**：**双击**包内的 `核对文件完整性.bat`"
              "（它和 `manifest.json` 放在同一层即可）—— 它会把每个文件重算一遍 sha1、"
              "和清单逐条对账，几秒给结论：全一致就是干净的；有提示就重新解压一次，"
              "仍不对就把那段拍给制作方。",
              "  **Mac / Linux 用户**：本包只带 Windows 双击核对（`.bat`），没附 `.sh`；"
              "在「终端」里 `cd` 到解压出来的目录，用系统自带的 python3 一行核对"
              "（判定逻辑与 `.bat` 相同，同一段代码已在 python 3.11 上实测）："
              "`python3 -c \"import json,hashlib,os;m=json.load(open('manifest.json',encoding='utf-8'));"
              "bad=[r['path'] for r in m['files'] if not os.path.isfile(r['path']) "
              "or hashlib.sha1(open(r['path'],'rb').read()).hexdigest()!=r['sha1']];"
              "print('全部一致' if not bad else '有 %d 个对不上: %s' % (len(bad), bad[:5]))\"`"
              "（没装 python3 就先装 Xcode 命令行工具）。",
              *(["- **教师版 / 学员版**：`讲义/` 是教师版（含答案与解析）；`学员版讲义/` "
                 "是同一批讲义的学员版（去答案与解析、正确项不再标 ✓，题目与作答区保留）——"
                 "发学生的是后者。"] if counts.get("student_lectures") else []),
              "- 各章习题在 `笔记库/习题集/`；概念库与知识图谱在 `笔记库/概念库/`。",
              ""]
    if missing or degraded:
        lines += ["## 4. 本次未生成 / 降级项（原因写在这里，不是静默少文件）", ""]
        lines += [f"- **{_cell(row['item'])}**：{_cell(row['reason'])}" for row in missing + degraded]
    else:
        lines += ["## 4. 本次未生成 / 降级项", "",
                  ("- 无：八件套齐全；另按场景多出了 `录课包/`（见第 1 节清单）。"
                   if counts.get("recording_kit_files") else "- 无：八件套齐全。")]
    lines += ["",
              "> 本交付包由 CourseGenie 打包生成：**0 次 LLM 调用**（纯本地渲染与打包，可重复生成）。",
              "> `manifest.json` 自身不在文件清单内（哈希无法自引用），其余每个文件都有 sha1 与字节数。",
              ""]
    return lines


def _acceptance_lines(db: Session, course: Course, report: dict, plan: dict,
                      stats: dict, fig_files: dict | None = None,
                      degraded: list[dict] | None = None) -> list[str]:
    fig_files = fig_files or {}      # 任务 130：包内 `图表/` 的文件数（现算）
    degraded = degraded or []        # 任务 142：降级项也要印在验收单上（客户往往先看验收单）
    cover = report.get("coverage") or {}
    links = report.get("links") or {}
    consistency = report.get("consistency") or {}
    quality = report.get("quality") or {}
    export_health = report.get("export_health") or {}
    conclusion = report.get("conclusion") or {}
    external = delivery_mode(course)["external"]     # 任务 48p
    summary = (_public_summary(str(report.get("summary") or "")) if external
               else str(report.get("summary") or ""))
    lines = [
        "---",
        f'title: "{course.title} · 验收单"',
        'tags: "coursegenie 交付验收"',
        "---",
        "",
        f"# {course.title} · 交付验收单",
        "",
        "> 数字全部取自 CourseGenie 真实模块（`audit.course_report` / `kb.verify_course_notes` / "
        "`lecture_qc.course_report`），与网页端「质量报告 · 课程体检报告」逐项一致，未做人工调整。",
        f"> 质检分数一律**按现行判据现算**（`lecture_qc.rules_label()`：{lecture_qc.rules_label()}），"
        "打包前已把旧口径的质检重算并落库，不沿用任何过期缓存。",
        "",
        "## 1. 内容数量",
        "",
    ]
    lines += _md_table(["项", "数量", "说明"], [
        ["章", cover.get("chapters", 0), "大纲顶层章节"],
        ["知识点", cover.get("points", 0), "含小节"],
        ["讲义", cover.get("lectures", 0),
         f"覆盖 {_cell(round(float(cover.get('lecture_coverage') or 0) * 100))}% 的知识点"],
        ["教案", plan["counts"].get("lesson_plans", 0),
         f"Word {_cell(plan['counts'].get('lesson_docx_ok', 0))} 份 / PDF "
         f"{_cell(plan['counts'].get('lesson_pdf_ok', 0))} 份（0 LLM 骨架，反思栏留空）"],
        ["学案（学生版）", plan["counts"].get("student_sheets", 0),
         f"Word {_cell(plan['counts'].get('sheet_docx_ok', 0))} 份 / PDF "
         f"{_cell(plan['counts'].get('sheet_pdf_ok', 0))} 份（答案与解析已移除）"],
        # 任务 86：本包若含学员版讲义，验收单也列出篇数（教师版仍在 讲义/）
        *([["学员版讲义", plan["counts"].get("student_lectures", 0),
            "去答案与解析的学员版（教师版仍在 `讲义/`）"]]
          if plan["counts"].get("student_lectures") else []),
        ["随堂练习", cover.get("quizzes", 0), "含难度与解析"],
        ["课件", cover.get("ppt", 0), "整课 + 各章"],
        ["概念", cover.get("concepts", 0), "概念库条目"],
        ["概念图 / 流程图（条目）", f"{_cell(cover.get('diagrams', 0))} 个（条目）",
         "条目口径＝有几个知识点配了概念图 / 流程图；与包内 `图表/` 的文件数不是一个口径"],
        ["数据图表（条目）", f"{_cell(cover.get('charts', 0))} 个（条目）",
         "柱状/折线/表格/思维导图/时间线；条目口径，同上"],
        ["图表库文件", f"{_cell(export_health.get('charts', 0))} 个文件",
         "笔记库 `图表库/` 下的文件数（笔记库内部口径，与本包 `图表/` 的文件数不同）"],
    ])
    lines += ["", "## 2. 质量与一致性", ""]
    lines += _md_table(["检查项", "结果", "判定"], [
        ["课程体检总分",
         f"{_cell(conclusion.get('score', '—'))} / 100（{_cell(conclusion.get('level', '—'))}）",
         "≥80 良好" if int(conclusion.get("score") or 0) >= 80 else "需关注"],
        ["坏链", links.get("broken", "—"), "必须为 0"],
        ["网页端 ↔ 笔记库一致性",
         f"检查 {_cell(consistency.get('checked', 0))} 项，"
         f"不一致 {_cell(consistency.get('inconsistent', '—'))}",
         "必须为 0 不一致"],
        ["质检平均分", quality.get("average_score", 0),
         f"按现行判据现算（{_cell(quality.get('rules') or lecture_qc.rules_label())}）"
         f"· 共 {_cell(quality.get('checked', 0))} 篇"],
        ["讲义质量最低分", quality.get("min_score", 0), "低于 70 建议先修"],
    ])
    sync = plan.get("notes_sync") or {}
    refresh = plan.get("qc_refresh") or {}
    lines += ["",
              "## 2.1 数字快照（本单数字的时效）",
              "",
              f"- 构建前已同步笔记：{'是' if sync.get('synced') else '否'}"
              f"（不一致 {_cell(sync.get('inconsistent_after', '—'))} 项 / "
              f"检查 {_cell(sync.get('checked', 0))} 项；"
              f"同步前不一致 {_cell(sync.get('inconsistent_before', '—'))} 项）",
              f"- 质检口径：按现行判据现算（{_cell(quality.get('rules') or lecture_qc.rules_label())}）；"
              f"构建前复检讲义 {_cell(refresh.get('lectures', 0))} 篇 —— "
              f"旧口径重算 {_cell(refresh.get('refreshed', 0))} 篇、"
              f"已是最新 {_cell(refresh.get('current', 0))} 篇、"
              f"失败 {_cell(refresh.get('failed', 0))} 篇",
              f"- 数字快照时间：{_cell(sync.get('snapshot_label') or '—')}（构建时快照）",
              "- 若之后重生成内容，请重新出包：本单数字不会自动更新。",
              "",
              f"- 结论：{_cell(summary)}",
              f"- 课程体检报告：网页端「高级工具 → 质量报告」，或笔记库 `00-课程体检报告.md`",
              "",
              "## 2.2 降级项（本次交付里凡是打了折的都写在这里）", ""]
    # 任务 142：客户往往先看验收单，而文本层降级原先只写在 `00-交付说明.md` §4
    # （复测实测验收单里含「降级」0 行）⇒ 这里逐条点名：有几条、分别是什么；没有就明说无。
    if degraded:
        lines += [f"- 本次共 **{_cell(len(degraded))}** 条降级项（都只是打了折，其余内容照常交付）："]
        lines += [f"- **{_cell(row.get('item', ''))}**：{_cell(row.get('reason', ''))}"
                  for row in degraded]
    else:
        lines += ["- 本次无降级项：所有内容都按现行口径完整生成，没有使用任何降级方案。"]
    lines += ["", "> 同一份清单也写在 `00-交付说明.md` 第 4 节；这里逐条列出，客户不必翻两份文件。",
              "",
              "## 3. 交付清单核对", ""]
    # 任务 130：「图表」按**包内实际写出的文件数**现算（客户拿这个数去 `图表/` 里数，数得对）；
    # 条目数降为辅口径（「有几个知识点配了图」仍然有用，不删）。
    # 任务 131：总数取**整棵子树全量**、分项之外补「其他」桶 ⇒ 括号里的分项相加恒等于总数。
    fig_parts = [f"svg {_cell(fig_files.get('svg', 0))}", f"png {_cell(fig_files.get('png', 0))}"]
    if fig_files.get("tables"):
        fig_parts.append(f"表格类数据卡 Markdown {_cell(fig_files['tables'])}")
    if fig_files.get("graph"):
        fig_parts.append(f"概念知识图谱 Markdown {_cell(fig_files['graph'])}")
    figure_row = (f"图表：{_cell(fig_files.get('files', 0))} 个文件"
                  f"（{' + '.join(fig_parts)}{_figure_others_part(fig_files)}）"
                  f"；对应图条目 {_cell(plan['counts']['figures'])} 个"
                  f"（概念图/流程图 {_cell(plan['counts'].get('diagrams', 0))}"
                  f" + 数据图表 {_cell(plan['counts'].get('charts', 0))}）")
    lines += _md_table(["项", "是否包含", "备注"], [
        ["交付说明", "☑", "00-交付说明.md"],
        ["验收单", "☑", "本文件"],
        # 任务 142：这个数以前是**布尔值**（`pdf_ok` 被教案/学案解包覆盖）⇒ 印出「PDF True 篇」。
        # 现在它是**讲义 PDF 的真篇数**，客户可以在包内 `讲义/` 里数出同一个数。
        [f"讲义（{_cell(plan['counts']['lectures'])} 篇）", "☑" if plan["counts"]["lectures"] else "☐",
         f"PDF + markdown 源；本次生成 PDF {_cell(plan['counts'].get('pdf_ok', 0))} 篇"
         f"（= 包内 `{LECTURE_DIR}/` 下的 PDF 份数，与 `manifest.json` 的 `counts.pdf_ok` 同一个数）"],
        [f"课件（{_cell(plan['counts']['decks'])} 份）", "☑" if plan["counts"]["decks"] else "☐",
         "整课 + 各章 pptx" + (f"；另附网页版 HTML {_cell(plan['counts']['html_decks'])} 份"
                               "（`课件/HTML/`，可投影 / 打印 / 手机看）"
                               if plan["counts"].get("html_decks") else "")],
        [f"教案（{_cell(plan['counts'].get('lesson_plans', 0))} 份）",
         "☑" if plan["counts"].get("lesson_plans") else "☐",
         f"Word {_cell(plan['counts'].get('lesson_docx_ok', 0))} 份 + PDF "
         f"{_cell(plan['counts'].get('lesson_pdf_ok', 0))} 份；由大纲三要素与讲义派生"],
        [f"学案（{_cell(plan['counts'].get('student_sheets', 0))} 份）",
         "☑" if plan["counts"].get("student_sheets") else "☐",
         f"学生版：Word {_cell(plan['counts'].get('sheet_docx_ok', 0))} 份 + PDF "
         f"{_cell(plan['counts'].get('sheet_pdf_ok', 0))} 份；已剔除答案与解析"],
        [figure_row, "☑" if fig_files.get("files") else "☐",
         "打开包内 `图表/` 数这些文件即对得上：svg（矢量可编辑）+ png（图片）"
         "+ Markdown（表格类数据卡 / 概念知识图谱）；数据已写进讲义正文的表格并入正文，不再单独出文件"],
        ["笔记库（Obsidian）", "☑" if plan["counts"]["notes_files"] else "☐",
         f"{_cell(plan['counts']['notes_files'])} 个文件"],
        ["素材清单", "☑", "素材清单.md"],
        ["清单文件", "☑", "manifest.json（含 sha1）"],
        # 任务 132：客户不敲命令行 —— 双击即核对（Mac 的替代办法写在 00-交付说明.md §3）
        ["核对文件完整性.bat", "☑", "双击即逐条重算每个文件的 sha1 并与 manifest.json 对账；"
         "全一致退出码 0、有对不上或缺文件退出码非 0（现在就能接自动化）"],
    ])
    # 任务 67：承诺口径（自动化工具辅助生产 + 交付前逐篇经人工复核确认）两种模式都要写；
    # 它不是成本信息，对外模式那句复用 EXTERNAL_USAGE_NOTE —— 与交付说明 / 体检报告同一条正本。
    lines += ["",
              "## 4. 客户意见", "",
              "> 请把修改意见写在这里（或直接回给课程制作方），我们会按意见回到网页端修改并重新出包。",
              "",
              "- 意见 1：",
              "- 意见 2：",
              "- 意见 3：",
              "",
              f"- 验收人：____________　日期：____________",
              "",
              (f"> 本交付包由 CourseGenie 打包生成（构建号 {_cell(plan.get('build_id', ''))}）："
               f"打包为纯本地渲染与复制、可重复执行；{EXTERNAL_USAGE_NOTE}"
               if external else
               f"> 本包构建号 {_cell(plan.get('build_id', ''))}；打包本身 0 次 LLM 调用"
               f"（课程内容的既有用量：{_cell(stats.get('llm_calls', 0))} 次 / "
               f"{_cell(stats.get('total_tokens', 0))} tokens / ≈¥{_cell(stats.get('cost_cny', 0))}）。"
               f"{EXTERNAL_USAGE_NOTE}"),
              ""]
    return lines


def _material_lines(db: Session, course: Course, materials: list) -> list[str]:
    lines = ["---", f'title: "{course.title} · 素材清单"', 'tags: "coursegenie 素材"', "---", "",
             f"# {course.title} · 素材清单", "",
             "> 本课内容依据下列原始素材生成；讲义里的 `（来源:《素材名》第 N 段）` 指向**文本素材**的"
             "段落序号（素材页每段前有 `[N]`，按它定位）；音视频素材写 `（来源:《录像名》06:47）`"
             "（时间戳，素材页每段行首有 `[mm:ss]`，按它定位）。",
             "> **「来源」= 导入本课的素材**，不等同于人工原始资料：如果素材本身是 AI 生成的"
             "（粘贴的对话稿、模型产出的知识库等），下表「来源类型」会如实标注。",
             ""]
    if not materials:
        lines += ["- 本次未生成：这门课没有导入任何素材（讲义里的依据标注无法回溯）。", ""]
        return lines
    rows = []
    for index, row in enumerate(materials, 1):
        meta = dict(row.meta or {})
        pages = pages_of(row)
        grounding = grounding_of(row)
        rows.append([
            index,
            f"`{_cell(meta.get('original') or row.title)}`",
            row.kind,
            origin_label(row),
            f"{_cell(len(row.text or ''))} 字",
            f"{pages} 页" if pages else "—",
            meta.get("ext") or "",
            (f"有依据 {_cell(grounding['grounded_count'])} / "
             f"无依据 {_cell(grounding['ungrounded_count'])}"
             if origin_of(row) == ORIGIN_AI else "—"),
        ])
    lines += _md_table(["#", "文件", "类型", "来源类型", "解析字数", "页数", "格式", "AI 补充依据"],
                       rows)
    total_chars = sum(len(row.text or "") for row in materials)
    lines += ["",
              f"- 合计 {_cell(len(materials))} 个素材 / 解析 {_cell(total_chars)} 字。",
              "- 解析字数 = 抽取出的正文长度（PPT/PDF 只计文字，不含图片里的文字）。",
              "- 来源类型：`人工上传`（原件保持原样，页眉页脚**只提示、不强制清理**）/ "
              "`粘贴文本(疑似 AI 产物)` / `AI 补充`（后两类入库时自动剥离对话外壳，"
              "并在素材页提示「已清理 N 行」）。",
              "- **AI 补充依据**：那份 AI 补充素材里「给了来处」与「模型自己都说没来处」"
              "（`(无依据·模型补充)`）的条数 —— 无依据的表述不能当事实依据。",
              "",
              "## 来源标注口径", "",
              "- `（来源:《素材名》第 N 段）`：该句依据该**文本素材**的第 N 段（素材页按 `[N]` 定位）；",
              "- `（来源:《录像名》06:47）`：该句依据该**音视频素材**的 06:47 处"
              "（素材页每段行首有 `[mm:ss]`，按它定位）；",
              "- `（来源:AI补充——《素材名》第 N 段）`：这条依据**本身就是模型生成的补充参考**"
              "（不是人工原始资料），上课前请人工确认；",
              "- `（无直接依据,建议人工确认）`：没有找到素材依据、由模型补充，**上课前请人工确认**；",
              "- 没有任何标注的关键点会在质检里被标成「来源标注」问题（网页端「质量报告」可看到具体句子）。",
              ""]
    return lines


def _usage_model_label() -> str:
    settings = get_settings()
    provider = str(getattr(settings, "llm_provider", "") or "mock")
    model = {"openrouter": getattr(settings, "openrouter_model", ""),
             "openai": getattr(settings, "openai_model", ""),
             "anthropic": getattr(settings, "anthropic_model", ""),
             "mock": "mock（本地假模型）"}.get(provider, "")
    return f"{provider} / {model}" if model else provider


def _copy_notes_tree(course: Course, out_dir: Path, files: list[dict],
                     db: Session | None = None) -> int:
    """只读复制笔记库里的课程目录（不建目录、不写 vault）。

    任务 61：对外交付时，**包内那份** ``00-课程体检报告.md`` 用 ``external=True`` 重渲染
    （不出现金额 / tokens / LLM 调用次数 / 逐篇成本）；真 vault 里那份一个字不动，
    打包也不会往 vault 写任何东西（重渲染的产物只落在 ``out_dir``）。
    """
    vault_root = kb_mod._configured_vault_root(course)
    if vault_root is None:
        return 0
    rel = kb_mod.course_kb(course).get("output_rel") or f"课程/{kb_mod._slug(course.title, 40)}"
    src = Path(str(vault_root)).expanduser() / rel
    if not src.is_dir():
        return 0
    external = delivery_mode(course)["external"]
    count = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        arc = f"{NOTES_DIR}/{path.relative_to(src).as_posix()}"
        if external and db is not None and path.name == audit_mod.REPORT_NAME:
            # 任务 61：只换包内副本的口径，不动 vault 里的原文件（0 LLM，纯本地重渲染）
            report = audit_mod.course_report(db, course, external=True,
                                             extra_link_targets=(audit_mod.REPORT_NAME,))
            # 与 kb._write_safe 同口径盖章（正文指纹进 frontmatter），包内那份除成本段外
            # 与 vault 那份同形；指纹按正文算，因此两版报告只有成本相关行 + 指纹行不同。
            files.append(_write_text(out_dir, arc, kb_mod.stamp_output(arc, report["markdown"])))
        else:
            files.append(_copy_file(out_dir, arc, path))
        count += 1
    return count


def _vault_snapshot(course: Course) -> dict:
    """构建前只读快照笔记库课程目录：记录已存在目录与 vault 根，供构建后回收被顺带新建的空目录。"""
    vault_root = kb_mod._configured_vault_root(course)
    if vault_root is None:
        return {"dir": None, "existing": set(), "root": None}
    root = Path(str(vault_root)).expanduser()
    rel = kb_mod.course_kb(course).get("output_rel") or f"课程/{kb_mod._slug(course.title, 40)}"
    base = root / rel
    existing = {base} if base.is_dir() else set()
    if base.is_dir():
        existing |= {path for path in base.rglob("*") if path.is_dir()}
    return {"dir": base, "existing": existing, "root": root}


def _restore_vault(snapshot: dict) -> list[str]:
    """回收构建过程中被顺带创建的空目录（只 rmdir 本次新建且为空的目录，绝不删文件）。

    体检读取（`kb._course_out_dir`）会 mkdir 课程目录，这里在出包前把它恢复原样：
    只删「构建前不存在 + 现在仍为空」的目录，vault 根及以上一律不碰。
    """
    base = snapshot.get("dir")
    if base is None or not base.is_dir():
        return []
    candidates = [path for path in base.rglob("*") if path.is_dir()] + [base]
    removed: list[str] = []
    for path in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        if path in snapshot["existing"]:
            continue
        try:
            path.rmdir()
        except OSError:
            continue
        removed.append(str(path))
    root = snapshot.get("root")
    parent = base.parent
    while root is not None and parent != root and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        removed.append(str(parent))
        parent = parent.parent
    return removed


def _figure_others_part(fig_files: dict) -> str:
    """「其他」桶（任务 131）：总数 − 已知分项之和 ≠ 0 时**显式写出**，不静默吞掉。"""
    others = int(fig_files.get("others") or 0)
    return f" + 其他 {others}" if others else ""


def _figure_file_counts(out_dir: Path) -> dict:
    """``图表/`` 下**实际写出**的文件数（整棵子树，任务 130 / 131）。

    交付说明 / 验收单里的「图表」数字一律由它**现算** —— 不再手写常数（客户拿这个数
    去包内 ``图表/`` 里数，数得对）。条目数（``plan["counts"]["figures"]``）只作辅口径。

    任务 131：``files`` 改成**整棵子树的全量文件数**（不再只加白名单桶 —— 将来 ``图表/``
    下多出别的文件类型也不会少算）；``others`` = 总数 − 已知分项之和 ⇒「分项之和 == 总数」
    恒成立（客户把括号里的加一遍，必须等于前面的总数）。目录不存在 / 空目录一律全 0。
    """
    root = out_dir / FIGURE_DIR
    if not root.is_dir():
        return {"svg": 0, "png": 0, "tables": 0, "graph": 0, "others": 0, "files": 0}

    def _count(sub_name: str, pattern: str) -> int:
        folder = root / sub_name if sub_name else root
        if not folder.is_dir():
            return 0
        return sum(1 for p in folder.glob(pattern) if p.is_file())

    svg = _count("svg", "*.svg")
    png = _count("png", "*.png")
    tables = _count("表格", "*.md")      # 表格类数据卡（Markdown 随包）
    graph = _count("", "*.md")           # 顶层：概念知识图谱（Mermaid）
    files = sum(1 for p in root.rglob("*") if p.is_file())   # 整棵子树：客户能数到的全量
    return {"svg": svg, "png": png, "tables": tables, "graph": graph,
            "others": files - (svg + png + tables + graph), "files": files}


def _figures_section(db: Session, course: Course, items: list[ContentItem],
                     out_dir: Path, files: list[dict], degraded: list[dict],
                     merged_chart_ids: set[str] | None = None) -> dict:
    """图表：概念图 / 数据图表 → svg（复制现成矢量）+ png（Pillow 重绘）。

    ``merged_chart_ids``（任务 66）：数据已经写进所属讲义正文表格的 ``chart_table`` ——
    渲染时已跳过（任务 64），``图表/表格/*.md`` 降级件也不再单独出：同一组数据在包里
    只留正文那一份，说明与 manifest 里如实写明「已并入正文表格」（不是静默少文件）。
    """
    from app.services.figure_png import render_figure

    figures = [item for item in items if item.kind in FIGURE_KINDS]
    seq: dict[str, int] = {}
    svg_ok = png_ok = 0
    for item in figures:
        node_key = str((item.meta or {}).get("node_key") or "00")
        seq[node_key] = seq.get(node_key, 0) + 1
        stamp = f"{node_key}-{seq[node_key]:02d}"
        caption = kb_mod._slug(_figure_caption(item), 30)
        base = f"{stamp}-{caption}"
        svg_src = _svg_source_of(item)
        if svg_src is not None:
            files.append(_copy_file(out_dir, f"{FIGURE_DIR}/svg/{base}.svg", svg_src))
            svg_ok += 1
        target = out_dir / FIGURE_DIR / "png" / f"{base}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered, problem = render_figure(item, target)
        # 重绘会在 PNG 旁留一份 .sha1 缓存（figure_png 的口径），交付包里不需要
        Path(str(target) + ".sha1").unlink(missing_ok=True)
        table_rel = ""
        merged_into_body = (item.kind == "chart_table"
                            and item.id in (merged_chart_ids or ()))
        if svg_src is None and item.kind == "chart_table" and not merged_into_body:
            table = str((item.body or {}).get("markdown_table") or "").strip()
            if table:
                table_rel = f"{FIGURE_DIR}/表格/{base}.md"
                files.append(_write_text(out_dir, table_rel,
                                         f"# {_figure_caption(item)}\n\n{table}\n"))
        if rendered is not None:
            rel = _relative(out_dir, Path(rendered))
            files.append({"path": rel, "bytes": Path(rendered).stat().st_size,
                          "sha1": _sha1(Path(rendered))})
            png_ok += 1
        elif merged_into_body:
            degraded.append({"item": f"{FIGURE_DIR}/表格/{base}.md",
                             "reason": f"{_figure_kinds_label(item)}「{_figure_caption(item)}」"
                                       f"与所属讲义正文表格同数据：已并入讲义正文表格"
                                       f"（讲义 md / html / pdf 里都能看到），不再单独出表格文件"})
        elif table_rel:
            # 表格类数据图：文字表格本身就是它的交付形态（讲义 PDF 里也以表格呈现）
            degraded.append({"item": f"{FIGURE_DIR}/{base}",
                             "reason": f"{_figure_kinds_label(item)}「{_figure_caption(item)}」"
                                       f"以 Markdown 表格随包交付（{table_rel}），未单独出图片"})
        else:
            reason = str(problem or "重绘失败")
            degraded.append({"item": f"{FIGURE_DIR}/{base}",
                             "reason": f"{_figure_kinds_label(item)}「{_figure_caption(item)}」"
                                       f"没有矢量源也没有可重绘元素：{reason}"})
    # 概念知识图谱（mermaid 源，Obsidian / 网页端可渲染成图）
    try:
        from app.services import concept_graph

        mermaid = concept_graph.to_mermaid(concept_graph.graph_data(db, course))
        if mermaid.strip():
            files.append(_write_text(out_dir, f"{FIGURE_DIR}/概念图-{course_slug(course)}.md",
                                     "# 概念知识图谱（Mermaid）\n\n"
                                     "> 在 Obsidian / 支持 Mermaid 的编辑器里可直接渲染成图。\n\n"
                                     "```mermaid\n" + mermaid.strip() + "\n```\n"))
    except Exception as exc:  # 概念图是加分项：取不到就如实写明
        degraded.append({"item": f"{FIGURE_DIR}/概念图",
                         "reason": f"概念图谱没能生成：{type(exc).__name__}: {exc}"})
    counted = _figure_file_counts(out_dir)      # 客户在包里能数到的数（任务 130）
    return {"figures": len(figures), "svg": svg_ok, "png": png_ok,
            "figure_files": counted}


def _sync_refusal_message(sync: dict, report: dict) -> str:
    """不一致且不允许强制出包时的拒绝原因 + 可照做的指引（任务 38）。"""
    rows = [row for row in (report.get("nodes") or [])
            if str(row.get("status") or "") != "consistent"]
    sample = "；".join(
        f"{row.get('node_key') or '00'} {row.get('title') or ''}"
        f"（{row.get('part') or '笔记'}）" for row in rows[:3]) or "详见网页端「笔记库」页签"
    why = f"（自动导出失败：{sync['export_error']}）" if sync.get("export_error") else ""
    preserved = int(sync.get("export_preserved") or 0)
    kept = f"本次导出保留了 {preserved} 个手改文件；" if preserved else ""
    return ("笔记库与网页端仍有 %d 项不一致，拒绝出包%s：验收单是构建时的快照，"
            "内容没对齐会让客户拿到过期数字。差异示例：%s。%s"
            "请先在网页端把内容重新导出到笔记库（「笔记库」页签 → 保存到 Obsidian 笔记，"
            "手改文件会被保留，必要时勾选强制覆盖），或传 allow_inconsistent=true 强制出包"
            "（此时验收单与 manifest.json 会标注「不一致 N 项」）。"
            % (int(sync.get("inconsistent_after") or 0), why, sample, kept))


def _notes_sync(db: Session, course: Course, *, allow_inconsistent: bool = False) -> dict:
    """出包前先把笔记库同步到网页端（任务 38）。

    交付包里的 `01-验收单.md` 是**构建时的快照**：如果笔记库落后于网页端（例如刚重生成课件
    没重导笔记），客户看到的数字就是过期的。所以默认先按网页端口径导一次笔记，再核对一致性：

    * 本来就一致 → 不动笔记库；
    * 不一致 → 自动导出一次，导出后一致即继续出包（验收单标注「已同步」+ 快照时间）；
    * 导出后仍不一致 → 拒绝出包（除非 ``allow_inconsistent=True``，此时照出但显式标注）。
    """
    before = kb_mod.verify_course_notes(db, course)
    sync = {
        "ok": bool(before.get("ok")),
        "reason": str(before.get("reason") or ""),
        "checked": int(before.get("checked") or 0),
        "inconsistent_before": int(before.get("inconsistent") or 0),
        "inconsistent_after": int(before.get("inconsistent") or 0),
        "exported": False,
        "export_files": 0,
        "export_preserved": 0,
        "export_error": "",
        "forced": bool(allow_inconsistent),
        "synced": True,
        "snapshot_at": datetime.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_label": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    # 没绑定笔记库（ok=False）时无从同步：照旧出包，验收单里如实标注「否」
    if not sync["ok"]:
        sync["synced"] = False
        return sync
    if sync["inconsistent_before"] == 0:
        return sync
    try:
        result = kb_mod.export_course(db, course, force=False, prune=True)
        sync["exported"] = True
        sync["export_files"] = len(result.get("files") or [])
        sync["export_preserved"] = len(result.get("preserved") or [])
    except Exception as exc:  # noqa: BLE001 - 导出失败也要如实记录并给指引
        sync["export_error"] = str(exc)
    after = kb_mod.verify_course_notes(db, course)
    sync["checked"] = int(after.get("checked") or 0)
    sync["inconsistent_after"] = int(after.get("inconsistent") or 0)
    sync["synced"] = sync["inconsistent_after"] == 0
    if not sync["synced"] and not allow_inconsistent:
        raise ValueError(_sync_refusal_message(sync, after))
    return sync


# ---------------------------------------------------------------------------
# 任务 141：交付包不许漏进临时文件 / 文本层修复失败不许静默
# ---------------------------------------------------------------------------
# 来历：复测 140 在 AI 那个包里数到磁盘 797 个文件、比 manifest 的 792 多 5 个 —— 多出来的
# 是 5 个 ``*.pdf.textfix``（文本层修复的临时文件，``tmp_path.replace`` 抛异常时没清掉），
# **同时进了包目录与 zip**；而且那一趟的文本层修复也就悄悄失败了（不报错、不进 degraded）。
# 两道闸门：① 源头 ``repair_pdf_text_layer`` 成败都删临时文件 + 把失败回传出来写进 degraded；
# ② 打包前扫一遍目标目录，兜住任何同类残留（清理 + 记降级项，绝不静默）。

#: 出包目录里**绝不该出现**的临时 / 中间文件后缀：只认这些明确后缀，正常交付件
#: （.md / .html / .pdf / .docx / .png / .svg / .excalidraw / .json / .bat …）一个都不会被误删。
#: 任务 142：同一套后缀的**目录**也算（`图表/foo.tmp/` 连同里面所有内容一起清）。
_STRAY_SUFFIXES = (".textfix", ".tmp", ".temp", ".bak", ".orig", ".swp", ".part", ".crdownload")

#: 任务 142：`~` 结尾**只认「像编辑器备份」的**，别把正当文件误删。
#: Vim / Emacs 的备份写作 `讲义.md~`（原件后缀 + `~`）⇒ 去掉末尾 `~` 后**本身就是**一份
#: 已知交付件或已知临时件才算备份；`z~`、`客户补充说明~` 这种证明不了的**一律保留**。
#:
#: 任务 143：白名单**照真包实测补齐** —— 2026-10-06 扫 29 个真包（真机 `storage\deliveries`
#: 24 个 + 临时环境 5 个）的后缀并集共 14 种：`.bat .canvas .docx .excalidraw .excalidrawlib
#: .html .js .json .md .pdf .png .pptx .srt .svg`（`.csv` / `.txt` 是本方预留的交付件后缀）。
#: 少了 `.excalidrawlib` / `.canvas` / `.js` 时，`_风格库.excalidrawlib~` 这类**真备份**会
#: 既不删、也不进 `degraded`、直接进包（141「这类东西不可能再漏给客户」的承诺就有个洞）。
#: 守它的是 `tests/test_delivery_stray_files.py` 里「白名单 ⊇ 真包后缀」那两条用例。
_DELIVERED_SUFFIXES = (".md", ".html", ".pdf", ".docx", ".pptx", ".png", ".svg",
                       ".excalidraw", ".excalidrawlib", ".canvas", ".json", ".js",
                       ".bat", ".srt", ".csv", ".txt")


def _is_editor_backup(name: str) -> bool:
    """`~` 结尾的名字算不算「编辑器备份」（任务 142）。

    只认三种形状：光秃秃的 `~`、`<已知交付后缀>~`（`讲义.md~`）、`<已知临时后缀>~`（`x.tmp~`）。
    其余以 `~` 结尾的名字（`z~`、`客户补充说明~`）**一律保留** —— 证不明是备份就不动它。
    """
    stem = name.rstrip("~")
    if not stem:
        return True
    return stem.endswith(_DELIVERED_SUFFIXES) or stem.endswith(_STRAY_SUFFIXES)


def _is_stray_name(name: str) -> bool:
    """文件 / 目录名是不是「绝不该进包」的临时件（`name` 需已 `lower()`）。"""
    return name.endswith(_STRAY_SUFFIXES) or (name.endswith("~") and _is_editor_backup(name))


def _purge_stray_files(out_dir: Path) -> list[dict]:
    """打包前兜底扫描：清掉不该进交付包的临时 / 中间**文件与目录**。

    返回被清掉的对象（`{"path": 相对路径, "kind": "file" | "dir", "files": 一起清掉的文件数}`，
    按相对路径排序；目录的 `path` 以 `/` 结尾）。调用方负责把结果**记进 degraded**
    （动手删了就要留痕，不许静默）。

    任务 142：① **目录也兜** —— 旧规则只扫 `is_file()`，`图表/foo.tmp/` 这类**目录**里的文件
    照旧留在包目录与 zip 里；现在连同里面所有内容一起清。② `~` 收紧（见 `_is_editor_backup`）。
    """
    dirs: list[Path] = []
    files: list[Path] = []
    for path in sorted(out_dir.rglob("*")):
        if not _is_stray_name(path.name.lower()):
            continue
        (dirs if path.is_dir() else files).append(path)
    # 嵌套的临时目录只记外层那一条（内层跟着一起没了，不再重复计数）
    roots = [d for d in dirs if not any(d.is_relative_to(other) for other in dirs if other != d)]
    removed: list[dict] = []
    for path in roots:
        count = sum(1 for inner in path.rglob("*") if inner.is_file())
        shutil.rmtree(path, ignore_errors=True)
        removed.append({"path": path.relative_to(out_dir).as_posix() + "/",
                        "kind": "dir", "files": count})
    for path in files:
        if any(path.is_relative_to(directory) for directory in roots):
            continue                      # 已经被上面整目录删掉了，不重复记一条
        path.unlink(missing_ok=True)
        removed.append({"path": path.relative_to(out_dir).as_posix(),
                        "kind": "file", "files": 1})
    removed.sort(key=lambda row: row["path"])
    return removed


def _note_text_layer(degraded: list[dict], item: str, result: dict) -> None:
    """``print_pdf`` 成功、但**文本层没修成**时，把这件事写进降级项（任务 141：不许静默）。

    只认 ``text_layer["unrepaired"]``（缺 pypdf / 读不动 / 回写失败）：PDF 本身仍原样交付、
    可读可打印，只是兼容区码位没归一到正字。**单个字体流报错不算**（既有口径，只在 reason 里带出）。
    """
    layer = result.get("text_layer") or {}
    if layer.get("unrepaired"):
        reason = layer.get("reason") or "未知原因"
        degraded.append({"item": item,
                         "reason": f"PDF 文本层修复未完成（PDF 本身已正常交付、可读可打印）：{reason}"})


def build_delivery(db: Session, course: Course, *, pdf_engine: str = "auto",
                   allow_inconsistent: bool = False,
                   external: bool | None = None,
                   student_lectures: bool | None = None) -> dict:
    """构建交付包：目录 + 同名 zip。**0 LLM 调用**（纯本地渲染与复制）。

    ``external``（任务 48p）不为 None 时先写回课程交付模式再出包；为 None 用已保存的模式
    （course.meta 没写过 = 对外）。两种模式的判别只影响三份说明文件与 manifest。

    ``student_lectures``（任务 86）控制是否多出 ``学员版讲义/``：``None`` = 按课程场景
    自动（含 ``self_study`` 才出）、``True`` = 出包页显式勾选。**默认八件套不变。**
    """
    if external is not None:
        set_delivery_mode(db, course, external=bool(external))
    mode = delivery_mode(course)
    started = time.time()
    # 构建号、说明里的交付日期、manifest.generated_at 都用同一个「开始构建」时刻，
    # 否则跨分钟的慢构建（27 篇 PDF 约 95 秒）会出现「构建号 1714 / 说明 1716」这种不一致。
    started_at = datetime.now()
    # 任务 112：判据集合变了（任务 106 新增第 11 项 `numbers`）⇒ 存量 QC 的分数是旧口径算的，
    # 必须先按现行判据重算（缺篇就现算、够新的不重算），否则验收单 / 笔记库体检报告
    # 写出去的是旧口径分数，与「按现行判据现算」对不上。放在同步笔记之前：紧随其后的
    # 导笔记（含 00-课程体检报告.md）也一并带上重算后的分数。0 LLM、幂等。
    qc_refresh = lecture_qc.refresh_stale_course(db, course)
    # 任务 38：先自检一致性 —— 不一致就默认先同步笔记，同步不掉且未授权强制 → 直接拒绝出包
    # （放在建目录之前：拒绝时磁盘不留半成品）
    sync = _notes_sync(db, course, allow_inconsistent=allow_inconsistent)
    root = deliveries_root()
    root.mkdir(parents=True, exist_ok=True)
    # 任务 126：构建号由**内容指纹**定 —— 同一门课、同一份内容、同一套出包选项，隔多久
    # 再出都复用同一个包（幂等）；内容变了才换号。旧口径「同一分钟 = 同一次交付」跨分钟
    # 必换号（DSH 2026-10-03 门禁：'…-1451' != '…-1450-f1f625'，而那两份产物逐字节相同）。
    fingerprint = delivery_content_fingerprint(db, course, mode=mode["mode"],
                                              pdf_engine=pdf_engine,
                                              student_lectures=student_lectures)
    build_id, out_dir = resolve_build_target(root, course, started_at, fingerprint)
    rebuilt = out_dir.exists()
    if rebuilt:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files: list[dict] = []
    missing: list[dict] = []
    degraded: list[dict] = []

    plan = collect_plan(db, course)
    plan["notes_sync"] = sync
    # 任务 112：本包数字的质检口径（「什么时候算」的留痕，写进验收单第 2.1 节）
    plan["qc_refresh"] = qc_refresh
    missing.extend(row for row in plan["missing"]
                   if not row["item"].startswith(FIGURE_DIR))
    # 笔记库只读：体检读取会 mkdir，先快照目录，出包前回收被顺带创建的空目录
    vault_snapshot = _vault_snapshot(course)

    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id)
             .order_by(ContentItem.created_at).all())
    report = audit_mod.course_report(db, course)
    usage = report.get("usage") or {}
    stats = {
        "model": _usage_model_label(),
        "llm_calls": int(usage.get("llm_calls") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cost_cny": float(usage.get("cost_cny") or 0.0),
        "build_llm_calls": 0,
    }

    # ---- 讲义：md 源 + HTML + PDF ----
    pairs = _lecture_pairs(db, course)
    chapter_dirs = _chapter_dirs(pairs)
    edge = find_edge()
    # 任务 86：是否多出一份「学员版讲义/」—— 显式参数优先（出包页勾选），
    # 否则按课程场景自动（含 self_study 才出）。判定用 scenario 的唯一出处，不另写一份。
    want_student = (bool(student_lectures) if student_lectures is not None
                    else "self_study" in (scenario.scenario_meta(course).get("selected") or []))
    mermaid_src = find_mermaid_js()
    engine = (pdf_engine or "auto").strip().lower()
    pdf_enabled = engine in ("auto", "edge") and edge is not None
    if engine == "off":
        degraded.append({"item": f"{LECTURE_DIR}/*.pdf",
                         "reason": "本次构建显式关闭了 PDF（pdf_engine=off），已交付可直接打印的 HTML"})
    elif engine in ("auto", "edge") and edge is None:
        logger.warning(
            "交付包降级：本机没找到 Microsoft Edge（--print-to-pdf）⇒ 讲义 PDF 这一项跳过、改为可直接打印的 HTML；"
            "这是**环境缺组件**（装 Edge 或设 DELIVERY_EDGE_EXE 后重新出包即可），不是课件有问题。")
        degraded.append({"item": f"{LECTURE_DIR}/*.pdf",
                         "reason": "本机没找到 Microsoft Edge（--print-to-pdf），已降级为可直接打印的 HTML"})
    if mermaid_src is None:
        degraded.append({"item": "讲义里的流程图",
                         "reason": "没找到本地 mermaid.js，流程图在 PDF/HTML 里以源代码块呈现"
                                   "（网页端可正常看图）"})
    mermaid_rel = ""
    if mermaid_src is not None:
        files.append(_copy_file(out_dir, f"{LECTURE_DIR}/{LECTURE_ASSETS_DIR}/mermaid.min.js",
                                mermaid_src))
        if want_student:
            # 学员版讲义与讲义同结构 ⇒ 它的 HTML 也要能找到同一份 mermaid（同相对路径）
            files.append(_copy_file(out_dir,
                                    f"{STUDENT_LECTURE_DIR}/{LECTURE_ASSETS_DIR}/mermaid.min.js",
                                    mermaid_src))
        mermaid_rel = f"../{LECTURE_ASSETS_DIR}/mermaid.min.js"

    figure_by_id = {item.id: item for item in items if item.kind in FIGURE_KINDS}
    # 任务 64：正文里已经写出同一张表的数据卡，渲染时跳过（别把同一张表画两遍）
    chart_markdowns = chart_table_markdowns(items)
    # 任务 66：这批卡被跳过的同时，它们的 图表/表格/*.md 降级件也不再单独出
    merged_chart_ids: set[str] = set()

    def resolve_image(url: str) -> str | None:
        kind, _, ident = url.partition("://")
        item = figure_by_id.get(ident)
        if item is None:
            return None
        if kind == "chart" and item.kind == "chart_table":
            table = str((item.body or {}).get("markdown_table") or "").strip()
            rows = [line.strip().strip("|").split("|") for line in table.splitlines() if line.strip()]
            if len(rows) >= 2:
                head = "".join(f"<th>{_inline_html(cell.strip())}</th>" for cell in rows[0])
                body = "".join("<tr>" + "".join(f"<td>{_inline_html(cell.strip())}</td>" for cell in row)
                               + "</tr>" for row in rows[2:])
                return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        svg_src = _svg_source_of(item)
        if svg_src is None:
            return None
        text = svg_src.read_text(encoding="utf-8", errors="ignore")
        return re.sub(r"<\?xml[^>]*\?>", "", text).strip()

    def emit_lecture(rel_dir: str, stem: str, title: str, subtitle: str,
                     markdown: str) -> tuple[int, list[dict]]:
        """落一份讲义（md 源 + HTML + PDF），返回 (PDF 成功数, 失败清单)。

        任务 86 从这个函数多出一个调用方（学员版讲义）：两个目录**同结构**，避免两套渲染。
        """
        files.append(_write_text(out_dir, f"{rel_dir}/{stem}.md", markdown))
        # 交付的 .md 源保持原样（引用仍在）；只有渲染出来的 HTML / PDF 去重
        html_md, skipped_ids = md_tables.strip_duplicate_chart_refs(markdown, chart_markdowns)
        merged_chart_ids.update(skipped_ids)
        body_html = markdown_to_html(html_md, resolve_image=resolve_image)
        page_html = html_page(title, body_html, subtitle=subtitle,
                              mermaid_js=mermaid_rel or None)
        files.append(_write_text(out_dir, f"{rel_dir}/{stem}.html", page_html))
        if not pdf_enabled:
            return 0, []
        # 任务 96：只给「打印用的那份」注入 @font-face —— 上面交付的 .html 一个字节不动
        print_html, font_warn = inject_print_fonts(page_html)
        if font_warn:
            degraded.append({"item": f"{rel_dir}/{stem}.pdf", "reason": font_warn})
        # 任务 98：把**源文本**（注入前那份 page_html）带进文本层修复 —— 异体字反查错位靠它对账
        result = print_document(print_html, out_dir / rel_dir / f"{stem}.pdf", edge=str(edge),
                                source_text=page_html)
        if not result["ok"]:
            return 0, [{"item": f"{rel_dir}/{stem}.pdf", "reason": result["reason"]}]
        # 任务 141：PDF 出来了、但文本层没修成也要留痕（不许静默）
        _note_text_layer(degraded, f"{rel_dir}/{stem}.pdf", result)
        pdf_path = out_dir / rel_dir / f"{stem}.pdf"
        files.append({"path": f"{rel_dir}/{stem}.pdf", "bytes": pdf_path.stat().st_size,
                      "sha1": _sha1(pdf_path)})
        return 1, []

    pdf_ok = 0
    student_pdf_ok = 0
    pdf_failed: list[dict] = []
    for chapter, node, item in pairs:
        chapter_dir = chapter_dirs.get(getattr(chapter, "id", "") or "00", "00-未分章")
        stem = f"{getattr(node, 'node_key', '') or '00'}-{kb_mod._slug(item.title, 40)}"
        subtitle = (f"{course.title} · {getattr(chapter, 'title', '') or '未分章'} · "
                    f"{getattr(node, 'node_key', '') or ''}")
        # 任务 57：讲义文末补「## 来源」小节（复用笔记库同一份渲染），顶部补「怎么读」图例
        markdown = _delivery_lecture_markdown(item)
        count, failed = emit_lecture(f"{LECTURE_DIR}/{chapter_dir}", stem,
                                     item.title or stem, subtitle, markdown)
        pdf_ok += count
        pdf_failed += failed
        if not want_student:
            continue
        # 任务 86：学员版讲义 —— 与 讲义/ 同结构，正文按 audience 的确定性规则去答案与解析
        count, failed = emit_lecture(f"{STUDENT_LECTURE_DIR}/{chapter_dir}", stem,
                                     f"{item.title or stem}（学员版）", f"{subtitle} · 学员版",
                                     audience.student_markdown(markdown))
        student_pdf_ok += count
        pdf_failed += failed
    if pdf_enabled and pdf_failed:
        degraded.extend(pdf_failed)

    # ---- 教案 / 学案（任务 49 / 50）：0 LLM 骨架 → md 源 + Word + PDF ----
    def emit_document(rel_dir: str, rel_stem: str, title: str, subtitle: str,
                      markdown: str, *, page_numbers: bool = False) -> tuple[bool, bool, bool]:
        """落一份「一课时一份」的文档：md 源 + Word(+ PDF)。返回 (md, docx, pdf) 是否成功。"""
        files.append(_write_text(out_dir, f"{rel_dir}/{rel_stem}.md", markdown))
        docx_bytes = b""
        try:
            docx_bytes = docx_export.markdown_to_docx(
                markdown, title=title, page_numbers=page_numbers,
                footer_text=kb_mod._slug(course.title, 30) if page_numbers else "")
        except Exception as exc:  # noqa: BLE001 —— Word 失败仍交付 markdown 源，且显式记原因
            degraded.append({"item": f"{rel_dir}/{rel_stem}.docx",
                             "reason": f"Word 渲染失败（markdown 源仍已交付）：{exc}"})
        if docx_bytes:
            files.append(_write_bytes(out_dir, f"{rel_dir}/{rel_stem}.docx", docx_bytes))
        if not pdf_enabled:
            return True, bool(docx_bytes), False
        # 任务 98：源文本（注入前那份）要带进文本层修复
        page_html = html_page(title, markdown_to_html(markdown), subtitle=subtitle)
        html_text, font_warn = inject_print_fonts(page_html)
        if font_warn:
            degraded.append({"item": f"{rel_dir}/{rel_stem}.pdf", "reason": font_warn})
        with tempfile.TemporaryDirectory(prefix="cg-doc-") as tmp:
            tmp_html = Path(tmp) / f"{rel_stem}.html"
            tmp_html.write_text(html_text, encoding="utf-8", newline="")
            result = print_pdf(tmp_html, out_dir / rel_dir / f"{rel_stem}.pdf", edge=str(edge),
                               source_text=page_html)
        if not result["ok"]:
            degraded.append({"item": f"{rel_dir}/{rel_stem}.pdf", "reason": result["reason"]})
            return True, bool(docx_bytes), False
        # 任务 141：PDF 出来了、但文本层没修成也要留痕（不许静默）
        _note_text_layer(degraded, f"{rel_dir}/{rel_stem}.pdf", result)
        pdf_path = out_dir / rel_dir / f"{rel_stem}.pdf"
        files.append({"path": f"{rel_dir}/{rel_stem}.pdf", "bytes": pdf_path.stat().st_size,
                      "sha1": _sha1(pdf_path)})
        return True, bool(docx_bytes), True

    plan_count = plan_docx_ok = plan_pdf_ok = 0
    sheet_count = sheet_docx_ok = sheet_pdf_ok = 0
    for chapter, node, item in pairs:
        chapter_dir = chapter_dirs.get(getattr(chapter, "id", "") or "00", "00-未分章")
        base_stem = f"{getattr(node, 'node_key', '') or '00'}-{kb_mod._slug(node.title, 40)}"
        subtitle = (f"{course.title} · {getattr(chapter, 'title', '') or '未分章'} · "
                    f"{getattr(node, 'node_key', '') or ''}")
        lecture = lecture_qc.lecture_markdown(item)
        try:
            markdown = lesson_plan.plan_markdown(db, course, node, lecture)
        except Exception as exc:  # noqa: BLE001 —— 单份教案失败不阻断整包
            degraded.append({"item": f"{LESSON_DIR}/{chapter_dir}/{base_stem}-教案.docx",
                             "reason": f"教案骨架渲染失败（讲义本身仍已交付）：{exc}"})
        else:
            md_ok, docx_ok, plan_pdf = emit_document(
                f"{LESSON_DIR}/{chapter_dir}", f"{base_stem}-教案", f"《{node.title}》教案",
                f"{subtitle}（教案）", markdown)
            plan_count += 1 if md_ok else 0
            plan_docx_ok += 1 if docx_ok else 0
            plan_pdf_ok += 1 if plan_pdf else 0
        try:
            sheet = student_sheet.sheet_markdown(db, course, node, lecture)
        except Exception as exc:  # noqa: BLE001
            degraded.append({"item": f"{SHEET_DIR}/{chapter_dir}/{base_stem}-学案.docx",
                             "reason": f"学案渲染失败（讲义本身仍已交付）：{exc}"})
        else:
            md_ok, docx_ok, sheet_pdf = emit_document(
                f"{SHEET_DIR}/{chapter_dir}", f"{base_stem}-学案", f"《{node.title}》学案",
                f"{subtitle}（学案·学生版）", sheet, page_numbers=True)
            sheet_count += 1 if md_ok else 0
            sheet_docx_ok += 1 if docx_ok else 0
            sheet_pdf_ok += 1 if sheet_pdf else 0
    # ---- 课件：直接复制现成 pptx ----
    deck_dir_items = db.query(ContentItem).filter(
        ContentItem.course_id == course.id, ContentItem.kind == "ppt").all()
    deck_rows = kb_mod._deck_items(db, course, list_nodes(db, course.id))
    for rel, src, _chapter in deck_rows:
        files.append(_copy_file(out_dir, f"{DECK_DIR}/{Path(rel).name}", src))
    if not deck_rows and deck_dir_items:
        degraded.append({"item": f"{DECK_DIR}/",
                         "reason": "库里记录有课件条目，但源 pptx 文件已不在 storage（请重新生成课件）"})

    # ---- HTML 课件（任务 88，0 LLM）：纯本地渲染现成页数据 ⇒ 课件/HTML/ ----
    # 数据只取自课件自己的页数据（meta.slides / meta.layouts）与该课现成的图（矢量源 / 数据卡），
    # **不重跑 PPT、不写 pptx、不调模型**；每个 deck 一份单文件 HTML（CSS/JS 内联、图内联 base64）。
    html_rows = deck_html.build_files(db, course, figures=figure_rows(items))
    for row in html_rows:
        files.append(_write_text(
            out_dir, f"{DECK_DIR}/{deck_html.DIR_NAME}/{row['name']}", row["text"]))

    # ---- 图表 ----
    figure_stats = _figures_section(db, course, items, out_dir, files, degraded,
                                    merged_chart_ids=merged_chart_ids)
    if figure_stats["figures"] == 0:
        missing.append({"item": f"{FIGURE_DIR}/", "reason": "这门课还没有图（概念图 / 数据图表）"})
    # 任务 130：说明文件里的「图表」数字一律取**包内实例文件数**（现算，见 _figure_file_counts）；
    # 条目数（plan["counts"]["figures"]）保留，作辅口径。**不进 manifest.counts**
    # （本任务只改文字口径，manifest 结构一个字不动）。

    # ---- 笔记库（只读复制）----
    notes_count = _copy_notes_tree(course, out_dir, files, db)
    if notes_count == 0 and not any(row["item"].startswith(NOTES_DIR) for row in missing):
        missing.append({"item": f"{NOTES_DIR}/",
                        "reason": "笔记库里还没有导出过这门课（先在网页端「保存到 Obsidian 笔记」）"})

    # ---- 录课包（任务 87，0 LLM）：仅当课程场景含 recorded 时生成 ----
    # 逐字稿（拼现有演讲备注，不改写）/ 时长表（6 分钟闸门）/ 字幕 SRT / 分镜表（+ 课堂互动点）。
    # 未选场景或场景不含 recorded ⇒ 一个文件都不写，交付包结构与从前逐字一致。
    kit_entries = recording_kit.collect_entries(db, course)
    kit_files = recording_kit.build_files(db, course) if kit_entries else []
    for row in kit_files:
        files.append(_write_text(out_dir, row["path"], row["text"]))
    kit_stats = recording_kit.summary(kit_entries)
    if recording_kit.kit_enabled(course) and not kit_files:
        # 场景要出、却一个文件都没拼出来（没课件 / 课件没有页级备注）—— 绝不静默少目录
        missing.append({"item": f"{recording_kit.DIR_NAME}/",
                        "reason": "场景含「录成视频课」，但这门课还没有课件：逐字稿 / 时长 / 字幕 / 分镜都从课件派生，"
                                  "先生成课件再重新出包"})

    # ---- 素材清单 ----
    materials = list_materials(db, course.id)
    files.append(_write_text(out_dir, MATERIAL_NAME,
                             "\n".join(_material_lines(db, course, materials)) + "\n"))

    # ---- 双击核对文件完整性（任务 132）----
    # 客户不敲命令行：包根目录放一个 .bat，双击即重算 sha1 与 manifest.json 对账。
    # 它**不特殊**：与其它文件一样进 files[]（只有 manifest.json 自身因哈希不能自引用不在清单里）。
    files.append(_write_text(out_dir, integrity_check.BAT_NAME, integrity_check.bat_text()))

    # ---- 打包前兜底扫描（任务 141）----
    # 任何临时 / 中间文件都不许跟着包走：清掉 + 记进 degraded。放在两份说明之前 ⇒ 客户在
    # `00-交付说明.md` 第 4 节能看到这条（不静默）。这条闸门就是为 `.pdf.textfix` 立的——
    # 它曾在回写失败时留在包目录里、跟着 zip 发给客户（复测 140 抓到 5 个）。
    for stray in _purge_stray_files(out_dir):
        # 任务 142：目录也兜（`图表/foo.tmp/` 整个清掉）⇒ 说明里写清是目录、里面几个文件
        what = (f"临时目录（连同里面的 {stray['files']} 个文件）" if stray["kind"] == "dir"
                else "临时文件")
        degraded.append({"item": stray["path"],
                         "reason": f"出包过程的{what}，已清理、未进交付包"
                                   "（正常交付不该出现这条；看到请把这段反馈给制作方）"})

    # ---- 说明 + 验收单 ----
    # 任务 142：`pdf_ok` 曾被下面教案 / 学案循环的解包覆盖成布尔值 ⇒ 客户在验收单上
    # 看到「本次生成 PDF True 篇」。教案 / 学案各自用 plan_pdf / sheet_pdf 计数后，这里
    # 恢复成本来的意思：**讲义 PDF 的成功篇数**。
    plan["counts"]["pdf_ok"] = pdf_ok
    # 任务 86：学员版讲义（0 = 本包没有这个目录）
    plan["counts"]["student_lectures"] = len(pairs) if want_student else 0
    plan["counts"]["student_pdf_ok"] = student_pdf_ok
    plan["counts"]["pdf_failed"] = len(pdf_failed)
    plan["counts"]["lesson_plans"] = plan_count
    plan["counts"]["lesson_docx_ok"] = plan_docx_ok
    plan["counts"]["lesson_pdf_ok"] = plan_pdf_ok
    plan["counts"]["student_sheets"] = sheet_count
    plan["counts"]["sheet_docx_ok"] = sheet_docx_ok
    plan["counts"]["sheet_pdf_ok"] = sheet_pdf_ok
    plan["counts"]["notes_files"] = notes_count
    # 任务 88：网页版 HTML 课件（0 = 本包没有这个目录）
    plan["counts"]["html_decks"] = len(html_rows)
    # 任务 87：录课包（0 = 本包没有这个目录）
    plan["counts"]["recording_kit_files"] = len(kit_files)
    plan["counts"]["recording_decks"] = kit_stats["decks"]
    plan["counts"]["recording_pages"] = kit_stats["pages"]
    plan["counts"]["recording_seconds"] = kit_stats["seconds"]
    plan["counts"]["recording_interactions"] = (
        1 if kit_files and recording_kit.wants_interactions(course) else 0)
    # 任务 86：「本包各篇的口径」汇总（取自每篇讲义生成时记下的 meta；老内容 = 通用(未标记)）
    plan["voice_tally"] = scenario.tally([item.meta for _chapter, _node, item in pairs])
    plan["build_id"] = build_id
    files.append(_write_text(out_dir, ACCEPT_NAME,
                             "\n".join(_acceptance_lines(db, course, report, plan, stats,
                                                         fig_files=figure_stats["figure_files"],
                                                         degraded=degraded))
                             + "\n"))
    files.append(_write_text(out_dir, README_NAME,
                             "\n".join(_readme_lines(db, course, plan, stats,
                                                    missing, degraded, build_id,
                                                    started_at,
                                                    fig_files=figure_stats["figure_files"]))
                             + "\n"))

    # ---- 笔记库零写入：回收体检读取过程中被顺带创建的空目录 ----
    vault_removed = _restore_vault(vault_snapshot)

    # ---- manifest（在 zip 之前写；自身不入清单）----
    duration_ms = int((time.time() - started) * 1000)
    manifest = {
        "build_id": build_id,
        "content_fingerprint": fingerprint,
        **({"build_version": BUILD_VERSION} if mode["mode"] == MODE_INTERNAL
           else {"content_version": CONTENT_VERSION}),
        "delivery_mode": mode["mode"],
        "delivery_mode_label": mode["label"],
        "course": {
            "id": course.id,
            "title": course.title,
            "audience": course.audience,
            "subject": course.subject,
            "duration_minutes": course.duration_minutes,
            "status": course.status,
            "output_rel": kb_mod.course_kb(course).get("output_rel") or "",
        },
        "generated_at": started_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "rebuild": rebuilt,
        "llm_calls": 0,
        "params": {
            **({"model": stats["model"]} if mode["mode"] == MODE_INTERNAL else {}),
            "pdf_engine": ("edge:" + str(edge)) if pdf_enabled else f"html-only({engine})",
            "mermaid": "local mermaid.min.js" if mermaid_src else "code-block",
            "build_duration_ms": duration_ms,
            **({"course_llm_calls": stats["llm_calls"],
                "course_total_tokens": stats["total_tokens"],
                "course_cost_cny": stats["cost_cny"]}
               if mode["mode"] == MODE_INTERNAL else {}),
            "note": ("本包构建 0 次 LLM 调用；课程用量为内容生成阶段的历史累计"
                     if mode["mode"] == MODE_INTERNAL else
                     "对外交付模式：不含模型 / 用量 / 金额（内部复盘数据见交付方留存）"),
        },
        "counts": {**plan["counts"], "files": len(files) + 1,
                   "svg": figure_stats["svg"], "png": figure_stats["png"]},
        "files": sorted(files, key=lambda row: row["path"]),
        "missing": missing,
        "degraded": degraded,
        "notice": "manifest.json 自身不在 files 清单内（哈希无法自引用）；"
                  "其余每个文件都有相对路径 + sha1 + 字节数。",
        "vault": {
            "read_only": not sync.get("exported"),
            "created_dirs_cleaned": vault_removed,
            "note": ("出包前发现不一致 → 已自动导出一次笔记（任务 38）；" if sync.get("exported")
                     else "出包前一致性自检通过，未写笔记库；")
                    + "读取体检数据时被顺带创建的空目录已在出包前回收。",
        },
        "notes_sync": {
            "synced_before_build": bool(sync.get("synced")),
            "checked": sync.get("checked", 0),
            "inconsistent_before": sync.get("inconsistent_before", 0),
            "inconsistent_after": sync.get("inconsistent_after", 0),
            "auto_exported": bool(sync.get("exported")),
            "export_files": sync.get("export_files", 0),
            "export_preserved": sync.get("export_preserved", 0),
            "export_error": sync.get("export_error", ""),
            "forced_by_allow_inconsistent": bool(sync.get("forced")),
            "snapshot_at": sync.get("snapshot_at"),
            "note": "验收单与现算口径一致的前提：出包前已把笔记库同步到网页端；若之后重生成内容，请重新出包。",
        },
        "acceptance": {
            "report_score": (report.get("conclusion") or {}).get("score"),
            "broken_links": (report.get("links") or {}).get("broken"),
            "inconsistent": (report.get("consistency") or {}).get("inconsistent"),
            "qc_average": (report.get("quality") or {}).get("average_score"),
        },
    }
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8", newline="")

    # ---- zip（同名，重复构建直接覆盖）----
    zip_path = root / f"{build_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_dir).as_posix())

    return {
        "build_id": build_id,
        "course_id": course.id,
        "dir": str(out_dir),
        "zip": str(zip_path),
        "zip_bytes": zip_path.stat().st_size,
        "files": len(manifest["files"]) + 1,
        "bytes": sum(row["bytes"] for row in manifest["files"]) + manifest_path.stat().st_size,
        "counts": manifest["counts"],
        "missing": missing,
        "degraded": degraded,
        "rebuilt": rebuilt,
        "duration_ms": duration_ms,
        "delivery_mode": mode,
        "llm_calls": 0,
        "vault": {"writes": 1 if sync.get("exported") else 0,
                  "created_dirs_cleaned": len(vault_removed)},
        "notes_sync": sync,
        # 任务 112：出包前那次质检复检的计数（旧口径重算 / 已是最新 / 失败），
        # 「第二次出包不再重算」这条幂等就是看它（验收单第 2.1 节同源）。
        "qc_refresh": qc_refresh,
        "pdf": {"generated": pdf_ok, "failed": len(pdf_failed),
                "engine": manifest["params"]["pdf_engine"]},
        "generated_at": manifest["generated_at"],
    }


def read_manifest(build_id: str) -> dict | None:
    path = deliveries_root() / build_id / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def zip_path_of(build_id: str) -> Path | None:
    path = deliveries_root() / f"{build_id}.zip"
    return path if path.is_file() else None


def list_deliveries(course_id: str) -> list[dict]:
    """历史交付包（按构建号倒序）：只读 manifest，不重新构建。"""
    root = deliveries_root()
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for manifest_path in sorted(root.glob(f"*/{MANIFEST_NAME}")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if str((data.get("course") or {}).get("id") or "") != course_id:
            continue
        build_id = str(data.get("build_id") or manifest_path.parent.name)
        zip_path = root / f"{build_id}.zip"
        rows.append({
            "build_id": build_id,
            "generated_at": data.get("generated_at"),
            "dir": str(manifest_path.parent),
            "zip": str(zip_path) if zip_path.is_file() else "",
            "zip_bytes": zip_path.stat().st_size if zip_path.is_file() else 0,
            "counts": data.get("counts") or {},
            "missing": data.get("missing") or [],
            "degraded": data.get("degraded") or [],
            "llm_calls": int(data.get("llm_calls") or 0),
            "duration_ms": int((data.get("params") or {}).get("build_duration_ms") or 0),
            "acceptance": data.get("acceptance") or {},
        })
    rows.sort(key=lambda row: str(row.get("generated_at") or ""), reverse=True)
    return rows
