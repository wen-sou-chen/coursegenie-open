"""任务 102 音视频素材回归测试：上传**立刻返回** → 后台转写 → `[mm:ss]` 分段 → `meta.asr`。

协作区铁律：``DATABASE_URL`` / ``STORAGE_DIR`` / ``OBSIDIAN_VAULT_ROOT`` / ``PPT_WORK_DIR`` /
``EXPORT_DIR`` 由 ``conftest`` 指向临时目录（五件套），本文件不碰真实库。

本任务附加纪律（同任务 54）：ASR 是**可选依赖**（``backend/requirements-asr.txt``）——
真引擎链路一律 ``importorskip`` 保护，**没装引擎的机器上本文件仍然全绿**：
降级路径用 monkeypatch 模拟，异步链路用「假引擎」注入（不依赖真模型、不耗时）。

任务 103：中文素材默认带简体引导语（``ASR_ZH_PROMPT``）、非中文一律不传；判据 = **转写
文本里不许出现繁体常用字**（素材用 Windows SAPI 现场合成，**wav 不进仓库**）—— 真引擎那条
用例在没装 faster-whisper / 本机没缓存模型 / 没有中文语音包时自动跳过。

任务 104：模型名 ``ASR_MODEL`` 走 ``.env``（经 pydantic ``Settings``，与本项目其它配置一致），
**进程环境变量仍然优先** —— 判据 = 临时目录里放一份 ``.env``，新进程里 ``asr_model()`` 必须读到它。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi import BackgroundTasks

from app.db.session import SessionLocal
from app.models.course import Course, GenerationJob, SourceMaterial
from app.services import material as material_mod
from app.utils.uploads import UploadTooLarge, read_upload_to_path

# 内容无关紧要：探针与引擎都被打桩（真 mp4 的端到端在临时五件套里跑）
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
KEYWORD = "photosynthesis"
ASR_KEYS = {"engine", "version", "model", "duration_sec", "segments", "language",
            "avg_logprob", "status", "skipped_reason", "initial_prompt"}
PAD = "This is a longer transcription unit used for the merging check. "
# 任务 103 判据：只挑**只在繁体里出现**的常用字（「我 / 系 / 教 / 军」两体同形，刻意不进表）
TRAD_ONLY = "們講產氣體責帶來風這個負軍學課與麼沒說時後會給過還開關電發頭機對現實點為處無聲樣問題間"
SAPI_ZH_TEXT = "今天我们讲多菌共生体系。酵母菌负责产气，乳酸菌负责产酸，醋酸菌带来风味。"
SAPI_EN_TEXT = "Today we talk about the multi bacteria symbiosis system."


class _Seg:
    def __init__(self, start: float, end: float, text: str, logprob: float = -0.4) -> None:
        self.start, self.end, self.text, self.avg_logprob = start, end, text, logprob


class _Info:
    def __init__(self, duration: float, language: str = "en") -> None:
        self.duration, self.language = duration, language


class _FakeEngine:
    """假引擎：吐 5 段、每段先 sleep（好让进度至少写库两次）。

    任务 105：每段都超过 ``ASR_SEGMENT_MAX_CHARS``（350 → 120）⇒ 合并后仍是 5 段；
    「按停顿合并 / 段长上限」的边界由 ``tests/test_material_anchor_stamp.py`` 覆盖。

    任务 103：``detect_language`` 返回 ``self.language``（默认 en）；``transcribe`` 收到什么
    kwargs 就记进 ``self.kwargs`` —— 「中文才传 initial_prompt」那条断言靠它。
    """

    def __init__(self, sleep: float = 0.35, duration: float = 12.0,
                 language: str = "en") -> None:
        self.sleep = sleep
        self.duration = duration
        self.language = language
        self.calls = 0
        self.kwargs: dict = {}

    def _rows(self) -> list[tuple[str, float, float]]:
        return [
            ("Welcome to this lesson. " + PAD * 2, 0.0, 2.4),
            ("Photosynthesis happens in the chloroplast. " + PAD * 2, 2.4, 5.2),
            ("Light reactions convert light energy into ATP. " + PAD * 2, 5.2, 8.0),
            ("NADPH carries the electrons further. " + PAD * 2, 8.0, 10.4),
            ("Thank you for listening. " + PAD, 10.4, 12.0),
        ]

    def detect_language(self, audio=None, **kwargs):
        return self.language, 0.99, [(self.language, 0.99)]

    def transcribe(self, path, **kwargs):
        self.calls += 1
        self.kwargs = dict(kwargs)

        def _gen():
            for text, start, end in self._rows():
                time.sleep(self.sleep)
                yield _Seg(start, end, " " + text)      # whisper 的 text 带前导空格

        return _gen(), _Info(self.duration, self.language)


def _ok_probe(duration: float = 12.0):
    return lambda path: {"ok": True, "has_audio": True, "duration_sec": duration, "error": ""}


@pytest.fixture()
def fake_asr(monkeypatch):
    """装上假引擎 + 「文件头探测成功」的探针：不依赖 faster-whisper / av。"""
    engine = _FakeEngine()
    monkeypatch.setattr(material_mod, "ASR_AVAILABLE", True)
    monkeypatch.setattr(material_mod, "_asr_engine", lambda: engine)
    monkeypatch.setattr(material_mod, "probe_av", _ok_probe(engine.duration))
    # 任务 103：解码是 faster-whisper 的事（可选依赖）—— 打桩成假波形，探语种只看引擎
    monkeypatch.setattr(material_mod, "_asr_decode", lambda path: [0.0] * 16)
    return engine


def _upload(client, course_id: str, name: str = "lecture.mp4", data: bytes = MP4):
    return client.post("/api/material/upload", data={"course_id": course_id},
                       files={"file": (name, data, "video/mp4")})


def test_upload_video_returns_before_transcription(client, course_id, fake_asr, monkeypatch):
    """验收②①②③④：响应体是**转写前**快照；素材行、作业、[mm:ss] 分段、meta.asr 全齐。"""
    # ① 关键断言（有牙的那种）：转写必须**先入队 BackgroundTasks、再在后台跑**。
    #    只比对响应体抓不住「同步变异」—— 响应体是入队前的快照，请求里同步跑完也还是 pending；
    #    所以这里按**调用顺序**断言：enqueue 必须排在 transcribe 之前。
    #    把 `background.add_task(_run)` 换成 `_run()`（同步变异）⇒ order 里没有 enqueue ⇒ 判红。
    order: list[str] = []
    real_add_task = BackgroundTasks.add_task

    def _spy_add_task(self, func, *args, **kwargs):
        order.append("enqueue")
        return real_add_task(self, func, *args, **kwargs)

    monkeypatch.setattr(BackgroundTasks, "add_task", _spy_add_task)
    real_transcribe = fake_asr.transcribe

    def _spy_transcribe(path, **kwargs):
        order.append("transcribe")
        return real_transcribe(path, **kwargs)

    monkeypatch.setattr(fake_asr, "transcribe", _spy_transcribe)

    resp = _upload(client, course_id)
    assert resp.status_code == 200, resp.text
    row = resp.json()

    assert order[:2] == ["enqueue", "transcribe"], \
        f"上传接口被转写阻塞了（转写没有先入队 BackgroundTasks）：调用顺序={order}"
    # 响应体仍是**转写前**快照：pending + 空正文
    assert row["asr"]["status"] == "pending", \
        "上传接口被转写阻塞了（响应里已经是转写后的状态）"
    assert row["chars"] == 0 and row["preview"] == ""
    assert row["kind"] == "video" and row["ext"] == ".mp4"
    assert row["job_id"], "音视频上传必须返回 job_id（进度要能轮询）"
    assert row["asr"]["engine"] == material_mod.ASR_ENGINE
    assert row["asr"]["model"] == material_mod.asr_model()
    assert set(row["asr"]) == ASR_KEYS

    # 作业链：转写完成后 GET /content/jobs/{job_id} 是 success + progress 1.0
    job = client.get(f"/api/content/jobs/{row['job_id']}").json()
    assert job["status"] == "success", job
    assert job["result"]["progress"] == 1.0 and job["result"]["segments"] == 5

    # 素材行照旧在列表里，且已回填正文
    listed = client.get(f"/api/material/{course_id}").json()
    mine = [m for m in listed if m["id"] == row["id"]]
    assert mine and mine[0]["asr"]["status"] == "done" and mine[0]["chars"] > 0

    # ③ 正文命中念过的关键词、每段带 [mm:ss]、段间空行
    db = SessionLocal()
    try:
        material = db.get(SourceMaterial, row["id"])
        text = material.text or ""
        assert KEYWORD in text.lower(), "转写正文里没有念过的关键词"
        paras = material_mod.split_paragraphs(text)
        assert len(paras) == 5, [p[:20] for p in paras]
        assert all(p.startswith("[") and p[3] == ":" for p in paras), paras
        assert "\n\n" in text, "段间必须空行"
        # 「（来源:《素材名》第 N 段）」的编号口径一个字没改：序号仍由 numbered_paragraphs 生成
        numbered = material_mod.numbered_paragraphs(text)
        assert numbered[0].startswith("[1] [00:00] ")
        assert len(numbered) == len(paras)
        # 素材页（导出）写明「由本地语音转写」+ 引擎/模型
        page = material_mod.asr_page_note(material)
        assert page.startswith("> 本页文字由本地语音转写") and material_mod.ASR_ENGINE in page
        # 注入链路同源：catalog 的段落 == 素材页 [N] 的段落
        catalog = material_mod.material_catalog(db, db.get(Course, course_id))
        flat = [p for entry in catalog for p in entry["paragraphs"]]
        assert flat and flat[0].startswith("[00:00] ")

        # ④ meta.asr 字段齐全（贴原文）
        meta = dict(material.meta["asr"])
        assert set(meta) == ASR_KEYS
        assert meta["status"] == "done" and meta["skipped_reason"] == ""
        assert meta["duration_sec"] == 12.0 and meta["language"] == "en"
        assert meta["segments"] == 5 and meta["avg_logprob"] == -0.4
        assert meta["version"] or meta["engine"] == material_mod.ASR_ENGINE
        assert material.meta["chars"] == len(text) and material.meta["truncated"] is False
        assert material.kind == "video" and material.file_rel.startswith("materials/")
    finally:
        db.close()

    # 素材详情（分页全文本）也带 asr；按段落模式同样带
    detail = client.get(f"/api/material/full/{row['id']}").json()
    assert detail["asr"]["status"] == "done" and detail["asr"]["segments"] == 5
    numbered_detail = client.get(f"/api/material/full/{row['id']}?numbered=true").json()
    assert numbered_detail["asr"]["model"] == material_mod.asr_model()


def test_progress_grows_while_transcribing(client, course_id, monkeypatch):
    """验收②：转写**过程中**进度在涨（同一进程里按 DB 采样两次以上）。"""
    engine = _FakeEngine(sleep=0.4)
    monkeypatch.setattr(material_mod, "ASR_AVAILABLE", True)
    monkeypatch.setattr(material_mod, "_asr_engine", lambda: engine)
    monkeypatch.setattr(material_mod, "probe_av", _ok_probe(engine.duration))
    samples: list[tuple[str, float]] = []

    class _ProbingEngine(_FakeEngine):
        def transcribe(self, path, **kwargs):
            rows_gen, info = _FakeEngine.transcribe(self, path, **kwargs)

            def _gen():
                for seg in rows_gen:
                    db = SessionLocal()
                    try:
                        job = (db.query(GenerationJob)
                               .filter(GenerationJob.job_type.like("material_asr%"))
                               .order_by(GenerationJob.created_at.desc()).first())
                        material = (db.query(SourceMaterial)
                                    .order_by(SourceMaterial.created_at.desc()).first())
                        if job is not None:
                            samples.append((str((material.meta or {}).get("asr", {}).get("status")),
                                            float((job.result or {}).get("progress") or 0.0)))
                    finally:
                        db.close()
                    yield seg

            return _gen(), info

    engine_sleep = engine.sleep
    probing = _ProbingEngine(sleep=engine_sleep)
    monkeypatch.setattr(material_mod, "_asr_engine", lambda: probing)

    resp = _upload(client, course_id, name="progress.mp4")
    assert resp.status_code == 200, resp.text
    row = resp.json()
    progresses = [p for _, p in samples if p > 0]
    assert len(progresses) >= 2, f"只采到 {progresses}（进度没写进作业）"
    assert progresses == sorted(progresses) and progresses[-1] < 1.0, progresses
    assert any(status == "running" for status, _ in samples), samples
    job = client.get(f"/api/content/jobs/{row['job_id']}").json()
    assert job["status"] == "success" and job["result"]["progress"] == 1.0


def test_retranscribe_entry_and_per_material_jobs(client, course_id, fake_asr):
    """显式「重新转写」入口 + 同课两条视频各转各的（互斥按素材，不会被 409 挡掉）。"""
    first = _upload(client, course_id, name="a.mp4").json()
    second = _upload(client, course_id, name="b.mp4").json()
    assert first["job_id"] != second["job_id"]
    assert fake_asr.calls == 2
    for row in (first, second):
        assert client.get(f"/api/content/jobs/{row['job_id']}").json()["status"] == "success"

    resp = client.post(f"/api/material/{first['id']}/transcribe")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["job_id"] and body["job_id"] != first["job_id"]
    assert fake_asr.calls == 3
    assert client.get(f"/api/content/jobs/{body['job_id']}").json()["status"] == "success"

    # 非音视频素材没有可转写的声音 ⇒ 400 + 人话
    txt = client.post("/api/material/upload", data={"course_id": course_id},
                      files={"file": ("a.txt", "光合作用".encode("utf-8"), "text/plain")}).json()
    bad = client.post(f"/api/material/{txt['id']}/transcribe")
    assert bad.status_code == 400 and "不是音视频" in bad.json()["detail"]
    # 不存在的素材 ⇒ 404
    assert client.post("/api/material/nope/transcribe").status_code == 404


def test_degradation_without_engine(client, course_id, monkeypatch):
    """验收③：没装 faster-whisper ⇒ 照旧入库 + skipped_reason 人话 + **不 500**。"""
    monkeypatch.setattr(material_mod, "ASR_AVAILABLE", False)
    resp = _upload(client, course_id, name="无引擎.mp4")
    assert resp.status_code == 200, resp.text          # 降级不 500
    row = resp.json()
    assert row["job_id"] is None, "没装引擎不该起作业"
    assert row["asr"]["status"] == "failed"
    assert "未安装语音转写组件" in row["asr"]["skipped_reason"]
    assert row["chars"] == 0 and row["kind"] == "video"
    assert set(row["asr"]) == ASR_KEYS

    listed = client.get(f"/api/material/{course_id}").json()
    assert any(m["id"] == row["id"] for m in listed), "素材行不许丢"
    detail = client.get(f"/api/material/full/{row['id']}").json()
    assert "未安装语音转写组件" in detail["asr"]["skipped_reason"]

    db = SessionLocal()
    try:
        material = db.get(SourceMaterial, row["id"])
        assert material.text == ""
        assert material_mod.asr_page_note(material).startswith("> ⚠️ 本页没有转写文字：")
    finally:
        db.close()

    # 「重新转写」显式入口：引擎没装 ⇒ 400 + 人话（不是 500）
    again = client.post(f"/api/material/{row['id']}/transcribe")
    assert again.status_code == 400
    assert "未安装语音转写组件" in again.json()["detail"]


def test_broken_and_silent_files_keep_row_and_are_retryable(client, course_id, monkeypatch):
    """验收④：损坏文件 / 无音轨 mp4 / 转写抛异常 ⇒ 原因写清、素材行不丢、可重试。"""
    monkeypatch.setattr(material_mod, "ASR_AVAILABLE", True)
    monkeypatch.setattr(material_mod, "_asr_engine", lambda: pytest.fail("不该走到转写"))
    monkeypatch.setattr(material_mod, "probe_av", lambda path: {
        "ok": False, "has_audio": False, "duration_sec": None,
        "error": "文件打不开或已损坏（Invalid data found when processing input）"})
    broken = _upload(client, course_id, name="broken.mp4", data=b"this is not an mp4").json()
    job = client.get(f"/api/content/jobs/{broken['job_id']}").json()
    assert job["status"] == "failed" and "损坏" in job["error"]
    listed = client.get(f"/api/material/{course_id}").json()
    mine = [m for m in listed if m["id"] == broken["id"]]
    assert mine, "失败也要留下素材行"
    assert mine[0]["asr"]["status"] == "failed" and "损坏" in mine[0]["asr"]["skipped_reason"]

    # 无音轨 mp4：探针能打开但没有 audio 流
    monkeypatch.setattr(material_mod, "probe_av", lambda path: {
        "ok": True, "has_audio": False, "duration_sec": 5.0,
        "error": "这个文件里没有音轨（可能是纯画面 / 纯数据），没有可转写的声音"})
    silent = _upload(client, course_id, name="noaudio.mp4").json()
    silent_job = client.get(f"/api/content/jobs/{silent['job_id']}").json()
    assert silent_job["status"] == "failed" and "没有音轨" in silent_job["error"]
    silent_detail = client.get(f"/api/material/full/{silent['id']}").json()
    assert "没有音轨" in silent_detail["asr"]["skipped_reason"]

    # 可重试：探针修好 + 引擎能跑 ⇒ 同一素材再转一次就成
    monkeypatch.setattr(material_mod, "probe_av", _ok_probe(8.0))
    monkeypatch.setattr(material_mod, "_asr_engine", lambda: _FakeEngine(sleep=0.0, duration=8.0))
    retry = client.post(f"/api/material/{broken['id']}/transcribe")
    assert retry.status_code == 200, retry.text
    detail = client.get(f"/api/material/full/{broken['id']}").json()
    assert detail["asr"]["status"] == "done" and detail["total_chars"] > 0

    # 引擎抛异常：同样只写失败原因，素材行与原件都不动
    def _boom(*args, **kwargs):
        raise RuntimeError("模型文件缺了")

    monkeypatch.setattr(material_mod, "_asr_engine", lambda: type(
        "E", (), {"transcribe": staticmethod(_boom)})())
    boom = _upload(client, course_id, name="boom.mp4").json()
    boom_job = client.get(f"/api/content/jobs/{boom['job_id']}").json()
    assert boom_job["status"] == "failed" and "模型文件缺了" in boom_job["error"]
    assert any(m["id"] == boom["id"] for m in client.get(f"/api/material/{course_id}").json())
    assert "可点「重新转写」重试" in client.get(f"/api/material/full/{boom['id']}").json()["asr"]["skipped_reason"]

    # 空文件：仍然 400 + 人话（老口径）
    empty = _upload(client, course_id, name="empty.mp4", data=b"")
    assert empty.status_code == 400 and "空文件" in empty.json()["detail"]


def test_limits_reject_with_400(client, course_id, monkeypatch):
    """验收④：超 2GB / 超 3 小时 ⇒ **400 + 人话**。

    ⚠️ 如实说明：真造 2GB 文件与 3 小时音视频（磁盘 / 时间都不划算）没有做 ——
    这里把**上限常量改小**、把**探针返回值改成 4 小时**来验证判定分支本身；
    真文件的端到端在临时五件套里跑（见交付小结 §B.4）。
    """
    monkeypatch.setattr(material_mod, "ASR_AVAILABLE", True)
    monkeypatch.setattr(material_mod, "probe_av", _ok_probe(10.0))
    monkeypatch.setattr(material_mod, "ASR_MAX_BYTES", 1024)      # 等效「上限 = 1KB」
    big = _upload(client, course_id, name="big.mp4", data=b"x" * 4096)
    assert big.status_code == 400 and "上限" in big.json()["detail"], big.text

    monkeypatch.setattr(material_mod, "ASR_MAX_BYTES", 2 * 1024 * 1024 * 1024)
    monkeypatch.setattr(material_mod, "probe_av", _ok_probe(4 * 3600.0))
    long = _upload(client, course_id, name="long.mp4")
    assert long.status_code == 400 and "3 小时" in long.json()["detail"], long.text
    # 拒收的素材**没有**入库（不留垃圾行）
    assert "音视频时长 4 小时 0 分 超过上限 3 小时" in long.json()["detail"]
    assert all(m["ext"] != ".mp4" or m["original"] != "long.mp4"
               for m in client.get(f"/api/material/{course_id}").json())


def test_two_gigabyte_cap_is_pinned_and_chunked_reader_cleans_up(tmp_path):
    """倒查 102 的缺口（DSH 复验时变异 M2「把 2GB 上限改掉」能全绿）：**真上限值**钉住。

    旧用例一律 monkeypatch ``ASR_MAX_BYTES``，从没断言过真实值 ⇒ 常量被改也没人拦；
    这里直接钉常量，并补上「逐块计数」那条分支（旧用例走的是 Content-Length 预检那条）。
    """
    assert material_mod.ASR_MAX_BYTES == 2 * 1024 * 1024 * 1024
    assert material_mod.ASR_MAX_SECONDS == 3 * 60 * 60
    assert material_mod.ASR_SEGMENT_MAX_CHARS == 120

    class _FakeUpload:                     # 不给 content-length ⇒ 绕开预检，逼出逐块计数那条
        def __init__(self, data: bytes) -> None:
            self._left = data
            self.headers: dict = {}

        async def read(self, size: int) -> bytes:
            chunk, self._left = self._left[:size], self._left[size:]
            return chunk

    dest = tmp_path / "big.bin"
    with pytest.raises(UploadTooLarge) as exc:
        asyncio.run(read_upload_to_path(_FakeUpload(b"x" * 5000), dest, 4096))
    assert "上限" in str(exc.value), str(exc.value)      # 人话：文件超过大小上限（N MB）
    assert not dest.exists(), "拒收时写了一半的原件必须删掉（不能留在盘上）"


def test_upload_over_cap_is_400_and_leaves_no_temp_dir(client, course_id, monkeypatch):
    """超上限 ⇒ **400 + 人话**，且路由的临时落盘目录一个不留（`finally` 那条要真生效）。"""
    monkeypatch.setattr(material_mod, "ASR_MAX_BYTES", 4096)
    before = set(Path(tempfile.gettempdir()).glob("cg_av_*"))
    resp = _upload(client, course_id, name="huge.mp4", data=b"x" * 20000)
    assert resp.status_code == 400 and "上限" in resp.json()["detail"], resp.text
    assert set(Path(tempfile.gettempdir()).glob("cg_av_*")) == before, "临时目录没清干净"


def test_extension_sets_and_old_formats_unchanged(client, course_id, monkeypatch):
    """不回归：四类老形态一个字没动；音视频只是**加进**白名单。"""
    assert material_mod.VIDEO_EXT == {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
    assert material_mod.AUDIO_EXT == {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
    assert material_mod.AV_EXT <= material_mod.SUPPORTED_EXT
    assert {".md", ".txt", ".csv", ".markdown"} <= material_mod.SUPPORTED_EXT
    assert {".docx", ".pdf", ".pptx"} <= material_mod.SUPPORTED_EXT
    assert {".png", ".jpg", ".jpeg", ".webp"} <= material_mod.SUPPORTED_EXT
    assert material_mod.MAX_TEXT_STORE == 200_000
    with pytest.raises(ValueError) as exc:
        material_mod.extract_text("a.xyz", b"x")
    assert "不支持的素材格式" in str(exc.value)

    # 文本素材：老路径（kind=file、没有 asr 字段、正文照旧）
    monkeypatch.setattr(material_mod, "ASR_AVAILABLE", False)
    txt = client.post("/api/material/upload", data={"course_id": course_id},
                      files={"file": ("a.md", "光合作用\n\n叶绿体".encode("utf-8"), "text/markdown")}).json()
    assert txt["kind"] == "file" and txt["asr"] is None and txt["chars"] > 0
    # 音频素材：kind=audio、没有「页」的概念（也不会去读原件字节）
    mp3 = _upload(client, course_id, name="talk.mp3", data=b"ID3\x04\x00").json()
    assert mp3["kind"] == "audio" and mp3["ext"] == ".mp3" and mp3["pages"] is None
    assert mp3["asr"]["status"] == "failed"


# ── 任务 103：中文素材默认走简体（initial_prompt）、非中文不传 ────────────────

def test_chinese_material_gets_simplified_prompt_and_records_it(client, course_id, fake_asr,
                                                               monkeypatch):
    """验收①③：中文素材 ⇒ 引擎收到简体引导语；``meta.asr.initial_prompt`` 如实记全文。"""
    monkeypatch.delenv("ASR_DOMAIN_PROMPT", raising=False)
    fake_asr.language = "zh"
    row = _upload(client, course_id, name="中文课.mp4").json()
    assert fake_asr.kwargs.get("initial_prompt") == material_mod.ASR_ZH_PROMPT
    detail = client.get(f"/api/material/full/{row['id']}").json()
    assert detail["asr"]["initial_prompt"] == material_mod.ASR_ZH_PROMPT
    assert detail["asr"]["language"] == "zh"
    assert set(detail["asr"]) == ASR_KEYS


def test_non_chinese_material_gets_no_prompt(client, course_id, fake_asr):
    """验收②：非中文**不传**引导语（别把英文素材带偏），meta 里如实记空串。"""
    row = _upload(client, course_id, name="english.mp4").json()      # 假引擎语种 = en
    assert "initial_prompt" not in fake_asr.kwargs
    assert client.get(f"/api/material/full/{row['id']}").json()["asr"]["initial_prompt"] == ""


def test_domain_prompt_off_by_default_recorded_when_on(client, course_id, fake_asr,
                                                       monkeypatch):
    """§A3：领域引导语**默认关**；开了把课程标题 / 学科拼进去，并如实落库全文。"""
    fake_asr.language = "zh"
    monkeypatch.delenv("ASR_DOMAIN_PROMPT", raising=False)
    off = _upload(client, course_id, name="domain_off.mp4").json()
    assert (client.get(f"/api/material/full/{off['id']}").json()["asr"]["initial_prompt"]
            == material_mod.ASR_ZH_PROMPT)

    monkeypatch.setenv("ASR_DOMAIN_PROMPT", "true")
    on = _upload(client, course_id, name="domain_on.mp4").json()
    prompt = client.get(f"/api/material/full/{on['id']}").json()["asr"]["initial_prompt"]
    assert prompt.startswith(material_mod.ASR_ZH_PROMPT)
    assert "光合作用入门" in prompt and "生物" in prompt      # conftest 建的课：标题 + 学科
    assert fake_asr.kwargs["initial_prompt"] == prompt       # 引擎收到的与落库的一字不差


def test_asr_prompt_rule_is_chinese_only():
    """纯函数口径：中文（含 zh-CN）给引导语；en / yue / 空串一律不给（带偏的坑就在这）。"""
    assert material_mod.asr_prompt("zh") == material_mod.ASR_ZH_PROMPT
    assert material_mod.asr_prompt("ZH") == material_mod.ASR_ZH_PROMPT
    assert material_mod.asr_prompt("zh-CN") == material_mod.ASR_ZH_PROMPT
    assert material_mod.asr_prompt("zh", domain_hint="") == material_mod.ASR_ZH_PROMPT
    for other in ("en", "yue", "ja", ""):
        assert material_mod.asr_prompt(other) == ""
    assert material_mod.is_chinese_language("zh") and not material_mod.is_chinese_language("yue")

    hint = "本课讲：多菌共生体系。"
    assert material_mod.asr_prompt("en", domain_hint=hint) == ""          # 非中文连领域语都不给
    assert material_mod.asr_prompt("zh", domain_hint=hint) == material_mod.ASR_ZH_PROMPT + hint

    assert material_mod._env_true("ASR_DOMAIN_PROMPT_NEVER_SET") is False
    for on in ("1", "true", "TRUE", "yes", "on"):
        os.environ["_CG103_FLAG"] = on
        assert material_mod._env_true("_CG103_FLAG") is True
    for off in ("", "0", "no", "off", "随便"):
        os.environ["_CG103_FLAG"] = off
        assert material_mod._env_true("_CG103_FLAG") is False
    del os.environ["_CG103_FLAG"]


# ── 真引擎用例：SAPI 现场合成素材（可选依赖 / 缓存 / 语音包缺一就跳过）──────────

_SAPI_PS1 = """param([string]$Text, [string]$Out, [string]$Voice)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice($Voice)
$s.SetOutputToWaveFile($Out)
$s.Speak($Text)
$s.Dispose()
"""


def _sapi_wav(text: str, voice: str, out: Path) -> bool:
    """用 Windows SAPI **现场**合成一段 wav（样例不进仓库）；没这个语音 / 失败就是 False。"""
    if sys.platform != "win32":
        return False
    script = out.with_suffix(".ps1")
    script.write_text(_SAPI_PS1, encoding="utf-8-sig")   # 带 BOM：PS 5.1 才按 UTF-8 读中文
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Text", text, "-Out", str(out), "-Voice", voice],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and out.exists() and out.stat().st_size > 0


def test_real_engine_chinese_is_simplified_english_untouched(tmp_path):
    """验收①②：真 faster-whisper + 现场合成的素材 —— 中文**不含繁体常用字**、英文不被带偏。

    跳过条件（任一）：没装可选依赖 / 本机没缓存模型（不替用户下 0.5GB）/ 没有对应语音包。
    """
    pytest.importorskip("faster_whisper", reason="可选依赖未装（backend/requirements-asr.txt）")
    hf_home = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    model = material_mod.asr_model()
    if not (hf_home / "hub" / f"models--Systran--faster-whisper-{model}").exists():
        pytest.skip(f"本机没缓存 {model} 模型（不替用户偷偷下载）")

    zh = tmp_path / "zh.wav"
    if not _sapi_wav(SAPI_ZH_TEXT, "Microsoft Huihui Desktop", zh):
        pytest.skip("本机没有中文 SAPI 语音（Microsoft Huihui Desktop）")
    out = material_mod.transcribe_av(zh)
    assert out["initial_prompt"] == material_mod.ASR_ZH_PROMPT
    hits = "".join(sorted({ch for ch in out["text"] if ch in TRAD_ONLY}))
    assert not hits, f"转写文本里出现繁体字：{hits}（{out['text']}）"

    en = tmp_path / "en.wav"
    if _sapi_wav(SAPI_EN_TEXT, "Microsoft Zira Desktop", en):
        out_en = material_mod.transcribe_av(en)
        assert out_en["initial_prompt"] == ""
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in out_en["text"]), out_en["text"]


# ── 任务 104：模型名走 `.env`（Settings 回落）+ 进程环境变量仍然优先 ───────────────

# 必须**新进程**里跑：Settings 有 lru_cache，且 env_file=".env" 相对 cwd ⇒ 同进程改 .env 不生效
_ASR_MODEL_PROBE = """
from app.services.material import asr_model

print(asr_model())
"""


def _probe_asr_model(workdir: Path, env: dict[str, str]) -> str:
    proc = subprocess.run([sys.executable, "-c", _ASR_MODEL_PROBE], cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"子进程跑不通：{proc.stderr[-400:]}"
    return proc.stdout.strip().splitlines()[-1]


def test_asr_model_reads_dotenv_and_process_env_wins(tmp_path):
    """验收①②：`.env` 的 ``ASR_MODEL`` 真的被读到；**进程环境变量仍然优先**（102 的口径不变）。"""
    backend = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "ASR_MODEL"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(backend), env.get("PYTHONPATH")]))
    env.pop("PYTHONIOENCODING", None)
    dotenv = tmp_path / ".env"
    miss = "`.env` 里的 ASR_MODEL 没被读到（只认进程环境变量）"

    dotenv.write_text("ASR_MODEL=medium\n", encoding="utf-8")
    assert _probe_asr_model(tmp_path, env) == "medium", miss
    dotenv.write_text("ASR_MODEL=tiny\n", encoding="utf-8")
    assert _probe_asr_model(tmp_path, env) == "tiny", miss
    dotenv.unlink()
    assert _probe_asr_model(tmp_path, env) == "small", "没配时应回落 ASR_MODEL_DEFAULT=small"

    dotenv.write_text("ASR_MODEL=small\n", encoding="utf-8")
    assert _probe_asr_model(tmp_path, dict(env, ASR_MODEL="medium")) == "medium", (
        "进程环境变量必须仍然优先（任务 102 的实测口径）")
