"""原始资料服务：教师上传/粘贴 → 提取文本 → 素材库 → 生成时注入提示词。

设计：默认教师没有现成知识库。教师只负责给出原始资料（.md/.txt/.csv/.docx/.pdf/.pptx/图片），
本系统负责提取、整理并作为课程生成的依据；所需「笔记系统」由系统自动生成。

任务 54：图片素材（.png/.jpg/.jpeg/.webp，``kind="image"``）走**本地 OCR** 提文字 ——
引擎是可选依赖 ``rapidocr-onnxruntime``（见 ``backend/requirements-ocr.txt``，约 400MB）。
没装引擎 / 识别失败都**显式说明、不 500**：素材照旧入库为「无文字素材」，
``meta.ocr`` 里写清引擎、版本、页数、平均置信度与跳过原因。

任务 102：音视频素材（.mp4/.mov/… 与 .mp3/.wav/…）走**本地语音转写** ——
引擎是可选依赖 ``faster-whisper``（见 ``backend/requirements-asr.txt``），**本机跑、不上传云端**；
转写是**异步**的（上传立刻返回，进度走 ``GET /jobs/{job_id}``），没装引擎 / 转写失败同样
**显式说明、不 500**，``meta.asr`` 里写清状态、时长、段数与跳过原因。

任务 103：中文素材转写**默认带简体引导语**（``ASR_ZH_PROMPT``）—— faster-whisper 出厂
默认吐繁体（实测「我們講 / 體系 / 產氣」），光传 ``language="zh"`` 一个字都不变；非中文
**不传**（实测给英文硬塞中文引导语会把整段转坏），``meta.asr.initial_prompt`` 如实记录。
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services import usage as usage_log
from app.models.course import Course, SourceMaterial

logger = logging.getLogger(__name__)

TEXT_EXT = {".md", ".txt", ".csv", ".markdown"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}     # 任务 54：图片素材（OCR 提文字）
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}           # 任务 102：视频（本地语音转写）
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}  # 任务 102：录音（同上）
AV_EXT = VIDEO_EXT | AUDIO_EXT
SUPPORTED_EXT = TEXT_EXT | {".docx", ".pdf", ".pptx"} | IMAGE_EXT | AV_EXT
MAX_TEXT_STORE = 200_000

# 素材全文接口的分页默认（P2-1：文件型素材原先读不到全文，只能 404）
FULL_TEXT_DEFAULT_LIMIT = 2000
FULL_TEXT_MAX_LIMIT = 20000
# 任务 69 §B1：注入不再「永远取前 6 份 × 每份前 2500 字」——
# 选材/分段/预算全部改由 :func:`plan_injection` 负责（以下是**历史口径**，
# 只为兼容老调用方与「反证据」脚本还原 68 的行为，生产路径不再拿它们当硬上限）。
MAX_INJECT_FILES = 6
MAX_INJECT_CHARS = 2500

# ── 任务 69 §B1：注入完整性 ─────────────────────────────────────────────────
# 总预算按 token 粗估（中文 1 字 ≈ 1 token 量级，留足余量；超预算按相关性取舍，不静默砍）。
INJECT_BUDGET_OUTLINE = 60_000
INJECT_BUDGET_LECTURE = 80_000
INJECT_BUDGET_MISC = 40_000
INJECT_BUDGET_BY_SCOPE = {
    "outline": INJECT_BUDGET_OUTLINE,
    "lecture": INJECT_BUDGET_LECTURE,
}
# 单份素材的**最小**注入量：预算不够时，低于这个数就不收它了（进「未参与」清单），
# 免得每份只进去 20 个字、既看不懂又占位置。
MIN_INJECT_PER_MATERIAL = 1200
# 长素材按段落取舍时**必须先留**的开头段数（标题/总览一般在前两段）。
INJECT_HEAD_PARAGRAPHS = 2

# ── 任务 69 §B2：权威性区分（教师素材 vs AI 补充）────────────────────────────
AI_SOURCE = "ai"
AI_TITLE_PREFIX = "AI补充——"
# 任务 69 §B2：注入文本分两段标注两类素材的**权威性**。
# 用户 2026-09-21 复核时的用词是「人工提供」（原规格 §B2-1 写成「教师提供的原始素材」）——
# 按用户最新用词：本课素材本身就是 AI 生成的，「教师 / 原始」会强化「有人工原稿」的误读。
TEACHER_SECTION_TITLE = "=== 人工提供的素材(以它为准;事实性表述的依据只认这里)==="
AI_SECTION_TITLE = "=== AI 补充参考(仅供结构与表述参考,不得作为事实依据)==="
# 素材来源类型（任务 69 §B1.5.1）：上传原件 / 粘贴文本 / AI 补充。
# 用户 2026-09-21 口径：**原始上传的可以接受（保持原样）**、**AI 补充与带对话外壳的
# 粘贴件必须干净** ⇒ 这里定义「谁必须清理」，卫生层与界面提示都按它分档。
ORIGIN_UPLOAD = "upload"
ORIGIN_PASTE = "paste"
ORIGIN_AI = "ai"
# 任务 118：链接导入（YouTube）—— 与上传 / 粘贴 / AI 补充并列的第四种来源。
# 字幕 / 转写稿都是机器出的干净文本 ⇒ **不进「必须清理」那一档**（要不要清理由老师判断）。
ORIGIN_LINK = "link"
MUST_CLEAN_ORIGINS = frozenset({ORIGIN_PASTE, ORIGIN_AI})
ORIGIN_LABELS = {
    ORIGIN_UPLOAD: "人工上传",
    ORIGIN_PASTE: "粘贴文本(疑似 AI 产物)",
    ORIGIN_AI: "AI 补充",
    ORIGIN_LINK: "链接导入（YouTube）",
}
# 「有来处」（任务 69 §B1.5.3）：AI 补充里给不出来处的句子必须写成这个标记
# 括号**两种都认**（模型偶尔写全角），但规范写法是这里的半角形态（与讲义侧的
# NO_BASIS_MARK「(无直接依据,建议人工确认)」同一形态）。
UNGROUNDED_MARK = "(无依据·模型补充)"
_UNGROUNDED_RE = re.compile(r"[(（]\s*无依据\s*[·・.,，]?\s*模型补充\s*[)）]")
GROUNDED_KEY = "grounded"
# 本次注入情况落库的位置（course.meta[...]；交付包 / 体检报告可查，与任务 70 的自检落库同一处）
MATERIAL_INJECT_KEY = "material_inject"

# 任务 54：OCR（本地引擎，数据不出本机）。置信度均值低于阈值 ⇒ 质检里给**素材级提示**
# （不动 10 项判据，见 lecture_qc.course_report 的 material_notices）。
OCR_ENGINE = "rapidocr-onnxruntime"
OCR_CONFIDENCE_HINT = 0.75
OCR_AVAILABLE: bool | None = None
OCR_SKIP_REASON = ("未安装 OCR 组件（可选依赖 requirements-ocr.txt），已跳过图片文字提取；"
                   "素材仍可作为「无文字素材」入库")

# 任务 102：音视频素材的**本地语音转写**（可选依赖 faster-whisper，纯本地、数据不出本机）。
# ASR 是本地模型，不是大模型调用 ⇒ **大模型花费 0**，真正花的是 CPU 时间（1 小时视频约 10–40 分钟）。
# 口径照任务 54 的 OCR：不装也能用（降级成「无文字素材」+ 写清跳过原因，接口不 500）。
ASR_ENGINE = "faster-whisper"
ASR_MODEL_DEFAULT = "small"              # 兜底；`.env` 或环境变量 ASR_MODEL 可覆盖（medium / large-v3 …）
ASR_SEGMENT_MAX_CHARS = 120              # 每段 ≤ 约 120 字（按语音停顿合并；段首仍 [mm:ss]，任务 105）
ASR_MAX_BYTES = 2 * 1024 * 1024 * 1024   # 单文件 ≤ 2GB
ASR_MAX_SECONDS = 3 * 60 * 60            # 时长 ≤ 3 小时
ASR_AVAILABLE: bool | None = None
ASR_SKIP_REASON = ("未安装语音转写组件（可选依赖 requirements-asr.txt），已跳过音视频转写；"
                   "素材仍作为「无文字素材」入库，装好组件后点「重新转写」即可")

# 任务 103：faster-whisper 出厂默认**出繁体**（实测「我們講多軍共生體系 / 負責產氣」），
# 而且光传 language="zh" 一点都不变 —— 必须给中文引导语，模型才吐简体（实测同音错字也少一截）。
ASR_ZH_PROMPT = "以下是普通话的句子，请用简体中文转写。"
ASR_ZH_LANG_PREFIX = "zh"                    # 语种码以 zh 开头算中文（yue 粤语不算：引导语是普通话）
ASR_DOMAIN_PROMPT_ENV = "ASR_DOMAIN_PROMPT"  # 领域引导语开关（默认关，见 asr_domain_hint）
ASR_SAMPLE_RATE = 16000                      # whisper 固定 16 kHz 单声道

# 任务 105（用户 2026-09-27 拍板）：**来源锚点口径** —— 音视频素材（有转写正文）的引用
# 写时间戳（读者能直接跳到视频那一刻），其余一律照旧写「第 N 段」；两种口径可以同时
# 存在于同一门课（每条素材各按自己的类型来）。
ANCHOR_STAMP = "stamp"                       # 引用写（来源:《录像名》06:47）
ANCHOR_PARA = "para"                         # 引用写（来源:《素材名》第 N 段）
AV_KINDS = {"video", "audio"}                # SourceMaterial.kind 的两个音视频取值

DIRECTION_LABELS = {
    "concept": "概念铺垫",
    "examples": "例题练习",
    "learner": "学情导入",
    "difficulty": "难点解析",
    "background": "背景拓展",
    "custom": "自定义补充",
}

MATERIAL_SYSTEM = """MATERIAL_JSON
你是资深学科备课助手。参考给定的课程信息与教师已有素材，为列出的每个方向各补充一份
可直接放进备课素材库的中文文本素材（符合大纲目标与受众，语言自然、内容准确）。

【必须接地：不许凭空讲（硬要求，任务 69 §B1.5.3）】
1. 只能基于「注入给你的已有素材」与课程信息撰写；不许引入素材里没有的数字、参数、案例、
   结论、人名、机构名或文献；
2. 每一条**事实性表述**后面必须紧跟它的来处，二选一：
   · 有来处 → 锚点照抄注入文本末尾「来源标注怎么写」那一行的口径：文本素材写
     `（来源:《素材标题》第 N 段）`（N 用注入文本里每段前的 `[序号]`），音视频素材写
     `（来源:《素材标题》06:47）`（照抄该段段首的时间戳）；标题照抄
     注入文本的素材标题 —— 引用带 `AI补充——` 前缀的素材时写成
     `（来源:AI补充——《素材标题》第 N 段）`（**前缀别丢**）；
   · 没有来处 → `(无依据·模型补充)`。只能用于过渡性 / 表述性内容，不许拿它冒充依据；
3. 数不出来处就写 `(无依据·模型补充)`，**宁可标无依据，也不许编**。

只输出 JSON（不要代码块），格式：
{"materials": [{"direction": "方向名（必须与输入列出的完全一致）",
                "title": "素材标题",
                "text": "素材正文（Markdown，不少于 300 字；每条事实性表述带上述来处标注）"}]}
"""

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def list_materials(db: Session, course_id: str) -> list[SourceMaterial]:
    return (
        db.query(SourceMaterial)
        .filter(SourceMaterial.course_id == course_id)
        .order_by(SourceMaterial.created_at)
        .all()
    )


def ext_of(filename: str) -> str:
    """素材扩展名（小写、带点）—— 路由判「是不是音视频」用，与入库解析同一算法。"""
    return Path(filename).suffix.lower()


def add_material_file(db: Session, course: Course, filename: str, data: bytes,
                      title: str | None = None, *,
                      source_path: Path | None = None) -> SourceMaterial:
    """上传原件入库（文本 / 文档 / 图片 / 音视频）。

    任务 102：音视频**不在这里转写** —— 只落素材行（``meta.asr``）并把原件搬进
    ``storage/materials/``，转写交给后台作业（见 :func:`transcribe_material`）：
    1 小时视频在 CPU 上要 10–40 分钟，同步做会把上传请求拖死。``source_path`` 是路由
    **分块落盘**的临时原件（2GB 上限的音视频不整份进内存），搬走后临时文件即消失。
    """
    ext = Path(filename).suffix.lower()
    ocr: dict | None = None
    asr: dict | None = None
    if ext in AV_EXT:
        text = ""
        kind = "video" if ext in VIDEO_EXT else "audio"
        ready = asr_available()
        asr = _asr_meta("pending" if ready else "failed",
                        skipped_reason="" if ready else ASR_SKIP_REASON)
        if source_path is not None:
            # 只读文件头探时长（不解码）：已知时长先填上；超 3 小时在这里就拒收
            probe = probe_av(source_path)
            if probe.get("duration_sec"):
                asr["duration_sec"] = round(float(probe["duration_sec"]), 3)
            if asr["duration_sec"] and asr["duration_sec"] > ASR_MAX_SECONDS:
                raise ValueError(f"音视频时长 {_asr_duration_text(asr['duration_sec'])} 超过上限 3 小时，"
                                 "请先裁剪或分段后再上传")
    elif ext in IMAGE_EXT:
        # 任务 54：图片素材 —— 没装 OCR 组件也要入库（无文字素材），并写清跳过原因
        ocr = ocr_image(data)
        text = _clean(str(ocr.get("text") or ""))
        if not text and not ocr.get("skipped"):
            ocr["skipped"] = "图里没有识别到文字（可能是纯图形/照片），已作为「无文字素材」入库"
        kind = "image"
    else:
        text = extract_text(filename, data)
        kind = "file"
        if not text.strip():
            raise ValueError("未能从文件中提取到文本（支持 md/txt/csv/docx/pdf/pptx/png/jpg）")
    # 任务 69 §B1.5.1（用户 2026-09-21 口径）：**上传原件保持原样、不强制清理** ——
    # 这里只**检测**页眉页脚 / 对话外壳（行数记进 ``meta.detected_lines``，素材行给提示 +
    # 「一键清理」入口），正文一个字不动；要清由老师点按钮（见 :func:`clean_material`）。
    raw_text = text
    _, detected_lines, detected_rules = strip_shell(text)
    settings = get_settings()
    mat_dir = settings.storage_dir / "materials"
    mat_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in Path(filename).name if c.isalnum() or c in "._-") or "material.txt"
    out = mat_dir / f"{uuid.uuid4().hex}_{safe}"
    if source_path is not None:
        shutil.move(str(source_path), str(out))     # 音视频：原件已经在盘上，别再进一次内存
    else:
        out.write_bytes(data)
    meta = {"ext": ext, "chars": len(text), "original": filename,
            # P2-1：存下解析出的原始字数与页数，
            # 老师才能核对「PPT 里的字到底读进去多少」。
            "raw_chars": len(raw_text), "truncated": len(text) > MAX_TEXT_STORE,
            # 任务 69 §B1.5.2：页数入库即落库（原先每次读原件现算，原件丢了就显示「—」）
            "pages": 1 if ext in IMAGE_EXT else page_count(filename, data),
            # 任务 69 §B1.5.1：来源类型（人工上传 / 粘贴文本(疑似 AI 产物) / AI 补充）
            "origin": ORIGIN_UPLOAD,
            "detected_lines": detected_lines, "detected_rules": detected_rules,
            "cleaned_lines": 0, "cleaned_rules": [],
            # 上传原件不剥 ⇒ 往外拿时也原样（老数据没这一位，见 material_text）
            "shell_stripped": False}
    if ocr is not None:
        meta["ocr"] = {key: ocr.get(key) for key in
                       ("engine", "version", "pages", "confidence", "lines", "skipped")}
    if asr is not None:
        meta["asr"] = asr
    row = SourceMaterial(
        course_id=course.id,
        title=(title or Path(filename).stem)[:100],
        kind=kind,
        file_rel=str(out.relative_to(settings.storage_dir)).replace("\\", "/"),
        text=text[:MAX_TEXT_STORE],
        meta=meta,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def asr_meta(status: str, *, skipped_reason: str = "") -> dict:
    """``meta.asr`` 的**公开**骨架（任务 118 的链接导入走本地 ASR 时也要写同一形状）。"""
    return _asr_meta(status, skipped_reason=skipped_reason)


def add_material_link(db: Session, course: Course, url: str, *, parsed: dict,
                      title: str = "") -> SourceMaterial:
    """链接导入：**先落素材行**（正文先空着），抓取交给后台作业（任务 118）。

    为什么先落行：抓取要几十秒到几十分钟（播放列表更久），而且外部依赖可能缺失 ——
    行先落库，「缺 yt-dlp / cookie 失效」这类失败才有地方写原因、也才有「重新导入」可点
    （规格 §三.2：不 500、素材行仍在、可重试）。``kind`` 取 ``video``：正文带 ``[mm:ss]``
    时间戳，与任务 105 的音视频锚点口径一致（讲义引用写 ``（来源:《素材名》06:47）``）。
    """
    meta = {"ext": ".youtube", "chars": 0, "raw_chars": 0, "truncated": False, "pages": None,
            "original": url, "origin": ORIGIN_LINK, "source": "youtube",
            "detected_lines": 0, "detected_rules": [], "cleaned_lines": 0, "cleaned_rules": [],
            # 字幕 / 转写稿是机器文本，没有「对话外壳」可剥 ⇒ 按已清理记（不动既有素材的判定）
            "shell_stripped": True,
            "link": {"source": "youtube", "url": url, "status": "pending"}}
    meta["link"].update({key: parsed.get(key) for key in ("kind", "video_id", "playlist_id")})
    row = SourceMaterial(course_id=course.id, title=(title or url)[:100], kind="video",
                         file_rel="", text="", meta=meta)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def set_link_import(db: Session, row: SourceMaterial, *, link: dict, text: str | None = None,
                    title: str = "", asr: dict | None = None) -> None:
    """写回抓取结果：``meta.link``（+ 正文 + 可选 ``meta.asr``）—— **只写 meta / text，不加列**。

    ``text`` 为空表示「这次没抓到」（失败态），照旧写 ``meta.link``；``asr`` 只有走了本地
    转写那条路才有（字幕路径**不写** ``meta.asr``，免得界面显示「转写完成」误导人）。
    """
    meta = dict(row.meta or {})
    meta["link"] = link
    if title.strip() and (not row.title or row.title == str(meta.get("original") or "")):
        row.title = title.strip()[:100]
    if text is not None:
        row.text = text[:MAX_TEXT_STORE]
        meta["chars"] = len(row.text)
        meta["raw_chars"] = len(text)
        meta["truncated"] = len(text) > MAX_TEXT_STORE
    if asr is not None:
        meta["asr"] = asr
    row.meta = meta
    db.commit()
    db.refresh(row)


def add_material_text(db: Session, course: Course, title: str, text: str) -> SourceMaterial:
    if not text.strip():
        raise ValueError("粘贴内容为空")
    # 任务 69 §B1.5.1（用户 2026-09-21 口径）：**粘贴件必须干净** —— 它最容易带
    # 「生成详细版 / ###### ChatGPT 说：」这类对话外壳 ⇒ 入库即剥，并记下「已清理 N 行」。
    raw_text = text
    cleaned, cleaned_lines, cleaned_rules = strip_shell(text)
    row = SourceMaterial(
        course_id=course.id,
        title=title[:100] or "未命名素材",
        kind="text",
        file_rel="",
        text=cleaned[:MAX_TEXT_STORE],
        meta={"ext": ".text", "chars": min(len(cleaned), MAX_TEXT_STORE), "original": "",
              "raw_chars": len(raw_text),
              "origin": ORIGIN_PASTE,
              "cleaned_lines": cleaned_lines, "cleaned_rules": cleaned_rules,
              "shell_stripped": True},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_material(db: Session, material_id: str) -> None:
    row = db.get(SourceMaterial, material_id)
    if row is None:
        raise ValueError("素材不存在")
    if row.file_rel:
        try:
            (get_settings().storage_dir / row.file_rel).unlink(missing_ok=True)
        except OSError:
            pass
    db.delete(row)
    db.commit()


def extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXT:
        return data.decode("utf-8", errors="replace")
    if ext in IMAGE_EXT:
        return _clean(str(ocr_image(data).get("text") or ""))
    if ext == ".docx":
        return _extract_docx(data)
    if ext == ".pptx":
        return _extract_pptx(data)
    if ext == ".pdf":
        return _extract_pdf(data)
    raise ValueError(
        f"不支持的素材格式：{ext or '未知'}（支持 md/txt/csv/docx/pdf/pptx/png/jpg/jpeg/webp"
        "/mp4/mov/mkv/webm/avi/m4v/mp3/wav/m4a/aac/flac/ogg/opus）")


# ---------------------------------------------------------------------------
# 任务 54：图片素材的 OCR（可选依赖 rapidocr-onnxruntime，本地跑、数据不出本机）
# ---------------------------------------------------------------------------
_OCR_ENGINE_CACHE = None


def ocr_available() -> bool:
    """OCR 引擎是否可用（import 探测一次并缓存）。**未装不许 500**：跳过提取、显式说明。"""
    global OCR_AVAILABLE
    if OCR_AVAILABLE is None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception:  # noqa: BLE001 —— 可选依赖，没装是正常情况
            OCR_AVAILABLE = False
        else:
            OCR_AVAILABLE = True
    return OCR_AVAILABLE


def _ocr_engine_version() -> str:
    try:
        from importlib.metadata import version

        return str(version(OCR_ENGINE))
    except Exception:  # noqa: BLE001
        return ""


def _ocr_engine():
    """进程内复用同一个引擎实例（实测初始化 ~0.4s、单张图 ~1.5s）。"""
    global _OCR_ENGINE_CACHE
    if _OCR_ENGINE_CACHE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE_CACHE = RapidOCR()
    return _OCR_ENGINE_CACHE


def ocr_image(data: bytes) -> dict:
    """图片 → ``{engine, version, pages, confidence, text, lines, skipped}``。

    三条护栏：① 引擎没装 → ``skipped`` 写「未安装 OCR 组件…」；② 识别抛异常 → ``skipped``
    写失败原因（**都不抛出去**，素材照旧能入库）；③ 识别出来的每一行文字用换行连接，
    交给 ``split_paragraphs`` 切成 ``[N]`` 段落 ⇒ 讲义里「第 N 段」能定位到图里第 N 行。
    """
    blank = {"engine": OCR_ENGINE, "version": "", "pages": 1, "confidence": None,
             "text": "", "lines": 0, "skipped": ""}
    if not data:
        return {**blank, "skipped": "空文件，没有可识别的文字"}
    if not ocr_available():
        return {**blank, "skipped": OCR_SKIP_REASON}
    started = time.time()
    try:
        result, _elapse = _ocr_engine()(data)
    except Exception as exc:  # noqa: BLE001 —— 识别失败降级，不让上传接口 500
        return {**blank, "skipped": f"OCR 识别失败（{exc}），已跳过图片文字提取"}
    rows = [row for row in (result or [])
            if len(row) >= 3 and str(row[1]).strip()]
    scores = [float(row[2]) for row in rows]
    return {
        "engine": OCR_ENGINE,
        "version": _ocr_engine_version(),
        "pages": 1,
        "confidence": round(sum(scores) / len(scores), 3) if scores else None,
        "text": "\n".join(str(row[1]).strip() for row in rows),
        "lines": len(rows),
        "elapsed_ms": int((time.time() - started) * 1000),
        "skipped": "",
    }


def ocr_summary(row: SourceMaterial) -> dict:
    """素材的 OCR 元信息（没有 OCR 过就是空 dict）—— 素材页与素材清单共用。"""
    return dict((row.meta or {}).get("ocr") or {})


def ocr_page_note(row: SourceMaterial) -> str:
    """素材页顶部那一行「本页文字由 OCR 识别（引擎/版本/平均置信度）」。

    没 OCR 过的素材返回空串；**跳过提取**的素材把原因写出来（读者才知道为什么没有正文）。
    """
    ocr = ocr_summary(row)
    if not ocr:
        return ""
    if ocr.get("skipped"):
        return "> ⚠️ 本页没有 OCR 文字：" + str(ocr.get("skipped"))
    confidence = ocr.get("confidence")
    engine = ocr.get("engine") or OCR_ENGINE
    version = f" {ocr['version']}" if ocr.get("version") else ""
    score = f"，平均置信度 {confidence}" if confidence is not None else ""
    low = confidence is not None and float(confidence) < OCR_CONFIDENCE_HINT
    hint = "（低于阈值，建议人工抽查）" if low else ""
    return f"> 本页文字由 OCR 识别（{engine}{version}{score}）{hint}"


def ocr_notices(db: Session, course: Course) -> list[dict]:
    """OCR 的**素材级提示**（任务 54）：低置信度 / 没装组件 / 识别失败。

    刻意**不塞进 10 项判据**（不动 ``CHECK_KEYS`` / 权重 / 分母）：它挂在整课质检报告的
    ``material_notices`` 上，由前端单独显示，让老师知道「这条依据本身可能不牢」。
    """
    out: list[dict] = []
    for row in list_materials(db, course.id):
        ocr = ocr_summary(row)
        if not ocr:
            continue
        skipped = str(ocr.get("skipped") or "")
        confidence = ocr.get("confidence")
        if skipped:
            if "没有识别到文字" in skipped:
                continue                       # 纯图形/照片不是问题，不打扰老师
            out.append({"material_id": row.id, "title": row.title, "level": "warn",
                        "confidence": None, "reason": skipped})
        elif confidence is not None and float(confidence) < OCR_CONFIDENCE_HINT:
            out.append({"material_id": row.id, "title": row.title, "level": "warn",
                        "confidence": float(confidence),
                        "reason": f"OCR 平均置信度 {confidence} 低于 {OCR_CONFIDENCE_HINT}："
                                  "图片里的文字可能是识别错的，作为依据前请人工抽查"})
    return out


# ---------------------------------------------------------------------------
# 任务 102：音视频素材的本地语音转写（可选依赖 faster-whisper，本地跑、数据不出本机）
# ---------------------------------------------------------------------------
_ASR_ENGINE_CACHE = None


def asr_model() -> str:
    """转写模型名：**进程环境变量 ``ASR_MODEL`` 优先**（启动前 export 仍然生效，102 的口径不变）
    ⇒ 再取 ``.env`` 里的 ``ASR_MODEL``（经 ``Settings``，与项目其它配置一致）⇒ 都没有才回落
    ``ASR_MODEL_DEFAULT``（``small``；也可写 medium / large-v3）。
    """
    return ((os.environ.get("ASR_MODEL") or "").strip()
            or (get_settings().asr_model or "").strip()
            or ASR_MODEL_DEFAULT)


def _env_true(name: str) -> bool:
    """环境变量真值判断（1 / true / yes / on 算开）—— 不设或写别的都算关。"""
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def is_chinese_language(language: str) -> bool:
    """语种码是不是中文（faster-whisper 给 ``zh``；``yue`` 粤语不算 —— 引导语写的是普通话）。"""
    return str(language or "").strip().lower().startswith(ASR_ZH_LANG_PREFIX)


def asr_prompt(language: str, *, domain_hint: str = "") -> str:
    """任务 103 §A1：**中文**才给的 ``initial_prompt``（简体引导语 + 可选领域引导语）。

    非中文一律返回空串 —— 实测给英文素材硬塞中文引导语会把整段转坏（同一段 15 秒英文：
    不传是 2.0 秒的正常英文，硬传变成 35.2 秒的串语种垃圾）。
    """
    if not is_chinese_language(language):
        return ""
    return ASR_ZH_PROMPT + (domain_hint or "")


def asr_available() -> bool:
    """转写引擎是否可用（import 探测一次并缓存）。**未装不许 500**：跳过转写、显式说明。"""
    global ASR_AVAILABLE
    if ASR_AVAILABLE is None:
        try:
            import faster_whisper  # noqa: F401
        except Exception:  # noqa: BLE001 —— 可选依赖，没装是正常情况
            ASR_AVAILABLE = False
        else:
            ASR_AVAILABLE = True
    return ASR_AVAILABLE


def _asr_engine_version() -> str:
    try:
        from importlib.metadata import version

        return str(version(ASR_ENGINE))
    except Exception:  # noqa: BLE001
        return ""


def _asr_engine():
    """进程内复用同一个模型实例（首次调用会从 HuggingFace 下模型，默认 small ≈0.5GB）。"""
    global _ASR_ENGINE_CACHE
    if _ASR_ENGINE_CACHE is None:
        from faster_whisper import WhisperModel

        _ASR_ENGINE_CACHE = WhisperModel(asr_model(), device="cpu", compute_type="int8")
    return _ASR_ENGINE_CACHE


def _asr_meta(status: str, *, skipped_reason: str = "") -> dict:
    """``meta.asr`` 的完整骨架（字段固定，前端不用猜哪一位缺）。

    任务 102 §A3：状态写 meta（**不许加数据库列 / 不许迁移**），``status`` 取
    pending / running / done / failed —— 未装可选依赖算 ``failed``，由 ``skipped_reason``
    说清那是「跳过」而不是「转写出错」。

    任务 103 §A2：**加第 10 个字段** ``initial_prompt``（中文简体引导语；没写就是空串）——
    前 9 个字段的含义一个字不动。
    """
    return {"engine": ASR_ENGINE, "version": _asr_engine_version(), "model": asr_model(),
            "duration_sec": None, "segments": 0, "language": "", "avg_logprob": None,
            "status": status, "skipped_reason": skipped_reason, "initial_prompt": ""}


def asr_summary(row: SourceMaterial) -> dict:
    """素材的 ASR 元信息（没转写过就是空 dict）—— 素材页与素材清单共用。"""
    return dict((row.meta or {}).get("asr") or {})


def probe_av(path: Path | str) -> dict:
    """音视频能不能转写：``{ok, has_audio, duration_sec, error}``。

    用 PyAV（随 faster-whisper 一起装）**只读文件头、不解码**：既挡得住「没有音轨」与
    「打不开 / 已损坏」，也不用把 2GB 原件读进内存。
    """
    info = {"ok": False, "has_audio": False, "duration_sec": None, "error": ""}
    try:
        import av
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"缺少解码组件 av（{exc}）"
        return info
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                info["ok"] = True
                info["error"] = "这个文件里没有音轨（可能是纯画面 / 纯数据），没有可转写的声音"
                return info
            info["ok"] = True
            info["has_audio"] = True
            if container.duration:
                info["duration_sec"] = float(container.duration) / 1_000_000.0
    except Exception as exc:  # noqa: BLE001 —— 打不开就报清楚，别让老师猜
        info["error"] = f"文件打不开或已损坏（{exc}）"
    return info


def _asr_stamp(seconds: float) -> str:
    """秒 → ``[mm:ss]``（超过 1 小时分钟位是三位，如 ``[105:03]``）。"""
    total = max(0, int(seconds or 0))
    return f"[{total // 60:02d}:{total % 60:02d}]"

def _asr_duration_text(seconds: float) -> str:
    """秒 → 给人看的时长（``4 小时 0 分``）—— 只用于**拒收 / 失败**的人话提示。

    正文里的段前缀仍按规格用 ``[mm:ss]``（见 :func:`_asr_stamp`）。
    """
    total = max(0, int(round(seconds or 0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def merge_asr_segments(rows: list[dict], *,
                       max_chars: int = ASR_SEGMENT_MAX_CHARS) -> list[dict]:
    """whisper 的细片段 → 「按语音停顿合并、每段 ≤ 约 120 字」的段 ``[{start, end, text}]``。

    合并后每段行首缀 ``[mm:ss]``、段间空行（见 :func:`asr_segments_to_text`）：既方便老师
    按时间回看，也让讲义里的时间戳锚点能直接跳到那一刻（任务 105：段长 ≈25–30 秒，
    锚点才精到「跳过去就能核对」）。``split_paragraphs`` 照旧按行 / 空行切，段落口径不变。
    """
    out: list[dict] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start = float(row.get("start") or 0.0)
        end = float(row.get("end") or start)
        if out and len(out[-1]["text"]) + len(text) + 1 <= max_chars:
            out[-1]["text"] = f"{out[-1]['text']} {text}"
            out[-1]["end"] = end
        else:
            out.append({"start": start, "end": end, "text": text})
    return out


def asr_segments_to_text(segments: list[dict]) -> str:
    """段 → 素材正文（每段行首 ``[mm:ss]``、段间空行）。"""
    return "\n\n".join(f"{_asr_stamp(seg.get('start'))} {str(seg.get('text') or '').strip()}"
                       for seg in segments if str(seg.get("text") or "").strip())


def _asr_decode(path: Path | str):
    """解码成 16 kHz 单声道 float32（faster-whisper 自带 PyAV 解码，不需要系统 ffmpeg）。

    转写内部本来也要解一次；这里解**同一份**给「探语种」与「转写」共用，不重复解码。
    """
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=ASR_SAMPLE_RATE)


def probe_asr_language(engine, audio) -> str:
    """探素材语种（只编码 1 段，实测 15 秒素材约 1.3 秒）：拿不到就返回空串。

    拿不到（引擎没有 ``detect_language`` / 解码异常）⇒ 空串 ⇒ 不给引导语，照旧转写。
    """
    try:
        language, _prob, _all = engine.detect_language(audio)
    except Exception:  # noqa: BLE001 —— 探语种失败不该拖垮转写（只是不给引导语）
        return ""
    return str(language or "")


def transcribe_av(path: Path | str, *, on_progress=None, domain_hint: str = "") -> dict:
    """音视频 → ``{text, segments, duration_sec, language, model, avg_logprob, initial_prompt}``。

    ``on_progress(已转写秒数, 总秒数)`` 每转完一段回调一次 —— 路由用它把进度写进
    ``GET /jobs/{job_id}``（进度 = 已转写时长 ÷ 总时长）。

    任务 103：**中文**素材默认带简体引导语（``initial_prompt``，出厂默认出繁体）；非中文
    一律不传。``domain_hint`` 是课程标题 / 学科拼的领域引导语，由 :func:`asr_domain_hint`
    按开关（``ASR_DOMAIN_PROMPT``，默认关）给。
    """
    engine = _asr_engine()
    try:
        audio = _asr_decode(path)
    except Exception:  # noqa: BLE001 —— 解不开就把路径交给引擎，报错口径与任务 102 一致
        audio = None
    language = probe_asr_language(engine, audio) if audio is not None else ""
    prompt = asr_prompt(language, domain_hint=domain_hint)
    options: dict = {"vad_filter": True, "beam_size": 5}
    if prompt:
        options["initial_prompt"] = prompt
    raw_segments, info = engine.transcribe(audio if audio is not None else str(path), **options)
    duration = float(getattr(info, "duration", None) or 0.0)
    rows: list[dict] = []
    for seg in raw_segments:                    # 惰性生成器：边转边报进度
        text = str(getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        end = float(getattr(seg, "end", start) or start)
        rows.append({"start": start, "end": end, "text": text,
                     "logprob": float(getattr(seg, "avg_logprob", 0.0) or 0.0)})
        if on_progress is not None and duration > 0:
            on_progress(min(end, duration), duration)
    if duration <= 0 and rows:
        duration = rows[-1]["end"]              # 容器没写时长就用最后一段兜底
    segments = merge_asr_segments(rows)
    logprobs = [row["logprob"] for row in rows]
    return {"text": asr_segments_to_text(segments), "segments": segments,
            "duration_sec": round(duration, 3),
            "language": str(getattr(info, "language", "") or ""), "model": asr_model(),
            "avg_logprob": round(sum(logprobs) / len(logprobs), 4) if logprobs else None,
            "initial_prompt": prompt}


def _set_asr(db: Session, row: SourceMaterial, asr: dict, *, text: str | None = None) -> None:
    """把 ``meta.asr``（必要时连正文与字数）写回素材行 —— **只写 meta / text，不加列**。"""
    meta = dict(row.meta or {})
    meta["asr"] = asr
    if text is not None:
        row.text = text[:MAX_TEXT_STORE]
        meta["chars"] = len(row.text)
        meta["raw_chars"] = len(text)
        meta["truncated"] = len(text) > MAX_TEXT_STORE
    row.meta = meta
    db.commit()
    db.refresh(row)


def asr_domain_hint(db: Session, row: SourceMaterial) -> str:
    """任务 103 §A3：**可选**领域引导语（课程标题 / 学科拼一句），开关默认关。

    开关 = 环境变量 ``ASR_DOMAIN_PROMPT``（1 / true / yes / on 算开）。实测把课程名塞进
    引导语能压掉同音错字（「多军」→「多菌」），但也有把无关词带进正文的风险 ⇒ 默认关，
    要用的人自己开；开了就在 ``meta.asr.initial_prompt`` 里**如实记全文**。
    """
    if not _env_true(ASR_DOMAIN_PROMPT_ENV):
        return ""
    course = db.get(Course, row.course_id) if row.course_id else None
    if course is None:
        return ""
    words = [str(course.title or "").strip(), str(course.subject or "").strip()]
    words = [w for w in words if w]
    if not words:
        return ""
    return "本课讲：" + "，".join(words) + "。"


def transcribe_material(db: Session, row: SourceMaterial, *, on_progress=None) -> dict:
    """把一条音视频素材转写掉：回填正文 + ``meta.asr``（**素材行与原件都不丢**）。

    失败（没装组件 / 没音轨 / 打不开 / 转写抛异常）只写 ``status=failed`` + ``skipped_reason``
    并返回同一形状的结果，**不抛异常** —— 由「重新转写」入口重试。
    """
    result = {"material_id": row.id, "status": "failed", "duration_sec": None,
              "segments": 0, "chars": len(row.text or ""), "skipped_reason": ""}

    def _fail(reason: str) -> dict:
        _set_asr(db, row, _asr_meta("failed", skipped_reason=reason))
        logger.warning("音视频转写失败：material=%s 原因=%s", row.id, reason)
        return {**result, "skipped_reason": reason}

    if not asr_available():
        return _fail(ASR_SKIP_REASON)
    path = (get_settings().storage_dir / row.file_rel) if row.file_rel else None
    if path is None or not path.exists():
        return _fail("原件不在 storage/materials 里（可能被手工删过），请重新上传后再转写")
    probe = probe_av(path)
    if not probe["ok"]:
        return _fail(probe["error"] or "这个音视频读不出来，没法转写")
    if not probe["has_audio"]:
        return _fail(probe["error"] or "这个文件里没有音轨，没有可转写的声音")
    duration = float(probe["duration_sec"] or 0.0)
    if duration > ASR_MAX_SECONDS:
        return _fail(f"音视频时长 {_asr_duration_text(duration)} 超过上限 3 小时，已跳过转写")
    running = _asr_meta("running")
    running["duration_sec"] = round(duration, 3) if duration else None
    _set_asr(db, row, running)
    try:
        out = transcribe_av(path, on_progress=on_progress,
                            domain_hint=asr_domain_hint(db, row))
    except Exception as exc:  # noqa: BLE001 —— 转写失败降级：素材行与原件都不动，可重试
        return _fail(f"转写失败（{exc}），可点「重新转写」重试")
    text = str(out.get("text") or "")
    done = _asr_meta("done")
    done.update({"model": out.get("model") or asr_model(), "duration_sec": out.get("duration_sec"),
                 "segments": len(out.get("segments") or []), "language": out.get("language") or "",
                 "avg_logprob": out.get("avg_logprob"),
                 "initial_prompt": out.get("initial_prompt") or ""})
    if not text.strip():
        done["skipped_reason"] = ("这段音视频里没有识别到语音（可能是无声 / 纯音乐），"
                                  "已作为「无文字素材」入库")
    _set_asr(db, row, done, text=text)
    logger.info("音视频转写完成：material=%s 时长=%.1fs 段数=%d 字数=%d",
                row.id, duration, done["segments"], len(text))
    return {"material_id": row.id, "status": "done", "duration_sec": done["duration_sec"],
            "segments": done["segments"], "chars": len(row.text or ""),
            "skipped_reason": done["skipped_reason"]}


def asr_page_note(row: SourceMaterial) -> str:
    """素材页顶部那一行「本页文字由本地语音转写（引擎 / 模型 / 时长 / 段数 / 平均置信度）」。

    没转写过的素材返回空串；**转写中 / 跳过 / 失败**都把状态或原因写出来（读者才知道
    为什么这页没有正文）。
    """
    asr = asr_summary(row)
    if not asr:
        return ""
    status = str(asr.get("status") or "")
    reason = str(asr.get("skipped_reason") or "")
    model = asr.get("model") or asr_model()
    if status in ("pending", "running"):
        return (f"> ⏳ 本页文字由本地语音转写（{asr.get('engine') or ASR_ENGINE} · 模型 {model}）"
                "**生成中**，稍后重新导出即可看到正文")
    if status != "done" or reason:
        return "> ⚠️ 本页没有转写文字：" + (reason or "转写未完成")
    bits = []
    if asr.get("duration_sec") is not None:
        bits.append(f"时长 {_asr_stamp(asr['duration_sec']).strip('[]')}")
    bits.append(f"{int(asr.get('segments') or 0)} 段")
    if asr.get("avg_logprob") is not None:
        bits.append(f"平均置信度 {asr['avg_logprob']}")
    version = f" {asr['version']}" if asr.get("version") else ""
    return (f"> 本页文字由本地语音转写（{asr.get('engine') or ASR_ENGINE}{version} · "
            f"模型 {model} · {' · '.join(bits)}）")


def _extract_docx(data: bytes) -> str:
    """用标准库解析 docx（zip + document.xml 段落文本），不依赖 python-docx。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    root = ET.fromstring(xml)
    lines: list[str] = []
    for para in root.iter(_W_NS + "p"):
        text = "".join(t.text or "" for t in para.iter(_W_NS + "t"))
        if text.strip():
            lines.append(text)
    return _clean("\n".join(lines))


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    lines: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text)
    return _clean("\n".join(lines))


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return _clean("\n".join((page.extract_text() or "") for page in reader.pages))


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 任务 69 §B1.5：素材入库卫生 —— 剥离对话外壳与页眉页脚
# ---------------------------------------------------------------------------
# 每条 =（标签, 正则）。**只在开头/结尾的窗口内**匹配，且要求该行足够短 ——
# 宁可不剥，也绝不把正文误删（剥离不了的只提示、不硬改）。
_SHELL_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("对话外壳", re.compile(r"^生成(详细|完整|正式)?(版|稿)?$")),
    ("对话外壳", re.compile(
        r"^#*\s*(ChatGPT|GPT|Claude|Kimi|文心|通义|DeepSeek|AI|助手|Assistant|You)\s*"
        r"(说|回复|回答|said)?\s*[:：]?\s*$", re.I)),
    ("对话外壳", re.compile(r"^好的[ ✅✔👍]*[，,]?\s*(按照|根据)?你(需要|要求|说的)")),
    ("对话外壳", re.compile(r"^好的[，,]?\s*(以下是|这是|我来|我为你|下面)")),
    ("对话外壳", re.compile(r"^(以下是|这是|这里是)(你)?(需要|要)的")),
    ("对话外壳", re.compile(
        r"^[*`_ ]*[\w\u4e00-\u9fff\-.]+\.(md|txt|markdown|docx?|pdf)[*`_ ]*[:：]?$", re.I)),
    ("对话外壳", re.compile(r"^(希望|如果)(对你)?(这)?(有帮助|有用|需要)")),
    ("页眉页脚", re.compile(r"^\s*(汇报人|演讲人|主讲人|制作人|作者|署名|分享人)\s*[:：]")),
    ("页眉页脚", re.compile(r"^\s*(时间|日期|制作时间|汇报时间)\s*[:：]\s*\d{2,4}[.\-/年]")),
    ("页眉页脚", re.compile(r"^\s*\d{4}[.\-/年]\s*\d{1,2}([.\-/月]\s*\d{1,2}日?)?\s*$")),
    ("页眉页脚", re.compile(r"^(谢谢(观看|聆听|大家|欣赏)|感谢(观看|聆听))[!！。.\s]*$")),
    ("页眉页脚", re.compile(r"^第\s*\d+\s*页(\s*/\s*共\s*\d+\s*页)?$")),
]
_SHELL_HEAD_WINDOW = 12      # 开头多少行内查「对话外壳 / 页眉」
_SHELL_TAIL_WINDOW = 6       # 结尾多少行内查「页脚」
_SHELL_MAX_LINE_CHARS = 60   # 超过这个长度的行一律不动（宁可漏剥）
_SHELL_MIN_KEEP_CHARS = 20   # 剥完剩不到这个字数就放弃剥离，保留原文


def strip_shell(text: str) -> tuple[str, int, list[str]]:
    """剥离对话外壳与页眉页脚 → ``(cleaned, removed_lines, matched_labels)``。

    实测形态（AI 课真素材，2026-09-21 走查）：开头 ``生成详细版`` /
    ``###### ChatGPT 说：`` / ``好的 ✅ 按照你需要的详细版，这里是 PE_LangChain工具.md：``；
    PPT 首行 ``汇报人:Kimi AI`` / ``时间:2025.01.01``。

    **保守是硬要求**：只查开头 12 行与结尾 6 行、只认短行（≤60 字）、只认固定措辞；
    剥完剩下不到 20 字就整体放弃（``removed_lines=0`` + 一条说明标签），
    绝不因为启发式把正文吃掉。
    """
    body = str(text or "")
    lines = body.splitlines()
    keep: list[str] = []
    labels: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        in_window = index < _SHELL_HEAD_WINDOW or index >= len(lines) - _SHELL_TAIL_WINDOW
        label = ""
        if stripped and in_window and len(stripped) <= _SHELL_MAX_LINE_CHARS:
            for name, pattern in _SHELL_RULES:
                if pattern.search(stripped):
                    label = name
                    break
        if label:
            labels.append(label)
            continue
        keep.append(line)
    removed = len(lines) - len(keep)
    if removed == 0:
        return body, 0, []
    cleaned = _clean("\n".join(keep))
    if len(cleaned) < _SHELL_MIN_KEEP_CHARS:
        return body, 0, ["剥完会清空正文,已保留原样(请人工确认)"]
    return cleaned, removed, labels


def origin_of(row: SourceMaterial) -> str:
    """素材来源类型：``upload`` / ``paste`` / ``ai``（老数据按 kind 兜底推断）。"""
    meta = row.meta or {}
    origin = str(meta.get("origin") or "")
    if origin in ORIGIN_LABELS:
        return origin
    if str(meta.get("source") or "") == AI_SOURCE:
        return ORIGIN_AI
    # 老数据没记 origin：kind=text 的是粘贴件（带对话外壳的概率最高），其余按上传原件
    return ORIGIN_PASTE if row.kind == "text" else ORIGIN_UPLOAD


def must_clean(row: SourceMaterial) -> bool:
    """这份素材是否**必须干净**（用户 2026-09-21 口径：AI 补充 + 粘贴件必须清理）。"""
    return origin_of(row) in MUST_CLEAN_ORIGINS


def origin_label(row: SourceMaterial) -> str:
    """来源类型的中文标签（素材清单.md / 素材页 / 界面共用同一口径）。"""
    return ORIGIN_LABELS.get(origin_of(row), ORIGIN_LABELS[ORIGIN_UPLOAD])


def cleaning_info(row: SourceMaterial) -> dict:
    """**已清理**信息 ``{"lines": N, "rules": [...]}``（只有「必须干净」的素材会被剥）。

    用户 2026-09-21 口径：上传原件**保持原样** —— 这里返回 0，检测结果看
    :func:`detection_info`，要不要清由老师点「一键清理」（:func:`clean_material`）。
    AI 补充 / 粘贴件必须干净：入库时剥过（``meta.shell_stripped``）就读 ``meta``；
    **老数据在这里现算**（不写库、不改正文，首次读取由 :func:`backfill_material_meta` 回填）。

    ⚠️ 任务 72 §B1：这里的 ``lines`` 只说明「**出库口径**会剥几行」，**不等于库里的正文已经改过**。
    正文到底有没有落库，看 ``meta.shell_stripped``（接口字段 ``shell_stripped``，见
    ``routes/material.py`` 的 ``_out``）—— 界面/交付页据此分三种说法：
    · ``shell_stripped=True`` ⇒ 「已清理 N 行（库内正文已更新）」；
    · ``must_clean`` 且 ``shell_stripped=False``（老数据）⇒ 「出库时会剥离 N 行（库内保留原文）」；
    · 上传原件（``must_clean=False``）⇒ 「检测到 N 行…（按口径保持原样）」，见 :func:`detection_info`。
    """
    # 上传原件**没清过**才返回 0；清过（老师点了一键清理，``shell_stripped=True``）就要如实报行数，
    # 否则界面会出现「点完清理反而看不到已清理 N 行」（实测踩过）。
    if not must_clean(row) and not (row.meta or {}).get("shell_stripped"):
        return {"lines": 0, "rules": []}
    meta = row.meta or {}
    if not meta.get("shell_stripped"):
        _, removed, rules = strip_shell(row.text or "")
        return {"lines": removed, "rules": rules}
    return {"lines": int(meta.get("cleaned_lines") or 0),
            "rules": list(meta.get("cleaned_rules") or [])}


def detection_info(row: SourceMaterial) -> dict:
    """**检测到、但按口径没清理**的外壳 ``{"lines": N, "rules": [...]}``（上传原件用）。

    ``lines == 0`` 但 ``rules`` 非空 ⇒ 是「剥不了」的情形（如「剥完会清空正文,已保留原样」），
    界面只提示原因、不硬改。
    """
    if must_clean(row):
        return {"lines": 0, "rules": []}
    meta = row.meta or {}
    if meta.get("detected_lines") is None:
        _, removed, rules = strip_shell(row.text or "")
        return {"lines": removed, "rules": rules}
    return {"lines": int(meta.get("detected_lines") or 0),
            "rules": list(meta.get("detected_rules") or [])}


def material_text(row: SourceMaterial) -> str:
    """素材的**卫生版正文**（往外拿的统一口径：注入 / 段落编号 / 素材页导出）。

    · **必须干净的**（AI 补充 / 粘贴件）：``meta.shell_stripped=True``（入库时剥过）无操作；
      **老数据就地剥** —— **不改库里的原文**（规格 §B1.5：绝不静默改内容）；
    · **上传原件**（用户 2026-09-21 口径：可以接受）：**原样返回** —— 页眉页脚只在素材行 /
      素材页提示（:func:`detection_info`），清不清由老师点「一键清理」。

    注入口径（注入 / 段落编号 / 导出素材页）都走这一个函数，口径才一致；
    **「看解析全文」接口故意仍返回库里的原文** —— 老师要能核对原件解析出了什么。

    ⚠️ 判据必须是 ``shell_stripped`` 而不是「meta 里有 cleaned_lines」：老数据回填时只记了
    行数、正文一个字没动 ⇒ 用后者会把「记了 3 行、正文里壳还在」当成已剥（实测踩过）。
    """
    if not must_clean(row):
        return row.text or ""
    if (row.meta or {}).get("shell_stripped"):
        return row.text or ""
    return strip_shell(row.text or "")[0]


def clean_material(db: Session, material_id: str) -> tuple[SourceMaterial, int]:
    """「一键清理」：把页眉页脚 / 对话外壳剥掉并写回正文（老师点按钮才跑）。

    返回 ``(row, 清掉的行数)``。剥完会清空正文时 :func:`strip_shell` 自己会放弃
    （``0`` 行 + 一条原因）⇒ 这里**不动正文**，只把原因记进 ``meta``：绝不因为启发式吃掉原件。

    适用对象（任务 72 §B1）：① 上传原件（默认保持原样，清了才算落库）；② **老数据**里
    ``shell_stripped`` 还没写上的必须干净的素材（AI 补充 / 粘贴件）—— 界面在「出库时会剥离
    N 行（库内保留原文）」那一态也放这个按钮，点它才把剥离结果落库。入库时已剥过的
    （``shell_stripped=True``）一律拒绝：再剥一次只会把「已清理 N 行」的记录抹成 0。
    """
    row = db.get(SourceMaterial, material_id)
    if row is None:
        raise ValueError("素材不存在")
    # 已清理过的一律不许二次清理：正文里已经没有壳，再跑一次会把 meta 里
    # 「已清理 N 行」的记录抹成 0（实测踩过）；入库即剥的 AI 补充 / 粘贴件走的也是这一条。
    if (row.meta or {}).get("shell_stripped"):
        raise ValueError("这份素材已经清理过，不需要二次清理")
    cleaned, removed, rules = strip_shell(row.text or "")
    meta = dict(row.meta or {})
    meta["detected_lines"] = removed
    meta["detected_rules"] = rules
    meta["cleaned_lines"] = removed
    meta["cleaned_rules"] = rules
    meta["shell_stripped"] = True
    if removed and cleaned:
        row.text = cleaned
        meta["chars"] = len(cleaned)
    row.meta = meta
    db.commit()
    db.refresh(row)
    return row, removed


def anchor_kind(row: SourceMaterial) -> str:
    """这条素材的来源锚点口径：``"stamp"``（时间戳）/ ``"para"``（第 N 段）。

    任务 105（用户 2026-09-27 拍板）：**音视频素材（且有转写正文）**的引用写时间戳
    ``（来源:《录像名》06:47）`` —— 读讲义的人能直接跳到视频那一刻，不必先在素材页
    数到第 N 段。其余一律照旧写「第 N 段」：文本 / 文档 / 图片素材、AI 补充素材，
    以及**转写失败或还没转写**的音视频（没有正文就没有 ``[mm:ss]`` 可锚）。
    """
    if str(row.kind or "") in AV_KINDS and str(material_text(row) or "").strip():
        return ANCHOR_STAMP
    return ANCHOR_PARA


# 「有来处」的解析正则 **与讲义侧同一口径**（``lecture_qc._CITE_RE``）：讲义里的
# 「（来源:《素材名》第 N 段）」能不能解析回素材、能不能对上段落，全靠它。
# 认两种写法：新（带《》/第 N 段）与旧（``（来源: 素材名 #33）``）；AI 补充的标题
# 带 ``AI补充——`` 前缀（前缀在《》外，见 lecture.py 的 🅰 段）。
# 任务 105：**讲义侧**（``lecture_qc._CITE_RE``）另认时间戳锚点（音视频素材）—— 这里
# 只统计「AI 补充素材正文」里的来处，而 AI 补充素材本身是文本素材（kind=text），
# 锚点一律是「第 N 段」，故不加 stamp 分支（界面按「第 N 段」显示才能一键跳段核对）。
GROUNDED_RE = re.compile(
    r"[(（]\s*来源\s*[:：]\s*(?P<name>[^#)）]+?)\s*(?:#\s*|第\s*)(?P<para>\d+)\s*(?:段)?\s*[)）]")
_SENTENCE_CUTS = "。！？!?；;\n"


def _claim_before(text: str, end: int, *, limit: int = 200) -> str:
    """标注前面那一句「事实性表述」（从最近的句末符 / 换行切到标注前）。"""
    head = text[:max(0, end)]
    cut = max(head.rfind(ch) for ch in _SENTENCE_CUTS)
    claim = _strip_bullet(head[cut + 1:])
    if not claim:                      # 标注顶在行首 ⇒ 退一步用上一行
        lines = [line for line in head.splitlines() if line.strip()]
        claim = _strip_bullet(lines[-1]) if lines else ""
    return claim[-limit:].strip()


def _strip_bullet(line: str) -> str:
    """去掉列表符号 / 编号 / 强调符，只留句子本体。"""
    return re.sub(r"^[\s>*+\-–—·\d.、)）]+", "", str(line or "")).replace("**", "").strip()


def parse_grounding(text: str) -> dict:
    """AI 补充正文 → 「有来处 / 无来处」清单（任务 69 §B1.5.3）。

    只认提示词里写死的两种写法（与 ``MATERIAL_SYSTEM`` 逐字对齐）：
    · 有来处：``（来源:《素材名》第 N 段）``；引用 AI 补充素材时写成
      ``（来源:AI补充——《素材名》第 N 段）``（前缀在《》外，与讲义侧同口径）
    · 无来处：``(无依据·模型补充)``
    """
    body = str(text or "")
    grounded: list[dict] = []
    for match in GROUNDED_RE.finditer(body):
        name = match.group("name").strip().strip("《》【】\"'").strip()
        is_ai = name.startswith(AI_TITLE_PREFIX)
        grounded.append({
            "claim": _claim_before(body, match.start()),
            "material": name[len(AI_TITLE_PREFIX):].strip() if is_ai else name,
            "para": int(match.group("para")),
            "ai": is_ai,
        })
    ungrounded = [_claim_before(body, m.start()) for m in _UNGROUNDED_RE.finditer(body)]
    return {"grounded": grounded, "ungrounded": ungrounded,
            "grounded_count": len(grounded), "ungrounded_count": len(ungrounded)}


def grounding_of(row: SourceMaterial) -> dict:
    """素材的「有来处」记录（AI 补充才有；老数据 meta 里没记就现算，不写库）。"""
    meta = row.meta or {}
    data = meta.get(GROUNDED_KEY)
    if isinstance(data, dict):
        grounded = [item for item in (data.get("grounded") or []) if isinstance(item, dict)]
        ungrounded = [str(item) for item in (data.get("ungrounded") or [])]
        return {"grounded": grounded, "ungrounded": ungrounded,
                "grounded_count": len(grounded), "ungrounded_count": len(ungrounded)}
    if str(meta.get("source") or "") != AI_SOURCE:
        return {"grounded": [], "ungrounded": [], "grounded_count": 0, "ungrounded_count": 0}
    return parse_grounding(row.text or "")


def backfill_material_meta(db: Session, rows: list[SourceMaterial]) -> int:
    """老数据回填 ``meta.pages`` / ``meta.origin`` / 外壳行数（只补缺失项，**不动正文**）。

    规格 §B1.5.2：页数原先每次读原件现算（原件丢了就显示「—」，交付包
    ``素材清单.md`` 出现过「21 页 → —」）⇒ 首次读取时落库。返回被回填的行数。

    外壳行数按来源分档回填：**必须干净的**（AI 补充 / 粘贴件）回填 ``cleaned_lines``，
    **上传原件**回填 ``detected_lines``（用户 2026-09-21 口径：只提示、不强制清理）。

    ⚠️ 任务 72 §B2：这是**写库**操作，只由**显式入口**调用
    （``POST /api/material/{course_id}/backfill``，素材页「补齐素材信息」按钮；
    新数据在入库时就算好了，见 :func:`add_material_file` / :func:`add_material_text` /
    :func:`generate_materials`）—— **``GET /api/material/{course_id}`` 里不再调它**，
    否则一次「打开素材页」就会悄悄改动真库（实测 2026-09-21 13:29:04 破过「GET 不改状态」）。
    执行时写一条 INFO 日志，便于事后回查是哪次操作改了库。
    """
    changed = 0
    for row in rows:
        meta = dict(row.meta or {})
        dirty = False
        if meta.get("pages") is None:
            pages = pages_of(row)
            if pages is not None:
                meta["pages"] = pages
                dirty = True
        if meta.get("origin") is None:
            meta["origin"] = origin_of(row)
            dirty = True
        if must_clean(row):
            if meta.get("cleaned_lines") is None:
                _, removed, rules = strip_shell(row.text or "")
                meta["cleaned_lines"] = removed
                meta["cleaned_rules"] = rules
                # 明确记下「正文没改，只是现算出来的行数」⇒ material_text 才会在往外拿时就地剥
                meta["shell_stripped"] = False
                dirty = True
        elif meta.get("detected_lines") is None:
            _, removed, rules = strip_shell(row.text or "")
            meta["detected_lines"] = removed
            meta["detected_rules"] = rules
            dirty = True
        if dirty:
            row.meta = meta
            changed += 1
    if changed:
        db.commit()
        logger.info("补齐素材信息：回填 %d/%d 份的 pages / origin / 外壳行数（只写 meta，正文未动）",
                    changed, len(rows))
    return changed


def split_paragraphs(text: str) -> list[str]:
    """素材正文 → 段落列表（空行或单换行都算分段，空段丢弃）。

    提示词与来源锚定解析共用：提示词里标的 ``[序号]`` 必须能回查到同一段
    （任务 22-B 的来源锚定靠这个口径对齐）。
    """
    body = re.sub(r"\r\n?", "\n", str(text or ""))
    out: list[str] = []
    for chunk in re.split(r"\n\s*\n|\n", body):
        para = chunk.strip()
        if para:
            out.append(para)
    return out


def numbered_paragraphs(text: str, *, max_chars: int | None = MAX_INJECT_CHARS) -> list[str]:
    """素材正文 → 带 ``[N]`` 序号的行（**导出素材页**与文本素材的提示词**共用同一口径**）。

    序号就是 :func:`split_paragraphs` 的解析顺序；提示词里让模型引用的 ``[N]`` 与
    导出素材页里读者能定位的 ``[N]`` 必须是同一个数 —— 否则讲义里的
    「（来源:《X》第 N 段）」在素材页按 ``[N]`` 找不到对应段落（任务 57）。

    ``max_chars=None`` 表示不截断（导出素材页要看全文）。截断只影响末尾，
    前面各段的序号与提示词一致。

    任务 69：注入侧改成**按段落取舍**（见 :func:`plan_injection`），但这里的口径一个字没变 ——
    被选中的段落保留它在**全篇里的原序号**，所以「第 N 段」在素材页仍按同一个 ``[N]`` 定位。

    任务 105：音视频素材（有转写正文）**注入提示词时不再加** ``[N]``（它每段本来就以
    ``[mm:ss]`` 开头，见 :func:`_material_body` / :func:`anchor_kind`）；**导出素材页仍保留**
     ``[N]`` —— 旧讲义里的「第 N 段」照旧能在同一页上定位（历史数据不受影响）。
    """
    body = str(text or "").strip()
    if max_chars is not None:
        body = body[:max_chars]
    return [f"[{index}] {para}" for index, para in enumerate(split_paragraphs(body), 1)]


_RE_PARA_STAMP = re.compile(r"^\[\s*(?P<stamp>\d{1,3}:\d{2}(?::\d{2})?)\s*\]")


def paragraph_stamp(para: str) -> str:
    """单段段首的 ``[mm:ss]`` 时间戳（没有则空串）—— 音视频素材的段落锚点就是它。"""
    match = _RE_PARA_STAMP.match(str(para or "").strip())
    return match.group("stamp") if match else ""


def stamp_seconds(value: str) -> int | None:
    """``06:47`` / ``105:03`` / ``1:02:03`` → 秒；不是时间戳返回 ``None``。

    口径与 :func:`_asr_stamp` 对齐（超 1 小时分钟位是三位），另外兼容 ``h:mm:ss``。
    锚点只按**秒**比对 ⇒ 模型写成 ``6:47`` 也能落回 ``[06:47]`` 那一段（任务 105）。
    """
    parts = str(value or "").strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return numbers[0] * 60 + numbers[1]


def cite_title(row: SourceMaterial) -> str:
    """引用时用的素材名：AI 补充素材带 ``AI补充——`` 前缀（任务 69 §B2.3）。

    讲义正文里写「（来源:AI补充——《X》第 N 段）」，读者一眼就能看出**这条依据本身是
    模型生成的**；教师素材不加任何前缀，口径与任务 57 完全一致。
    """
    title = str(row.title or "未命名素材")
    return f"{AI_TITLE_PREFIX}{title}" if is_ai_material(row) else title


def is_ai_material(row: SourceMaterial) -> bool:
    """是不是「AI 补充资料」（``POST /api/material/generate`` 生成，``meta.source="ai"``）。"""
    return (row.meta or {}).get("source") == AI_SOURCE


def injection_budget(scope: str) -> int:
    """该生成场景的注入总预算（字符）：大纲 60k / 讲义 80k / 其它 40k。"""
    return INJECT_BUDGET_BY_SCOPE.get(str(scope or ""), INJECT_BUDGET_MISC)


def material_catalog(db: Session, course: Course, *, limit: int | None = None,
                     max_chars: int | None = None) -> list[dict]:
    """素材目录：编号 + 标题 + 分段正文（注入提示词与来源解析**同一个口径**）。

    编号 = 课程素材的先后顺序（与导出笔记的 ``NN-标题.md`` 一致）；``title`` 走
    :func:`cite_title`（AI 补充带前缀）⇒ 模型按前缀写出的引用能解析回素材。

    任务 69 起默认**全量 + 全文**：注入侧不再砍份数、不再砍尾部，来源解析必须能看到
    同样的段落，否则被注入的「第 N 段」回查不到（任务 57 的可核对性会断）。
    ``limit`` / ``max_chars`` 保留为显式覆盖（老调用方与测试可继续传）。
    """
    out: list[dict] = []
    rows = list_materials(db, course.id)
    if limit is not None:
        rows = rows[:limit]
    for index, row in enumerate(rows, 1):
        body = material_text(row).strip()
        if max_chars is not None:
            body = body[:max_chars]
        if not body:
            continue
        out.append({
            "index": index,
            "id": row.id,
            "title": cite_title(row),
            "paragraphs": split_paragraphs(body),
            "source": (row.meta or {}).get("source") or "",
        })
    return out


_RE_MATCH_NOISE = re.compile(r"[\s\W_]+")


def _match_key(text: str) -> str:
    """词面匹配用的归一化：去空白与标点、转小写（与覆盖自检同思路：只做字面比对）。"""
    return _RE_MATCH_NOISE.sub("", str(text or "")).lower()


def _bigrams(text: str) -> set[str]:
    key = _match_key(text)
    if len(key) < 2:
        return {key} if key else set()
    return {key[i:i + 2] for i in range(len(key) - 1)}


def relevance_score(text: str, targets: list[str]) -> float:
    """素材（标题 + 正文）对一组目标（章节标题 / 课程目标）的**词面**相关度。

    目标短语整体出现在素材里 ⇒ 3 分；否则按二元组重合率给 0~1 分。
    刻意不做语义判断 —— 与覆盖自检一个口径（措辞不同但意思相同的可能仍算不相关，
    所以相关性只用来**排序与取舍**，不用来下结论）。
    """
    targets = [str(t).strip() for t in (targets or []) if str(t).strip()]
    if not targets:
        return 0.0
    norm = _match_key(text)
    grams = _bigrams(text)
    score = 0.0
    for target in targets:
        key = _match_key(target)
        if len(key) < 2:
            continue
        if key in norm:
            score += 3.0
            continue
        want = _bigrams(target)
        if want:
            score += len(want & grams) / len(want)
    return score


def _pick_paragraphs(paras: list[str], *, allow: int, targets: list[str]) -> list[int]:
    """在 ``allow`` 字以内挑要注入的段落下标（**保序**返回）。

    先留开头 ``INJECT_HEAD_PARAGRAPHS`` 段（标题/总览），其余按与目标的词面相关度
    从高到低补，直到预算用满 —— 超预算时按相关性取舍，而不是从尾巴一刀切。
    允许总量略超 ``allow`` 的只有一种情况：连第一段都放不下（否则会把这份素材整个丢掉）。
    """
    if not paras:
        return []
    picked: list[int] = []
    used = 0
    for index in range(min(INJECT_HEAD_PARAGRAPHS, len(paras))):
        if not picked or used + len(paras[index]) <= allow:
            picked.append(index)
            used += len(paras[index])
    taken = set(picked)
    rest = [index for index in range(len(paras)) if index not in taken]
    for index in sorted(rest, key=lambda i: (-relevance_score(paras[i], targets), i)):
        if used + len(paras[index]) <= allow:
            picked.append(index)
            used += len(paras[index])
    return sorted(picked)


def _allocate(rows: list[SourceMaterial], *, budget: int) -> dict[str, int]:
    """按素材长度**比例**分配预算，余量再补给仍被截断的素材（最多三轮）。"""
    lengths = {row.id: len(material_text(row)) for row in rows}
    allow = {row.id: 0 for row in rows}
    need = dict(lengths)
    left = int(budget)
    for _ in range(3):
        want = sum(need.values())
        if want <= left:
            for key, value in need.items():
                allow[key] += value
            break
        granted = 0
        for key, value in list(need.items()):
            share = min(value, max(1, int(left * value / want))) if value else 0
            allow[key] += share
            need[key] = value - share
            granted += share
        left -= granted
        if granted <= 0 or left <= 0:
            break
    return allow


def _material_body(paras: list[str], picks: list[int], *, numbered: bool,
                   anchor: str = ANCHOR_PARA) -> str:
    """被选中的段落拼成的一段正文（``numbered`` 时每段带**全篇**序号 ``[N]``，保序）。

    任务 105：``anchor="stamp"`` 的素材（音视频，有转写正文）**不加** ``[N]`` —— 它本来
    就以 ``[mm:ss]`` 开头，再叠一个序号只会让读者更绕；模型要照抄的正是那个时间戳。
    """
    if numbered and anchor != ANCHOR_STAMP:
        return "\n".join(f"[{i + 1}] {paras[i]}" for i in picks)
    return "\n\n".join(paras[i] for i in picks)


def anchor_notice(rows: list[SourceMaterial]) -> str:
    """给模型看的「来源标注怎么写」——措辞按**本次真正注入的素材**给（任务 105）。

    只有文本 / 文档 / 图片素材 ⇒ 只写「第 N 段」；只有音视频素材（有转写正文）⇒ 只写
    时间戳；两者都有 ⇒ 两句都写（同一门课两种口径并存）。标题一律照抄素材标题行。
    """
    kinds = {anchor_kind(row) for row in rows}
    notes: list[str] = []
    if ANCHOR_PARA in kinds:
        notes.append("文本素材写「（来源:《素材标题》第 N 段）」，N 就是该段前的 [序号]；"
                     "AI 补充素材必须写成「（来源:AI补充——《素材标题》第 N 段）」。")
    if ANCHOR_STAMP in kinds:
        notes.append("音视频素材的每段行首是 [mm:ss] ⇒ 引用它时**照抄该段段首方括号里的时间戳**"
                     "（如「（来源:《素材标题》06:47）」；超过 1 小时是 105:03 这种三位分钟），"
                     "不要写成第 N 段；AI 补充素材写成「（来源:AI补充——《素材标题》06:47）」。")
    if not notes:
        return ""
    return "\n（来源标注怎么写：标题照抄上文每份素材的标题行；" + "".join(notes) + "）"


def _material_block(row: SourceMaterial, index: int, body: str) -> str:
    """一份素材在提示词里的呈现（标题行 + 正文）。"""
    # 括号里写**来源类型**（人工上传 / 粘贴文本(疑似 AI 产物) / AI 补充）：模型一眼能看出
    # 这份依据是谁给的（任务 69 §B1.5.3 的「有来处」也要求它按标题回引）。
    head = (f"\n--- 素材 {index}：《{cite_title(row)}》"
            f"（{row.meta.get('original') or origin_label(row)}）---")
    return f"{head}\n{body}"


def injection_notice(plan: dict) -> str:
    """注入情况的**人话提示**（接口与界面共用；超限绝不静默）。"""
    if not plan.get("total_materials"):
        return "本课程还没有素材：本次生成没有注入任何素材。"
    bits = [f"本次用了 {plan['used_count']}/{plan['total_materials']} 份素材，"
            f"共注入 {plan['injected_chars']:,} 字（原始 {plan['original_chars']:,} 字）"]
    # 「被取舍掉了」的判据是**段落数**（injected_chars 与 original_chars 口径不同：
    # 前者是实际拼进去的正文含 [N] 与段间分隔，后者是库里的原文含换行 ⇒ 不能直接比大小，
    # 否则每份都会显示成「超过预算、已按段落取舍」——实测踩过这个假警报）。
    truncated = [u for u in plan["used"] if u["paragraphs_injected"] < u["paragraphs_total"]]
    if truncated:
        shown = "、".join(f"《{u['title']}》{u['paragraphs_injected']}/{u['paragraphs_total']} 段"
                          f"（{u['injected_chars']:,}/{u['original_chars']:,} 字）"
                          for u in truncated[:4])
        bits.append(f"其中 {len(truncated)} 份超过预算、已按段落取舍：{shown}")
    if plan["unused"]:
        shown = "、".join(f"《{u['title']}》" for u in plan["unused"][:8])
        more = f" 等 {len(plan['unused'])} 份" if len(plan["unused"]) > 8 else ""
        bits.append(f"未参与：{shown}{more}")
    if not truncated and not plan["unused"]:
        bits.append("全部素材都已注入")
    return "；".join(bits) + "。"


def plan_injection(db: Session, course: Course, *, scope: str = "outline",
                   targets: list[str] | None = None, numbered: bool = False,
                   budget: int | None = None) -> dict:
    """算这一次注入「用哪些素材、每份注入多少字」→ 注入文本 + 未参与清单 + 字数统计。

    纯计算 + 只读（**不写库**；落库见 :func:`record_injection`）。规则（任务 69 §B1）：

    ① **选材按相关性**——不再「永远取前 6 份」；并且保证**每个 target（章节标题）至少有一份**
       相关素材入选，这样每一章都有素材可依；
    ② **长素材按段落注入**——不再硬切前 2,500 字；预算按长度比例分摊，单份素材内部
       留头段 + 按相关性挑高分段（段序保持原文）；
    ③ **超预算按相关性取舍**（相关性高的先拿份额），取舍结果写进 ``notice`` / ``unused``，
       接口与界面都会显示 —— 绝不静默丢弃。
    """
    targets = [str(t).strip() for t in (targets or []) if str(t).strip()]
    budget = int(budget if budget is not None else injection_budget(scope))
    rows = [row for row in list_materials(db, course.id) if material_text(row).strip()]
    base = {"text": "", "used": [], "unused": [], "notice": "", "injected_chars": 0,
            "original_chars": 0, "used_count": 0, "total_materials": len(rows),
            "budget": budget, "scope": scope, "targets": targets}
    if not rows:
        base["notice"] = injection_notice(base)
        return base

    texts = {row.id: material_text(row) for row in rows}
    order = {row.id: index for index, row in enumerate(rows, 1)}
    scores = {row.id: relevance_score(f"{row.title} {texts[row.id]}", targets) for row in rows}

    # ① 每个 target 至少一份最相关的素材入选（「每一章都有相关素材进入」）
    chosen: list[SourceMaterial] = []
    for target in targets:
        ranked_for_target = sorted(
            (row for row in rows if row not in chosen),
            key=lambda row: (-relevance_score(f"{row.title} {texts[row.id]}", [target]),
                             order[row.id]))
        if ranked_for_target and relevance_score(
                f"{ranked_for_target[0].title} {texts[ranked_for_target[0].id]}", [target]) > 0:
            chosen.append(ranked_for_target[0])
    # ② 其余按整组相关性补进来：只有当「摊到每份还够用」时才继续收
    ranked = sorted((row for row in rows if row not in chosen),
                    key=lambda row: (-scores[row.id], order[row.id]))
    for row in ranked:
        if budget / (len(chosen) + 1) < MIN_INJECT_PER_MATERIAL:
            break
        chosen.append(row)

    allow = _allocate(chosen, budget=budget)
    used: list[dict] = []
    unused: list[dict] = []
    for row in sorted(chosen, key=lambda r: order[r.id]):
        text = texts[row.id]
        paras = split_paragraphs(text)
        share = allow.get(row.id, 0)
        full = len(text) <= share
        if not full and share < min(MIN_INJECT_PER_MATERIAL, len(text)):
            unused.append({"id": row.id, "title": cite_title(row), "original_chars": len(text),
                           "reason": f"预算不足,按相关性取舍(只排到 {share} 字)"})
            continue
        picks = _pick_paragraphs(paras, allow=share, targets=targets)
        body = _material_body(paras, picks, numbered=numbered, anchor=anchor_kind(row))
        used.append({"id": row.id, "title": cite_title(row), "index": order[row.id],
                     "source": (row.meta or {}).get("source") or "",
                     # 「注入多少字」= 被选中段落的**净正文字数**（不含系统加的换行分隔与
                     # ``[N] `` 序号 —— 那些是排版不是素材内容）⇒ 永远 ≤ original_chars
                     "injected_chars": sum(len(paras[i]) for i in picks),
                     "original_chars": len(text),
                     "paragraphs_total": len(paras), "paragraphs_injected": len(picks)})
        used[-1]["_row"] = row
        used[-1]["_body"] = body
    if not chosen:
        base["unused"] = [{"id": row.id, "title": cite_title(row),
                           "original_chars": len(texts[row.id]),
                           "reason": "预算不足,按相关性取舍"} for row in rows]
        base["original_chars"] = sum(len(texts[row.id]) for row in rows)
        base["notice"] = injection_notice(base)
        return base

    # ② 里被预算挡在门外、压根没进 chosen 的素材，也必须进「未参与」清单 ——
    # 规格 §B1.3「超限绝不静默」：少了这一段会出现「用了 5/12 份、未参与却是空」。
    chosen_ids = {row.id for row in chosen}
    for row in rows:
        if row.id not in chosen_ids:
            unused.append({"id": row.id, "title": cite_title(row),
                           "original_chars": len(texts[row.id]),
                           "reason": "预算不足,按相关性未入选(本轮未参与)"})

    teacher = [item for item in used if item["source"] != AI_SOURCE]
    ai = [item for item in used if item["source"] == AI_SOURCE]
    parts: list[str] = []
    if teacher:
        parts.append(f"\n\n{TEACHER_SECTION_TITLE}")
        parts += [_material_block(item["_row"], item["index"], item["_body"]) for item in teacher]
    if ai:
        parts.append(f"\n{AI_SECTION_TITLE}")
        parts.append("（下面这些是模型生成的补充参考，不是人工提供的素材：可以参考它的"
                     "**结构与表述**，但**不得当作事实依据** —— 凡是只有它支撑的事实、数字、"
                     "案例、结论，一律写成「（无直接依据,建议人工确认）」。"
                     "若某段本身标着「" + UNGROUNDED_MARK + "」（模型自己都说没来处），"
                     "它撑起来的句子更要写成「（无直接依据,建议人工确认）」。）")
        parts += [_material_block(item["_row"], item["index"], item["_body"]) for item in ai]
    if numbered and used:
        # 任务 105：措辞按**本次真正注入的素材**给（注入里真有音视频素材才提时间戳口径）。
        parts.append(anchor_notice([item["_row"] for item in used]))

    plan = {
        "text": "\n".join(parts),
        "used": [{key: value for key, value in item.items() if not key.startswith("_")}
                 for item in used],
        "unused": unused,
        "injected_chars": sum(item["injected_chars"] for item in used),
        "original_chars": sum(len(texts[row.id]) for row in rows),
        "used_count": len(used),
        "total_materials": len(rows),
        "budget": budget,
        "scope": scope,
        "targets": targets,
    }
    plan["notice"] = injection_notice(plan)
    return plan


def record_injection(db: Session, course: Course, plan: dict) -> None:
    """把本次注入情况落库到 ``course.meta[MATERIAL_INJECT_KEY]``（**不 commit**，由调用方提交）。

    规格 §B1.4：交付包 / 体检报告要能查到「本次实际用了哪些素材、各注入多少字、原始多少字」。
    与任务 70 的自检落库放在同一处（``course.meta``）。
    """
    course.meta = {**(course.meta or {}), MATERIAL_INJECT_KEY: {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scope": plan.get("scope") or "",
        "budget": plan.get("budget") or 0,
        "used": plan.get("used") or [],
        "unused": plan.get("unused") or [],
        "injected_chars": plan.get("injected_chars") or 0,
        "original_chars": plan.get("original_chars") or 0,
        "used_count": plan.get("used_count") or 0,
        "total_materials": plan.get("total_materials") or 0,
        "notice": plan.get("notice") or "",
    }}


def stored_injection(course: Course) -> dict | None:
    """上一次注入的落库记录（没有则 ``None``）。"""
    value = (course.meta or {}).get(MATERIAL_INJECT_KEY)
    return value if isinstance(value, dict) else None


def inject_material(db: Session, course: Course, *, scope: str = "outline",
                    targets: list[str] | None = None, numbered: bool = False) -> str:
    """生成链路入口：算注入计划 → **落库本次用量** → 返回注入文本（无素材返回空串）。"""
    plan = plan_injection(db, course, scope=scope, targets=targets, numbered=numbered)
    record_injection(db, course, plan)
    return plan["text"]


def material_prompt_section(db: Session, course: Course, *, numbered: bool = False,
                            scope: str = "outline",
                            targets: list[str] | None = None) -> str:
    """把课程素材拼成提示词片段（任务 69：按相关性选材 + 分段注入 + 总预算；无素材返回空串）。

    兼容老调用方（只要文本）；需要「未参与清单 / 字数」时用 :func:`plan_injection`。
    """
    return plan_injection(db, course, scope=scope, targets=targets, numbered=numbered)["text"]


def generate_materials(db: Session, course: Course, directions: list[str],
                       custom_instruction: str = "") -> list[SourceMaterial]:
    """按用户选择的方向调用 LLM 补充备课素材；结果标记 source=ai 供编辑/删除。"""
    from app.services.llm import build_llm_client

    directions = [d.strip() for d in directions if d and d.strip()]
    if not directions:
        raise ValueError("请至少选择一个补充方向")
    labels: list[str] = []
    for key in directions:
        label = DIRECTION_LABELS.get(key)
        if label is None:
            raise ValueError(f"不支持的补充方向：{key}")
        if label not in labels:
            labels.append(label)

    client = build_llm_client()
    user = (
        f"课程《{course.title}》\n"
        f"学科：{course.subject}\n受众：{course.audience}\n预计时长：{course.duration_minutes} 分钟\n"
        # 任务 69 §B1.5.3：**必须带 `[N]` 段落号** —— 否则模型没法写「第 N 段」，
        #   也就没法给「有来处」；口径与讲义侧 material_prompt_section(numbered=True) 同一函数。
        + material_prompt_section(db, course, numbered=True, targets=[course.title])
        + f"\n\n请为以下方向各补充一份备课素材：{'、'.join(labels)}"
        + "\n每条事实性表述都要跟一个来处（锚点写法照抄上文「来源标注怎么写」那一行，"
          "标题照抄素材标题行）；给不出来处的一律写 `" + UNGROUNDED_MARK + "`，不许编。"
    )
    custom = (custom_instruction or "").strip()[:500]
    if custom:
        user += f"\n额外要求（教师指定）：{custom}"

    data = usage_log.complete_json(client, MATERIAL_SYSTEM, user,
                                   max_tokens=4000, purpose="material")
    rows: list[SourceMaterial] = []
    for item in (data.get("materials") or [])[:8]:
        label = str(item.get("direction") or "").strip()
        if label not in labels:
            continue
        text = str(item.get("text") or "").strip()[:MAX_TEXT_STORE]
        if not text:
            continue
        title = str(item.get("title") or "").strip()[:100] or f"{label}补充素材"
        # 任务 69 §B1.5.1：AI 补充素材**必须干净** ⇒ 入库即剥对话外壳，并记「已清理 N 行」
        text, cleaned_lines, cleaned_rules = strip_shell(text)
        if not text.strip():
            continue
        # 任务 69 §B1.5.3：把「有来处 / 无来处」解析出来落库（grounded / ungrounded_count）
        grounding = parse_grounding(text)
        row = SourceMaterial(
            course_id=course.id,
            title=title,
            kind="text",
            file_rel="",
            text=text,
            meta={"ext": ".text", "chars": len(text), "original": "",
                  "source": AI_SOURCE, "origin": ORIGIN_AI, "direction": label,
                  "cleaned_lines": cleaned_lines, "cleaned_rules": cleaned_rules,
                  "shell_stripped": True,
                  "grounded_count": grounding["grounded_count"],
                  "ungrounded_count": grounding["ungrounded_count"],
                  GROUNDED_KEY: {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "grounded": grounding["grounded"],
                                 "ungrounded": grounding["ungrounded"]}},
        )
        db.add(row)
        rows.append(row)
    if not rows:
        raise ValueError("AI 未能生成有效素材，请换个方向或稍后重试")
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def update_material_text(db: Session, material_id: str, title: str | None = None,
                         text: str | None = None) -> SourceMaterial:
    """编辑文本素材（仅限 kind=text；文件素材不可直接改正文）。"""
    row = db.get(SourceMaterial, material_id)
    if row is None:
        raise ValueError("素材不存在")
    if row.kind != "text":
        raise ValueError("只有文本素材支持编辑，请删除后重新粘贴")
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("标题不能为空")
        row.title = title[:100]
    if text is not None:
        text = text.strip()
        if not text:
            raise ValueError("正文不能为空")
        row.text = text[:MAX_TEXT_STORE]
    # 教师手工编辑过 ⇒ 记 ``cleaned_lines=0``：这份正文不再参与「入库卫生」的自动剥离
    # （免得把老师自己写的行当成外壳删掉）
    row.meta = {**(row.meta or {}), "chars": len(row.text or ""),
                "cleaned_lines": 0, "cleaned_rules": ["教师手工编辑,不再自动清理"],
                "shell_stripped": True}
    db.commit()
    db.refresh(row)
    return row


def get_material_text(db: Session, material_id: str) -> SourceMaterial:
    """读取文本素材全文（供编辑弹窗使用）。"""
    row = db.get(SourceMaterial, material_id)
    if row is None:
        raise ValueError("素材不存在")
    if row.kind != "text":
        raise ValueError("只有文本素材支持读取全文")
    return row


def page_count(filename: str, data: bytes) -> int | None:
    """文件型素材的「页数」：pptx = 幻灯片数、pdf = 页数；其余返回 None。

    页数只是展示信息，解析失败不应影响上传与列表。
    """
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".pptx":
            from pptx import Presentation

            return len(Presentation(io.BytesIO(data)).slides)
        if ext == ".pdf":
            from pypdf import PdfReader

            return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001
        return None
    return None


def pages_of(row: SourceMaterial) -> int | None:
    """取素材页数：优先用入库时记下的 meta.pages；老数据就地读原件现算（只读）。"""
    meta = row.meta or {}
    if str(meta.get("ext") or "").lower() in AV_EXT:
        return None      # 任务 102：音视频没有「页」的概念，也别去读 2GB 原件的字节
    if meta.get("pages") is not None:
        try:
            return int(meta["pages"])
        except (TypeError, ValueError):
            return None
    if not row.file_rel:
        return None
    path = get_settings().storage_dir / row.file_rel
    if not path.exists():
        return None
    try:
        return page_count(str(meta.get("original") or path.name), path.read_bytes())
    except OSError:
        return None


def get_material_full(db: Session, material_id: str, *, offset: int = 0,
                      limit: int = FULL_TEXT_DEFAULT_LIMIT,
                      numbered: bool = False) -> dict:
    """任意素材的解析全文（含文件型），分页返回。

    P2-1：原先只有纯文本素材能读全文（``get_material_text`` 对 file kind 报 404），
    老师看不到 PPT/PDF 到底解析出了什么。这里给统一入口。

    ``numbered=True``（任务 69 §B1.5.3）：按**段落**分页并带上段号
    （``offset`` / ``limit`` 此时是段号 / 段数）—— 素材卡片点「第 N 段」能精确跳到
    注入与素材页的**同一个 ``N``**（正文走 :func:`material_text`，与注入同一口径）。
    """
    row = db.get(SourceMaterial, material_id)
    if row is None:
        raise ValueError("素材不存在")
    if numbered:
        body = material_text(row)
        paras = split_paragraphs(body)
        start = max(0, min(int(offset or 0), len(paras)))
        size = max(1, min(int(limit or FULL_TEXT_DEFAULT_LIMIT), 500))
        end = min(start + size, len(paras))
        meta = row.meta or {}
        return {
            "id": row.id,
            "title": row.title,
            "kind": row.kind,
            "ext": meta.get("ext", ""),
            "original": meta.get("original", ""),
            "pages": pages_of(row),
            "ocr": meta.get("ocr") or None,
            "asr": meta.get("asr") or None,
            "numbered": True,
            "offset": start,
            "limit": size,
            "total_chars": len(body),
            "total_paragraphs": len(paras),
            "paragraphs": [{"n": i + 1, "text": paras[i]} for i in range(start, end)],
            "has_more": end < len(paras),
            "next_offset": end if end < len(paras) else None,
        }
    text = row.text or ""
    total = len(text)
    start = max(0, min(int(offset or 0), total))
    size = max(1, min(int(limit or FULL_TEXT_DEFAULT_LIMIT), FULL_TEXT_MAX_LIMIT))
    end = min(start + size, total)
    meta = row.meta or {}
    return {
        "id": row.id,
        "title": row.title,
        "kind": row.kind,
        "ext": meta.get("ext", ""),
        "original": meta.get("original", ""),
        "chars": int(meta.get("chars") or total),
        "raw_chars": int(meta.get("raw_chars") or total),
        "truncated": bool(meta.get("truncated")),
        "pages": pages_of(row),
        # 任务 54：图片素材的 OCR 元信息（引擎/版本/置信度/跳过原因）随素材透出
        "ocr": meta.get("ocr") or None,
        # 任务 54：图片素材的 OCR 元信息（引擎/版本/置信度/跳过原因）随素材透出
        "ocr": meta.get("ocr") or None,
        # 任务 102：音视频素材的转写元信息（引擎/模型/时长/段数/置信度/状态/跳过原因）
        "asr": meta.get("asr") or None,
        "offset": start,
        "limit": size,
        "total_chars": total,
        "text": text[start:end],
        "has_more": end < total,
        "next_offset": end if end < total else None,
    }
 