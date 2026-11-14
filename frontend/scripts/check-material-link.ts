/**
 * 任务 118 前端断言：素材的第三种来源 —— **从链接导入**（YouTube）。
 *
 * 规格（任务书 §二 / HANDOFF 文末「任务 118 说明」）要求六件事，缺一条就是坑：
 * ① 入口：素材页有「从链接导入」，后端先落一条 `SourceMaterial`（`kind="video"` + `meta.link`）；
 * ② 顺序**写死**：① 抓字幕（en / zh-Hans）→ ② 没字幕才下音频走**既有**本地 ASR →
 *    ③ 都不行则明确失败原因、**素材行保留可重试**；三种情况都写进 `meta.link`；
 * ③ 外部依赖探测**复用**任务 116 的 `capabilities`（yt-dlp / JS 运行时）：只查可执行文件与版本、
 *    **不联网**；缺了要在界面说人话（缺什么 / 影响什么 / 怎么装），**⛔ 不许 500**；
 * ④ 限流：每请求 ≥4s、同一列表内两条之间 ≥20s、429 指数退避（8→16→32，封顶 64）、上限 3 次；
 *    进度走**既有** `GET /content/jobs/{job_id}`（不新造第二套进度机制）；
 * ⑤ cookie 只按**路径**交给 yt-dlp：不复制、不进日志、不进接口返回、不写进库；
 *    （任务 119：`YTDLP_COOKIES` 的默认值与 `YTDLP_EXE` / `YTDLP_CMD` 走**同一条回落链** ——
 *    进程环境变量优先 ⇒ `.env`（经 Settings）；以前只读 `os.environ`，往 `.env` 里写不生效）
 * ⑥ **0 LLM**：抓取与转写都在本地（转写复用任务 102 / 105 的既有链路）。
 *
 * 三类断言：① 源码级（含「探测不联网」与「cookie 只留布尔」的核对）② 行为级（真 import
 * `app.services.link_import`：链接解析 / 间隔与退避 / 字幕去重 / **有字幕不许出现 `-x`** /
 * cookie 默认值的回落链（真子进程 + 临时 `.env`））③ 变异自检（源码 5 条 + 行为 2 条：
 * 把「先字幕后音频」反过来、把回落链里「读 Settings」那步去掉，都必须判红）。
 *
 * 运行：`node scripts/check-material-link.ts`（Node >= 22.18 自带类型擦除）
 * 失败以非零码退出，已接入 scripts/verify.ps1。
 */
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const failures: string[] = [];

const readRepoFile = (rel: string) =>
  readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

const LINK_REL = "backend/app/services/link_import.py";
const ROUTE_REL = "backend/app/api/routes/material.py";
const MATERIAL_REL = "backend/app/services/material.py";
const CAP_REL = "backend/app/services/capabilities.py";
const CONFIG_REL = "backend/app/core/config.py";
const STEP_REL = "frontend/src/components/steps/MaterialsStep.tsx";
const TYPES_REL = "frontend/src/types.ts";
const API_REL = "frontend/src/api.ts";

type Src = {
  link: string; route: string; material: string; cap: string; config: string;
  step: string; types: string; api: string;
};

/** 取函数体：从 `def name(` 到下一个顶格 `def` / `class` / `@` 之前（比正则抠更稳）。 */
function body(src: string, name: string): string {
  const at = src.indexOf(`def ${name}(`);
  if (at < 0) return "";
  const rest = src.slice(at);
  const next = rest.slice(1).search(/\n(?:def |class |@)/);
  return next < 0 ? rest : rest.slice(0, next + 1);
}

/** 出现次数（断言「就是这个形状、且只出现这么多次」用）。 */
function count(src: string, needle: string): number {
  return src.split(needle).length - 1;
}
/** 源码级审计（抽成函数：下面拿「变异后的源码」做判定自检，断言才不是摆设）。 */
function audit(s: Src): string[] {
  const bad: string[] = [];

  // ── ① 抓取口径：顺序写死 + 三种情况都写 meta + 限流参数 ──────────────────
  if (!/LINK_SOURCE = "youtube"/.test(s.link)) bad.push(`${LINK_REL} 少了 LINK_SOURCE`);
  if (!/SUB_LANGS = \("en", "zh-Hans", "zh-Hant", "zh"\)/.test(s.link)) {
    bad.push("字幕语种优先级变了（规格：en / zh-Hans 优先）");
  }
  for (const [needle, label] of [
    ["RATE_MIN_INTERVAL = 4.0", "每请求间隔 ≥4 秒"],
    ["RATE_PLAYLIST_INTERVAL = 20.0", "同一列表内两条之间 ≥20 秒"],
    ["RATE_MAX_ATTEMPTS = 3", "429 重试上限 3 次"],
    ["RATE_BACKOFF_BASE = 8.0", "退避基数 8s"],
    ["RATE_BACKOFF_CAP = 64.0", "退避封顶 64s"],
  ] as const) {
    if (!s.link.includes(needle)) bad.push(`限流口径少了「${label}」（${needle}）`);
  }
  if (!/return min\(RATE_BACKOFF_CAP, RATE_BACKOFF_BASE \* \(2 \*\* max\(0, attempt - 1\)\)\)/.test(s.link)) {
    bad.push("backoff_delay 不是指数退避（8→16→32，封顶 64）");
  }
  if (!/def wait\(self, \*, item_switch: bool = False\) -> float:/.test(s.link)) {
    bad.push("RateLimiter.wait 不见了（限流靠它落地）");
  }
  if (!/if not is_rate_limited\(run\.out\) or attempt >= max\(1, opts\.max_attempts\):/.test(s.link)) {
    bad.push("RateLimiter.run 没有「429 退避重试 + 明确上限」");
  }

  const one = body(s.link, "import_one");
  if (!one) bad.push(`${LINK_REL} 里没有 import_one`);
  const subAt = one.indexOf("fetch_subtitle(");
  const audioAt = one.indexOf("download_audio(");
  if (subAt < 0 || audioAt < 0 || subAt > audioAt) {
    bad.push("抓取顺序不是「先字幕后音频」（规格 §二.2：① 字幕 → ② 音频 → ③ 明确失败）");
  }
  if (!one.includes('result.update({"path": "subtitle"')) {
    bad.push("import_one 没把「抓到了字幕」写进结果（path=subtitle）");
  }
  if (!one.includes('result.update({"path": "asr"')) {
    bad.push("import_one 没把「走了本地转写」写进结果（path=asr）");
  }
  if (!/if not material_service\.asr_available\(\):/.test(one)) {
    bad.push("没字幕时没有先探「本机有没有转写组件」（缺了要写人话、不许 500）");
  }
  const failed = body(s.link, "_finish_failed");
  if (!failed.includes('"status": "failed"')) {
    bad.push("_finish_failed 没写失败态（素材行与原因必须留在库里）");
  }
  if (!failed.includes("素材行已保留")) {
    bad.push("失败态没写「素材行已保留、可重试」这句人话");
  }
  const runner = body(s.link, "run_link_import");
  if (!runner.includes('code="no_tool"')) {
    bad.push("run_link_import 在缺 yt-dlp 时没有走「缺工具」那条失败态");
  }
  if (!runner.includes('"status": "done" if text.strip() else "failed"')) {
    bad.push("run_link_import 没有「抓到字 / 没抓到」两种状态的如实记录");
  }

  const errLine = body(s.link, "_first_error_line");
  if (!errLine.includes('if line.upper().startswith("ERROR:")')) {
    bad.push("报错取行没有优先挑 ERROR:（yt-dlp 会先打一行 null，会变成「抓取失败：null」）");
  }
  if (!s.link.includes('"this video is unavailable"')) {
    bad.push("GONE_PATTERNS 少了真机的原话（This video is unavailable）—— 会被归成「其它错误」");
  }

  // ── ② 外部依赖探测：只查文件 / 版本，不联网 ─────────────────────────────
  const probe = body(s.link, "_probe_version");
  if (!probe.includes('"--version"')) bad.push("yt-dlp 探测不是跑 --version（只查版本，不联网）");
  for (const flag of ["--dump", "--flat-playlist", "--write-subs", "--extract-audio", "http://", "https://"]) {
    if (probe.includes(flag)) {
      bad.push(`yt-dlp 探测里带了网络参数 ${flag}（规格 §二.3：只查可执行文件 / 版本）`);
    }
  }
  const js = body(s.link, "js_runtime_probe");
  if (!js) bad.push("少了 js_runtime_probe（JS 运行时探测）");
  for (const needle of ["subprocess", "check_output", "run_ytdlp"]) {
    if (js.includes(needle)) bad.push(`js_runtime_probe 不该起进程（只查文件在不在）：${needle}`);
  }
  for (const [needle, label] of [
    ["YTDLP_PROBE_CACHE", "yt-dlp 探测结果缓存（health 不能每读一次起一次进程）"],
    ["JS_RUNTIME_CACHE", "JS 运行时探测结果缓存"],
    ["YTDLP_MISSING_REASON", "缺 yt-dlp 的人话"],
    ["JS_RUNTIME_MISSING_REASON", "缺 JS 运行时的人话"],
    ["YTDLP_INSTALL", "yt-dlp 装法"],
    ["JS_RUNTIME_INSTALL", "JS 运行时装法"],
  ] as const) {
    if (!s.link.includes(needle)) bad.push(`少了「${label}」（${needle}）`);
  }
  // cookie 默认值的回落链改由「行为级 ⑦」盯（任务 119）：源码正则只认实现细节，
  // 一重构就误报、却钉不住「`.env` 真生效、进程环境变量仍优先」这个真正要的行为。

  // ── ③ cookie 口径（安全第一）：只按路径给 yt-dlp，其它地方只留布尔 ──────
  if (!/args \+= \["--cookies", str\(opts\.cookies\)\]/.test(s.link)) {
    bad.push("cookie 不是「只按路径」交给 yt-dlp（应是 --cookies <path>）");
  }
  if (count(s.link, '"cookies": bool(opts.cookies)') < 2) {
    bad.push("meta.link 里的 cookie 只许留布尔（带没带 / 域对不对）—— 不许把路径或值写进库");
  }
  if (/"cookies":\s*str\(/.test(s.link) || s.link.includes('"cookies_path"')) {
    bad.push("meta.link 把 cookie 路径写进去了（规格 §二.5：不落库）");
  }

  // ── ④ 0 LLM 护栏：这条链路里不许出现任何模型调用 ────────────────────────
  for (const needle of ["openai", "llm_service", "call_llm", "requests.get", "httpx", "urllib.request"]) {
    if (s.link.includes(needle)) bad.push(`链接导入链路里出现了 ${needle}（硬规则：0 LLM、不额外联网）`);
  }
  // ── ⑤ 路由：异步入队 + 既有作业口径 + 降级不 500 ────────────────────────
  if (!s.route.includes('@router.post("/import-link")')) bad.push(`少了 POST /import-link（入口）`);
  if (!s.route.includes('@router.post("/{material_id}/import-link")')) {
    bad.push("少了「重新导入」入口（失败后必须能重试）");
  }
  if (!/_LINK_JOB_PREFIX = "material_link:"/.test(s.route)) {
    bad.push("链接导入没有自己的作业类型前缀（与生成混用会互相顶掉）");
  }
  for (const [needle, label] of [
    ["acquire_scope(", "acquire_scope（进程内互斥）"],
    ["claim_job_exclusive(", "claim_job_exclusive（开工声明）"],
    ["release_scope(", "release_scope（收工释放）"],
    ["GenerationJob(", "GenerationJob（既有作业表）"],
    ["job_row.result = {", "作业进度写 result"],
    ['"material_id": material_id, "progress": ratio', "进度 = 抓成的条数 ÷ 总条数"],
  ] as const) {
    if (!s.route.includes(needle)) bad.push(`路由少了 ${label}`);
  }
  if (/__tablename__/.test(s.route)) {
    bad.push("路由里新造了一张表（规格 §二.4：进度必须走既有作业口径）");
  }
  if (!/if probe\["available"\]:/.test(s.route)) bad.push("缺 yt-dlp 时没有分叉（降级那条路不见了）");
  if (!/message = \(str\(result\.get\("skipped_reason"\) or "没能抓取"\)/.test(s.route)) {
    bad.push("缺工具时的返回没带「为什么没抓成」的人话");
  }
  if (!s.route.includes("YTDLP_INSTALL")) bad.push("缺工具时没告诉用户怎么装");
  if (!/raise HTTPException\(400, str\(exc\)\)/.test(s.route)) {
    bad.push("无效链接不是 400 人话（脏链接会落进库）");
  }
  if (!/"link": \(m\.meta or \{\}\)\.get\("link"\) or None/.test(s.route)) {
    bad.push("_out 没带 link 字段（前端拿不到抓取状态）");
  }
  if (!/bool\(cookies\)/.test(s.route)) bad.push("日志里写 cookie 只许写布尔（不许写路径）");
  if (!/原件不在本机，请点「重新导入」重跑抓取/.test(s.route)) {
    bad.push("链接素材走 /transcribe 时没有 400 人话（会变成 500）");
  }

  // ── ⑥ 素材侧：第四种来源 + 先落行、后写结果 ─────────────────────────────
  for (const [needle, label] of [
    ['ORIGIN_LINK = "link"', "ORIGIN_LINK"],
    ['ORIGIN_LINK: "链接导入（YouTube）",', "来源标签"],
    ["MUST_CLEAN_ORIGINS = frozenset({ORIGIN_PASTE, ORIGIN_AI})", "「必须清理」那一档不含链接素材"],
    ["def add_material_link(", "add_material_link()"],
    ["def set_link_import(", "set_link_import()"],
    ["def asr_meta(", "asr_meta()（链接导入复用同一份 meta.asr 骨架）"],
    ['"status": "pending"', "先落 pending 行（抓取前就有行，失败才有地方写原因）"],
    ['meta["link"] = link', "meta.link 落库"],
  ] as const) {
    if (!s.material.includes(needle)) bad.push(`素材侧少了「${label}」（${needle}）`);
  }
  if (!body(s.material, "add_material_link").includes('kind="video"')) {
    bad.push("链接素材的 kind 不是 video（讲义侧的时间戳锚点认它）");
  }

  // ── ⑦ 能力清单（复用任务 116 的口径，别新造一套）───────────────────────
  for (const [needle, label] of [
    ['YTDLP_KEY = "ytdlp"', "YTDLP_KEY"],
    ['JS_RUNTIME_KEY = "js_runtime"', "JS_RUNTIME_KEY"],
    ["def ytdlp_capability()", "ytdlp_capability()"],
    ["def js_runtime_capability()", "js_runtime_capability()"],
    ["ytdlp_capability(), js_runtime_capability()]", "能力清单里并入这两项"],
  ] as const) {
    if (!s.cap.includes(needle)) bad.push(`能力清单少了 ${label}`);
  }
  for (const name of ["ytdlp_capability", "js_runtime_capability"]) {
    const fn = body(s.cap, name);
    for (const key of ["available", "install", "reason", "affects", "unaffected", "probe"]) {
      if (!fn.includes(`"${key}"`)) bad.push(`${name} 少了 ${key} 字段（缺什么 / 影响什么 / 怎么装）`);
    }
  }
  if (!s.cap.includes("--version") || !s.cap.includes("不联网")) {
    bad.push("能力清单没说清「只查版本、不联网」");
  }
  if (!/ytdlp_cookies: str = ""/.test(s.config)) bad.push("配置里少了 ytdlp_cookies（cookie 路径不落库，得能配）");

  // ── ⑧ 前端入口与降级人话 ───────────────────────────────────────────────
  if (!/export interface MaterialLink \{/.test(s.types)) bad.push(`${TYPES_REL} 少了 MaterialLink`);
  if (!/link\?: MaterialLink \| null;/.test(s.types)) bad.push(`${TYPES_REL} 的 MaterialRow 没带 link`);
  if (!/importMaterialLink: \(data: \{ course_id: string; url: string; title\?: string; cookies\?: string \}\) =>/.test(s.api)) {
    bad.push(`${API_REL} 少了 importMaterialLink`);
  }
  if (!/reimportMaterialLink: \(id: string, data\?: \{ cookies\?: string \}\) =>/.test(s.api)) {
    bad.push(`${API_REL} 少了 reimportMaterialLink`);
  }
  if (!/capabilities\?: \{/.test(s.api)) bad.push(`${API_REL} 的 HealthPayload 没带 capabilities`);
  for (const [needle, label] of [
    ["LinkOutlined", "LinkOutlined 图标"],
    [">从链接导入</Button>", "「从链接导入」按钮"],
    ["function LinkStatusTag(", "素材行的抓取状态标签"],
    ["const linkMissing", "缺组件时的警示块"],
    ["item.install", "「怎么装」的落点"],
    ["item.key === \"ytdlp\" || item.key === \"js_runtime\"", "只挑与「从链接导入」相关的两条能力"],
    ['name="cookies"', "cookie 路径输入框"],
    ["api.importMaterialLink", "首次导入的接线"],
    ["api.reimportMaterialLink", "重新导入的接线"],
    ['"重新导入" : "重试导入"', "失败后「重试导入」的说法"],
    ["cookies.txt 路径（可选）", "cookie 只给路径、不复制不进日志的说明"],
  ] as const) {
    if (!s.step.includes(needle)) bad.push(`${STEP_REL} 少了 ${label}`);
  }
  if (!s.step.includes("不调模型")) {
    bad.push("弹窗里没有「不调模型 / 不花钱」这句（0 LLM 要让用户看得见）");
  }

  return bad;
}
/* ---------------- 行为级：真 import 跑一遍（不联网、不起进程）--------------- */

type RunOut = {
  parse: Record<string, { kind?: string; video_id?: string; playlist_id?: string; err?: string }>;
  backoff: number[];
  waits: number[];
  retry: { code: number; calls: number; sleeps: number[] };
  giveup: { code: number; calls: number; sleeps: number[] };
  cues: string[];
  first_line: string;
  segments: number;
  classify: Record<string, string>;
  cookie: { youtube_domain: boolean; domains: string[]; leak: boolean };
  order_subs: { path: string; audio: boolean };
  order_nosubs: { path: string; audio: boolean };
};

const BEHAVIOR = (modName?: string) => `
import json, sys, tempfile
from pathlib import Path

repo = Path(r"${repo}")
sys.path.insert(0, str(repo / "backend"))
if ${modName ? "True" : "False"}:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("mut_link", r"${modName ?? ""}")
    L = importlib.util.module_from_spec(_spec)
    sys.modules["mut_link"] = L
    _spec.loader.exec_module(L)
else:
    from app.services import link_import as L


class FakeClock:
    """假时钟：不真等 4 / 20 秒，但「等了多久」照样能被断言。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += float(seconds)


VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
alpha one

00:00:02.000 --> 00:00:04.000
alpha one

00:00:04.000 --> 00:00:06.000
beta two
"""


def _out_dir(args):
    return Path(args[args.index("-o") + 1]).parent


class FakeYTDLP:
    """假 yt-dlp：有字幕就写 .vtt，没字幕才写音频（顺序反了会立刻暴露）。"""

    def __init__(self, subs):
        self.subs = subs
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        folder = _out_dir(args)
        vid = args[-1].split("v=")[-1]
        if "--skip-download" in args:
            if vid in self.subs:
                (folder / (vid + ".en.vtt")).write_text(self.subs[vid], encoding="utf-8")
            return L.ToolRun(list(args), 0, out="")
        if "-x" in args:
            (folder / (vid + ".m4a")).write_bytes(b"FAKE-AUDIO")
            return L.ToolRun(list(args), 0, out="")
        return L.ToolRun(list(args), 1, out="ERROR: unexpected yt-dlp call")

    @property
    def audio(self):
        return any("-x" in call for call in self.calls)


class StubMaterial:
    """挡住真素材服务（这里只验顺序与分支；meta.link 落库由 pytest 用例盯）。"""

    ASR_SKIP_REASON = "no local asr"
    merge_asr_segments = staticmethod(L.material_service.merge_asr_segments)
    asr_segments_to_text = staticmethod(L.material_service.asr_segments_to_text)

    def asr_available(self):
        return True

    def asr_domain_hint(self, db, row):
        return ""

    def asr_meta(self, status="done", **kwargs):
        return {"engine": "faster-whisper", "model": "small", "status": status}

    def asr_model(self):
        return "small"


ITEM = {"id": "AAAAAAAAAAA", "title": "demo", "duration_sec": 12,
        "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"}


def _transcribe(path, **kwargs):
    return {"text": "[00:00] hi there", "segments": [{"start": 0, "end": 1, "text": "hi there"}],
            "language": "en", "model": "small", "duration_sec": 12,
            "avg_logprob": -0.2, "initial_prompt": ""}


def _order_case(subs, work_dir):
    fake = FakeYTDLP(subs)
    real = L.material_service
    L.material_service = StubMaterial()
    try:
        opts = L.FetchOptions(runner=fake, sleep=lambda seconds: None, clock=FakeClock(),
                              work_dir=work_dir, transcribe=_transcribe)
        limiter = L.RateLimiter(clock=FakeClock(), sleep=lambda seconds: None)
        out = L.import_one(ITEM, opts=opts, limiter=limiter, work=work_dir, db=None, row=None)
        return {"path": str(out["path"]), "audio": bool(fake.audio)}
    finally:
        L.material_service = real


work = Path(tempfile.mkdtemp())
for name in ("subs", "nosubs"):
    (work / name).mkdir(exist_ok=True)

clock = FakeClock()
limiter = L.RateLimiter(clock=clock, sleep=clock.sleep)
limiter.wait()
limiter.wait()
limiter.wait(item_switch=True)

calls = {"n": 0}


def _flaky(args):
    calls["n"] += 1
    if calls["n"] <= 2:
        return L.ToolRun(list(args), 1, out="ERROR: HTTP Error 429: Too Many Requests")
    return L.ToolRun(list(args), 0, out="{}")


sleeps = []
clock2 = FakeClock()
limiter2 = L.RateLimiter(clock=clock2, sleep=sleeps.append)
res2 = limiter2.run(["x"], L.FetchOptions(runner=_flaky, sleep=sleeps.append, clock=clock2))

calls2 = {"n": 0}


def _always_limited(args):
    calls2["n"] += 1
    return L.ToolRun(list(args), 1, out="ERROR: HTTP Error 429: Too Many Requests")


sleeps2 = []
clock3 = FakeClock()
limiter3 = L.RateLimiter(clock=clock3, sleep=sleeps2.append)
res3 = limiter3.run(["x"], L.FetchOptions(runner=_always_limited, sleep=sleeps2.append, clock=clock3))

body_text, segs = L.subtitle_to_transcript(VTT, name="AAAAAAAAAAA.en.vtt")
cue_texts = [cue["text"] for cue in L.parse_subtitle_text(VTT, name="AAAAAAAAAAA.en.vtt")]

cookie_file = work / "cookies.txt"
cookie_file.write_text("# Netscape HTTP Cookie File\\n"
                       ".youtube.com\\tTRUE\\t/\\tTRUE\\t0\\tSID\\tSECRET-VALUE-123\\n",
                       encoding="utf-8")
info = L.inspect_cookies(cookie_file)


def _parse(url):
    try:
        parsed = L.parse_link(url)
        return {"kind": parsed["kind"], "video_id": parsed["video_id"],
                "playlist_id": parsed["playlist_id"]}
    except ValueError as exc:
        return {"err": str(exc)[:24]}


print(json.dumps({
    "parse": {
        "watch": _parse("https://www.youtube.com/watch?v=AAAAAAAAAAA&t=30"),
        "short": _parse("https://youtu.be/AAAAAAAAAAA?t=30"),
        "shorts": _parse("https://www.youtube.com/shorts/AAAAAAAAAAA"),
        "playlist": _parse("https://www.youtube.com/playlist?list=PLabcdefghij"),
        "bad": _parse("https://vimeo.com/12345"),
    },
    "backoff": [L.backoff_delay(n) for n in (1, 2, 3, 4)],
    "waits": [round(wait, 3) for wait in limiter.waits],
    "retry": {"code": res2.code, "calls": calls["n"], "sleeps": sleeps},
    "giveup": {"code": res3.code, "calls": calls2["n"], "sleeps": sleeps2},
    "cues": cue_texts,
    "first_line": (body_text.splitlines() or [""])[0],
    "segments": len(segs),
    "classify": {
        "login": L.classify_tool_error("ERROR: Sign in to confirm you're not a bot")[0],
        "rate": L.classify_tool_error("ERROR: HTTP Error 429: Too Many Requests")[0],
        "js": L.classify_tool_error("ERROR: No supported JavaScript runtime found")[0],
        "gone": L.classify_tool_error("ERROR: Video unavailable")[0],
        "gone_real": L.classify_tool_error(
            "null\\nERROR: [youtube] AAAAAAAAAAA: This video is unavailable")[0],
    },
    "cookie": {"youtube_domain": bool(info["youtube_domain"]), "domains": info["domains"],
               "leak": "SECRET-VALUE-123" in json.dumps(info, ensure_ascii=False)},
    "order_subs": _order_case({"AAAAAAAAAAA": VTT}, work / "subs"),
    "order_nosubs": _order_case({}, work / "nosubs"),
}, ensure_ascii=False))
`;
/** 解释器路径：优先仓库里的 venv（与 verify.ps1 跑的是同一个）。 */
const pythonExe = () =>
  existsSync(join(repo, "backend/.venv/Scripts/python.exe"))
    ? join(repo, "backend/.venv/Scripts/python.exe")
    : (process.platform === "win32" ? "python" : "python3");

/** 子进程里问一次 default_cookies()：**cwd = 临时目录**（`.env` 相对 cwd 读）。
 *
 * 任务 119：cookie 的默认值要与 YTDLP_EXE / YTDLP_CMD 走同一条回落链（进程环境变量优先 ⇒
 * `.env`）。必须用真子进程 —— Settings 有 lru_cache，同进程里换了 `.env` 也读不出来。
 */
function probeDefaultCookies(
  dir: string, name: string, opts: { envCookies?: string; mutFile?: string } = {},
): string {
  const probe = join(dir, name);
  writeFileSync(probe, `
import json, sys
sys.path.insert(0, sys.argv[1])
if len(sys.argv) > 2 and sys.argv[2]:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("probe_link", sys.argv[2])
    L = importlib.util.module_from_spec(_spec)
    sys.modules["probe_link"] = L
    _spec.loader.exec_module(L)
else:
    from app.services import link_import as L
print(json.dumps({"cookies": L.default_cookies()}))
`, "utf8");
  const env = { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" } as Record<string, string | undefined>;
  for (const key of Object.keys(env)) {
    if (key.toUpperCase().startsWith("YTDLP_")) delete env[key];
  }
  if (opts.envCookies !== undefined) env.YTDLP_COOKIES = opts.envCookies;
  const result = spawnSync(pythonExe(), [probe, join(repo, "backend"), opts.mutFile ?? ""],
    { cwd: dir, encoding: "utf8", env });
  if (result.status !== 0) {
    throw new Error(`cookie 探针跑不通（exit=${result.status}）：${(result.stderr ?? "").slice(-600)}`);
  }
  return (JSON.parse((result.stdout ?? "").trim().split("\n").pop() ?? "").cookies ?? "") as string;
}

function runPython(script: string, tmp: string, name: string): RunOut {
  const python = pythonExe();
  const file = join(tmp, name);
  writeFileSync(file, script, "utf8");
  const result = spawnSync(python, [file], {
    cwd: repo, encoding: "utf8",
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
  });
  if (result.status !== 0) {
    throw new Error(`行为级跑不通（exit=${result.status}）：${(result.stderr ?? "").slice(-600)}`);
  }
  return JSON.parse((result.stdout ?? "").trim().split("\n").pop() ?? "") as RunOut;
}

/* ---------------- 主流程 ---------------- */

const src: Src = {
  link: readRepoFile(LINK_REL),
  route: readRepoFile(ROUTE_REL),
  material: readRepoFile(MATERIAL_REL),
  cap: readRepoFile(CAP_REL),
  config: readRepoFile(CONFIG_REL),
  step: readRepoFile(STEP_REL),
  types: readRepoFile(TYPES_REL),
  api: readRepoFile(API_REL),
};

failures.push(...audit(src));

const tmp = mkdtempSync(join(tmpdir(), "cg_link118_"));
let out: RunOut;
try {
  out = runPython(BEHAVIOR(), tmp, "check118.py");
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

// 行为级 ①：链接解析（视频 / 播放列表 / 无效）
if (out.parse.watch.kind !== "video" || out.parse.watch.video_id !== "AAAAAAAAAAA") {
  failures.push(`watch 链接解析不对：${JSON.stringify(out.parse.watch)}`);
}
if (out.parse.short.video_id !== "AAAAAAAAAAA" || out.parse.shorts.kind !== "video") {
  failures.push(`youtu.be / shorts 解析不对：${JSON.stringify([out.parse.short, out.parse.shorts])}`);
}
if (out.parse.playlist.kind !== "playlist" || out.parse.playlist.playlist_id !== "PLabcdefghij") {
  failures.push(`播放列表解析不对：${JSON.stringify(out.parse.playlist)}`);
}
if (!out.parse.bad.err) failures.push("无效链接没有报错（脏链接会落进库）");

// 行为级 ②：顺序写死 —— 有字幕**不许**下音频；没字幕才下音频走本地转写
if (out.order_subs.path !== "subtitle" || out.order_subs.audio) {
  failures.push(`有字幕却下了音频 / 走的不是字幕那条路：${JSON.stringify(out.order_subs)}`
    + "（规格 §二.2 顺序写死：先字幕，没字幕才下音频）");
}
if (out.order_nosubs.path !== "asr" || !out.order_nosubs.audio) {
  failures.push(`没字幕时没走「下音频 + 本地转写」：${JSON.stringify(out.order_nosubs)}`);
}

// 行为级 ③：限流与退避
if (out.waits.join(",") !== "0,4,20") {
  failures.push(`限流间隔不对（应 0 / 4 / 20 秒）：${out.waits.join(",")}`);
}
if (out.backoff.join(",") !== "8,16,32,64") {
  failures.push(`指数退避不对（应 8→16→32→封顶 64）：${out.backoff.join(",")}`);
}
if (out.retry.calls !== 3 || out.retry.sleeps.join(",") !== "8,16" || out.retry.code !== 0) {
  failures.push(`429 退避重试不对（应第 3 次成功、等 8 与 16 秒）：${JSON.stringify(out.retry)}`);
}
if (out.giveup.calls !== 3 || out.giveup.code === 0) {
  failures.push(`429 一直不退时没在明确上限放弃：${JSON.stringify(out.giveup)}`);
}

// 行为级 ④：字幕解析 / 去重 / 与任务 102 同一套 [mm:ss] 口径
if (out.cues.join("|") !== "alpha one|beta two") {
  failures.push(`滚动字幕去重不对（应留 alpha one|beta two）：${out.cues.join("|")}`);
}
if (!/^\[\d{2}:\d{2}\] /.test(out.first_line)) {
  failures.push(`转写稿行首不是 [mm:ss]（讲义侧锚点认不出）：${out.first_line}`);
}
if (out.segments !== 1) failures.push(`合并段数不对（应 1 段，实得 ${out.segments}）`);

// 行为级 ⑤：报错翻译（登录态问题要一眼看得出）
if (out.classify.login !== "login" || out.classify.rate !== "rate_limit"
  || out.classify.js !== "js_runtime" || out.classify.gone !== "unavailable"
  || out.classify.gone_real !== "unavailable") {
  failures.push(`yt-dlp 报错翻译不对：${JSON.stringify(out.classify)}`);
}

// 行为级 ⑥：cookie 只看域，值一个字都不许出现在返回里
if (!out.cookie.youtube_domain || out.cookie.leak) {
  failures.push(`cookie 体检不对：youtube_domain=${out.cookie.youtube_domain}`
    + ` leak=${out.cookie.leak}（只许看域，值不许进返回）`);
}

// 行为级 ⑦（任务 119）：cookie 默认值的回落链 —— `.env` 真生效、进程环境变量仍优先
const cfgTmp = mkdtempSync(join(tmpdir(), "cg_cfg119_"));
try {
  const dotenvCookies = join(cfgTmp, "from-dotenv.txt");
  writeFileSync(join(cfgTmp, ".env"), `YTDLP_COOKIES=${dotenvCookies}\n`, "utf8");
  const readDotenv = probeDefaultCookies(cfgTmp, "probe_dotenv.py");
  if (readDotenv !== dotenvCookies) {
    failures.push("cookie 的默认值没读 .env（任务 119：往 .env 里写 YTDLP_COOKIES 必须生效）"
      + `：期望 ${dotenvCookies}，实得 ${JSON.stringify(readDotenv)}`);
  }
  const fromProcess = probeDefaultCookies(cfgTmp, "probe_process.py",
    { envCookies: "C:/tmp/from-process-cookies.txt" });
  if (fromProcess !== "C:/tmp/from-process-cookies.txt") {
    failures.push("cookie 的进程环境变量不再优先于 .env（既有口径：进程环境变量优先）"
      + `：实得 ${JSON.stringify(fromProcess)}`);
  }
  // 变异自检：把回落链里「读 Settings」那步去掉 ⇒ 上面第 1 条必须判红（否则断言等于没写）
  const mutChain = src.link.replace(
    '    return (os.environ.get(env_name) or "").strip() or (value or "").strip()',
    '    return (os.environ.get(env_name) or "").strip()');
  if (mutChain === src.link) {
    failures.push("变异自检失效：源码里找不到回落链那一行（_env_or_setting 的 return）");
  } else {
    const mutChainFile = join(cfgTmp, "mut_link_config.py");
    writeFileSync(mutChainFile, mutChain, "utf8");
    const mutRead = probeDefaultCookies(cfgTmp, "probe_mut.py", { mutFile: mutChainFile });
    if (mutRead === dotenvCookies) {
      failures.push("变异没被抓住（去掉「读 Settings」后 .env 仍生效）—— cookie 的 .env 断言等于没写");
    }
  }
} finally {
  rmSync(cfgTmp, { recursive: true, force: true });
}

// 变异自检：五种「不该发生」的样子必须被判红（否则断言等于没写）
const mutations: [string, Src, string][] = [
  ["抓取顺序反过来（不看字幕直接下音频）",
    { ...src, link: src.link.replace(
      "    raw, lang, run = fetch_subtitle(item, opts, limiter, work, item_switch=switch)",
      "    raw, lang, run = '', '', ToolRun([], 0)") },
    "顺序"],
  ["cookie 路径写进 meta（不落库是硬规则）",
    { ...src, link: src.link.replace('"cookies": bool(opts.cookies)', '"cookies": str(opts.cookies)') },
    "cookie"],
  ["能力清单少一项（js_runtime）",
    { ...src, cap: src.cap.replace("ytdlp_capability(), js_runtime_capability()]", "ytdlp_capability()]") },
    "能力清单"],
  ["探测带上联网参数",
    { ...src, link: src.link.replace('[*cmd, "--version"]', '[*cmd, "--dump-single-json", "--version"]') },
    "探测"],
  ["前端入口被摘掉",
    { ...src, step: src.step.replace(">从链接导入</Button>", "></Button>") },
    "从链接导入"],
];
for (const [label, mutated, expect] of mutations) {
  const hit = audit(mutated);
  if (!hit.some((line) => line.includes(expect))) {
    failures.push(`变异没被抓住（${label}）—— 断言等于没写：${expect}`);
  }
}

// 行为级变异：把「先字幕」那句改成 False（等于不看字幕），行为断言必须判红
const mutDir = mkdtempSync(join(tmpdir(), "cg_link118mut_"));
try {
  const broken = src.link.replace("    if raw.strip():", "    if False:");
  if (broken === src.link) failures.push("变异自检失效：源码里找不到「if raw.strip():」");
  const mutFile = join(mutDir, "mut_link.py");
  writeFileSync(mutFile, broken, "utf8");
  const mutOut = runPython(BEHAVIOR(mutFile), mutDir, "check118mut.py");
  if (mutOut.order_subs.path === "subtitle" && !mutOut.order_subs.audio) {
    failures.push("变异没被抓住（不看字幕直接下音频）—— 行为断言等于没写");
  }
} finally {
  rmSync(mutDir, { recursive: true, force: true });
}

if (failures.length) {
  console.error("任务 118 断言失败：");
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log("任务 118 断言全通过：从链接导入入口 / 顺序写死（先字幕 → 音频 → 明确失败）/ "
  + "依赖探测只查版本不联网、缺了说人话且不 500 / 限流 4s·20s + 429 退避 8→16→32（上限 3 次）/ "
  + "cookie 只留布尔、默认值走 .env ⇒ 进程环境变量 ⇒ PATH 的回落链 / "
  + "0 LLM 护栏（含 5 条源码变异 + 2 条行为变异自检）");
