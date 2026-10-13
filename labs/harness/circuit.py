"""熔断与降级：一个依赖持续失败时，停止反复撞它，并给出一个能用的退路。

对照正文 hx-20。重试（hx-18）处理的是「偶尔失败」；当一个依赖持续失败时，
重试会把问题放大：每个请求都要等完整个退避序列才失败，用户等得更久，
而大量重试又把本来就过载的服务压得更死。

熔断器是一个三态的开关：

- closed（闭合）：正常放行，记录失败。失败率或连续失败数超过阈值 → open。
- open（断开）：直接拒绝，不调用依赖，立刻返回。冷却时间到 → half_open。
- half_open（半开）：放行少量试探请求。成功 → closed；失败 → 重新 open。

降级是熔断打开（或者重试耗尽）之后走的退路：换一个模型、用缓存、返回一个
「功能暂时不可用」的明确结果，或者把任务停在一个可恢复的状态。

注意：参考的几个仓库都没有真正的三态熔断器。OpenHarness 的「连续失败 3 次
就不再自动压缩」只有 closed → open 一个方向，没有半开和恢复；DSH 用重试预算
和「有进展才继续」来防止空转。本模块是按通用做法实现的教学版。

只用标准库。时钟可以注入。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Generic, Sequence, TypeVar

T = TypeVar("T")
Clock = Callable[[], float]

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """熔断器处于打开状态，这次调用没有发出去。retry_in 是距离半开还有多少秒。"""

    def __init__(self, name: str, retry_in: float) -> None:
        self.name = name
        self.retry_in = retry_in
        super().__init__(f"{name} 已熔断，{retry_in:.0f}s 后试探恢复")


@dataclass
class BreakerConfig:
    """熔断器参数。数值是教学示意。

    同时用「连续失败数」和「窗口内失败率」两个判据：
    连续失败数反应快（依赖彻底挂掉时几次就断开），
    失败率能抓住「时好时坏、但大部分在失败」的情况（连续计数会被偶尔的成功清零）。
    """

    failure_threshold: int = 5          # 连续失败多少次就断开
    window: int = 20                    # 失败率统计的窗口（最近 N 次调用）
    failure_rate: float = 0.5           # 窗口内失败率超过多少就断开
    min_calls: int = 10                 # 窗口里至少有这么多调用才计算失败率，避免冷启动误判
    cooldown: float = 30.0              # 断开后多久进入半开
    half_open_probes: int = 1           # 半开时允许同时试探几个请求
    counts_as_failure: Callable[[BaseException], bool] = field(default=lambda exc: True)


@dataclass
class Transition:
    """一次状态变化。真实实现里它应该进日志和监控：熔断打开是需要人知道的事。"""

    at: float
    old: str
    new: str
    reason: str


class CircuitBreaker:
    """一个依赖一个熔断器。按依赖分，而不是全局一个：模型 A 挂了不该连带模型 B。"""

    def __init__(self, name: str, config: BreakerConfig | None = None, clock: Clock = time.monotonic) -> None:
        self.name = name
        self.config = config or BreakerConfig()
        self.clock = clock
        self.state = CLOSED
        self.consecutive_failures = 0
        self.recent: deque[bool] = deque(maxlen=self.config.window)   # True 表示失败
        self.opened_at: float | None = None
        self.in_flight_probes = 0
        self.transitions: list[Transition] = []

    # ---- 状态 ----

    def _move(self, new: str, reason: str) -> None:
        if new != self.state:
            self.transitions.append(Transition(self.clock(), self.state, new, reason))
            self.state = new
        if new == OPEN:
            self.opened_at = self.clock()
            self.in_flight_probes = 0
        if new == CLOSED:
            self.consecutive_failures = 0
            self.recent.clear()
            self.opened_at = None

    def current_state(self) -> str:
        """读状态时顺便处理「冷却时间已到」：open 到期就变 half_open。"""
        if self.state == OPEN and self.opened_at is not None:
            if self.clock() - self.opened_at >= self.config.cooldown:
                self._move(HALF_OPEN, f"冷却 {self.config.cooldown:.0f}s 已到，开始试探")
        return self.state

    def failure_rate(self) -> float:
        return sum(self.recent) / len(self.recent) if self.recent else 0.0

    # ---- 记录 ----

    def record_success(self) -> None:
        if self.state == HALF_OPEN:
            self._move(CLOSED, "试探请求成功")
            return
        self.consecutive_failures = 0
        self.recent.append(False)

    def record_failure(self, reason: str = "") -> None:
        if self.state == HALF_OPEN:
            self._move(OPEN, f"试探请求失败：{reason}" if reason else "试探请求失败")
            return
        self.consecutive_failures += 1
        self.recent.append(True)
        cfg = self.config
        if self.consecutive_failures >= cfg.failure_threshold:
            self._move(OPEN, f"连续失败 {self.consecutive_failures} 次")
        elif len(self.recent) >= cfg.min_calls and self.failure_rate() >= cfg.failure_rate:
            self._move(OPEN, f"最近 {len(self.recent)} 次失败率 {self.failure_rate():.0%}")

    # ---- 调用 ----

    def call(self, fn: Callable[[], T]) -> T:
        """通过熔断器调用 fn。打开时直接抛 CircuitOpen，不调用 fn。"""
        state = self.current_state()
        if state == OPEN:
            assert self.opened_at is not None
            raise CircuitOpen(self.name, self.config.cooldown - (self.clock() - self.opened_at))
        if state == HALF_OPEN:
            if self.in_flight_probes >= self.config.half_open_probes:
                raise CircuitOpen(self.name, 0.0)
            self.in_flight_probes += 1
        try:
            value = fn()
        except BaseException as exc:
            if state == HALF_OPEN:
                self.in_flight_probes -= 1
            if self.config.counts_as_failure(exc):
                self.record_failure(f"{type(exc).__name__}: {exc}")
            else:
                # 不计入失败的异常（例如参数错误）说明依赖本身是好的
                self.record_success()
            raise
        if state == HALF_OPEN:
            self.in_flight_probes -= 1
        self.record_success()
        return value


@dataclass
class Route:
    """降级链上的一个选项。"""

    name: str
    fn: Callable[[], object]
    note: str = ""               # 走到这一级时要告诉用户 / 模型什么


@dataclass
class FallbackResult:
    ok: bool
    value: object = None
    route: str = ""
    degraded: bool = False       # 是否走了非首选路线
    note: str = ""
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (路线, 原因)


def with_fallback(routes: Sequence[Route], breakers: dict[str, CircuitBreaker] | None = None) -> FallbackResult:
    """按顺序尝试降级链。熔断打开的路线直接跳过，不浪费一次调用。

    降级必须**说出来**：结果里带 degraded 和 note，调用方要把它告诉用户。
    悄悄换成一个更弱的模型，用户只会觉得「今天它变笨了」。
    """
    breakers = breakers or {}
    skipped: list[tuple[str, str]] = []
    for index, route in enumerate(routes):
        breaker = breakers.get(route.name)
        try:
            value = breaker.call(route.fn) if breaker else route.fn()
        except CircuitOpen as exc:
            skipped.append((route.name, f"熔断中（{exc.retry_in:.0f}s 后试探）"))
            continue
        except Exception as exc:  # noqa: BLE001
            skipped.append((route.name, f"{type(exc).__name__}: {exc}"))
            continue
        return FallbackResult(True, value, route.name, degraded=index > 0, note=route.note, skipped=skipped)
    return FallbackResult(False, route="", skipped=skipped,
                          note="所有路线都不可用：" + "；".join(f"{n}（{why}）" for n, why in skipped))


def describe_open(exc: CircuitOpen) -> str:
    """熔断打开时给模型的一句话：不要让它在这一轮里反复重试同一个依赖。"""
    return (f"Error: {exc.name} 暂时不可用（已熔断，约 {exc.retry_in:.0f}s 后自动试探恢复）。"
            "不要在这一轮里重复调用它；如果任务离不开它，请停下来说明情况。")
