"""任务 129：把「字幕语种优先级」钉住（**0 LLM**、真机只读）。

来历（2026-10-04 变异反证，报告 `验收-反证-117-118-119.md`）：`backend/tests/` 全目录
grep `SUB_LANGS|zh-Hans|zh-Hant` **0 命中** —— 把 `link_import.SUB_LANGS` 从
`("en", "zh-Hans", "zh-Hant", "zh")` 换位成 `("zh-Hans", "en", "zh-Hant", "zh")` 之后
**19 条用例全绿**；而这个顺序**真的驱动行为**（既进 `yt-dlp --sub-langs`，又决定按
`opts.langs` 挑哪一个字幕文件）。根因：既有假执行器只造 `{id}.en.vtt`，**多语种与顺序
那条路从来没被走到**。

本文件只**补护栏、不改行为**：`SUB_LANGS` 的值与顺序一个字符都不动。假执行器不联网、
不起进程 —— 它把 argv 记下来，按给定语种写出 `{id}.{lang}{suffix}` 字幕文件，再由**生产代码**
（`fetch_subtitle` / `run_link_import`）去挑；「挑中谁」是断言出来的，不是假执行器指定的。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import link_import as L
from app.services import material as M

VIDEO_URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
VIDEO_ID = "AAAAAAAAAAA"
#: 冻结的优先级（逐字）—— 换位 / 改字都会让本文件的用例红。
FROZEN = ("en", "zh-Hans", "zh-Hant", "zh")
FROZEN_CSV = "en,zh-Hans,zh-Hant,zh"


def _vtt(label: str) -> str:
    """一份能解析出正文的 vtt（正文里带 label，用来断言「内容取自哪一份」）。"""
    return (f"WEBVTT\nLanguage: {label}\n\n"
            f"00:00:00.000 --> 00:00:03.000\nhello from {label}\n\n"
            f"00:00:03.000 --> 00:00:06.000\nsecond cue from {label}\n")


class FakeSubtitleTool:
    """假 yt-dlp：认「列清单」与「抓字幕」两步，把 argv 记下来、按语种写文件（**不联网**）。"""

    def __init__(self, files=()) -> None:
        #: 要造哪些字幕文件：``(lang, suffix, text)`` 三元组
        self.files = list(files)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> L.ToolRun:
        self.calls.append(list(args))
        if "--flat-playlist" in args:
            payload = {"id": VIDEO_ID, "title": "示例视频", "duration": 372}
            return L.ToolRun(list(args), 0, out=json.dumps(payload, ensure_ascii=False))
        if "--skip-download" in args:
            folder = Path(args[args.index("-o") + 1]).parent
            for lang, suffix, text in self.files:
                (folder / f"{VIDEO_ID}.{lang}{suffix}").write_text(text, encoding="utf-8")
            return L.ToolRun(list(args), 0, out="")
        return L.ToolRun(list(args), 1, out="ERROR: unexpected yt-dlp call")

    @property
    def last_sub_args(self) -> list[str]:
        """最后一次**抓字幕**调用的 argv。"""
        return next(call for call in reversed(self.calls) if "--skip-download" in call)

    @property
    def sub_langs_value(self) -> str:
        """``--sub-langs`` 的值（逐字，不按集合比）。"""
        call = self.last_sub_args
        return call[call.index("--sub-langs") + 1]


def _opts(tmp_path: Path, fake: FakeSubtitleTool, **kwargs) -> L.FetchOptions:
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("clock", lambda: 0.0)
    return L.FetchOptions(work_dir=tmp_path, runner=fake, **kwargs)


def _fetch(tmp_path: Path, fake: FakeSubtitleTool, **kwargs):
    """直接跑生产代码那一步：``fetch_subtitle``（不联网、不落库）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)   # 假执行器要往这个目录写字幕文件
    opts = _opts(tmp_path, fake, **kwargs)
    limiter = L.RateLimiter(min_interval=0.0, playlist_interval=0.0,
                            clock=opts.clock, sleep=opts.sleep)
    return L.fetch_subtitle({"id": VIDEO_ID, "url": VIDEO_URL, "title": "示例视频"},
                            opts, limiter, tmp_path)


@pytest.fixture
def ytdlp_ok(monkeypatch):
    """让端到端那条用例过掉「yt-dlp 可用」这一步（假执行器仍在，**不起进程**）。"""
    monkeypatch.setattr(L, "ytdlp_probe", lambda **kwargs: {
        "key": "ytdlp", "available": True, "cmd": ["yt-dlp"], "exe": "yt-dlp",
        "version": "2026.08.19", "reason": ""})


# ---------------------------------------------------------------------------
# ① 默认值与顺序（逐字）+ ② 顺序真的透传进 argv
# ---------------------------------------------------------------------------

def test_default_langs_are_the_frozen_priority_order():
    """默认值就是那份顺序：``FetchOptions().langs == SUB_LANGS == (…)``（逐字）。"""
    assert isinstance(L.SUB_LANGS, tuple)
    assert L.SUB_LANGS == FROZEN
    assert L.FetchOptions().langs == L.SUB_LANGS == FROZEN
    assert ",".join(L.FetchOptions().langs) == FROZEN_CSV


def test_sub_langs_flag_carries_the_exact_order(tmp_path):
    """② ``--sub-langs`` 的值**逐字**等于 ``en,zh-Hans,zh-Hant,zh``（字符串相等）。"""
    fake = FakeSubtitleTool([("en", ".vtt", _vtt("en"))])
    text, lang, _run = _fetch(tmp_path, fake)
    args = fake.last_sub_args
    assert "--sub-langs" in args
    assert fake.sub_langs_value == FROZEN_CSV
    assert "--write-subs" in args and "--write-auto-subs" in args
    assert lang == "en" and "hello from en" in text


# ---------------------------------------------------------------------------
# ③④⑤ 多语种并存时按顺序挑（造文件顺序故意打乱，挑中谁由生产代码决定）
# ---------------------------------------------------------------------------

def test_multiple_languages_present_prefers_en(tmp_path):
    """③ en / zh-Hans / zh-Hant 同时存在 ⇒ 选 **en**，内容取自 en 那份。"""
    fake = FakeSubtitleTool([("zh-Hant", ".vtt", _vtt("zh-Hant")),
                             ("zh-Hans", ".vtt", _vtt("zh-Hans")),
                             ("en", ".vtt", _vtt("en"))])
    text, lang, _run = _fetch(tmp_path, fake)
    assert lang == "en"
    assert "hello from en" in text
    assert "zh-Hans" not in text and "zh-Hant" not in text


def test_without_en_falls_back_to_simplified_chinese(tmp_path):
    """④ 没有 en ⇒ 退到 **zh-Hans**（连内容也必须是这一份）。"""
    fake = FakeSubtitleTool([("zh-Hant", ".vtt", _vtt("zh-Hant")),
                             ("zh-Hans", ".vtt", _vtt("zh-Hans"))])
    text, lang, _run = _fetch(tmp_path, fake)
    assert lang == "zh-Hans"
    assert "hello from zh-Hans" in text and "zh-Hant" not in text


def test_without_en_or_simplified_falls_back_to_traditional(tmp_path):
    """⑤ 再退一步：只剩 zh-Hant ⇒ 选 **zh-Hant**（不是「随便挑一个」）。"""
    fake = FakeSubtitleTool([("zh-Hant", ".vtt", _vtt("zh-Hant"))])
    text, lang, _run = _fetch(tmp_path, fake)
    assert lang == "zh-Hant" and "hello from zh-Hant" in text


def test_traditional_beats_the_generic_zh_and_generic_zh_is_the_tail(tmp_path):
    """⑤b 顺序的尾巴也要钉住：繁中在泛中之前；只剩泛中时选 **zh**。"""
    both = FakeSubtitleTool([("zh", ".vtt", _vtt("zh")), ("zh-Hant", ".vtt", _vtt("zh-Hant"))])
    _text, lang, _run = _fetch(tmp_path / "both", both)
    assert lang == "zh-Hant"
    only_zh = FakeSubtitleTool([("zh", ".vtt", _vtt("zh"))])
    text, lang2, _run2 = _fetch(tmp_path / "only-zh", only_zh)
    assert lang2 == "zh" and "hello from zh" in text


# ---------------------------------------------------------------------------
# ⑥ 自定义 langs 走同一条路（防「硬编码顺序」）
# ---------------------------------------------------------------------------

def test_custom_langs_keep_their_own_order(tmp_path):
    """⑥ ``FetchOptions(langs=("zh-Hans","en"))`` ⇒ argv 与挑文件都按这个顺序。"""
    fake = FakeSubtitleTool([("en", ".vtt", _vtt("en")), ("zh-Hans", ".vtt", _vtt("zh-Hans"))])
    text, lang, _run = _fetch(tmp_path, fake, langs=("zh-Hans", "en"))
    assert fake.sub_langs_value == "zh-Hans,en"
    assert lang == "zh-Hans" and "hello from zh-Hans" in text


# ---------------------------------------------------------------------------
# ⑦ 同一语种多份文件：按**现有代码口径**（.vtt → .srt → .json3 的第一个存在者）
# ---------------------------------------------------------------------------

def test_same_language_multiple_files_follow_existing_suffix_order(tmp_path):
    """⑦ 同语种多份文件按现有代码口径挑：``.vtt`` 优先，其次 ``.srt``，最后 ``.json3``。"""
    first = tmp_path / "first"
    fake = FakeSubtitleTool([("en", ".json3", "JSON3 那份"), ("en", ".srt", "SRT 那份"),
                             ("en", ".vtt", _vtt("en"))])
    text, lang, _run = _fetch(first, fake)
    assert lang == "en" and text == _vtt("en")

    second = tmp_path / "second"
    fake2 = FakeSubtitleTool([("en", ".json3", "JSON3 那份"), ("en", ".srt", "SRT 那份")])
    text2, lang2, _run2 = _fetch(second, fake2)
    assert lang2 == "en" and text2 == "SRT 那份"


# ---------------------------------------------------------------------------
# 端到端：顺序真的驱动行为（素材行正文与 meta.link.language）
# ---------------------------------------------------------------------------

def test_language_priority_reaches_the_material_row(course_id, tmp_path, ytdlp_ok):
    """真跑一次导入：多语种并存 ⇒ 素材行正文取自 en、``meta.link.language == "en"``。"""
    fake = FakeSubtitleTool([("zh-Hans", ".vtt", _vtt("zh-Hans")), ("en", ".vtt", _vtt("en"))])
    db = SessionLocal()
    try:
        row = M.add_material_link(db, db.get(Course, course_id), VIDEO_URL,
                                  parsed=L.parse_link(VIDEO_URL), title="")
        out = L.run_link_import(db, row, parsed=L.parse_link(VIDEO_URL), opts=_opts(tmp_path, fake))
        assert out["status"] == "done" and out["path"] == "subtitle"
        assert out["language"] == "en"
        assert row.meta["link"]["language"] == "en"
        assert row.meta["link"]["videos"] == 1
        assert "hello from en" in row.text and "zh-Hans" not in row.text
    finally:
        db.close()
