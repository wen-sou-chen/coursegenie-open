"""LLM 客户端抽象：openai / anthropic / mock。

- 默认 ``LLM_PROVIDER=mock``（零配置）：不调真模型也能跑通全流程（交付可复现性的关键）。
  **任务 117**：显式选了 openai / anthropic / openrouter 却没配对应 Key（或 provider 名不认识）
  ⇒ 视为**配置没弄好**，抛 ``LLMConfigError``（人话：缺哪个变量、去哪填），**不再静默退回 mock** ——
  静默退回会让老师以为拿到的是模型产出，其实是一段占位文本。
- 统一返回字符串；JSON 结构化输出走 complete_json（自动剥离代码块）。
- 每次调用都计量（任务 23-A）：provider 返回 usage 时按真实 token 记账，
  没返回（mock/部分兼容端点）时按文本长度估算并标注 estimated；
  命中重试的失败尝试也各记一条，保证「重试累加」。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager

import httpx

from app.core.config import get_settings
from app.services import usage as usage_log

logger = logging.getLogger(__name__)


# XML / Word 允许的控制字符只有 \t \n \r；其余 C0 控制字符会让 docx 渲染直接失败，
# 也是「LaTeX 反斜杠被 JSON 吃掉」后残留的痕迹（任务 48w）。
_RE_XML_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_RE_JSON_UHEX = re.compile(r"[0-9a-fA-F]{4}")  # \uXXXX 的 4 位十六进制
_JSON_CTRL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


# `\t` / `\n` 是**合法** JSON 转义（真制表 / 真换行），只有写成 LaTeX 命令时才要补反斜杠。
# 规格里的「后跟字母」判据太宽：真机回归用例里 ```mermaid\nflowchart TD``` 的换行后面
# 正好是字母 `f`，会被修成字面 `\nflowchart` ⇒ 流程图整块识别不出来（`verify.ps1` 的
# HTML 交付包用例实测变红）。所以收窄为「后跟**已知的** t/n 开头的 LaTeX 命令名」，
# 或后跟 `{`（`\text{...}` 这类写法）。`\times`/`\theta`/`\neq`/`\nabla` 都在表里。
_LATEX_TN_COMMANDS = frozenset({
    # \t…
    "tan", "tanh", "tau", "tbinom", "text", "textbf", "textit", "textrm", "textstyle",
    "tfrac", "therefore", "theta", "thickapprox", "thicksim", "thinspace", "tilde",
    "times", "to", "top", "triangle", "triangledown", "triangleleft", "trianglelefteq",
    "triangleq", "triangleright", "trianglerighteq", "tt", "twoheadleftarrow",
    "twoheadrightarrow",
    # \n…
    "nabla", "natural", "nearrow", "neg", "negthinspace", "neq", "newcommand", "newline",
    "newpage", "ni", "nleq", "ngeq", "nmid", "noindent", "nolimits", "nonumber", "not",
    "notin", "nparallel", "nprec", "nrightarrow", "nleftarrow", "nsubseteq", "nsupseteq",
    "nu", "nwarrow",
})
_LATEX_TN_ALPHA = re.compile(r"[A-Za-z]+")


def _is_latex_tn_command(text: str, index: int) -> bool:
    """``text[index]`` 是 ``\\t`` / ``\\n`` 的反斜杠时，判断它是不是 LaTeX 命令的开头。"""
    nxt = text[index + 1]
    after = text[index + 2] if index + 2 < len(text) else ""
    if after == "{":
        return True
    run = _LATEX_TN_ALPHA.match(text[index + 2:])
    return bool(run) and nxt + run.group(0) in _LATEX_TN_COMMANDS


def _escape_raw_ctrl(char: str) -> str:
    """字符串里的**裸**控制字符 → JSON 转义（语义不变，只让文本成为合法 JSON）。"""
    return _JSON_CTRL_ESCAPES.get(char) or f"\\u{ord(char):04x}"


def repair_json_escapes(text: str) -> str:
    """把「少写一个反斜杠」的 LaTeX 命令修回字面量（任务 48w）。

    模型在 JSON 里写单反斜杠有两种下场：``\\frac`` 的 ``\\f`` 是**合法** JSON 转义
    ⇒ 静默变成换页符 U+000C（公式坏掉、Word 交付整篇渲染失败）；``\\alpha`` /
    ``\\times`` 是**非法**转义 ⇒ ``json.loads`` 直接抛错（表现为「LLM JSON 解析错」）。

    这里只按"是不是 LaTeX 命令"判定，不动真转义：

    - ``\\f`` / ``\\b`` / ``\\r`` 一律补成字面量（正文里几乎不可能真要换页/退格/回车，
      而 ``\\frac`` / ``\\beta`` / ``\\rightarrow`` 全靠这三个首字母）；
    - ``\\t`` / ``\\n`` **仅当它们真的是 LaTeX 命令的开头**时补成字面量
      （``\\times`` / ``\\theta`` / ``\\neq`` / ``\\nabla`` / ``\\newline`` / ``\\text{``…）；
      判据见 ``_LATEX_TN_COMMANDS``：后跟**已知的 t/n 开头命令名**或 ``{``。反过来，
      后面不是这些命令时，换行/制表就是**真换行真制表**，必须原样保留
      （中文正文换行后跟汉字、``\\nflowchart`` 这类代码块首行，一刀切会毁掉排版）；
    - 其余非法转义补成字面量 ``\\\\X``；``\\uXXXX``（4 位十六进制）等真转义原样保留；
    - **字符串里的裸控制字符**（真机实测：模型直接把换行写进 JSON 字符串 ⇒
      ``Invalid control character``，讲义生成整篇失败）转义成 ``\\n`` 等等价写法 ——
      解码结果里的换行/制表**一个不变**，只是让文本成为合法 JSON；字符串外的换行是
      合法空白，一律不动。
    """
    out: list[str] = []
    index = 0
    size = len(text)
    in_string = False
    while index < size:
        char = text[index]
        if not in_string:
            in_string = char == '"'
            out.append(char)
            index += 1
            continue
        if char == '"':
            in_string = False
            out.append(char)
            index += 1
            continue
        if char != "\\":
            if _RE_XML_CTRL.match(char) or char in "\n\r\t":
                out.append(_escape_raw_ctrl(char))  # 裸控制字符 ⇒ 等价的 JSON 转义
            else:
                out.append(char)
            index += 1
            continue
        if index + 1 >= size:
            out.append(char)
            index += 1
            continue
        nxt = text[index + 1]
        if nxt in '"\\/':  # JSON 真转义：引号 / 反斜杠 / 斜杠
            out.append(char + nxt)
        elif nxt in "bfr":
            out.append("\\\\" + nxt)
        elif nxt in "tn":
            if _is_latex_tn_command(text, index):
                out.append("\\\\" + nxt)
            else:
                out.append(char + nxt)
        elif nxt == "u" and _RE_JSON_UHEX.match(text[index + 2 : index + 6]):
            out.append(text[index : index + 6])
            index += 4
        else:  # 非法转义（\alpha / \times …）⇒ 补成字面量
            out.append("\\\\" + (_escape_raw_ctrl(nxt) if ord(nxt) < 32 else nxt))
        index += 2
    return "".join(out)


def find_control_chars(value) -> list[str]:
    """递归收集 JSON 解析结果里 XML 不允许的控制字符（除 ``\\t``/``\\n``/``\\r``）。

    留给上层的可观测钩子：命中说明内容里混进了"被吃掉的 LaTeX"或别的坏字符
    （任务 48w 第三层：记 warning + 写进 ``meta.content_issues``，不加判据）。
    """
    found: list[str] = []
    stack: list = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            found.extend(_RE_XML_CTRL.findall(current))
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return found


_INNER_QUOTE_ENDS = frozenset(",}]:\r\n")
_STRING_WS = " \t"


def repair_inner_quotes(text: str) -> str:
    """把「字符串内部本该转义、却写成裸半角」的引号补成 ``\"``（任务 63）。

    真机实测（2026-09-19 发版总闸第 3 轮，两处都让整步失败）：

    - ``"quote": "二年级学生第一次接触"夸张"这一修辞,可用"三千尺"与真实瀑布高度对比引入;"``
    - ``"explanation": "《望天门山》中的水是"碧水东流至此回"，表现出江水的流动和回旋；……"``

    中文正文里「引号里再套引号」很常见，模型一写成半角就把字符串提前闭合，
    ``json.loads`` 抛 ``Expecting ',' delimiter``。

    判据（保守，只动「明显不是串尾」的那些）：串内遇到半角 ``"`` 时往后看
    （只跳空格 / 制表符），下一个字符是 ``,`` / ``}`` / ``]`` / ``:``、
    或是换行 / 文本结束 ⇒ 认为它是**真·串尾**，原样保留；否则认为它是写进
    字符串里的引号，补成 ``\"``。反斜杠序列（``\"`` / ``\\``）整体跳过，
    不会把已经转义好的引号再转义一遍。

    ⚠️ 这是启发式，**只在 ``extract_json`` 首次解析失败后才调用**（默认路径绝不改
    正文）；修完还必须再 ``json.loads`` 一次，失败就照旧抛原来的错误。
    """
    out: list[str] = []
    index = 0
    size = len(text)
    in_string = False
    while index < size:
        char = text[index]
        if not in_string:
            in_string = char == '"'
            out.append(char)
            index += 1
            continue
        if char == "\\":  # 已转义序列整体跳过（含 \"），别把转义好的再动一遍
            out.append(text[index : index + 2])
            index += 2
            continue
        if char != '"':
            out.append(char)
            index += 1
            continue
        look = index + 1
        while look < size and text[look] in _STRING_WS:
            look += 1
        if look >= size or text[look] in _INNER_QUOTE_ENDS:
            in_string = False
            out.append(char)
        else:
            out.append('\\"')
        index += 1
    return "".join(out)


def bad_json_error(exc: json.JSONDecodeError, *,
                   response_meta: dict | None = None) -> LLMJsonError:
    """把解析失败包成「带位置与现场」的**同类**异常（任务 63 第 3 点）。

    类型仍继承 ``json.JSONDecodeError``（``LLMJsonError``）：``lecture._failure_reason``
    与既有 ``except json.JSONDecodeError`` 的归类不变；``main.py`` 另外把它转成 503。
    消息形如
    ``LLM 返回了坏 JSON（位置 1615，现场：……可用"三千尺"……）：Expecting ',' delimiter``。
    """
    doc = str(exc.doc or "")
    snippet = doc[max(0, exc.pos - 100): exc.pos + 40]
    msg = f"LLM 返回了坏 JSON（位置 {exc.pos}，现场：{snippet}）：{exc.msg}"
    return LLMJsonError(msg, doc, exc.pos, response_meta=response_meta)


def extract_json(text: str, *, response_meta: dict | None = None) -> dict:
    """从 LLM 回复中稳健提取 JSON：容忍代码块包裹 / LaTeX 单反斜杠 / 串内半角引号。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    repaired = repair_json_escapes(text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:
        # 修完反斜杠还失败 ⇒ 再试一层「串内半角引号」修复（任务 63）。这一层**只在
        # 解析失败这条路径上**启用（默认路径绝不改正文），且修完必须再解析一次才算数。
        quoted = repair_inner_quotes(repaired)
        if quoted != repaired:
            try:
                data = json.loads(quoted)
            except json.JSONDecodeError:
                pass
            else:
                logger.warning("LLM 串内半角引号已按 JSON 转义（使用了引号修复）："
                               "%s @pos=%s 现场=%r", exc.msg, exc.pos,
                               repaired[max(0, exc.pos - 100): exc.pos + 40])
                return data
        # 两种修复都救不回来 ⇒ 是另一种坏 JSON（漏引号 / 少逗号 / 截断）。留一段
        # 有界现场，免得只看到「Expecting ',' delimiter」却查不到原文（任务 48w 实测）；
        # 抛出的异常带位置与现场，让 job 的失败原因能一眼定位（任务 63 第 3 点）。
        logger.warning("LLM JSON 解析失败（已修 LaTeX 反斜杠）：%s @pos=%s 现场=%r",
                       exc.msg, exc.pos, repaired[max(0, exc.pos - 100): exc.pos + 40])
        raise bad_json_error(exc, response_meta=response_meta) from None


_RETRY_STATUS = {429, 500, 502, 503, 504}

# 任务 154：演示模式下「同时只跑 1 个生成任务」的兜底 —— 即使路由闸门漏了某条生成入口，
# 真正打到模型的请求也只会串行执行（各 provider 都走 _post_retry）。
_DEMO_LLM_SLOT = threading.BoundedSemaphore(1)


@contextmanager
def _demo_llm_guard():
    settings = get_settings()
    if not getattr(settings, "demo_only", False):
        yield
        return
    _DEMO_LLM_SLOT.acquire()
    try:
        yield
    finally:
        _DEMO_LLM_SLOT.release()


# 坏 JSON 纠错重试用的极短提示（任务 63）。只针对「模型输出不是合法 JSON」，
# **不混进**上面那套按 HTTP 状态码的重试；这是**同一次调用的纠错重试**（同一份
# prompt 再问一次 + 尾部一句约束），不是新的提示词版本 ⇒ PROMPT_TEMPLATE_VERSION 不动。
# 用量不用另补：每个尝试都由各自的 ``complete()`` 记一条流水（重试那次照样记）。
_BAD_JSON_HINT = (
    "\n\n（上一次回复不是合法 JSON。请只输出 JSON 本体：字符串内部的引号必须转义成 "
    "\\\" 或改用全角引号“”，不要用半角 \" 直接嵌在字符串里。）"
)


def record_call(
    *,
    provider: str,
    model: str,
    purpose: str = "",
    prompt_tokens: int | None,
    completion_tokens: int | None,
    system: str,
    user: str,
    content: str,
    latency_ms: int,
    attempts: int = 1,
) -> None:
    """记一次成功调用；provider 未给 usage 时按文本长度估算（estimated=True）。"""
    estimated = prompt_tokens is None or completion_tokens is None
    usage_log.record(
        provider=provider,
        model=model,
        purpose=purpose or usage_log.current_purpose(),
        prompt_tokens=int(prompt_tokens) if prompt_tokens is not None
        else usage_log.estimate_tokens(system) + usage_log.estimate_tokens(user),
        completion_tokens=int(completion_tokens) if completion_tokens is not None
        else usage_log.estimate_tokens(content),
        latency_ms=latency_ms,
        attempts=attempts,
        estimated=estimated,
    )


def record_retry(*, provider: str, model: str, purpose: str = "",
                 system: str, user: str, latency_ms: int) -> None:
    """记一次失败后重试的尝试：输入 token 已消耗、无输出（重试累加计量）。"""
    usage_log.record(
        provider=provider,
        model=model,
        purpose=purpose or usage_log.current_purpose(),
        prompt_tokens=usage_log.estimate_tokens(system) + usage_log.estimate_tokens(user),
        completion_tokens=0,
        latency_ms=latency_ms,
        attempts=1,
        estimated=True,
        retried=True,
    )


def _post_retry(url: str, *, payload: dict, headers: dict, timeout: float,
                retries: int = 4, on_retry=None) -> tuple[httpx.Response, list[dict]]:
    """带退避的 POST 入口；演示模式下用全局信号量把模型请求串行化。"""
    with _demo_llm_guard():
        return _post_retry_inner(
            url, payload=payload, headers=headers, timeout=timeout,
            retries=retries, on_retry=on_retry,
        )


def _post_retry_inner(url: str, *, payload: dict, headers: dict, timeout: float,
                      retries: int = 4, on_retry=None) -> tuple[httpx.Response, list[dict]]:
    """带退避的 POST：429/5xx/网络抖动自动重试，避免免费档限流打断生成。

    返回 (响应, 重试记录)；``on_retry(latency_ms)`` 让计量模块把重试也累加进用量。
    """
    retried: list[dict] = []
    for attempt in range(retries):
        started = time.perf_counter()
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.TransportError:
            spent = int((time.perf_counter() - started) * 1000)
            retried.append({"attempt": attempt + 1, "status": 0, "latency_ms": spent})
            if on_retry is not None:
                on_retry(spent)
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt + random.uniform(0, 0.6))
            continue
        if resp.status_code not in _RETRY_STATUS or attempt == retries - 1:
            resp.raise_for_status()
            return resp, retried
        spent = int((time.perf_counter() - started) * 1000)
        retried.append({"attempt": attempt + 1, "status": resp.status_code, "latency_ms": spent})
        if on_retry is not None:
            on_retry(spent)
        time.sleep(2 ** attempt + random.uniform(0, 0.6))
    raise RuntimeError("LLM 请求重试后仍失败")


class BaseLLMClient(ABC):
    name = "base"

    @abstractmethod
    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        ...

    def _complete_with_meta(self, system: str, user: str, *,
                            max_tokens: int = 2000) -> tuple[str, dict]:
        """返回（正文, 本次响应元数据）。默认无元数据；OpenAI 客户端带上 finish_reason。"""
        return self.complete(system, user, max_tokens=max_tokens), {}

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
        """结构化输出；模型写出**不是合法 JSON** 的回复时，对同一次调用纠错重试 1 次（任务 63）。

        只兜「坏 JSON」这一类（解析异常），网络/限流那套重试仍在 ``_post_retry`` 里，
        两者互不干扰。两次尝试各自的用量由 ``complete()`` 各记一条，不会少记。
        两次都坏 ⇒ 抛出带 ``pos`` 与现场片段的 ``json.JSONDecodeError``，让 job 的
        失败原因一眼可定位。
        """
        text, meta = self._complete_with_meta(system, user, max_tokens=max_tokens)
        try:
            return extract_json(text, response_meta=meta)
        except json.JSONDecodeError as exc:
            logger.warning("LLM 返回了坏 JSON，追加纠错提示后重试 1 次（purpose=%s）：%s",
                           usage_log.current_purpose(), exc.msg)
            text, meta = self._complete_with_meta(
                system, f"{user}{_BAD_JSON_HINT}", max_tokens=max_tokens
            )
            return extract_json(text, response_meta=meta)


class OpenAIClient(BaseLLMClient):
    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1",
                 timeout: float | None = None, *, disable_thinking: bool = False) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout if timeout is not None else get_settings().llm_timeout_s)
        self.disable_thinking = disable_thinking

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        content, _meta = self._complete_with_meta(system, user, max_tokens=max_tokens)
        return content

    def _complete_with_meta(self, system: str, user: str, *,
                            max_tokens: int = 2000) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if self.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        started = time.perf_counter()
        resp, retried = _post_retry(
            f"{self.base_url}/chat/completions",
            payload=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
            on_retry=lambda ms: record_retry(
                provider=self.name, model=self.model,
                system=system, user=user, latency_ms=ms),
        )
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = str((choice.get("message") or {}).get("content") or "")
        tokens = data.get("usage") or {}
        record_call(
            provider=self.name, model=self.model,
            prompt_tokens=tokens.get("prompt_tokens"),
            completion_tokens=tokens.get("completion_tokens"),
            system=system, user=user, content=content,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=len(retried) + 1,
        )
        finish_reason = choice.get("finish_reason") or "unknown"
        completion_tokens = tokens.get("completion_tokens", "unknown")
        meta = {
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "max_tokens": max_tokens,
        }
        if not content.strip():
            raise LLMResponseError(
                f"LLM 返回空 content（provider={self.name}, model={self.model}, "
                f"finish_reason={finish_reason}, completion_tokens={completion_tokens}, "
                f"max_tokens={max_tokens}）：该模型的推理 token 计入 max_tokens，"
                "预算可能被吃光；可调大 max_tokens 或设 OPENAI_DISABLE_THINKING=true"
            )
        return content, meta


class AnthropicClient(BaseLLMClient):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout if timeout is not None else get_settings().llm_timeout_s)

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        started = time.perf_counter()
        resp, retried = _post_retry(
            "https://api.anthropic.com/v1/messages",
            payload={
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=self.timeout,
            on_retry=lambda ms: record_retry(
                provider=self.name, model=self.model,
                system=system, user=user, latency_ms=ms),
        )
        data = resp.json()
        content = data["content"][0]["text"]
        tokens = data.get("usage") or {}
        record_call(
            provider=self.name, model=self.model,
            prompt_tokens=tokens.get("input_tokens"),
            completion_tokens=tokens.get("output_tokens"),
            system=system, user=user, content=content,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=len(retried) + 1,
        )
        return content


class MockLLMClient(BaseLLMClient):
    """无密钥时确保全流程可跑；返回结构化、内容可用的占位生成结果。"""

    name = "mock"

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        started = time.perf_counter()
        text = self._route(system, user)
        # mock 没有真实 usage：按文本长度估算并标注 estimated，保证计量链路可测
        record_call(
            provider=self.name, model=self.name,
            prompt_tokens=None, completion_tokens=None,
            system=system, user=user, content=text,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return text

    def _route(self, system: str, user: str) -> str:
        if "OUTLINE_JSON" in system:
            return self._outline(user)
        if "QUIZ_JSON" in system:
            return self._quiz(user)
        if "CONTENT_JSON" in system:
            return self._content(user)
        if "MERMAID" in system:
            return self._mermaid(user)
        if "CHART_JSON" in system:
            return self._chart(user)
        if "MATERIAL_JSON" in system:
            return self._material_json(user)
        if "CARDS_JSON" in system:
            return self._cards(user)
        if "DIAGRAM_JSON" in system:
            return self._diagram(user)
        if "LECTURE_JSON" in system:
            return self._lecture(user)
        if "CONCEPT_JSON" in system:
            return self._concepts(user)
        return self._text(user)

    def _diagram(self, user: str) -> str:
        """概念图占位：按布局类型给出可用的 DIAGRAM_JSON（保持无密钥可跑）。"""
        topic = (user.splitlines()[0] if user.splitlines() else "知识点").strip()
        layout_match = re.search(r"布局类型[:：]\s*(\w+)", user)
        layout = layout_match.group(1) if layout_match else "radial"
        caption = f"{topic} · 一图看懂"
        emoji = ["\U0001F4D8", "\U0001F4D7", "\U0001F7E0", "\U0001F7E3",
                 "\U0001F7E6", "\U0001F7E4"]
        if layout == "chain":
            titles = ["明确问题", "梳理条件", "执行步骤", "核对结果"]
            subs = [["先界定要解决的核心问题"], ["把已知条件逐条列出"],
                    ["按顺序推进每个环节"], ["检查输出是否达标"]]
            nodes = [{"emoji": emoji[i], "title": titles[i], "subs": [subs[i][0]]}
                     for i in range(4)]
            edges = [{"from": 0, "to": 1, "label": "整理"},
                     {"from": 1, "to": 2, "label": "执行"},
                     {"from": 2, "to": 3, "label": "验证"}]
        elif layout == "comic":
            titles = ["概念引入", "核心定义", "关键原理", "实例巩固"]
            extra = [["这一节要解决什么问题"], ["一句话说清概念边界"],
                     ["拆解工作机制"], ["用一个例子串起来"]]
            nodes = [{"emoji": emoji[i], "title": titles[i], "subs": extra[i]}
                     for i in range(4)]
            edges = []
        elif layout in ("radial", "ring"):
            titles = ["核心定义", "关键要点", "内在原理", "典型应用", "易错提醒"]
            notes = [["一句话说清边界"], ["记住最重要的结论"],
                     ["理解为什么成立"], ["会做典型题目"], ["避开常见误区"]]
            nodes = [{"emoji": emoji[i], "title": titles[i], "subs": notes[i]}
                     for i in range(5)]
            edges = []
        elif layout == "timeline":
            titles = ["引入", "定义", "原理", "应用", "巩固", "检验"]
            notes = [["明确学习目标"], ["掌握核心概念"], ["拆解工作机制"],
                     ["结合实例理解"], ["用自己的话复述"], ["完成随堂练习"]]
            nodes = [{"emoji": emoji[i], "title": titles[i], "subs": notes[i]}
                     for i in range(6)]
            edges = []
        elif layout == "compare":
            nodes = [
                {
                    "side": "left", "emoji": emoji[0],
                    "title": "方式 A", "subs": ["最常用的常规做法"],
                    "rows": [
                        {"label": "优点", "value": "稳妥、易上手"},
                        {"label": "局限", "value": "步骤偏多"},
                        {"label": "适用", "value": "基础场景"},
                    ],
                },
                {
                    "side": "right", "emoji": emoji[1],
                    "title": "方式 B", "subs": ["更高效的进阶做法"],
                    "rows": [
                        {"label": "优点", "value": "简洁、效率高"},
                        {"label": "局限", "value": "需要经验"},
                        {"label": "适用", "value": "进阶场景"},
                    ],
                },
            ]
            edges = []
        elif layout == "decision":
            nodes = [
                {"kind": "start", "title": "开始判断，条件是否满足？"},
                {"kind": "check", "title": "是否满足全部前置条件？",
                 "subs": ["不满足：先补齐前置条件"]},
                {"kind": "check", "title": "执行结果是否符合预期？",
                 "subs": ["不满足：定位差异并修正"]},
                {"kind": "good", "title": "\u2705 满足条件",
                 "subs": ["继续推进", "输出结论"]},
                {"kind": "bad", "title": "\u274C 未满足条件",
                 "subs": ["调整策略", "重新验证"]},
            ]
            edges = []
        else:
            nodes = [{"emoji": emoji[i], "title": f"要点 {i + 1}", "subs": ["围绕学习目标展开"]}
                     for i in range(4)]
            edges = []
        return json.dumps({"layout": layout, "caption": caption,
                           "nodes": nodes, "edges": edges}, ensure_ascii=False)

    def _cards(self, user: str) -> str:
        """卡片规划占位：第 1 张概念卡 + 第 2 张数据图卡（验证自动分流链路）。"""
        anchors: list[str] = []
        for line in (user or "").splitlines():
            s = line.strip()
            if s.startswith("### ") and not any(k in s for k in ("小结", "随堂", "习题")):
                t = s[4:].split("（")[0].split("：")[0].strip()[:20]
                if t and t not in anchors:
                    anchors.append(t)
        emoji = ["\U0001F4CC", "\U0001F9E9", "\u2705"]
        cards = []
        if anchors:
            a = anchors[0]
            cards.append({
                "anchor": a, "caption": (a[:10] or "概念卡"),
                "chart_type": "concept", "layout": "chain",
                "nodes": [
                    {"emoji": emoji[0], "title": (a[:12] or "概念"), "subs": ["把概念拆成小要点"]},
                    {"emoji": emoji[1], "title": "拆解", "subs": ["一图即可看懂"]},
                    {"emoji": emoji[2], "title": "自查", "subs": ["尝试复述一遍"]},
                ],
                "edges": [{"from": 0, "to": 1, "label": "再看"},
                          {"from": 1, "to": 2, "label": "复述"}],
            })
            if len(anchors) >= 2:
                b = anchors[1]
                cards.append({
                    "anchor": b, "caption": (b[:10] or "数值对比"),
                    "chart_type": "bar", "title": f"{b[:12]}对比",
                    "data": {"labels": ["第一组", "第二组", "第三组"],
                             "datasets": [{"label": "数值对比",
                                           "data": [3, 5, 8]}]},
                })
        return json.dumps({"cards": cards}, ensure_ascii=False)
    def _lecture(self, user: str) -> str:
        """完整讲义占位：纯文字正文 + mermaid 流程与随堂练习（默认零 AI 位图）。"""
        topic = (user.splitlines()[0] if user.splitlines() else "知识点").strip()
        markdown = (
            f"## {topic}\n\n"
            f"### 概念引入\n"
            f"先从贴近生活的现象切入「{topic}」，明确本节要解决的问题。\n\n"
            f"### 核心讲解\n"
            f"1. **定义**：先给出{topic}的准确描述，划清边界。\n"
            f"2. **原理**：拆解{topic}的工作机制与关键要素。\n"
            f"3. **举例**：用一个实例串起上述要点。\n\n"
            f"```mermaid\n"
            f"flowchart TD\n"
            f"    A([开始]) --> B[理解{topic}]\n"
            f"    B --> C[掌握关键点]\n"
            f"    C --> D([运用与巩固])\n"
            f"```\n\n"
            f"### 小结\n"
            f"{topic}是本节核心，掌握它有助于理解后续内容。\n\n"
            f"> 学习提示：尝试用自己的话复述定义并找出生活实例。"
        )
        quizzes = [
            {
                "qtype": "choice",
                "difficulty": "easy",
                "question": f"下列关于{topic}的说法，最准确的是？",
                "options": ["定义清晰", "定义模糊", "与实例无关", "无需学习"],
                "answer": "0",
                "explanation": f"掌握{topic}首先要明确其定义边界。",
            },
            {
                "qtype": "short",
                "difficulty": "medium",
                "question": f"请用自己的话说明{topic}的关键原理。",
                "options": [],
                "answer": f"能复述{topic}的核心机制即可。",
                "explanation": "考查对原理的理解而非背诵。",
            },
        ]
        return json.dumps({
            "markdown": markdown,
            "summary": f"{topic}：定义、原理与实例的一体化讲义。",
            "keywords": [topic, "定义", "原理"],
            "quizzes": quizzes,
        }, ensure_ascii=False)

    def _content(self, user: str) -> str:
        topic = (user.splitlines()[0] if user.splitlines() else "知识点").strip()
        md = (
            f"## {topic}\n\n"
            "### 讲解\n"
            f"我们先从定义、原理与实例三个层面掌握「{topic}」。\n\n"
            "1. **定义**：明确概念边界，与相近概念区分。\n"
            "2. **原理**：拆解工作机制，找到关键变量。\n"
            "3. **实例**：结合一个贴近生活的场景加深理解。\n\n"
            "### 小结\n"
            f"「{topic}」是后续内容的基础，务必用自己的话复述一遍。\n\n"
            "> 学习提示：尝试举例说明，并对照生活中的现象。"
        )
        return json.dumps(
            {
                "markdown": md,
                "summary": f"本知识点讲解{topic}的定义、原理与实例。",
                "keywords": [topic, "定义", "实例"],
            },
            ensure_ascii=False,
        )

    def _outline(self, user: str) -> str:
        lines = [ln for ln in user.splitlines() if ln.strip()]
        topic = lines[0].strip() if lines else "课程主题"
        subject = topic
        match = re.search(r"章节数[:：]\s*(\d+)", user)
        chapters = int(match.group(1)) if match else 4
        nodes: list[dict] = []
        for c in range(1, chapters + 1):
            nodes.append({
                "key": str(c),
                "title": f"第{c}章 {subject}基础概念",
                "node_type": "chapter",
                "objective": f"掌握{subject}第{c}章的核心概念",
                "teaching_strategy": "讲授 + 提问",
                "assessment": "选择题与简答",
                "prerequisites": [str(c - 1)] if c > 1 else [],
            })
        return json.dumps({"title": subject, "goal": topic, "nodes": nodes}, ensure_ascii=False)

    def _quiz(self, user: str) -> str:
        topic = (user.splitlines()[0] if user.splitlines() else "知识点").strip()
        return json.dumps({
            "items": [
                {
                    "qtype": "choice",
                    "difficulty": "medium",
                    "question": f"关于「{topic}」下列说法正确的是？",
                    "options": [f"{topic}的定义是 A", f"{topic}的定义是 B", f"{topic}的定义是 C", "以上都不对"],
                    "answer": "0",
                    "explanation": f"根据「{topic}」的标准定义，A 选项表述最准确。",
                },
                {
                    "qtype": "choice",
                    "difficulty": "easy",
                    "question": f"「{topic}」属于以下哪一类？",
                    "options": ["概念类", "流程类", "数据类", "方法类"],
                    "answer": "0",
                    "explanation": f"「{topic}」首先是一个概念性知识点。",
                },
                {
                    "qtype": "blank",
                    "difficulty": "medium",
                    "question": f"{topic}的核心特征是 ______。",
                    "options": [],
                    "answer": "结构清晰、可验证",
                    "explanation": "结构化与可验证是判断掌握程度的关键。",
                },
                {
                    "qtype": "short",
                    "difficulty": "hard",
                    "question": f"请用 1-2 句话解释「{topic}」为什么重要。",
                    "options": [],
                    "answer": "要点：解释其作用、场景与影响。",
                    "explanation": "简答题考察综合表达能力，建议结合实例作答。",
                },
            ]
        }, ensure_ascii=False)

    def _mermaid(self, user: str) -> str:
        desc = (user.splitlines()[0] if user.splitlines() else "流程").strip()
        steps = [s.strip() for s in re.split(r"[,，;；。]|然后|接着|再", desc) if s.strip()][:6]
        if len(steps) < 2:
            steps = [desc, "验证结果", "输出结论"]
        lines = ["flowchart TD", "    A([开始])"]
        for i, step in enumerate(steps):
            lines.append(f"    B{i}([{step}])")
            lines.append(f"    {'A' if i == 0 else 'B' + str(i - 1)} --> B{i}")
        lines.append(f"    B{len(steps) - 1} --> C([结束])")
        return "\n".join(lines)

    def _chart(self, user: str) -> str:
        """按图表类型返回可渲染的占位图表数据（保持全流程可测）。

        mindmap/timeline 也输出结构化数据（root/branches、steps），
        Excalidraw 原件由服务端据此生成，mermaid 由服务端回推。
        """
        match = re.search(r"图类型[:：]\s*(\w+)", user)
        ctype = match.group(1) if match else "bar"
        line0 = next((ln.strip() for ln in user.splitlines() if "课程：" in ln), "课程主题")
        topic = line0.split("：", 1)[-1].strip()[:24]
        if ctype == "line":
            data = {"labels": ["2021", "2022", "2023", "2024"],
                    "datasets": [{"label": "观测值", "data": [120, 150, 132, 180]}]}
        elif ctype == "pie":
            data = {"labels": ["第一类", "第二类", "第三类"],
                    "datasets": [{"data": [45, 30, 25],
                                  "backgroundColor": ["#4F46E5", "#10B981", "#F59E0B"]}]}
        elif ctype == "mindmap":
            data = {"root": topic[:12],
                    "branches": [{"emoji": "\U0001F7E2", "title": "分支一",
                                  "subs": ["要点甲", "要点乙"]},
                                 {"emoji": "\U0001F7E1", "title": "分支二",
                                  "subs": ["要点丙"]},
                                 {"emoji": "\U0001F534", "title": "分支三",
                                  "subs": ["要点丁"]}]}
        elif ctype == "timeline":
            data = {"steps": [{"emoji": "\U0001F4CB", "title": "起点",
                               "subs": ["明确目标"]},
                              {"emoji": "\U0001F527", "title": "推进",
                               "subs": ["逐步执行"]},
                              {"emoji": "\u2705", "title": "落地",
                               "subs": ["校验结果"]}]}
        elif ctype == "table":
            data = {"headers": ["指标", "数值", "说明"],
                    "rows": [["指标一", "100", "说明一"], ["指标二", "80", "说明二"]]}
        elif ctype == "quadrant":
            # 两轴四象限：x = 紧急度、y = 重要度；四格按「左上 → 右上 → 左下 → 右下」
            data = {"x_low": "不紧急", "x_high": "紧急",
                    "y_low": "不重要", "y_high": "重要",
                    "quadrants": [{"name": "计划做", "desc": "重要但不紧急"},
                                  {"name": "马上做", "desc": "又重要又紧急"},
                                  {"name": "不要做", "desc": "既不重要也不紧急"},
                                  {"name": "委托做", "desc": "紧急但不重要"}]}
        else:
            data = {"labels": ["第一组", "第二组", "第三组"],
                    "datasets": [{"label": "数值对比", "data": [30, 52, 41]}]}
        return json.dumps({"title": f"{topic}·{ctype}图表", "data": data}, ensure_ascii=False)

    def _material_json(self, user: str) -> str:
        """按输入方向列表生成占位 AI 补充素材（保持完整闭环可测）。"""
        match = re.search(r"方向各补充一份备课素材[:：]\s*([^\n]+)", user)
        labels = [s.strip() for s in match.group(1).split("、") if s.strip()] if match else []
        if not labels:
            labels = ["概念铺垫"]
        materials = []
        for label in labels:
            materials.append({
                "direction": label,
                "title": f"{label}补充素材",
                "text": (
                    f"## {label}补充素材\n\n"
                    f"（AI 补充）围绕课程主题补充“{label}”相关内容，供教师核对后选用。\n\n"
                    "1. **要点一**：……\n"
                    "2. **要点二**：……\n"
                    "3. **教学提示**：……\n\n"
                    "> 本素材由 AI 生成，建议结合教师经验校对后使用。"
                ),
            })
        return json.dumps({"materials": materials}, ensure_ascii=False)

    _TERM_SEP = re.compile(r"[与和的及、/｜|]")

    def _term(self, title: str) -> str:
        """占位概念名：取标题最后一段名词短语（≤8 字），避免与标题同名被质量约束过滤。"""
        parts = [p.strip() for p in self._TERM_SEP.split(str(title or "")) if p.strip()]
        term = re.sub(r"^第\s*[0-9]+\s*[章节]\s*", "", parts[-1] if parts else "")
        return term.strip()[:8] or "核心概念"

    def _concepts(self, user: str) -> str:
        """概念库占位：按提示词里的知识点生成原子笔记概念（无密钥可跑全流程）。

        真实 LLM 会给出课程特有的概念；占位版保证结构完整且可测：每个知识点一个名词性
        术语概念，外加一个「每个知识点都提到」的跨章概念（用于验证合并只追加 chapters）。
        任务 41 起概念要走「跨 ≥2 个知识点」准入，所以占位版给每个术语声明相邻知识点，
        否则它们都会被降级成 meta.terminology（无密钥演示就抽不出概念了）。
        """
        pairs = re.findall(r"^- key=(\S+) \| title=(.+?) \| objective=", user, re.M)
        keys = [key for key, _title in pairs]
        terms = [self._term(t) for _k, t in pairs]
        concepts: list[dict] = []
        for index, (key, title) in enumerate(pairs):
            name = self._term(title)
            others = [t for t in terms if t != name][:2]
            neighbour = keys[(index + 1) % len(keys)] if len(keys) > 1 else key
            concepts.append({
                "name": name,
                "aliases": [],
                "definition": f"{name}是本课程的基础概念，先弄清它的含义与作用。",
                "points": [f"{name}的定义与适用范围", f"{name}的关键要素",
                           f"{name}在实践中的表现"],
                "broader": others[:1],
                "narrower": [],
                "related": others[1:2],
                "pitfalls": [f"只记住{name}这个名字，不理解它的作用"],
                "chapters": sorted({key, neighbour}),
                "sources": [{"kind": "outline", "ref": key,
                             "quote": f"{key} {name}"}],
                "_index": index,
            })
        for key in keys:
            concepts.append({
                "name": "发酵",
                "aliases": ["fermentation"] if key == (keys[0] if keys else "") else [],
                "definition": "微生物分解有机物、产生能量或气体的过程。",
                "points": ["需要合适的温度与湿度", "产气是发酵的直接表现",
                           "发酵过度会发酸"],
                "broader": [],
                "narrower": [self._term(t) for _k, t in pairs[:2]],
                "related": [],
                "pitfalls": ["把发酵与腐败混为一谈"],
                "chapters": [key],
                "sources": [{"kind": "lecture", "ref": key, "quote": "发酵"}],
            })
        return json.dumps({"concepts": concepts}, ensure_ascii=False)

    def _text(self, user: str) -> str:
        topic = (user.splitlines()[0] if user.splitlines() else "知识点").strip()
        return (
            f"## {topic}\n\n"
            f"### 讲解\n{topic}是本节的核心知识点。我们从定义、原理与实例三个层面展开：\n\n"
            f"1. **定义**：先给出{topic}的准确描述，明确边界。\n"
            f"2. **原理**：解释其工作机制与关键要素。\n"
            f"3. **实例**：结合一个贴近生活的例子加深理解。\n\n"
            f"### 小结\n{topic}的重要性体现在理解后续内容的基础性上。\n\n"
            f"> 学习提示：尝试用自己的话复述定义，并找一个生活中的例子。"
        )


# ---- 任务 117：模型配置「有没有配好」的判定与文案（唯一出处）----
# 为什么有这条：以前「显式选了真 provider 却忘了填 Key」会**静默退回 mock** ——
# 老师以为拿到的是模型产出，其实是一段占位文本，`/api/health` 里也完全看不出来。
# 现在这一处判定同时服务两件事（**不新造第二套**）：
#   ① 能力探测：`capabilities.llm_capability()` 把结果挂进 `GET /api/health`；
#   ② 真拒绝：`build_llm_client()` 抛 `LLMConfigError`，由 `main.py` 转成 503 人话。
# ⚠️ 安全（任务 117 §二.A.3）：这里只碰**变量名**，任何情况下都不读、不拼、不回显
#    Key 的值或它的前缀；文案里也不带 Key 的长度与掩码。
LLM_PROVIDER_ENV = "LLM_PROVIDER"
LLM_PROVIDER_KEYS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
LLM_MOCK_PROVIDER = "mock"
LLM_CONFIG_HINT = (f"在 backend/.env 里设置 {LLM_PROVIDER_ENV} 与对应的 Key（改完重启后端）；"
                   f"不想花钱就用 {LLM_PROVIDER_ENV}={LLM_MOCK_PROVIDER}（零配置、不调真模型）")
LLM_UNCONFIGURED_TAIL = "这是环境配置没弄好，不是你的课有问题"


class LLMJsonError(json.JSONDecodeError):
    """模型返回的正文不是合法 JSON（截断 / 夹坏引号 / 不是 JSON）。

    仍继承 ``json.JSONDecodeError``：``lecture._failure_reason`` 的 ``llm-json``
    归类不变。``main.py`` 把它转成 503 + ``detail``；若本次响应是
    ``finish_reason=length``，消息里会补一句推理预算的人话提示。
    """

    def __init__(self, msg: str, doc: str, pos: int, *,
                 response_meta: dict | None = None) -> None:
        meta = dict(response_meta or {})
        finish_reason = meta.get("finish_reason")
        if finish_reason == "length":
            completion_tokens = meta.get("completion_tokens", "unknown")
            max_tokens = meta.get("max_tokens", "unknown")
            msg = (
                f"{msg}；finish_reason=length（completion_tokens={completion_tokens}, "
                f"max_tokens={max_tokens}）：多半是推理 token 吃光 max_tokens，"
                "可调大 max_tokens 或设 OPENAI_DISABLE_THINKING=true"
            )
        super().__init__(msg, doc, pos)
        self.response_meta = meta
        self.finish_reason = finish_reason


class LLMResponseError(RuntimeError):
    """模型返回了空 content（多为推理 token 吃光 max_tokens）。

    由 ``main.py`` 转成 503 + ``detail``：前端/连接器直接显示这句话，
    而不是一条裸的 500 ``Internal Server Error``。
    """


class LLMConfigError(RuntimeError):
    """模型没配好（缺 Key / provider 不认识）：要调模型的动作给**人话拒绝**，不是 500、也不静默给假内容。"""

    def __init__(self, message: str, *, provider: str = "", env_var: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.env_var = env_var

    def as_payload(self) -> dict[str, str]:
        """接口返回体：只有变量名与人话，**没有 Key**（也不含 Key 的长度/掩码）。"""
        return {
            "detail": str(self),
            "code": "llm_not_configured",
            "provider": self.provider,
            "env_var": self.env_var,
            "hint": LLM_CONFIG_HINT,
        }


def normalized_provider(settings=None) -> str:
    """``LLM_PROVIDER`` 归一化（小写、去空白）—— 探测与实际构造共用这一个出口。"""
    settings = settings if settings is not None else get_settings()
    return str(getattr(settings, "llm_provider", "") or "").strip().lower()


def llm_config_problem(settings=None) -> dict[str, str] | None:
    """模型配置有没有问题：没问题返回 None，有问题返回 {provider, env_var, message}（全是人话，无 Key）。"""
    settings = settings if settings is not None else get_settings()
    provider = normalized_provider(settings)
    if provider == LLM_MOCK_PROVIDER:
        return None
    env_var = LLM_PROVIDER_KEYS.get(provider)
    if env_var is None:
        return {
            "provider": provider,
            "env_var": LLM_PROVIDER_ENV,
            "message": (f"{LLM_PROVIDER_ENV}={provider or '（空）'} 不认识：只认 {LLM_MOCK_PROVIDER}"
                        f"（零配置、不花钱）与 {' / '.join(LLM_PROVIDER_KEYS)}（要配对应 Key）。"
                        f"要调模型的动作会被人话拒绝，其余功能照常可用 —— {LLM_UNCONFIGURED_TAIL}"),
        }
    if not str(getattr(settings, f"{provider}_api_key", "") or "").strip():
        return {
            "provider": provider,
            "env_var": env_var,
            "message": (f"没配 {env_var}（{LLM_PROVIDER_ENV}={provider} 需要它）："
                        f"要调模型的动作（大纲 / 讲义 / 课件 / 图表 / 习题）会被人话拒绝，"
                        f"其余功能照常可用 —— {LLM_UNCONFIGURED_TAIL}"),
        }
    return None


def llm_config_summary(settings=None) -> str:
    """一句话说清「模型配置现在是什么档位」（**只含变量名，绝不含 Key**）。"""
    settings = settings if settings is not None else get_settings()
    provider = normalized_provider(settings)
    problem = llm_config_problem(settings)
    if problem is not None:
        return f"{problem['message']}（{LLM_CONFIG_HINT}）"
    if provider == LLM_MOCK_PROVIDER:
        return f"{LLM_PROVIDER_ENV}={LLM_MOCK_PROVIDER}（零配置档位：不调真模型、不花钱）"
    return (f"{LLM_PROVIDER_ENV}={provider}（已配 {LLM_PROVIDER_KEYS[provider]}；"
            f"Key 值不回显、也不进日志与接口返回）")


def build_llm_client() -> BaseLLMClient:
    """根据配置构造 LLM 客户端（openai / anthropic / openrouter / mock）。

    任务 117：**先按 `llm_config_problem()`（唯一出处）判一次** ——
    「显式选了真 provider 却没填 Key」或「provider 名不认识」时抛 `LLMConfigError`，
    由 `main.py` 转成 503 人话；**不再静默退回 mock**（那会让老师以为拿到的是模型产出）。
    只有配置没问题的 mock / 真 provider 才会走到下面的分支。
    """
    settings = get_settings()
    problem = llm_config_problem(settings)
    if problem is not None:
        raise LLMConfigError(problem["message"], provider=problem["provider"],
                             env_var=problem["env_var"])
    provider = normalized_provider(settings)
    timeout = settings.llm_timeout_s
    if provider == "openai":
        return OpenAIClient(settings.openai_api_key, settings.openai_model,
                            base_url=settings.openai_base_url, timeout=timeout,
                            disable_thinking=settings.openai_disable_thinking)
    if provider == "anthropic":
        return AnthropicClient(settings.anthropic_api_key, settings.anthropic_model, timeout=timeout)
    if provider == "openrouter":
        return OpenAIClient(
            settings.openrouter_api_key,
            settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            timeout=timeout,
        )
    return MockLLMClient()
