"""超时：给每一层等待都设一个上限，并且让超时变成一个能说清楚的结局。

对照正文 hx-19。harness 里到处都在等：等模型第一个 token、等流式响应的下一块、
等工具跑完、等用户批准、等整轮结束。任何一处等不到头，整个 agent 就卡住了。

这一层的四个要点：

1. **分层的预算**。整轮有总预算，一次模型调用有自己的预算，一个工具有自己的预算；
   内层预算永远不能超过外层还剩下的时间（deadline 往下传，而不是每层各算各的）。
2. **两种超时要区分**。「总时长超时」（这件事总共不能超过 N 秒）和「空闲超时」
   （流式响应 N 秒没有新数据）。一个正在稳定输出的长回复不应该被总时长杀掉，
   一个连上之后就不说话的连接应该被空闲超时尽快发现。
3. **超时是协作的**。Python 线程没法被强行终止；超时只能让等待的一方放弃，
   被等待的那件事要自己检查取消信号。做不到这一点的工作（子进程除外）会在后台继续跑。
4. **超时要可归因**。是哪一层的哪个预算触发的，要能说清楚：
   「工具 run_tests 超过自己的 120s 预算」和「整轮 10 分钟预算用完」对模型的意义完全不同。

只用标准库。时钟可以注入。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, TypeVar

T = TypeVar("T")

Clock = Callable[[], float]


class Timeout(Exception):
    """某一层的预算用完了。scope 说明是哪一层，budget 是那一层的预算。"""

    def __init__(self, scope: str, budget: float, elapsed: float, kind: str = "total") -> None:
        self.scope = scope
        self.budget = budget
        self.elapsed = elapsed
        self.kind = kind          # total：总时长；idle：两次进展之间的空闲
        what = "空闲" if kind == "idle" else "总时长"
        super().__init__(f"{scope} 超时：{what}预算 {budget:.1f}s，已过 {elapsed:.1f}s")


@dataclass
class Deadline:
    """一个截止时刻。可以派生出更短的子截止时刻，但永远不会比父级更晚。

    deadline 往下传，而不是每层各自计时：外层还剩 5 秒时，内层声明的 60 秒预算
    实际上只能用 5 秒。各层各算各的，就会出现「整轮已经超时，工具还在跑」。
    """

    scope: str
    budget: float
    clock: Clock = time.monotonic
    started: float = field(default=-1.0)
    parent: "Deadline | None" = None

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError(f"{self.scope} 的预算必须为正数")
        if self.started < 0:
            self.started = self.clock()

    @property
    def expires_at(self) -> float:
        own = self.started + self.budget
        return min(own, self.parent.expires_at) if self.parent else own

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock())

    def elapsed(self) -> float:
        return self.clock() - self.started

    def expired(self) -> bool:
        return self.remaining() <= 0

    def binding_scope(self) -> "Deadline":
        """真正卡住的是哪一层：自己的预算，还是某个更早到期的祖先。"""
        node, best = self, self
        while node is not None:
            if node.started + node.budget < best.started + best.budget:
                best = node
            node = node.parent
        return best

    def check(self) -> None:
        """到期就抛 Timeout，归因到真正到期的那一层。"""
        if self.expired():
            owner = self.binding_scope()
            raise Timeout(owner.scope, owner.budget, owner.elapsed())

    def child(self, scope: str, budget: float) -> "Deadline":
        """派生子预算。子预算按自己声明的值计时，但到期时刻不晚于父级。"""
        return Deadline(scope, budget, self.clock, parent=self)


def run_with_timeout(fn: Callable[[threading.Event], T], deadline: Deadline) -> T:
    """在后台线程里跑 fn，最多等到 deadline。

    fn 会收到一个 Event：超时后它被 set，fn 应该定期检查并尽快返回。
    **这是协作式超时**：如果 fn 不检查 Event，它会在后台一直跑完，
    只是调用方不再等它。子进程不在此列——子进程可以被真的杀掉（见 hx-15 的沙箱）。
    """
    cancel = threading.Event()
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 — 原样转交给调用方
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(deadline.remaining())
    if worker.is_alive():
        cancel.set()
        owner = deadline.binding_scope()
        raise Timeout(owner.scope, owner.budget, owner.elapsed())
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]


def idle_guard(chunks: Iterable[T], idle: float, *, total: Deadline | None = None,
               clock: Clock = time.monotonic, scope: str = "stream") -> Iterator[T]:
    """包装一个流：两块之间间隔超过 idle 秒，或总时长到期，就抛 Timeout。

    教学版在每一块**到达后**检查间隔，所以只能发现「已经恢复的停顿」。
    生产实现要用一个独立的看门狗计时器，在停顿进行中就打断读取（DSH 的
    流式适配器就是这样做的：空闲超时映射为可重试的 TIMEOUT）。
    """
    last = clock()
    for chunk in chunks:
        now = clock()
        if now - last > idle:
            raise Timeout(scope, idle, now - last, kind="idle")
        if total is not None:
            total.check()
        last = now
        yield chunk


@dataclass(frozen=True)
class TimeoutPlan:
    """一份分层超时配置。数值是教学示意，不是推荐值。"""

    turn: float = 600.0             # 一轮（处理一条用户输入）的总预算
    model_total: float = 300.0      # 一次模型调用的总预算
    model_idle: float = 60.0        # 流式响应两块之间的最长空闲
    tool_default: float = 60.0      # 没有声明预算的工具
    tool_overrides: tuple[tuple[str, float], ...] = (("run_tests", 600.0), ("read_file", 10.0))
    approval: float | None = None   # 等人审批：None 表示不设上限（人可以去吃饭）

    def tool_budget(self, name: str) -> float:
        return dict(self.tool_overrides).get(name, self.tool_default)


def describe_timeout(exc: Timeout) -> str:
    """给模型的一句话。区分「这个工具太慢」和「整体时间用完」，后续动作完全不同。"""
    if exc.kind == "idle":
        return (f"Error: {exc.scope} 在 {exc.elapsed:.0f}s 内没有新的输出（空闲上限 {exc.budget:.0f}s），"
                "连接可能已经卡住；这类错误可以重试。")
    if exc.scope.startswith("tool:"):
        name = exc.scope.split(":", 1)[1]
        return (f"Error: 工具 {name} 超过了它的 {exc.budget:.0f}s 预算，已放弃等待。"
                "换一个范围更小的调用（比如只跑一个测试文件），不要原样重试。")
    return (f"Error: {exc.scope} 的总时间预算 {exc.budget:.0f}s 已用完。"
            "请停下来，总结已经完成的部分和剩下的工作。")
