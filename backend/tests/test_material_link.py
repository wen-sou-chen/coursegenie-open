"""任务 118：从链接导入（YouTube 字幕优先 / 没字幕转既有本地 ASR）—— 全部**不联网**。

六条硬要求（任务书 §二.6 + §三.4/§三.5 的反证据）：

1. 链接解析（视频 / 播放列表 / youtu.be / shorts / 无效链接）；
2. 「**有字幕 → 绝不下音频**」分支；
3. 「没字幕 → 走既有本地 ASR」分支（用假引擎，不真转写）；
4. 缺 yt-dlp（含「文件在、但跑不起来」的假可用）时的降级：**不 500、素材行仍在、可重试**；
5. 429 退避：间隔与次数都写死（8s → 16s → 32s，最多 3 次）+ 列表内两条之间 ≥20 秒；
6. cookie 的**值 / 路径**不出现在返回、日志、meta 里（规格 §二.5）。

反证据（提交前真跑过，见交付文档）：把「先字幕后音频」的顺序反过来 ⇒ 2 号用例必红；
把 cookie 值塞进返回 ⇒ 6 号用例必红。

纪律：**真机只读** —— conftest 已把 DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT /
PPT_WORK_DIR / EXPORT_DIR 指向临时目录；本文件的假执行器不发起任何网络请求
（真 yt-dlp 只在手工验收里跑过）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import link_import as L
from app.services import material as M

VIDEO_URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"
VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:03.120
hello from the first cue

00:00:03.120 --> 00:00:06.000
second cue here
"""


def _out_dir(args: list[str]) -> Path:
    """从 ``-o <dir>/%(id)s.%(ext)s`` 里取工作目录（假 yt-dlp 要知道文件写哪）。"""
    return Path(args[args.index("-o") + 1]).parent


def _url_id(url: str) -> str:
    return url.split("v=")[-1]


class FakeYTDLP:
    """假 yt-dlp：按命令行认活，并把真 yt-dlp 的坑照搬过来。

    · ``subs`` 里没有的 id ⇒ **退出码 0 但没有字幕文件**（真 yt-dlp 就是「有告警、不报错」）；
    · ``rate_limit_times`` / ``rate_limit_always`` ⇒ 吐 429 文本（验收退避与上限）；
    · 每次调用都进 ``self.calls``（断言「有没有下音频」「重试几次」「等了多久」）。
    """

    def __init__(self, *, items=None, subs=None, rate_limit_times: int = 0,
                 rate_limit_always: bool = False, audio_ext: str = ".m4a") -> None:
        self.items = items if items is not None else [
            {"id": "AAAAAAAAAAA", "title": "\u793a\u4f8b\u89c6\u9891", "duration": 372}]
        self.subs = subs or {}
        self.rate_limit_times = rate_limit_times
        self.rate_limit_always = rate_limit_always
        self.audio_ext = audio_ext
        self.calls: list[list[str]] = []
        self.limited = 0

    def __call__(self, args: list[str]) -> L.ToolRun:
        self.calls.append(list(args))
        if self.rate_limit_always or self.limited < self.rate_limit_times:
            self.limited += 1
            return L.ToolRun(list(args), 1, out="ERROR: HTTP Error 429: Too Many Requests")
        if "--flat-playlist" in args:
            payload: dict = {"_type": "playlist", "title": "\u793a\u4f8b\u64ad\u653e\u5217\u8868",
                             "entries": list(self.items)}
            if len(self.items) == 1:
                payload = dict(self.items[0])
            return L.ToolRun(list(args), 0, out=json.dumps(payload, ensure_ascii=False))
        folder = _out_dir(args)
        vid = _url_id(args[-1])
        if "--skip-download" in args:
            if vid in self.subs:
                (folder / f"{vid}.en.vtt").write_text(self.subs[vid], encoding="utf-8")
            return L.ToolRun(list(args), 0, out="")
        if "-x" in args:
            (folder / f"{vid}{self.audio_ext}").write_bytes(b"FAKE-AUDIO")
            return L.ToolRun(list(args), 0, out="")
        return L.ToolRun(list(args), 1, out="ERROR: unexpected yt-dlp call")

    def count(self, flag: str) -> int:
        return sum(1 for call in self.calls if flag in call)

    @property
    def downloaded_audio(self) -> bool:
        return any("-x" in call for call in self.calls)

class FakeClock:
    """假时钟：不真等 4 / 20 秒，但「该等多久」照样能被断言（``sleeps`` 里就是真实间隔）。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ytdlp_ok(monkeypatch):
    """让「yt-dlp 可用」这一步过掉（本机真有假可用也不影响；**不联网、不起进程**）。"""
    monkeypatch.setattr(L, "ytdlp_probe", lambda **kwargs: {
        "key": "ytdlp", "available": True, "cmd": ["yt-dlp"], "exe": "yt-dlp",
        "version": "2026.08.19", "reason": ""})


@pytest.fixture
def ytdlp_missing(monkeypatch):
    """假装本机没有可用的 yt-dlp（验收 §三.2 的降级人话）。"""
    monkeypatch.setattr(L, "ytdlp_probe", lambda **kwargs: {
        "key": "ytdlp", "available": False, "cmd": [], "exe": "",
        "version": "", "reason": L.YTDLP_MISSING_REASON})


@pytest.fixture
def use_fake_tools(monkeypatch, tmp_path):
    """``use_fake_tools(fake, transcribe=...)``：让**路由自己构造的** FetchOptions 用假执行器。

    路由里 ``link_import.FetchOptions(cookies=...)`` 是从外面注不进去的 ⇒ 这里把类换成工厂
    （自带临时工作目录 + 假时钟 + 不真睡的 sleep），端到端用例才能**不联网**地跑完。
    """
    real = L.FetchOptions

    def _apply(fake: FakeYTDLP, transcribe=None):
        def _factory(**kwargs):
            kwargs.setdefault("runner", fake)
            kwargs.setdefault("transcribe", transcribe)
            return real(work_dir=tmp_path, sleep=lambda seconds: None,
                        clock=FakeClock(), **kwargs)
        monkeypatch.setattr(L, "FetchOptions", _factory)
        return fake

    return _apply


def _opts(tmp_path, fake: FakeYTDLP, **kwargs) -> L.FetchOptions:
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("clock", FakeClock())
    return L.FetchOptions(work_dir=tmp_path, runner=fake, **kwargs)


def _material(course_id: str, parsed: dict, title: str = "") -> tuple:
    """在临时库里落一条链接素材行（等价于路由那一步），返回 ``(db, row)``。"""
    db = SessionLocal()
    course = db.get(Course, course_id)
    row = M.add_material_link(db, course, parsed["url"], parsed=parsed, title=title)
    return db, row


def test_parse_link_video_playlist_and_invalid():
    """① 链接解析：四种形态都认，无效链接给 400 那句话（不落素材行）。"""
    assert L.parse_link(VIDEO_URL)["kind"] == "video"
    assert L.parse_link(VIDEO_URL)["video_id"] == "AAAAAAAAAAA"
    assert L.parse_link("https://youtu.be/AAAAAAAAAAA?t=30")["video_id"] == "AAAAAAAAAAA"
    assert L.parse_link("https://www.youtube.com/shorts/AAAAAAAAAAA")["kind"] == "video"
    assert L.parse_link("youtube.com/watch?v=AAAAAAAAAAA")["kind"] == "video"
    pl = L.parse_link(PLAYLIST_URL)
    assert pl["kind"] == "playlist" and pl["playlist_id"] == "PLabcdefghijklmnop"
    both = L.parse_link(f"{VIDEO_URL}&list=PLabcdefghijklmnop&index=2")
    assert both["kind"] == "playlist" and both["playlist_id"] == "PLabcdefghijklmnop"
    for bad in ("", "   ", "https://vimeo.com/12345", "https://www.youtube.com/",
                "https://www.youtube.com/watch?v=tooshort", "just some words"):
        with pytest.raises(ValueError):
            L.parse_link(bad)
    with pytest.raises(ValueError) as exc:
        L.parse_link("https://vimeo.com/12345")
    assert "YouTube" in str(exc.value)


def test_subtitle_path_never_downloads_audio(course_id, tmp_path, ytdlp_ok):
    """② 有字幕 ⇒ 只用字幕，**一个音频字节都不下**（顺序写死的反证据就钉在这条上）。"""
    fake = FakeYTDLP(subs={"AAAAAAAAAAA": VTT})
    db, row = _material(course_id, L.parse_link(VIDEO_URL))
    try:
        out = L.run_link_import(db, row, parsed=L.parse_link(VIDEO_URL), opts=_opts(tmp_path, fake))
        assert out["status"] == "done" and out["path"] == "subtitle"
        assert fake.downloaded_audio is False          # ★ 顺序：字幕优先，不下音频
        assert fake.count("--write-subs") == 1
        assert row.text.startswith("[00:00] ") and "hello from the first cue" in row.text
        link = row.meta["link"]
        assert link["path"] == "subtitle" and link["language"] == "en"
        assert link["kind"] == "video" and link["url"] == VIDEO_URL
        assert link["cookies"] is False and link["status"] == "done"
        assert link["fetched_at"] and link["rate"]["requests"] >= 1
    finally:
        db.close()


def test_default_runner_prepends_probed_executable(course_id, tmp_path, monkeypatch):
    """真机路径的反证据：默认执行器必须把**探测到的可执行文件**补进 argv。

    测试里的假执行器（``FakeYTDLP``）根本不看 ``argv[0]``，所以「真机拿 ``--ignore-config``
    当程序名 ⇒ ``[WinError 2]``」这个坑只有钉在这里才拦得住：``ytdlp_base()`` 只拼参数，
    可执行文件由 ``bind_runner()`` 补（真机实测就是这里先踩的）。
    """
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=0.0):
        calls.append(list(args))
        if "--version" in args:
            return L.ToolRun(list(args), 0, out="2026.08.19")
        return L.ToolRun(list(args), 0, out=json.dumps(
            {"title": "one video", "entries": [{"id": "AAAAAAAAAAA", "title": "one"}]}))

    monkeypatch.setattr(L, "run_ytdlp", fake_run)
    monkeypatch.setattr(L, "ytdlp_probe", lambda **kwargs: {
        "key": "ytdlp", "available": True, "cmd": ["yt-dlp.exe"], "exe": "yt-dlp.exe",
        "version": "2026.08.19", "reason": ""})
    db, row = _material(course_id, L.parse_link(VIDEO_URL))
    try:
        out = L.run_link_import(db, row, parsed=L.parse_link(VIDEO_URL),
                                opts=L.FetchOptions(work_dir=tmp_path, enable_asr=False))
        assert calls, "默认执行器一次都没跑"
        assert all(call[0] == "yt-dlp.exe" for call in calls), calls
        assert out["status"] == "failed" and row.meta["link"]["status"] == "failed"
    finally:
        db.close()


def test_error_line_prefers_error_and_real_unavailable_wording():
    """真机反证据：yt-dlp 单视频抓不动时**先打一行 ``null`` 再打 ``ERROR:``**（真机实测踩过）。

    直接取第一行 ⇒ 提示会变成「抓取失败：null」（等于没说话）；``This video is unavailable``
    又是真机原话，不在 GONE_PATTERNS 里就会被归成「其它错误」。
    """
    raw = "null\nERROR: [youtube] AAAAAAAAAAA: This video is unavailable\n"
    assert L._first_error_line(raw) == "[youtube] AAAAAAAAAAA: This video is unavailable"
    code, message = L.classify_tool_error(raw)
    assert code == "unavailable"
    assert "null" not in message and message.strip()


def test_playlist_imports_each_video_and_keeps_gaps(course_id, tmp_path, ytdlp_ok):
    """②b 播放列表：逐条抓、条目之间按「列表内 ≥20 秒」排队，正文带视频小标题。"""
    items = [{"id": "AAAAAAAAAAA", "title": "\u7b2c\u4e00\u96c6", "duration": 100},
             {"id": "BBBBBBBBBBB", "title": "\u7b2c\u4e8c\u96c6", "duration": 200}]
    fake = FakeYTDLP(items=items, subs={"AAAAAAAAAAA": VTT, "BBBBBBBBBBB": VTT})
    sleeps: list[float] = []
    opts = L.FetchOptions(work_dir=tmp_path, runner=fake, clock=FakeClock(),
                          sleep=sleeps.append)
    db, row = _material(course_id, L.parse_link(PLAYLIST_URL))
    try:
        out = L.run_link_import(db, row, parsed=L.parse_link(PLAYLIST_URL), opts=opts)
        assert out["items"] == 2 and out["items_done"] == 2
        assert fake.downloaded_audio is False
        assert 20.0 in sleeps and 4.0 in sleeps      # 换条目 20 秒、同条目内 4 秒
        assert "## 01" in row.text and "## 02" in row.text
        assert row.meta["link"]["videos"] == 2
        assert set(row.meta["link"]["rate"]["waits"]) >= {0.0, 20.0}
    finally:
        db.close()

def test_no_subtitle_falls_back_to_existing_local_asr(course_id, tmp_path, ytdlp_ok, monkeypatch):
    """③ 没字幕 ⇒ 下音频走**既有本地 ASR**（假引擎），meta 写清「用了哪条路 + 模型 + 语言」。"""
    monkeypatch.setattr(M, "asr_available", lambda: True)      # 本机装没装都不影响这条用例
    fake = FakeYTDLP(subs={})
    seen: list[dict] = []

    def fake_transcribe(path, *, on_progress=None, domain_hint=""):
        seen.append({"path": str(path), "domain_hint": domain_hint})
        return {"text": "[00:00] local transcript",
                "segments": [{"start": 0.0, "end": 2.0, "text": "local transcript"}],
                "duration_sec": 12.5, "language": "en", "model": "small",
                "avg_logprob": -0.32, "initial_prompt": ""}

    db, row = _material(course_id, L.parse_link(VIDEO_URL))
    try:
        out = L.run_link_import(db, row, parsed=L.parse_link(VIDEO_URL),
                                opts=_opts(tmp_path, fake, transcribe=fake_transcribe))
        assert out["status"] == "done" and out["path"] == "asr"
        assert fake.downloaded_audio is True           # ★ 没字幕才下音频（顺序的另一半）
        assert seen and Path(seen[0]["path"]).suffix == ".m4a"
        assert "local transcript" in row.text
        asr = row.meta["asr"]
        assert asr["status"] == "done" and asr["model"] == "small" and asr["language"] == "en"
        assert asr["segments"] == 1 and asr["duration_sec"] == 12.5
        assert row.meta["link"]["path"] == "asr"
    finally:
        db.close()


def test_rate_limit_backs_off_then_succeeds(course_id, tmp_path, ytdlp_ok):
    """⑤ 429：指数退避 8s → 16s；退避后成功仍算 done（上限内重试）。"""
    fake = FakeYTDLP(subs={"AAAAAAAAAAA": VTT}, rate_limit_times=2)
    sleeps: list[float] = []
    opts = L.FetchOptions(work_dir=tmp_path, runner=fake, clock=FakeClock(),
                          sleep=sleeps.append)
    db, row = _material(course_id, L.parse_link(VIDEO_URL))
    try:
        out = L.run_link_import(db, row, parsed=L.parse_link(VIDEO_URL), opts=opts)
        assert out["status"] == "done"
        assert sleeps[:2] == [8.0, 16.0]                   # 退避 8s → 16s
        assert sleeps[2] == 4.0                            # 抓成之后那条请求仍守「每请求 ≥4 秒」
        assert fake.count("--flat-playlist") == 3      # 原请求 + 2 次重试
    finally:
        db.close()


def test_rate_limit_gives_up_at_explicit_cap(course_id, tmp_path, ytdlp_ok):
    """⑤b 429 连着来 ⇒ 退避到上限（3 次）就放弃，并把「限流」这句人话写进素材行。"""
    fake = FakeYTDLP(rate_limit_always=True)
    sleeps: list[float] = []
    opts = L.FetchOptions(work_dir=tmp_path, runner=fake, clock=FakeClock(),
                          sleep=sleeps.append)
    db, row = _material(course_id, L.parse_link(VIDEO_URL))
    try:
        out = L.run_link_import(db, row, parsed=L.parse_link(VIDEO_URL), opts=opts)
        assert out["status"] == "failed"
        assert sleeps == [8.0, 16.0]                   # 上限 3 次 ⇒ 只退避两次
        assert fake.count("--flat-playlist") == 3
        assert "429" in out["skipped_reason"] and "重新导入" in out["skipped_reason"]
        assert row.meta["link"]["status"] == "failed"
        assert row.meta["link"]["error_code"] == "rate_limit"
    finally:
        db.close()


def test_missing_ytdlp_keeps_material_row_and_says_how_to_install(client, course_id, ytdlp_missing):
    """④ 缺 yt-dlp：**不 500**、素材行仍在、人话写清怎么装（§三.2）。"""
    body = client.post("/api/material/import-link", json={"course_id": course_id, "url": VIDEO_URL})
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["job_id"] is None
    link = payload["link"]
    assert link["status"] == "failed" and link["path"] == "none"
    assert "yt-dlp" in link["skipped_reason"]
    assert "\u4e0d\u662f\u4f60\u7684\u8bfe\u6709\u95ee\u9898" in payload["message"]
    assert "YTDLP_EXE" in payload["message"] or "YTDLP" in payload["message"]
    rows = client.get(f"/api/material/{course_id}").json()
    assert [m["id"] for m in rows] == [payload["id"]]         # 素材行还在（可重试）


def test_bad_link_is_rejected_without_writing_a_row(client, course_id):
    """输入错不算降级：链接解析不了 ⇒ 400 + 人话，**不落素材行**。"""
    before = client.get(f"/api/material/{course_id}").json()
    body = client.post("/api/material/import-link",
                       json={"course_id": course_id, "url": "https://vimeo.com/12345"})
    assert body.status_code == 400 and "YouTube" in body.json()["detail"]
    assert client.get(f"/api/material/{course_id}").json() == before


def test_cookie_value_never_leaks_into_response_logs_or_meta(client, course_id, caplog, tmp_path,
                                                            ytdlp_ok, use_fake_tools):
    """⑥ cookie 的**值**与**路径**都不进返回、不进日志、不落库（规格 §二.5）。"""
    secret = "SECRET-COOKIE-VALUE-1234567890"
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n"
                   ".youtube.com\tTRUE\t/\tTRUE\t2000000000\t__Secure-3PSID\t" + secret + "\n",
                   encoding="utf-8")
    use_fake_tools(FakeYTDLP(subs={"AAAAAAAAAAA": VTT}))
    caplog.set_level(logging.DEBUG)
    body = client.post("/api/material/import-link",
                       json={"course_id": course_id, "url": VIDEO_URL, "cookies": str(jar)})
    assert body.status_code == 200, body.text
    assert secret not in body.text and str(jar) not in body.text
    assert secret not in caplog.text and str(jar) not in caplog.text
    material_id = body.json()["id"]
    assert secret not in client.get(f"/api/material/{course_id}").text
    assert secret not in client.get(f"/api/material/full/{material_id}").text
    assert secret not in json.dumps(body.json(), ensure_ascii=False)


def test_cookie_without_youtube_domain_warns_in_plain_words(client, course_id, tmp_path,
                                                            ytdlp_ok, use_fake_tools):
    """cookie 域不对（硬事实 1）⇒ 提前用人话说清「会被当成未登录」，但不拦着不跑。"""
    jar = tmp_path / "google-only.txt"
    jar.write_text("# Netscape HTTP Cookie File\n"
                   ".google.com\tTRUE\t/\tTRUE\t2000000000\tSID\twhatever\n", encoding="utf-8")
    use_fake_tools(FakeYTDLP(subs={"AAAAAAAAAAA": VTT}))
    body = client.post("/api/material/import-link",
                       json={"course_id": course_id, "url": VIDEO_URL, "cookies": str(jar)})
    assert body.status_code == 200, body.text
    warning = body.json()["warning"]
    assert "youtube.com" in warning and "未登录" in warning
    rows = client.get(f"/api/material/{course_id}").json()
    assert rows[0]["link"]["cookies_youtube_domain"] is False


def test_reimport_link_enqueues_again_without_adding_rows(client, course_id, ytdlp_ok,
                                                          use_fake_tools):
    """「重新导入」：链接存在 ``meta.link`` 里 ⇒ 不用再贴链接，也不新增素材行。"""
    use_fake_tools(FakeYTDLP(subs={"AAAAAAAAAAA": VTT}))
    created = client.post("/api/material/import-link",
                          json={"course_id": course_id, "url": VIDEO_URL}).json()
    assert created["job_id"]
    rows = client.get(f"/api/material/{course_id}").json()
    assert rows[0]["link"]["status"] == "done"
    again = client.post(f"/api/material/{created['id']}/import-link", json={})
    assert again.status_code == 200, again.text
    assert again.json()["ok"] is True and again.json()["job_id"]
    assert len(client.get(f"/api/material/{course_id}").json()) == 1
    other = client.post(f"/api/material/{created['id']}/import-link", json={})
    assert other.status_code == 200


def test_transcribe_entry_points_link_material_back_to_reimport(client, course_id, ytdlp_ok,
                                                               use_fake_tools):
    """链接素材没有本机原件 ⇒ 旧「重新转写」入口给 400 人话（既有语义不动，用例钉住）。"""
    use_fake_tools(FakeYTDLP(subs={"AAAAAAAAAAA": VTT}))
    created = client.post("/api/material/import-link",
                          json={"course_id": course_id, "url": VIDEO_URL}).json()
    body = client.post(f"/api/material/{created['id']}/transcribe")
    assert body.status_code == 400
    assert "\u91cd\u65b0\u5bfc\u5165" in body.json()["detail"]