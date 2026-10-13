"""可观测性与成本：让「它刚才做了什么、花了多少、慢在哪」都有答案。

对照正文 hx-24。会话日志（hx-04）回答的是「发生了什么」，而且是为了回放；
可观测性回答的是另外三类问题：

- **慢在哪**：一轮里模型调用、每个工具、等待审批各花了多少时间。用 span 表示：
  每段工作有开始、结束、父 span，组成一棵树。
- **花了多少**：token 用量按四个桶（hx-05）乘以各自的单价，按轮、按会话、按模型汇总。
  缓存读和缓存写的价格不同，混在一起账就错。
- **整体怎么样**：成功率、平均轮数、重试次数、熔断打开次数。这些是跨会话的指标。

两条原则：
1. **观测不能改变行为**。观测代码出错（写文件失败、上报超时）不能让 agent 失败。
2. **默认不上报内容**。span 里记工具名和耗时，不记参数和输出的全文；
   用户的代码和对话属于用户，上报要用户明确同意。

只用标准库。时钟可以注入；导出格式是每行一条 JSON，任何日志系统都能接。
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

Clock = Callable[[], float]

# 每百万 token 的单价（美元）。数值是教学示意，不是任何厂商的真实价格；真实价格要从配置读。
DEMO_PRICES: dict[str, dict[str, float]] = {
    "big-model":   {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "small-model": {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00},
}


@dataclass
class Span:
    """一段有开始和结束的工作。attrs 只放元信息，不放内容全文。"""

    name: str
    span_id: str
    parent_id: str | None
    start: float
    end: float | None = None
    status: str = "ok"
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return (self.end if self.end is not None else self.start) - self.start


SENSITIVE_KEYS = ("content", "input", "output", "arguments", "args", "text", "prompt", "messages")


def redact(attrs: dict[str, Any], *, keep_content: bool = False) -> dict[str, Any]:
    """去掉可能包含用户内容的字段，只保留长度。keep_content=True 时原样保留（需要用户同意）。"""
    if keep_content:
        return dict(attrs)
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if any(s in key.lower() for s in SENSITIVE_KEYS):
            out[f"{key}_chars"] = len(str(value))
        else:
            out[key] = value
    return out


class Tracer:
    """收集 span。嵌套靠一个栈：with tracer.span(...) 里面再开的 span 自动成为子 span。"""

    def __init__(self, clock: Clock = time.monotonic, *, keep_content: bool = False,
                 sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.clock = clock
        self.keep_content = keep_content
        self.sink = sink
        self.spans: list[Span] = []
        self._stack: list[Span] = []
        self.sink_errors = 0
        self.trace_id = uuid.uuid4().hex[:16]

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        parent = self._stack[-1].span_id if self._stack else None
        current = Span(name, uuid.uuid4().hex[:8], parent, self.clock(), attrs=redact(attrs, keep_content=self.keep_content))
        self._stack.append(current)
        try:
            yield current
        except BaseException as exc:
            current.status = "error"
            current.attrs["error"] = type(exc).__name__
            raise
        finally:
            current.end = self.clock()
            self._stack.pop()
            self.spans.append(current)
            self._emit(current)

    def _emit(self, span: Span) -> None:
        """导出一条 span。导出失败只计数，不影响 agent：观测不能改变行为。"""
        if self.sink is None:
            return
        try:
            self.sink(to_record(span, self.trace_id))
        except Exception:  # noqa: BLE001
            self.sink_errors += 1

    def children(self, span_id: str) -> list[Span]:
        return [s for s in self.spans if s.parent_id == span_id]

    def slowest(self, n: int = 3, *, leaf_only: bool = True) -> list[Span]:
        """最慢的几段工作。默认只看叶子 span：父 span 的耗时包含子 span，排在前面没有信息量。"""
        parents = {s.parent_id for s in self.spans}
        pool = [s for s in self.spans if not leaf_only or s.span_id not in parents]
        return sorted(pool, key=lambda s: s.duration, reverse=True)[:n]


def to_record(span: Span, trace_id: str) -> dict[str, Any]:
    """一行 JSON 的形状。字段名接近 OpenTelemetry 的约定，方便以后换成真正的导出器。"""
    return {"trace_id": trace_id, "span_id": span.span_id, "parent_id": span.parent_id,
            "name": span.name, "start": round(span.start, 6), "duration": round(span.duration, 6),
            "status": span.status, "attrs": span.attrs}


def jsonl_sink(path: str) -> Callable[[dict[str, Any]], None]:
    """每条 span 追加一行到文件。"""

    def write(record: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return write


# ---------- 成本 ----------


@dataclass
class Usage:
    """hx-05 的四个桶。input 不含缓存命中的部分。"""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input + other.input, self.output + other.output,
                     self.cache_read + other.cache_read, self.cache_write + other.cache_write)


def cost_of(usage: Usage, model: str, prices: dict[str, dict[str, float]] = DEMO_PRICES) -> float:
    """按四个桶各自的单价算钱。未知模型直接报错：静默按 0 算会让账本看起来很便宜。"""
    if model not in prices:
        raise KeyError(f"没有模型 {model!r} 的价格；请在价格表里补上，不要按 0 计")
    p = prices[model]
    return (usage.input * p["input"] + usage.output * p["output"] +
            usage.cache_read * p["cache_read"] + usage.cache_write * p["cache_write"]) / 1_000_000


@dataclass
class LedgerEntry:
    turn: int
    model: str
    usage: Usage
    cost: float
    kind: str = "main"          # main / retry / subagent / compaction


@dataclass
class CostLedger:
    """按轮记账。重试、子代理、压缩各自单独一类：它们是最常见的「隐形开销」。"""

    prices: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEMO_PRICES))
    entries: list[LedgerEntry] = field(default_factory=list)
    budget: float | None = None

    def record(self, turn: int, model: str, usage: Usage, kind: str = "main") -> LedgerEntry:
        entry = LedgerEntry(turn, model, usage, cost_of(usage, model, self.prices), kind)
        self.entries.append(entry)
        return entry

    def total(self) -> float:
        return sum(e.cost for e in self.entries)

    def by(self, key: str) -> dict[Any, float]:
        out: dict[Any, float] = {}
        for e in self.entries:
            k = getattr(e, key)
            out[k] = out.get(k, 0.0) + e.cost
        return out

    def cache_hit_rate(self) -> float:
        """缓存命中占全部输入的比例。分母是 input + cache_read + cache_write。"""
        u = sum((e.usage for e in self.entries), Usage())
        denominator = u.input + u.cache_read + u.cache_write
        return u.cache_read / denominator if denominator else 0.0

    def over_budget(self) -> bool:
        return self.budget is not None and self.total() > self.budget

    def summary(self) -> str:
        parts = [f"共 ${self.total():.4f}"]
        kinds = self.by("kind")
        for kind in ("retry", "subagent", "compaction"):
            if kinds.get(kind):
                parts.append(f"{kind} ${kinds[kind]:.4f}")
        parts.append(f"缓存命中率 {self.cache_hit_rate():.0%}")
        if self.budget is not None:
            parts.append(f"预算 ${self.budget:.2f}" + ("（已超出）" if self.over_budget() else ""))
        return "，".join(parts)


# ---------- 跨会话指标 ----------


@dataclass
class Metrics:
    """计数器。跨会话聚合，用来看「整体怎么样」，而不是排查某一次。"""

    counters: dict[str, int] = field(default_factory=dict)

    def inc(self, name: str, by: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + by

    def rate(self, part: str, whole: str) -> float:
        total = self.counters.get(whole, 0)
        return self.counters.get(part, 0) / total if total else 0.0
