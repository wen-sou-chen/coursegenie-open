/**
 * 任务 102 前端断言：素材支持**视频 / 音频**（本地语音转写，可选依赖）。
 *
 * 规格（HANDOFF 文末「任务 102 说明」）要求四件事，缺一条就是坑：
 * ① 扩展名白名单前后端**两侧必须一致**（后端 `SUPPORTED_EXT` ↔ 两个 `accept=`）；
 * ② 转写**必须异步**：上传请求只负责「落素材行 + 入队」，在请求里直接转写 = 把接口拖死；
 * ③ 分段按语音停顿、每段 ≤120 字（任务 105：350 → 120）、**每段行首 [mm:ss]、段间空行**
 *    （「（来源:《素材名》第 N 段）」编号口径一个字都不许改；任务 105 起音视频素材的
 *    **引用**改走段首时间戳 `（来源:《录像名》06:47）`，文本素材照旧「第 N 段」）；
 * ④ 没装 faster-whisper（可选依赖）时**照旧入库 + 写清跳过原因 + 不 500**。
 *
 * 三类断言：① 源码级（含前端 accept 与后端白名单的交叉核对）② 接线（BackgroundTasks /
 * 作业机制 / 重新转写入口 / meta.asr 落库）③ 行为级（真 import `app.services.material`
 * 跑分段与格式化；用**变异的副本**验证断言有牙）。
 *
 * 任务 103 追加三件事：① 中文素材默认带简体引导语（`ASR_ZH_PROMPT`）而**非中文一律不传**
 * （实测硬塞中文引导语会把英文整段转坏）；② `meta.asr.initial_prompt` 如实记录（没写=空串）；
 * ③ 领域引导语走 `ASR_DOMAIN_PROMPT` 开关、**默认关**。⚠️ 本机 `.venv` 现在**已装**
 * faster-whisper（用户批准后装的）⇒ 行为级跑 `probe_av` 前**故意挡住 av**，让「没装解码组件」
 * 那条降级文案在任何机器上都测得到（断言才有牙）；「可选依赖不许进核心环境」由
 * `backend/requirements.txt` 那条断言 + 变异自检守。
 *
 * 任务 104：模型名 `ASR_MODEL` 走 `.env`（经 pydantic `Settings`，与本项目其它配置一致）——
 * `asr_model()` 的回落链 = 进程环境变量 ⇒ `.env`(Settings) ⇒ `ASR_MODEL_DEFAULT`，不许只剩第一段。
 *
 * 运行：`node scripts/check-material-asr.ts`（Node >= 22.18 自带类型擦除）
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

const MATERIAL_REL = "backend/app/services/material.py";
const ROUTE_REL = "backend/app/api/routes/material.py";
const UPLOADS_REL = "backend/app/utils/uploads.py";
const KB_REL = "backend/app/services/kb.py";
const STEP_REL = "frontend/src/components/steps/MaterialsStep.tsx";
const KB_PANEL_REL = "frontend/src/components/KbPanel.tsx";
const TYPES_REL = "frontend/src/types.ts";
const API_REL = "frontend/src/api.ts";
const REQ_REL = "backend/requirements.txt";
const REQ_ASR_REL = "backend/requirements-asr.txt";

const VIDEO_EXT = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"];
const AUDIO_EXT = [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"];
/** 任务 103：中文简体引导语常量（与 material.ASR_ZH_PROMPT 一字不差）。 */
const ZH_PROMPT = "以下是普通话的句子，请用简体中文转写。";

/** 从 `NAME = {".a", ".b"}` 里取出扩展名（后端白名单正本）。 */
function extSet(src: string, name: string): string[] {
  const body = src.match(new RegExp(`${name}\\s*=\\s*\\{([^}]*)\\}`))?.[1] ?? "";
  return (body.match(/"(\.[A-Za-z0-9]+)"/g) ?? []).map((s) => s.replace(/"/g, ""));
}

/** `accept="..."` 里的扩展名。 */
function acceptSet(src: string): string[] {
  const raw = src.match(/accept="([^"]*)"/)?.[1] ?? "";
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

type Src = {
  material: string; route: string; uploads: string; kb: string;
  step: string; kbPanel: string; types: string; api: string;
  req: string; reqAsr: string;
};

/** 源码级审计（抽成函数：下面拿「变异后的源码」做判定自检）。 */
function audit(s: Src): string[] {
  const bad: string[] = [];

  // ① 扩展名白名单（后端正本 + 并入 SUPPORTED_EXT）
  const video = extSet(s.material, "VIDEO_EXT");
  const audio = extSet(s.material, "AUDIO_EXT");
  if (video.join(",") !== VIDEO_EXT.join(",")) {
    bad.push(`VIDEO_EXT 不是规格里那 6 个：${video.join(",")}`);
  }
  if (audio.join(",") !== AUDIO_EXT.join(",")) {
    bad.push(`AUDIO_EXT 不是规格里那 8 个：${audio.join(",")}`);
  }
  if (!/AV_EXT\s*=\s*VIDEO_EXT\s*\|\s*AUDIO_EXT/.test(s.material)) {
    bad.push("AV_EXT 不是 VIDEO_EXT | AUDIO_EXT");
  }
  if (!/SUPPORTED_EXT\s*=.*\|\s*AV_EXT/.test(s.material)) {
    bad.push("SUPPORTED_EXT 没有把 AV_EXT 并进去（上传会被「不支持的素材格式」挡掉）");
  }
  // 两侧一致：后端正本 vs 前端两个 accept
  const backend = [...extSet(s.material, "TEXT_EXT"), ".docx", ".pdf", ".pptx",
    ...extSet(s.material, "IMAGE_EXT"), ...video, ...audio];
  for (const [label, src] of [[STEP_REL, s.step], [KB_PANEL_REL, s.kbPanel]] as const) {
    const accept = acceptSet(src);
    const missing = backend.filter((e) => !accept.includes(e));
    const extra = accept.filter((e) => !backend.includes(e));
    if (missing.length || extra.length) {
      bad.push(`${label} 的 accept= 与后端白名单不一致（缺 ${missing.join(",") || "无"}；`
        + `多 ${extra.join(",") || "无"}）`);
    }
    if (!accept.includes(".mp4")) bad.push(`${label} 的 accept= 少了视频扩展名`);
  }

  // ② 转写常量 / 函数齐不齐
  const must = [
    ['ASR_ENGINE = "faster-whisper"', "ASR_ENGINE"],
    ["ASR_MODEL_DEFAULT = \"small\"", "ASR_MODEL_DEFAULT（默认 small）"],
    ["ASR_SEGMENT_MAX_CHARS = 120", "ASR_SEGMENT_MAX_CHARS = 120（任务 105 收紧后的段长上限）"],
    ["ASR_MAX_BYTES = 2 * 1024 * 1024 * 1024", "ASR_MAX_BYTES = 2GB"],
    ["ASR_MAX_SECONDS = 3 * 60 * 60", "ASR_MAX_SECONDS = 3 小时"],
    ["ASR_SKIP_REASON", "ASR_SKIP_REASON（降级说明）"],
    ["def asr_available(", "asr_available()"],
    ["def _asr_engine_version(", "_asr_engine_version()"],
    ["def asr_model(", "asr_model()（ASR_MODEL 可配）"],
    ["os.environ.get(\"ASR_MODEL\")", "环境变量 ASR_MODEL"],
    ["def probe_av(", "probe_av()（只读文件头探时长/音轨）"],
    ["def transcribe_av(", "transcribe_av()"],
    ["def merge_asr_segments(", "merge_asr_segments()（≤120 字合并）"],
    ["def asr_segments_to_text(", "asr_segments_to_text()"],
    ["def transcribe_material(", "transcribe_material()（后台那条路）"],
    ["def asr_page_note(", "asr_page_note()（素材页写明来路）"],
  ] as const;
  for (const [needle, label] of must) {
    if (!s.material.includes(needle)) bad.push(`${MATERIAL_REL} 里少了 ${label}`);
  }
  // 分段：行首 [mm:ss] + 段间空行
  if (!/_asr_stamp/.test(s.material) || !/f"\[\{total \/\/ 60:02d}:\{total % 60:02d}\]"/.test(s.material)) {
    bad.push("没有 [mm:ss] 时间戳（_asr_stamp）—— 老师按时间回看就没了");
  }
  if (!/"\\n\\n"\.join/.test(s.material)) {
    bad.push("asr_segments_to_text 没有用空行分段（段间空行是规格要求）");
  }
  // meta.asr 落库（不许加列 / 迁移）
  if (!/meta\["asr"\] = asr/.test(s.material)) bad.push("meta[\"asr\"] 没有落库");
  if (!/if asr is not None:/.test(s.material)) bad.push("add_material_file 没把 asr 写进 meta");
  for (const key of ["engine", "version", "model", "duration_sec", "segments",
    "language", "avg_logprob", "status", "skipped_reason", "initial_prompt"]) {
    if (!new RegExp(`"${key}"`).test(s.material)) bad.push(`meta.asr 少了 ${key} 字段`);
  }
  if (!/AV_EXT:\n\s+return None/.test(s.material)) {
    bad.push("pages_of 没有给音视频早退（会去读 2GB 原件的字节）");
  }

  // ②b 任务 103：中文默认简体引导语（非中文不传）+ meta 如实记 initial_prompt
  if (!s.material.includes(`ASR_ZH_PROMPT = "${ZH_PROMPT}"`)) {
    bad.push("material.py 里没有 ASR_ZH_PROMPT（中文简体引导语常量）");
  }
  if (!/if not is_chinese_language\(language\):\n        return ""/.test(s.material)) {
    bad.push(`asr_prompt 不是「非中文一律不给引导语」（硬塞会把英文素材带偏）`);
  }
  if (!/return ASR_ZH_PROMPT \+ \(domain_hint or ""\)/.test(s.material)) {
    bad.push("asr_prompt 没把领域引导语拼进 initial_prompt（ASR_DOMAIN_PROMPT 就白做了）");
  }
  if (!/if prompt:\n        options\["initial_prompt"\] = prompt/.test(s.material)) {
    bad.push("transcribe_av 没有「有引导语才传 initial_prompt」");
  }
  if (!/initial_prompt": out\.get\("initial_prompt"\) or ""/.test(s.material)) {
    bad.push("meta.asr 没把 initial_prompt 如实落库（done 那条路）");
  }
  if (!/ASR_DOMAIN_PROMPT_ENV = "ASR_DOMAIN_PROMPT"/.test(s.material)
    || !/_env_true\(ASR_DOMAIN_PROMPT_ENV\)/.test(s.material)) {
    bad.push("领域引导语开关（ASR_DOMAIN_PROMPT，默认关）没接上");
  }
  if (!/initial_prompt\?: string;/.test(s.types)) {
    bad.push(`${TYPES_REL} 的 MaterialAsr 少了 initial_prompt 字段`);
  }

  // ②c 任务 104：模型名走 .env（Settings 回落）—— 只认进程环境变量的话，.env 里写什么都不生效
  if (!/get_settings\(\)\.asr_model/.test(s.material)) {
    bad.push("asr_model() 没有回落 .env 的 ASR_MODEL（Settings）—— 写进 .env 也不生效");
  }

  // ③ 路由：异步 + 作业机制 + 显式重新转写 + 进度
  if (!/from fastapi import [^\n]*BackgroundTasks/.test(s.route)) {
    bad.push(`${ROUTE_REL} 没 import BackgroundTasks`);
  }
  const uploadBody = s.route.match(/async def _upload_av\([\s\S]*?\n(?=@router|def |async def )/)?.[0] ?? "";
  if (!uploadBody) bad.push(`${ROUTE_REL} 里没有 _upload_av（音视频上传那条路）`);
  if (/^ {4,}_run\(\)\s*$/m.test(s.route)) {
    bad.push("上传接口被转写阻塞了（请求里直接调 _run()，转写没有入队）");
  }
  if (/transcribe_material\(|transcribe_av\(/.test(uploadBody)) {
    bad.push("上传接口被转写阻塞了（_upload_av 里直接转写，不是入队）");
  }
  if (!/background\.add_task\(_run\)/.test(s.route)) {
    bad.push("上传接口没有把转写交给 BackgroundTasks（background.add_task 不见了）");
  }
  if (!/_upload_av\(/.test(s.route.match(/@router\.post\("\/upload"\)[\s\S]*?(?=\n@router\.)/)?.[0] ?? "")) {
    bad.push("POST /upload 没有按扩展名分流到 _upload_av");
  }
  for (const [needle, label] of [
    ["acquire_scope(", "acquire_scope（进程内互斥）"],
    ["claim_job_exclusive(", "claim_job_exclusive（开工声明）"],
    ["release_scope(", "release_scope（收工释放）"],
    ["GenerationJob(", "GenerationJob（既有作业表）"],
    ['"progress"', "进度写进作业 result"],
    ["_PROGRESS_MIN_SECONDS", "进度写库节流"],
  ] as const) {
    if (!s.route.includes(needle)) bad.push(`${ROUTE_REL} 里少了 ${label}`);
  }
  if (!/job_type = f"\{_ASR_JOB_PREFIX\}\{material_id\}"/.test(s.route)) {
    bad.push("转写作业的 job_type 没带素材 id（同课多条视频会被 409 挡掉）");
  }
  if (!/@router\.post\("\/\{material_id\}\/transcribe"\)/.test(s.route)) {
    bad.push("没有显式「重新转写」入口 POST /material/{id}/transcribe");
  }
  if (!/from app\.utils\.uploads import [^\n]*read_upload_to_path/.test(s.route)
    || !/def read_upload_to_path\(/.test(s.uploads)) {
    bad.push("没有分块落盘的 read_upload_to_path（2GB 音视频不能整份进内存）");
  }
  // 素材页（导出）也写明来路
  if (!/asr_page_note\(m\)/.test(s.kb)) bad.push("kb._material_page 没用 asr_page_note（导出素材页看不到转写说明）");

  // ④ 可选依赖：不进核心 requirements
  if (!/faster-whisper>=1\.0/.test(s.reqAsr)) bad.push(`${REQ_ASR_REL} 里没有 faster-whisper>=1.0`);
  if (!/不装也能用/.test(s.reqAsr)) bad.push(`${REQ_ASR_REL} 没写「不装也能用」的降级口径`);
  if (/faster-whisper/.test(s.req)) {
    bad.push(`⛔ 核心 ${REQ_REL} 里出现了 faster-whisper（可选依赖不许进核心）`);
  }

  // ⑤ 前端：降级 / 进度 / 重新转写 / 文案
  for (const [needle, label] of [
    ["本机转写", "「本机转写」的说法"],
    ["不上传云端", "「不上传云端」的承诺"],
    ["asr.status", "asr 状态判断"],
    ["api.getJobStatus", "轮询 GET /content/jobs/{job_id}"],
    ["refetchInterval", "轮询间隔"],
    ["重新转写", "「重新转写」入口"],
    ["fmtDuration", "时长展示"],
    ["avg_logprob", "平均置信度展示"],
  ] as const) {
    if (!s.step.includes(needle)) bad.push(`${STEP_REL} 里少了 ${label}`);
  }
  if (!/api\.transcribeMaterial\(/.test(s.step)) bad.push(`${STEP_REL} 没接「重新转写」接口`);
  if (!/export interface MaterialAsr/.test(s.types)) bad.push(`${TYPES_REL} 里没有 MaterialAsr`);
  if ((s.types.match(/asr\?: MaterialAsr \| null;/g) ?? []).length !== 2) {
    bad.push(`${TYPES_REL} 的 MaterialRow / MaterialFull 没有都带上 asr`);
  }
  if (!/getJobStatus: \(jobId: string\)/.test(s.api)) bad.push(`${API_REL} 里没有 getJobStatus`);
  if (!/transcribeMaterial: \(id: string\)/.test(s.api)) bad.push(`${API_REL} 里没有 transcribeMaterial`);
  return bad;
}

/* ---------------- 行为级：真 import 跑一遍 ---------------- */

type RunOut = {
  stamps: string[]; merged: number[]; paragraphs: string[]; numbered0: string;
  blank_line: boolean; text_one_line: boolean;
  asr_available: boolean; skip_reason_ok: boolean; probe_error: string;
  duration_text: string[]; supported_has_av: boolean;
  zh_prompt: string; zh_const: string; en_prompt: string; yue_prompt: string;
  domain_prompt: string; env_true: boolean; env_false: boolean;
};

const BEHAVIOR = (modName?: string) => `
import json, sys, os
from pathlib import Path

# 任务 103：本机 .venv 已装 faster-whisper（用户批准后装的）⇒ 故意挡住 av，
# 让 probe_av 的「没装解码组件」降级文案在任何机器上都测得到（否则断言被环境悄悄绕过）。
class _BlockAv:
    def find_spec(self, name, path=None, target=None):
        if name == "av":
            raise ImportError("check 里故意挡住 av 来测降级文案")
        return None


sys.meta_path.insert(0, _BlockAv())


def _flag(value):
    os.environ["_CG103_FLAG"] = value
    return bool(m._env_true("_CG103_FLAG"))


repo = Path(r"${repo}")
sys.path.insert(0, str(repo / "backend"))
if ${modName ? "True" : "False"}:
    import importlib.util
    spec = importlib.util.spec_from_file_location("mut_material", r"${modName ?? ""}")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
else:
    from app.services import material as m

pad = "This is a longer transcription unit used for the merging check."
rows = [{"start": t, "end": t + 2.4,
         "text": " Sentence number %d on the topic. %s" % (i, pad * 3)}
        for i, t in enumerate([0.0, 2.4, 5.2, 8.0, 10.4])]
big = [{"start": 0, "end": 1, "text": "x" * 200}, {"start": 1, "end": 2, "text": "y" * 200}]
segs = m.merge_asr_segments(rows)
text = m.asr_segments_to_text(segs)
print(json.dumps({
    "stamps": [m._asr_stamp(s) for s in (0, 65, 3725)],
    "merged": [len(x["text"]) for x in m.merge_asr_segments(big)],
    "paragraphs": m.split_paragraphs(text),
    "numbered0": m.numbered_paragraphs(text)[0],
    "blank_line": "\\n\\n" in text,
    "text_one_line": len(m.split_paragraphs(text)) == 1,
    "asr_available": bool(m.asr_available()),
    "skip_reason_ok": "requirements-asr.txt" in m.ASR_SKIP_REASON,
    "probe_error": m.probe_av(r"C:\\Windows\\Temp\\nope.mp4")["error"],
    "duration_text": [m._asr_duration_text(s) for s in (45, 3600, 4 * 3600)],
    "supported_has_av": set(m.AV_EXT) <= set(m.SUPPORTED_EXT),
    "zh_prompt": m.asr_prompt("zh"),
    "zh_const": m.ASR_ZH_PROMPT,
    "en_prompt": m.asr_prompt("en"),
    "yue_prompt": m.asr_prompt("yue"),
    "domain_prompt": m.asr_prompt("zh", domain_hint="本课讲：多菌共生体系。"),
    "env_true": _flag("true"),
    "env_false": _flag("0"),
}, ensure_ascii=False))
`;

function runPython(script: string, tmp: string, name: string): RunOut {
  const python = existsSync(join(repo, "backend/.venv/Scripts/python.exe"))
    ? join(repo, "backend/.venv/Scripts/python.exe")
    : (process.platform === "win32" ? "python" : "python3");
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
  material: readRepoFile(MATERIAL_REL),
  route: readRepoFile(ROUTE_REL),
  uploads: readRepoFile(UPLOADS_REL),
  kb: readRepoFile(KB_REL),
  step: readRepoFile(STEP_REL),
  kbPanel: readRepoFile(KB_PANEL_REL),
  types: readRepoFile(TYPES_REL),
  api: readRepoFile(API_REL),
  req: readRepoFile(REQ_REL),
  reqAsr: readRepoFile(REQ_ASR_REL),
};

failures.push(...audit(src));

const tmp = mkdtempSync(join(tmpdir(), "cg_asr102_"));
let out: RunOut;
try {
  out = runPython(BEHAVIOR(), tmp, "check102.py");
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

// 行为级：分段 / 时间戳 / 编号口径 / 降级
if (out.stamps.join(",") !== "[00:00],[01:05],[62:05]") {
  failures.push(`[mm:ss] 时间戳不对：${out.stamps.join(",")}`);
}
if (out.merged.length !== 2 || out.merged.some((n) => n !== 200)) {
  failures.push(`≤120 字的合并规则不对：${out.merged.join(",")}（两段各 200 字，应各自成段）`);
}
if (!out.blank_line) failures.push("段间没有空行");
if (out.paragraphs.length !== 5) failures.push(`5 段应切出 5 个段落，实得 ${out.paragraphs.length}`);
if (out.paragraphs.some((p) => !/^\[\d{2}:\d{2}\] /.test(p))) {
  failures.push(`段落行首不是 [mm:ss]：${out.paragraphs[0]}`);
}
if (!out.numbered0.startsWith("[1] [00:00] ")) {
  failures.push(`编号口径变了（应是 [1] [00:00] …）：${out.numbered0}`);
}
// 任务 103：本机 .venv **已装** faster-whisper（用户批准后装的）⇒「本机没装」不再是判据。
// 真口径 = 可选依赖不许进核心 requirements.txt（下面那条 + 变异自检），降级文案照测（av 被挡）。
if (out.asr_available) {
  console.log("  · 提示：本机 backend/.venv 已装 faster-whisper（可选依赖，用户批准后装的）");
}
if (!out.skip_reason_ok) failures.push("ASR_SKIP_REASON 没写清装法与「不装也能用」");
if (!/av|解码/.test(out.probe_error)) failures.push(`没装 av 时的探测原因不像人话：${out.probe_error}`);
if (out.duration_text.join("|") !== "45 秒|1 小时 0 分|4 小时 0 分") {
  failures.push(`人话时长不对：${out.duration_text.join("|")}`);
}
if (!out.supported_has_av) failures.push("AV_EXT 没有被 SUPPORTED_EXT 覆盖");

// 任务 103：中文给简体引导语、非中文一个字都不给、领域语拼在后面、开关判真值
if (out.zh_prompt !== ZH_PROMPT || out.zh_const !== ZH_PROMPT) {
  failures.push(`中文没拿到简体引导语（正文会出繁体）：${JSON.stringify(out.zh_prompt)}`);
}
if (out.en_prompt !== "" || out.yue_prompt !== "") {
  failures.push("非中文也被塞了引导语（会把英文素材带偏）："
    + `en=${JSON.stringify(out.en_prompt)} yue=${JSON.stringify(out.yue_prompt)}`);
}
if (out.domain_prompt !== ZH_PROMPT + "本课讲：多菌共生体系。") {
  failures.push(`领域引导语没拼在简体引导语后面：${JSON.stringify(out.domain_prompt)}`);
}
if (!out.env_true || out.env_false) {
  failures.push("ASR_DOMAIN_PROMPT 的真值判断不对（1 / true / yes / on 才算开）");
}

// 变异自检：五种「不该发生」的样子必须被判红（否则断言等于没写）
const mutations: [string, Src, string][] = [
  ["把异步改回同步（请求里直接转写）",
    { ...src, route: src.route.replace("background.add_task(_run)", "_run()") },
    "上传接口被转写阻塞了"],
  ["前端 accept 漏掉视频扩展名",
    { ...src, step: src.step.replace(".mp4,.mov,.mkv,.webm,.avi,.m4v,", "") },
    "accept="],
  ["SUPPORTED_EXT 不再并 AV_EXT",
    { ...src, material: src.material.replace(
      'SUPPORTED_EXT = TEXT_EXT | {".docx", ".pdf", ".pptx"} | IMAGE_EXT | AV_EXT',
      'SUPPORTED_EXT = TEXT_EXT | {".docx", ".pdf", ".pptx"} | IMAGE_EXT') },
    "SUPPORTED_EXT"],
  ["可选依赖塞进核心 requirements.txt",
    { ...src, req: src.req + "\nfaster-whisper>=1.0\n" },
    "核心"],
  ["meta.asr 不再落库",
    { ...src, material: src.material.replaceAll('meta["asr"] = asr', "pass") },
    "meta[\"asr\"]"],
  ["非中文也被塞简体引导语（会把英文带偏）",
    { ...src, material: src.material.replace(
      '    if not is_chinese_language(language):\n        return ""',
      "    if False:\n        return \"\"") },
    "非中文一律不给引导语"],
  ["领域引导语拼不进 initial_prompt",
    { ...src, material: src.material.replace(
      '    return ASR_ZH_PROMPT + (domain_hint or "")',
      "    return ASR_ZH_PROMPT") },
    "领域引导语"],
  ["meta.asr 不再如实记 initial_prompt（done 那条路写死空串）",
    { ...src, material: src.material.replace(
      '"initial_prompt": out.get("initial_prompt") or ""',
      '"initial_prompt": ""') },
    "initial_prompt"],
  ["asr_model() 不再读 .env（Settings 回落被拿掉）",
    { ...src, material: src.material.replaceAll("get_settings().asr_model", '""') },
    "没有回落 .env 的 ASR_MODEL"],
];
for (const [label, mutated, expect] of mutations) {
  const hit = audit(mutated);
  if (!hit.some((line) => line.includes(expect))) {
    failures.push(`变异没被抓住（${label}）—— 断言等于没写：${expect}`);
  }
}

// 行为级变异：把 [mm:ss] 前缀 / 120 字上限改坏，行为断言必须判红
const tmp2 = mkdtempSync(join(tmpdir(), "cg_asr102mut_"));
try {
  const brokenPrefix = src.material.replace(
    /return "\\n\\n"\.join\(f"\{_asr_stamp\(seg\.get\('start'\)\)\} \{str\(seg\.get\('text'\) or ''\)\.strip\(\)\}"/,
    'return "\\n\\n".join(str(seg.get(\'text\') or "").strip()');
  const mutFile = join(tmp2, "mut_material.py");
  writeFileSync(mutFile, brokenPrefix, "utf8");
  const mutOut = runPython(BEHAVIOR(mutFile), tmp2, "check102mut.py");
  if (mutOut.paragraphs.every((p) => /^\[\d{2}:\d{2}\] /.test(p))) {
    failures.push("变异没被抓住（去掉 [mm:ss] 前缀）—— 行为断言等于没写");
  }
  const noCap = src.material.replace("max_chars: int = ASR_SEGMENT_MAX_CHARS) -> list[dict]:",
    "max_chars: int = 10 ** 9) -> list[dict]:");
  const mutFile2 = join(tmp2, "mut_material2.py");
  writeFileSync(mutFile2, noCap, "utf8");
  const mutOut2 = runPython(BEHAVIOR(mutFile2), tmp2, "check102mut2.py");
  if (mutOut2.merged.length !== 1) {
    failures.push("变异没被抓住（去掉 120 字上限）—— 行为断言等于没写");
  }
  const noZhPrompt = src.material.replace(
    '    if not is_chinese_language(language):\n        return ""\n'
    + '    return ASR_ZH_PROMPT + (domain_hint or "")',
    '    return ""');
  const mutFile3 = join(tmp2, "mut_material3.py");
  writeFileSync(mutFile3, noZhPrompt, "utf8");
  const mutOut3 = runPython(BEHAVIOR(mutFile3), tmp2, "check103mut3.py");
  if (mutOut3.zh_prompt !== "") {
    failures.push("变异没被抓住（中文不再给简体引导语）—— 行为断言等于没写");
  }
} finally {
  rmSync(tmp2, { recursive: true, force: true });
}

if (failures.length) {
  console.error("任务 102/103/104/105 断言失败：");
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log("任务 102/103/104/105 断言全通过：扩展名两侧一致 / 转写异步入队 / [mm:ss] 分段口径 / "
  + "meta.asr 落库 / 降级不 500 / 中文默认简体引导语、非中文不传 / ASR_MODEL 走 .env"
  + "（进程环境变量优先）/ 段长上限 120（任务 105）（含 9 条源码变异 + 3 条行为变异自检）");
