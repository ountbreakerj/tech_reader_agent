"""重试与退避：失败之后，什么时候再试、隔多久、试几次、什么时候放弃。

对照正文 hx-18。重试是 harness 里最容易「看起来做了、其实做错」的机制：
写一个 for 循环加 sleep 只要三行，但下面这些问题每一个都会在线上出事：

1. **哪些错误能重试**。限流、服务端 5xx、超时、连接断开可以；鉴权失败、参数错误、
   上下文超长不行——它们重试一万次结果也一样，只会烧钱、拖时间。
2. **隔多久**。固定间隔会让所有客户端在同一时刻一起重试，把刚恢复的服务再压垮；
   要用指数退避加随机抖动，并且优先听服务端的 Retry-After。
3. **试几次、总共等多久**。次数上限和总时长上限都要有；上限是按「这一步」算，
   不是按整个会话累计。
4. **重试前后历史怎么办**。失败那次的半截输出、错误文本不能进下一次请求的上下文。
5. **重试本身要留痕**。先记录「要重试了、等多久」，再等待——进程在等待期间被杀，
   恢复后能知道刚才在重试，而不是从头再来或者丢掉计数。

只用标准库。时钟和随机数都可以注入，测试不真的睡觉。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

# 错误分类：可重试的是「再来一次可能就好了」的那一类。
RATE_LIMIT = "rate_limit"        # 429、厂商的 overloaded
SERVER = "server"                # 5xx
TIMEOUT = "timeout"              # 请求超时、流式响应停滞
TRANSPORT = "transport"          # 连接被重置、DNS 失败
EMPTY = "empty_response"         # 返回成功但内容为空
AUTH = "auth"                    # 401 / 403、密钥缺失
INVALID = "invalid_request"      # 400、参数错误
TOO_LONG = "context_too_long"    # 上下文超长：该压缩，不该重试
CANCELLED = "cancelled"          # 用户取消
UNKNOWN = "unknown"

DEFAULT_RETRYABLE = frozenset({RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT, EMPTY})


class Failure(Exception):
    """一次失败的结构化事实。分类在这里做好，策略只看 kind。

    retry_after: 服务端给出的等待秒数（Retry-After 头）；None 表示没给。
    """

    def __init__(self, kind: str, message: str = "", *, retry_after: float | None = None,
                 status: int | None = None) -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.retry_after = retry_after
        self.status = status


def classify_status(status: int | None, body: str = "") -> str:
    """把 HTTP 状态码和错误正文归到一个 kind。

    按状态码分类，不按错误文本里有没有 "timeout" 这个词：文本匹配会把
    「请求参数 timeout 不合法」这种 400 错误误判成可重试的超时。
    """
    lowered = body.lower()
    if status == 429 or status == 529 or "overloaded" in lowered:
        return RATE_LIMIT
    if status in (401, 403):
        return AUTH
    if status == 413 or "prompt is too long" in lowered or "context length" in lowered:
        return TOO_LONG
    if status == 408 or status == 504:
        return TIMEOUT
    if status is not None and 500 <= status < 600:
        return SERVER
    if status is not None and 400 <= status < 500:
        return INVALID
    return UNKNOWN


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After 可以是秒数，也可以是 HTTP 日期。这里只处理秒数，日期返回 None 交给本地退避。"""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


@dataclass(frozen=True)
class RetryPolicy:
    """一条重试策略。默认值是教学示意，DSH 的默认是 5 次 / 0.5 秒 / 10 秒 / 抖动 0.1。"""

    max_retries: int = 5                 # 首次请求之外最多再试几次
    initial_delay: float = 0.5           # 秒
    max_delay: float = 10.0              # 单次等待上限
    jitter: float = 0.1                  # 对称抖动比例：等待时间在 ±10% 之间浮动
    max_total: float | None = 60.0       # 这一步里所有等待加起来的上限；None 表示不限
    retryable: frozenset[str] = DEFAULT_RETRYABLE
    honor_retry_after: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries 不能为负")
        if self.initial_delay <= 0 or self.max_delay < self.initial_delay:
            raise ValueError("需要 0 < initial_delay <= max_delay")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter 必须在 [0, 1) 之间")


def backoff_delay(policy: RetryPolicy, retry: int, rand: Callable[[], float] = random.random) -> float:
    """第 retry 次重试（从 0 开始）之前要等多久。

    指数增长、先夹上限、再加对称抖动、最后再夹一次上限：
    抖动加在夹上限之后，否则到了上限附近所有客户端又会对齐到同一个值。
    """
    base = min(policy.max_delay, policy.initial_delay * (2 ** retry))
    factor = 1 + policy.jitter * (2 * rand() - 1)
    return min(policy.max_delay, base * factor)


@dataclass
class Attempt:
    """一次尝试的记录。真实实现里它应该先写进会话日志，再开始等待。"""

    number: int                  # 第几次请求（从 1 开始）
    kind: str | None             # 失败类型；成功为 None
    delay: float = 0.0           # 这次失败之后安排的等待
    source: str = ""             # "retry-after" 或 "backoff"


@dataclass
class RetryOutcome:
    """retry_call 的结果。不抛异常：调用方总能拿到发生过什么。"""

    ok: bool
    value: object = None
    failure: Failure | None = None
    attempts: list[Attempt] = field(default_factory=list)
    gave_up_because: str = ""

    @property
    def total_wait(self) -> float:
        return sum(a.delay for a in self.attempts)


def plan_wait(policy: RetryPolicy, failure: Failure, retry: int, waited: float,
              rand: Callable[[], float] = random.random) -> tuple[float | None, str, str]:
    """决定下一次等多久。返回 (等待秒数, 来源, 放弃原因)；等待为 None 表示放弃。"""
    if failure.kind not in policy.retryable:
        return None, "", f"{failure.kind} 不可重试"
    if retry >= policy.max_retries:
        return None, "", f"已重试 {retry} 次，达到上限 {policy.max_retries}"
    if policy.honor_retry_after and failure.retry_after is not None:
        if failure.retry_after > policy.max_delay:
            # 服务端要求的等待超过了我们愿意等的上限：不要偷偷缩短它（那等于无视限流），
            # 也不要无条件照等（可能是几小时）。放弃，让上层决定。
            return None, "", f"服务端要求等待 {failure.retry_after:.0f}s，超过单次上限 {policy.max_delay:.0f}s"
        delay, source = failure.retry_after, "retry-after"
    else:
        delay, source = backoff_delay(policy, retry, rand), "backoff"
    if policy.max_total is not None and waited + delay > policy.max_total:
        return None, "", f"累计等待将达到 {waited + delay:.1f}s，超过总上限 {policy.max_total:.0f}s"
    return delay, source, ""


def retry_call(
    fn: Callable[[], T],
    policy: RetryPolicy | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    should_stop: Callable[[], bool] = lambda: False,
    on_attempt: Callable[[Attempt], None] | None = None,
) -> RetryOutcome:
    """调用 fn，按策略重试。fn 失败时必须抛 Failure；其他异常视为 UNKNOWN、不重试。

    on_attempt 在等待**之前**调用：这是「先写日志再等待」的挂点。
    """
    policy = policy or RetryPolicy()
    attempts: list[Attempt] = []
    waited = 0.0
    retry = 0
    while True:
        if should_stop():
            return RetryOutcome(False, failure=Failure(CANCELLED), attempts=attempts,
                                gave_up_because="调用方取消")
        number = len(attempts) + 1
        try:
            value = fn()
        except Failure as failure:
            delay, source, why = plan_wait(policy, failure, retry, waited, rand)
            attempt = Attempt(number, failure.kind, delay or 0.0, source)
            attempts.append(attempt)
            if delay is None:
                return RetryOutcome(False, failure=failure, attempts=attempts, gave_up_because=why)
            if on_attempt is not None:
                on_attempt(attempt)
            sleep(delay)
            waited += delay
            retry += 1
            continue
        except Exception as exc:  # noqa: BLE001 — 没分类的异常一律不重试
            failure = Failure(UNKNOWN, f"{type(exc).__name__}: {exc}")
            attempts.append(Attempt(number, UNKNOWN))
            return RetryOutcome(False, failure=failure, attempts=attempts,
                                gave_up_because="未分类的异常不重试")
        attempts.append(Attempt(number, None))
        return RetryOutcome(True, value=value, attempts=attempts)


def describe_for_model(outcome: RetryOutcome) -> str:
    """重试最终失败时给模型（或用户）看的一句话。不要把每次失败的原始错误都塞进去。"""
    if outcome.ok:
        return ""
    tried = len(outcome.attempts)
    kind = outcome.failure.kind if outcome.failure else UNKNOWN
    return (f"Error: 模型请求失败（{kind}），共尝试 {tried} 次，等待 {outcome.total_wait:.1f}s；"
            f"停止原因：{outcome.gave_up_because}")


def herd_spread(delays: Iterable[float]) -> float:
    """一组客户端的等待时间里，最晚和最早相差多少秒。用来演示抖动如何打散重试洪峰。"""
    values = list(delays)
    return max(values) - min(values) if values else 0.0
