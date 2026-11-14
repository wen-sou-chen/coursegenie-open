"""LLM 用量与成本计量（任务 23-A）。

设计要点：
- 计量发生在 LLM 客户端内部（``llm.py``）：只有客户端拿得到 provider 返回的
  ``usage`` 与真实耗时；重试的每一次尝试也各自记一条（重试累加）。
- 采集用 ``contextvars``：``with collect() as col:`` 包住一次生成，期间所有
  LLM 调用（含嵌套的配图/概念卡/习题）都会累加到同一个采集器；采集器可重入，
  内层不会再开新账，保证「讲义总用量 = 讲义 + 它的配图 + 卡片 + 习题」。
- 多线程安全：``contextvars`` 在新线程里是独立的空上下文，并发生成时每个
  线程各记各的，互不串账。
- 计价：``model_prices`` 可配置（元 / 百万 tokens）；未知模型照记 tokens，
  单价缺失时 ``cost_cny=0`` 并显式标注「未配置单价」，不猜价格。
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# DeepSeek 官方定价（元 / 百万 tokens，2025-09 官方价：deepseek-chat 输入 2 / 输出 3，
# deepseek-reasoner 输入 4 / 输出 16）。官方调价后用 MODEL_PRICES 覆盖即可。
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 2.0, "output": 3.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    # OpenRouter 走的仍是 DeepSeek 官方价，模型名带厂商前缀
    "deepseek/deepseek-chat": {"input": 2.0, "output": 3.0},
    "deepseek/deepseek-reasoner": {"input": 4.0, "output": 16.0},
}

UNPRICED_NOTE = "未配置单价"

# 辅助用量的历史账本（落在 course.meta）：aux_usage 是「重跑覆盖」，
# 被覆盖的版本不计入就会让页签上的「累计费用」名不副实（P9-4）。
AUX_HISTORY_KEY = "aux_usage_history"

# 单条计量的字段（落盘 / 接口透传统一用这些 key）
CALL_FIELDS = ("provider", "model", "purpose", "prompt_tokens", "completion_tokens",
               "latency_ms", "attempts", "estimated", "cost_cny", "priced")


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中日韩字符按 1 个、其余按 4 字符 1 个（无 usage 时兜底）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    return max(1, cjk + (len(text) - cjk) // 4)


def _user_prices() -> dict[str, dict[str, float]]:
    """解析 MODEL_PRICES（JSON）：``{"model": {"input": 2, "output": 3}}`` 或 ``{"model": 2}``。"""
    raw = str(getattr(get_settings(), "model_prices", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for model, value in parsed.items():
        if isinstance(value, dict):
            price_in = value.get("input", value.get("in"))
            price_out = value.get("output", value.get("out"))
        else:
            price_in = price_out = value
        try:
            out[str(model)] = {"input": float(price_in), "output": float(price_out)}
        except (TypeError, ValueError):
            continue
    return out


def price_of(model: str) -> dict[str, float] | None:
    """取模型单价（元 / 百万 tokens）；未配置返回 None。"""
    name = str(model or "")
    prices = _user_prices()
    if name in prices:
        return prices[name]
    if name in DEFAULT_PRICES:
        return DEFAULT_PRICES[name]
    return None


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
    """按单价换算费用（元）。返回 (cost_cny, priced)。"""
    price = price_of(model)
    if price is None:
        return 0.0, False
    cost = (int(prompt_tokens) / 1_000_000) * price["input"] + \
        (int(completion_tokens) / 1_000_000) * price["output"]
    return round(cost, 6), True


class UsageCollector:
    """一次生成期间的调用流水（线程内独立）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.started = time.perf_counter()
        # 嵌套深度：1 = 本层新建（外层生成器负责把汇总写进条目），>1 = 复用了外层的采集器
        self.depth = 0

    def add(self, call: dict) -> dict:
        self.calls.append(call)
        return call

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def summary(self, *, duration_ms: int | None = None) -> dict:
        return summarize(self.calls, duration_ms=self.elapsed_ms() if duration_ms is None else duration_ms)


_current: ContextVar[UsageCollector | None] = ContextVar("cg_usage_collector", default=None)
_purpose: ContextVar[str] = ContextVar("cg_usage_purpose", default="other")


def current_purpose() -> str:
    """当前调用用途（lecture/diagram/cards/...），由 ``complete``/``complete_json`` 标注。"""
    return _purpose.get()


@contextmanager
def use_purpose(name: str) -> Iterator[None]:
    token = _purpose.set(str(name or "other"))
    try:
        yield
    finally:
        _purpose.reset(token)


def complete(client, system: str, user: str, *, max_tokens: int = 2000,
             purpose: str = "other") -> str:
    """带用途标注的 LLM 调用：用途走上下文变量，客户端接口保持稳定（测试替身不受影响）。"""
    with use_purpose(purpose):
        return client.complete(system, user, max_tokens=max_tokens)


def complete_json(client, system: str, user: str, *, max_tokens: int = 2000,
                  purpose: str = "other") -> dict:
    with use_purpose(purpose):
        data = client.complete_json(system, user, max_tokens=max_tokens)
    _warn_control_chars(data, purpose)
    return data


def _warn_control_chars(data, purpose: str) -> None:
    """解析结果里混进 XML 不允许的控制字符就记一条 warning（任务 48w 第三层）。

    带 ``purpose``（lecture / cards / diagram …），定位是哪一步的 LLM 输出；
    **只记日志、不加判据、不改质检口径**。
    """
    from app.services.llm import find_control_chars  # 延迟导入：llm 依赖本模块记用量

    chars = find_control_chars(data)
    if not chars:
        return
    summary = "、".join(f"U+{ord(c):04X}×{chars.count(c)}" for c in sorted(set(chars)))
    logger.warning("LLM 输出含 XML 控制字符（本该是 LaTeX 反斜杠被 JSON 吃掉的痕迹）："
                   "purpose=%s 共 %d 处 [%s]", purpose, len(chars), summary)


@contextmanager
def collect() -> Iterator[UsageCollector]:
    """开启（或复用）一个采集器；可重入，外层负责最终汇总（depth==1 时）。"""
    existing = _current.get()
    if existing is not None:
        existing.depth += 1
        try:
            yield existing
        finally:
            existing.depth -= 1
        return
    collector = UsageCollector()
    collector.depth = 1
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def record(
    *,
    provider: str,
    model: str,
    purpose: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    attempts: int = 1,
    estimated: bool = False,
    retried: bool = False,
) -> dict | None:
    """记一次 LLM 调用（没有活动采集器时静默丢弃）。"""
    collector = _current.get()
    if collector is None:
        return None
    cost, priced = cost_of(model, prompt_tokens, completion_tokens)
    return collector.add({
        "provider": provider,
        "model": model,
        "purpose": purpose or "other",
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(prompt_tokens) + int(completion_tokens),
        "latency_ms": int(latency_ms),
        "attempts": int(attempts),
        "estimated": bool(estimated),
        "retried": bool(retried),
        "cost_cny": cost,
        "priced": priced,
    })


def summarize(calls: list[dict], *, duration_ms: int = 0) -> dict:
    """把调用流水折成一份汇总（单篇 / 任务 / 课程三级共用同一口径）。"""
    prompt = sum(int(c.get("prompt_tokens") or 0) for c in calls)
    completion = sum(int(c.get("completion_tokens") or 0) for c in calls)
    cost = round(sum(float(c.get("cost_cny") or 0.0) for c in calls), 6)
    unpriced = sorted({str(c.get("model") or "") for c in calls if not c.get("priced", True)})
    models = sorted({str(c.get("model") or "") for c in calls})
    by_purpose: dict[str, dict] = {}
    for call in calls:
        bucket = by_purpose.setdefault(
            str(call.get("purpose") or "other"),
            {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "total_tokens": 0, "cost_cny": 0.0, "latency_ms": 0},
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += int(call.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(call.get("completion_tokens") or 0)
        bucket["total_tokens"] += int(call.get("total_tokens") or 0)
        bucket["cost_cny"] = round(bucket["cost_cny"] + float(call.get("cost_cny") or 0.0), 6)
        bucket["latency_ms"] += int(call.get("latency_ms") or 0)
    summary = {
        "llm_calls": len(calls),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cost_cny": cost,
        "duration_ms": int(duration_ms),
        "models": models,
        "unpriced_models": unpriced,
        "estimated_calls": sum(1 for c in calls if c.get("estimated")),
        "retried_calls": sum(1 for c in calls if c.get("retried")),
        "by_purpose": by_purpose,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if unpriced:
        summary["note"] = f"以下模型{UNPRICED_NOTE}，费用计 0：{'、'.join(unpriced)}"
    return summary


def merge(*summaries: dict | None) -> dict:
    """合并多份汇总（任务级 / 课程级）。"""
    rows = [s for s in summaries if s]
    if not rows:
        return summarize([])
    merged_calls: list[dict] = []
    by_purpose: dict[str, dict] = {}
    duration = 0
    for row in rows:
        duration += int(row.get("duration_ms") or 0)
        merged_calls += _calls_of(row)
        for purpose, bucket in (row.get("by_purpose") or {}).items():
            target = by_purpose.setdefault(purpose, {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cost_cny": 0.0, "latency_ms": 0,
            })
            for field in ("calls", "prompt_tokens", "completion_tokens",
                          "total_tokens", "latency_ms"):
                target[field] += int(bucket.get(field) or 0)
            target["cost_cny"] = round(target["cost_cny"] + float(bucket.get("cost_cny") or 0.0), 6)
    summary = summarize(merged_calls, duration_ms=duration)
    summary["by_purpose"] = by_purpose
    summary["estimated_calls"] = sum(int(r.get("estimated_calls") or 0) for r in rows)
    summary["retried_calls"] = sum(int(r.get("retried_calls") or 0) for r in rows)
    summary["models"] = sorted({m for r in rows for m in (r.get("models") or [])})
    unpriced = sorted({m for r in rows for m in (r.get("unpriced_models") or [])})
    summary["unpriced_models"] = unpriced
    if unpriced:
        summary["note"] = f"以下模型{UNPRICED_NOTE}，费用计 0：{'、'.join(unpriced)}"
    else:
        summary.pop("note", None)
    return summary


def _calls_of(summary: dict) -> list[dict]:
    """从汇总还原出「够用」的调用流水（合并用；逐条明细不单独落盘）。"""
    calls: list[dict] = []
    for purpose, bucket in (summary.get("by_purpose") or {}).items():
        calls.append({
            "purpose": purpose,
            "prompt_tokens": int(bucket.get("prompt_tokens") or 0),
            "completion_tokens": int(bucket.get("completion_tokens") or 0),
            "total_tokens": int(bucket.get("total_tokens") or 0),
            "latency_ms": int(bucket.get("latency_ms") or 0),
            "cost_cny": float(bucket.get("cost_cny") or 0.0),
            "priced": True,
            "model": "",
        })
    if not calls:
        calls.append({
            "purpose": "other",
            "prompt_tokens": int(summary.get("prompt_tokens") or 0),
            "completion_tokens": int(summary.get("completion_tokens") or 0),
            "total_tokens": int(summary.get("total_tokens") or 0),
            "cost_cny": float(summary.get("cost_cny") or 0.0),
            "priced": True,
            "model": "",
        })
    return calls


def attach(item, collector: UsageCollector, *, duration_ms: int | None = None) -> dict | None:
    """把本次采集结果写进条目 ``meta.usage``（无调用则不写）。"""
    if item is None or collector is None or not collector.calls:
        return None
    summary = collector.summary(duration_ms=duration_ms)
    item.meta = {**(item.meta or {}), "usage": summary}
    return summary


def item_usage(item) -> dict | None:
    return (item.meta or {}).get("usage") if item is not None else None


def record_aux(course, purpose: str, summary: dict | None) -> bool:
    """把「不属于任何内容条目」的一次生成用量记到课程级 ``course.meta['aux_usage']``。

    大纲生成落在 Outline 行、概念抽取一次调用产出 N 条概念，两者都没有对应的
    内容条目，原先也没有采集器 → 调用与 token 被 ``record()`` 静默丢弃
    （P3-4 / P6-2 / P9-3，实测少报 18.5%）。这里给它们一个课程级账本，
    ``analytics.course_usage`` 会把它折成页签里的一行。

    同一 purpose 重跑时**覆盖**上一份：重新生成大纲 / 重新抽概念本就会替换旧结果，
    历史累加会与实际产出对不上。无调用（或没有 summary）时不写，返回是否写入。
    """
    if not summary or not int(summary.get("llm_calls") or 0):
        return False
    meta = dict(course.meta or {})
    aux = dict(meta.get("aux_usage") or {})
    aux[purpose] = summary
    meta["aux_usage"] = aux
    # P9-4：aux_usage 是覆盖写，历史累计单独记一份账本，
    # 页签才能同时给出「当前版本」与「历史累计」两个口径。
    history = dict(meta.get(AUX_HISTORY_KEY) or {})
    row = dict(history.get(purpose) or {})
    now = str(summary.get("updated_at") or datetime.now(timezone.utc).isoformat())
    row["runs"] = int(row.get("runs") or 0) + 1
    for field in ("llm_calls", "prompt_tokens", "completion_tokens",
                  "total_tokens", "duration_ms"):
        row[field] = int(row.get(field) or 0) + int(summary.get(field) or 0)
    row["cost_cny"] = round(float(row.get("cost_cny") or 0.0)
                            + float(summary.get("cost_cny") or 0.0), 6)
    row["first_at"] = row.get("first_at") or now
    row["last_at"] = now
    history[purpose] = row
    meta[AUX_HISTORY_KEY] = history
    course.meta = meta
    return True


def aux_usage(course, purpose: str) -> dict | None:
    """读课程级辅助用量（大纲 / 概念抽取）；没有记录时返回 None。"""
    return ((course.meta or {}).get("aux_usage") or {}).get(purpose)


def aux_history(course) -> dict:
    """读辅助用量的历史账本：purpose → 多次跑的累计（含被重跑覆盖的版本）。"""
    return dict((course.meta or {}).get(AUX_HISTORY_KEY) or {})


def batch_usage(per_node: list[dict], summaries: list[dict]) -> dict:
    """任务级用量：合计 + per_node（每个知识点一行）。"""
    merged = merge(*summaries)
    merged["per_node"] = per_node
    merged["item_count"] = len(per_node)
    return merged
