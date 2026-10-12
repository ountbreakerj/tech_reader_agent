"""Token 预算与计数：估算、窗口切分、分级信号、用量账本。

本模块只用标准库，是 hx-05 的可运行示例。三件事分开：

1. `HeuristicCounter` 是一个可替换的计数器接口：没有分词器时按字符类别估算，
   有 provider 返回的 usage 时用真实值回填校准。
2. `Budget` 把上下文窗口切成「可放多少输入」，并对当前请求给出分级信号。
3. `UsageLedger` 累计每次响应的 input/output/cache 读写 token，按轮次汇总并可算成本。

教学示意：`HeuristicCounter` 里的三个系数（ASCII 4 字符 / token、CJK 1 字 / token、
其他 2 字符 / token）是为了让示例能跑而编的近似值，不是任何厂商的分词器。
真实 harness 用 provider 返回的 usage 覆盖它，见 `UsageLedger.record_usage`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# ---------------------------------------------------------------- 计数器

# 教学示意：下面三个系数都不是分词器，只是让「无分词器时也能估个大概」。
ASCII_CHARS_PER_TOKEN = 4.0  # 英文与代码大致 4 字符 1 token
CJK_CHARS_PER_TOKEN = 1.0    # 中日韩文字大致 1 字 1 token
OTHER_CHARS_PER_TOKEN = 2.0  # 其余（重音字母、emoji 等）取中间值

# 结构开销：一条消息的 role 框、一个块的类型标签，都要花 token。
ROLE_OVERHEAD = 4
BLOCK_OVERHEAD = 4


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF      # 日文假名
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x4E00 <= code <= 0x9FFF   # 统一表意文字
        or 0xAC00 <= code <= 0xD7AF   # 韩文音节
        or 0xF900 <= code <= 0xFAFF   # 兼容表意文字
    )


class Counter(Protocol):
    """计数器接口。换成分词器实现即可，预算逻辑不认识具体算法。"""

    def count(self, text: str) -> int:
        ...


@dataclass
class HeuristicCounter:
    """按字符类别估算的计数器：没有分词器时的兜底。

    单调不减：count("") == 0，且 a 是 b 的前缀时 count(a) <= count(b)。
    估算必然有误差，误差用 provider 的 usage 修正，不靠调系数。
    """

    ascii_chars_per_token: float = ASCII_CHARS_PER_TOKEN
    cjk_chars_per_token: float = CJK_CHARS_PER_TOKEN
    other_chars_per_token: float = OTHER_CHARS_PER_TOKEN

    def count(self, text: str) -> int:
        if not text:
            return 0
        ascii_n = cjk_n = other_n = 0
        for ch in text:
            if ord(ch) < 128:
                ascii_n += 1
            elif _is_cjk(ch):
                cjk_n += 1
            else:
                other_n += 1
        tokens = (
            ascii_n / self.ascii_chars_per_token
            + cjk_n / self.cjk_chars_per_token
            + other_n / self.other_chars_per_token
        )
        return int(tokens) if tokens == int(tokens) else int(tokens) + 1


def count_messages(counter: Counter, messages: list[dict[str, Any]]) -> int:
    """估算一组消息的 token：内容 + 每条消息的 role 开销 + 每个块的结构开销。"""
    total = 0
    for message in messages:
        total += ROLE_OVERHEAD
        content = message.get("content", "")
        if isinstance(content, str):
            total += counter.count(content)
            continue
        for block in content:
            total += BLOCK_OVERHEAD
            if block.get("type") in ("text", "reasoning"):
                total += counter.count(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                total += counter.count(str(block.get("name", "")))
                total += counter.count(str(block.get("input", "")))
            elif block.get("type") == "tool_result":
                total += counter.count(str(block.get("content", "")))
            else:
                # 未知块按 JSON 结构保守计价，宁可估高不可估低
                total += counter.count(str(block))
    return total


def count_tools(counter: Counter, tool_schemas: list[dict[str, Any]]) -> int:
    """工具 schema 也占上下文：每个请求都会把整份定义发过去。"""
    if not tool_schemas:
        return 0
    return counter.count(str(tool_schemas)) + BLOCK_OVERHEAD


# ---------------------------------------------------------------- 预算与分级

Level = Literal["ok", "near_limit", "must_compact", "overflow"]


@dataclass
class BudgetPlan:
    """一次请求的窗口切分结果。所有数字都是 token。"""

    context_window: int
    output_reserve: int          # 输出预留（max_tokens）
    safety_margin: int           # 安全余量
    system_prompt: int
    tool_schemas: int
    history: int
    message_budget: int          # 窗口减去输出预留
    pressure_budget: int         # 再减去安全余量：历史允许触顶的位置
    used: int                    # 系统提示 + 工具 schema + 历史
    available_input: int         # 本次请求还能放多少输入
    level: Level
    ratio: float                 # used / pressure_budget

    @property
    def should_compact(self) -> bool:
        return self.level in ("must_compact", "overflow")


@dataclass
class Budget:
    """窗口切分与分级信号。阈值全部是参数，不写死。

    context_window 由部署按路由的模型配（DSH 里它挂在适配器上，
    经 ctx.llm.resolveModelInfo().context 读出来）。
    """

    context_window: int
    output_reserve: int = 8_192      # 教学示意：常见默认值量级
    safety_margin: int = 8_192       # 教学示意：给估算误差和结构开销留的余量
    near_limit_ratio: float = 0.8    # 教学示意：到这里先说一声
    hard_ratio: float = 0.95         # 教学示意：到这里必须先压缩

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window 必须是正整数")
        if self.output_reserve < 0 or self.safety_margin < 0:
            raise ValueError("output_reserve 与 safety_margin 不能为负")
        if not 0.0 < self.near_limit_ratio < self.hard_ratio <= 1.0:
            raise ValueError("要求 0 < near_limit_ratio < hard_ratio <= 1")
        if self.pressure_budget <= 0:
            raise ValueError(
                f"窗口 {self.context_window} 减去输出预留 {self.output_reserve} "
                f"和安全余量 {self.safety_margin} 后没有可用预算"
            )

    @property
    def message_budget(self) -> int:
        return self.context_window - self.output_reserve

    @property
    def pressure_budget(self) -> int:
        return self.message_budget - self.safety_margin

    def level_for(self, used: int) -> Level:
        """分级：正常 / 接近上限 / 必须压缩 / 已经放不下。

        四个档位对应四种决策：继续、先发一次提示、压缩或拒绝、直接拒绝。
        """
        if used > self.pressure_budget:
            return "overflow"
        if used >= self.pressure_budget * self.hard_ratio:
            return "must_compact"
        if used >= self.pressure_budget * self.near_limit_ratio:
            return "near_limit"
        return "ok"

    def plan(
        self,
        counter: Counter,
        system_prompt: str = "",
        tool_schemas: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> BudgetPlan:
        """把窗口切成几块，并给出这次请求的分级。"""
        system_tokens = counter.count(system_prompt) if system_prompt else 0
        tools_tokens = count_tools(counter, tool_schemas or [])
        history_tokens = count_messages(counter, messages or [])
        used = system_tokens + tools_tokens + history_tokens
        budget = self.pressure_budget
        return BudgetPlan(
            context_window=self.context_window,
            output_reserve=self.output_reserve,
            safety_margin=self.safety_margin,
            system_prompt=system_tokens,
            tool_schemas=tools_tokens,
            history=history_tokens,
            message_budget=self.message_budget,
            pressure_budget=budget,
            used=used,
            available_input=budget - used,
            level=self.level_for(used),
            ratio=used / budget,
        )


# ---------------------------------------------------------------- 用量账本

# 两个厂商的 usage 字段名。Anthropic 用 input_tokens / output_tokens /
# cache_creation_input_tokens / cache_read_input_tokens；OpenAI 兼容用
# prompt_tokens / completion_tokens。
ANTHROPIC_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}
OPENAI_FIELDS = {
    "input": "prompt_tokens",
    "output": "completion_tokens",
    "cache_read": "prompt_tokens_details.cached_tokens",
    "cache_write": None,  # OpenAI 兼容端点不单独报「写缓存」
}


def normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    """把厂商 usage 归一化成四个桶：input / output / cache_read / cache_write。

    Anthropic 的三个输入桶互不重叠（input_tokens 只是未命中缓存的部分），
    所以原样搬运即可。OpenAI 的 prompt_tokens 已经把命中缓存的量算在里面，
    要减掉，否则成本会被重复计入。
    """
    if not raw:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    # 形状判定按「有没有 Anthropic 风格的 input_tokens」，不按有没有缓存字段：
    # 一次未命中任何缓存的响应里，两个 cache 字段都不会出现。
    if "input_tokens" in raw or "output_tokens" in raw:
        return {
            "input": int(raw.get("input_tokens", 0) or 0),
            "output": int(raw.get("output_tokens", 0) or 0),
            "cache_read": int(raw.get("cache_read_input_tokens", 0) or 0),
            "cache_write": int(raw.get("cache_creation_input_tokens", 0) or 0),
        }

    prompt = int(raw.get("prompt_tokens", 0) or 0)
    details = raw.get("prompt_tokens_details") or {}
    cache_read = int(details.get("cached_tokens", 0) or 0)
    return {
        "input": max(0, prompt - cache_read),
        "output": int(raw.get("completion_tokens", 0) or 0),
        "cache_read": cache_read,
        "cache_write": 0,
    }


@dataclass
class Prices:
    """单价（每百万 token）。价格必须由调用方传入，本模块不写死任何价格。

    缓存读通常比未命中输入便宜一个数量级，缓存写比未命中输入贵——这是
    缓存能不能省钱的判断依据，所以四个桶必须分开算。
    """

    input: float
    output: float
    cache_read: float
    cache_write: float

    def cost(self, buckets: dict[str, int]) -> float:
        return (
            buckets.get("input", 0) * self.input
            + buckets.get("output", 0) * self.output
            + buckets.get("cache_read", 0) * self.cache_read
            + buckets.get("cache_write", 0) * self.cache_write
        ) / 1_000_000


def _add(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    totals = dict(left)
    for key, value in right.items():
        totals[key] = totals.get(key, 0) + value
    return totals


@dataclass
class UsageLedger:
    """累计每次响应的用量，按轮次汇总。

    `record_usage` 只吃 provider 实际返回的 usage，不吃估算值——估算用来做预算
    决策，账本用来对账，两者不能混。
    """

    prices: Prices | None = None
    turn: int = 1
    total: dict[str, int] = field(default_factory=lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
    })
    per_turn: dict[int, dict[str, int]] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def record_usage(self, raw_usage: dict[str, Any], calls: int = 1) -> dict[str, int]:
        """记一次响应。`calls` 用于一次响应里含多次计费调用的情况（例如重试）。"""
        buckets = normalize_usage(raw_usage)
        if calls > 1:
            buckets = {key: value * calls for key, value in buckets.items()}
        self.total = _add(self.total, buckets)
        self.per_turn[self.turn] = _add(self.per_turn.get(self.turn, {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        }), buckets)
        self.attempts.append({"turn": self.turn, "calls": calls, **buckets})
        return buckets

    def new_turn(self) -> int:
        """进入下一轮，返回新的轮号。"""
        self.turn += 1
        return self.turn

    def tokens_at_turn(self, turn: int) -> dict[str, int]:
        return dict(self.per_turn.get(turn, {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        }))

    def cost(self, scope: int | Literal["total"] = "total") -> float:
        """按桶分别计价。scope 传轮号只算那一轮。"""
        if self.prices is None:
            raise ValueError("没有单价：构造 UsageLedger 时传入 Prices 才能算成本")
        buckets = self.total if scope == "total" else self.tokens_at_turn(int(scope))
        return self.prices.cost(buckets)
