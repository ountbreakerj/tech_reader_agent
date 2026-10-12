"""教学用工具执行管线：把一步里的多个 tool_use 切成「并行段」和「独占段」。

只用标准库，Python 3.10+。对应 harness.loop 里 execute_tools() 的升级版：那里是
顺序跑的，这里把只读、并行安全的调用放进有界滚动池，写操作单独成批，并且始终
**按模型给出的原顺序**提交结果。

两条不变量（测试逐条钉住）：

1. 提交顺序 == 输入顺序。结果列表的第 i 项永远属于 calls[i]，与完成先后无关。
   模型看到的消息顺序因此与它的请求顺序一致。
2. 每个 tool_use 都有对应的结果。取消、超时、异常、未知工具都会变成一条
   is_error 结果，而不是让调用凭空消失。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Sequence

# 教学示意：DSH 的真实默认值是 10（packages/core/agent-loop/src/constants.ts:6）。
DEFAULT_MAX_PARALLEL = 10
UNKNOWN_TOOL_RESULT = "Error: unknown tool {name!r}"
CANCELLED_RESULT = "Error: tool call skipped because the run was cancelled before dispatch"


@dataclass
class ToolSpec:
    """一个工具被调度时需要知道的全部信息。

    is_parallel 对应 DSH 的 isConcurrencySafe(args)：只读当前调用的参数，
    必须同步且纯。返回真才表示「可以和任何同样返回真的兄弟调用重叠」。
    """

    fn: Callable[[dict], str]
    is_parallel: bool = False        # 默认独占：没声明就是屏障
    timeout: float | None = None     # 单个工具体自己的预算，优先于整批预算


@dataclass
class ToolCall:
    """一条待执行的工具调用（call_id 用于和 tool_result 配对）。"""

    call_id: str
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class CallResult:
    """一条工具结果。它在有序结果列表里的位置就是它被提交的位置。"""

    call_id: str
    name: str
    content: str
    is_error: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    cancelled: bool = False       # 派发前就被取消，工具体从未运行
    timed_out: bool = False       # 工具体跑了但超出自己的预算


class ToolCallTimeout(Exception):
    """工具体超出自己的预算仍没返回。

    这是本模块自己抛的、可归因的超时，所以 timed_out 只认它——工具体自己抛的
    TimeoutError 是工具的实现细节，应该原样归因为一次普通失败。
    """


def error_result(call: ToolCall, exc: BaseException) -> CallResult:
    return CallResult(
        call_id=call.call_id,
        name=call.name,
        content=f"Error: {type(exc).__name__}: {exc}",
        is_error=True,
    )


def cancelled_result(call: ToolCall) -> CallResult:
    return CallResult(
        call_id=call.call_id,
        name=call.name,
        content=CANCELLED_RESULT,
        is_error=True,
        cancelled=True,
    )


def plan_batches(
    calls: Sequence[ToolCall],
    specs: dict[str, ToolSpec],
    max_parallel: int = DEFAULT_MAX_PARALLEL,
) -> list[list[str]]:
    """把一批调用切成按顺序执行的批次。纯函数，不做任何 I/O。

    规则（与 DSH tool-calls.ts 的 runGroup 分组一致）：
    - 连续的可并行调用合成一批，批内同时最多 max_parallel 个在跑；
    - 每个独占调用单独成一批，构成屏障：它开始前，前面的批必须全部提交完；
    - 未声明的工具按 fail-closed 归为独占（它仍会拿到一条 unknown tool 错误结果）。
    """
    batches: list[list[str]] = []
    current: list[str] = []
    for call in calls:
        spec = specs.get(call.name)
        parallel = spec.is_parallel if spec is not None else False
        if parallel:
            current.append(call.call_id)
            continue
        if current:
            batches.append(current)
            current = []
        batches.append([call.call_id])
    if current:
        batches.append(current)
    return batches


def execute_batch(
    call_ids: Sequence[str],
    calls: dict[str, ToolCall],
    specs: dict[str, ToolSpec],
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    timeout: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[CallResult]:
    """执行一批调用，按传入顺序返回结果（完成顺序不影响返回顺序）。

    并发上限就是线程池的 worker 数：同一时刻真在跑的工具体不超过 max_parallel，
    无论这一批有多少个调用（多出来的提交排队等额度）。每个工具自己的 timeout
    优先于本批的 timeout；超时、异常、未知工具都只是这一条调用变成 is_error，
    同批其他调用照常跑完、各自归因。
    """
    limit = max(1, min(int(max_parallel), len(call_ids)))
    is_cancelled = cancelled or (lambda: False)
    results: dict[str, CallResult] = {}

    def run(call: ToolCall) -> None:
        if is_cancelled():
            # 取消之后不再启动新的工具体；已经启动的会跑完，结果照样按序提交
            results[call.call_id] = cancelled_result(call)
            return
        spec = specs.get(call.name)
        # 计时从拿到 worker 额度、真正开始调工具体算起：排队不该算进工具自己的预算
        started = time.monotonic()
        try:
            if spec is None:
                raise LookupError(UNKNOWN_TOOL_RESULT.format(name=call.name))
            budget = timeout if spec.timeout is None else spec.timeout
            outcome = spec.fn(dict(call.args))
            if budget is not None and time.monotonic() - started > budget:
                raise ToolCallTimeout(f"tool {call.name!r} exceeded its {budget}s budget")
            results[call.call_id] = CallResult(
                call.call_id, call.name, str(outcome), False, started, time.monotonic(),
            )
        except Exception as exc:
            failed = error_result(call, exc)
            failed.started_at, failed.finished_at = started, time.monotonic()
            failed.timed_out = isinstance(exc, ToolCallTimeout)
            results[call.call_id] = failed

    with ThreadPoolExecutor(max_workers=limit) as pool:
        for call in (calls[cid] for cid in call_ids):
            pool.submit(run, call)
    # 结果按输入顺序取，不是按完成顺序
    return [results[cid] for cid in call_ids]


def run_pipeline(
    calls: Sequence[ToolCall],
    specs: dict[str, ToolSpec],
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    timeout: float | None = None,
    on_cancel: Callable[[], bool] | None = None,
) -> list[CallResult]:
    """按批次跑完一整步，返回与 calls 等长、且同序的结果列表。

    批次在开跑前一次规划好；发生取消后，当前批的结果照常提交，之后不再启动
    新批次，剩余未派发的调用各补一条「派发前已中止」的错误结果——这样模型的
    历史里不会留下没有 tool_result 的 tool_use。
    """
    planned = plan_batches(calls, specs, max_parallel)
    by_id = {call.call_id: call for call in calls}
    results: list[CallResult] = []

    for batch in planned:
        if on_cancel is not None and on_cancel():
            results.extend(cancelled_result(by_id[cid]) for cid in batch)
            continue
        results.extend(execute_batch(
            batch, by_id, specs,
            max_parallel=max_parallel, timeout=timeout, cancelled=on_cancel,
        ))
    return results


def summarize(results: Sequence[CallResult]) -> list[tuple[str, bool]]:
    """(call_id, is_error) 的有序摘要，正文和测试都用它来看提交顺序。"""
    return [(r.call_id, r.is_error) for r in results]
