"""教学用 agent loop：采样 → 判定 → 执行工具 → 回填结果，直到某个停止条件成立。

只用标准库，Python 3.10+。模型通过 harness.llm 的适配器调用（测试里用 FakeLLM），
消息历史是 Anthropic 风格的 dict 列表，由 harness.messages 构造。

设计不变量：run_agent 的每一个返回点上，messages 都能原样作为下一次请求的历史——
不会留下没有 tool_result 的 tool_use，也不会把半截工具调用写进去。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Literal

from harness.llm import LLM
from harness.messages import AssistantTurn, ToolCall, assistant_from_turn, tool_results, user

Tool = Callable[[dict], str]
StopReason = Literal[
    "completed",     # 模型不再要工具，正常结束
    "max_turns",     # 模型调用次数用完
    "max_tokens",    # 输出被截断，且续写预算已用完（或为 0）
    "refusal",       # 模型拒答
    "error_budget",  # 连续失败的工具调用超出预算
    "interrupted",   # 外部 should_stop() 要求停下
    "llm_error",     # 模型调用本身抛了异常
]
Step = Literal["run_tools", "continue", "completed", "max_tokens", "refusal"]

CONTINUE_PROMPT = (
    "Your previous reply was cut off by the output token limit. "
    "Continue from where you stopped, and re-issue any tool call in full."
)
INTERRUPTED_RESULT = "Error: tool call skipped because the run was interrupted before dispatch."


@dataclass
class LoopConfig:
    max_turns: int = 20               # 模型调用次数上限（默认值是教学示意）
    max_consecutive_errors: int = 3   # 连续失败的工具调用数上限（默认值是教学示意）
    max_continuations: int = 0        # 被 max_tokens 截断后最多自动续写几次；0 表示像 DSH 默认那样直接停


@dataclass
class LoopState:
    config: LoopConfig
    turns: int = 0
    continuations: int = 0
    consecutive_errors: int = 0
    truncated: bool = False           # 粘滞：只要截断过一次就保持 True，续写成功也不清除
    last_text: str = ""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class LoopResult:
    final_text: str
    stop: StopReason
    turns: int
    messages: list[dict]
    truncated: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def next_step(turn: AssistantTurn, state: LoopState) -> Step:
    """采样之后决定下一步。纯函数：只读 turn 和 state，不改任何东西。"""
    if turn.stop_reason == "refusal":
        return "refusal"  # 拒答可能截在工具参数中间，这一轮的工具一律不执行
    if turn.stop_reason == "max_tokens":
        # 先看截断再看工具调用：被截断的 tool_use 参数可能只有半截
        if state.continuations < state.config.max_continuations:
            return "continue"
        return "max_tokens"
    if not turn.tool_calls:
        return "completed"
    return "run_tools"


def run_tool(call: ToolCall, tools: dict[str, Tool]) -> tuple[str, str, bool]:
    """执行单个调用。未知工具和工具异常都变成 is_error 结果，交给模型自己纠正。"""
    fn = tools.get(call.name)
    if fn is None:
        return call.id, f"Error: unknown tool {call.name!r}", True
    try:
        return call.id, str(fn(call.args)), False
    except Exception as exc:  # 工具的 bug 不能打断循环；KeyboardInterrupt 之类仍然上抛
        return call.id, f"Error: {type(exc).__name__}: {exc}", True


def execute_tools(
    calls: list[ToolCall],
    tools: dict[str, Tool],
    should_stop: Callable[[], bool],
) -> tuple[list[tuple[str, str, bool]], bool]:
    """按模型给出的顺序执行一批调用。返回 (结果列表, 是否被中断)。"""
    results: list[tuple[str, str, bool]] = []
    interrupted = False
    for call in calls:
        if interrupted or should_stop():
            interrupted = True
            # 中断后没执行的调用也要配一个错误结果：每个 tool_use 都必须有对应的 tool_result
            results.append((call.id, INTERRUPTED_RESULT, True))
            continue
        results.append(run_tool(call, tools))
    return results, interrupted


def run_agent(
    llm: LLM,
    system: str,
    task: str,
    tools: dict[str, Tool],
    tool_schemas: list[dict],
    config: LoopConfig | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> LoopResult:
    """跑一个任务直到停止条件成立。"""
    state = LoopState(config or LoopConfig())
    stop_requested = should_stop or (lambda: False)
    messages: list[dict] = [user(task)]
    while True:
        # 边界检查放在采样之前：此时上一批工具结果已经入史，停在这里历史是完整的
        if stop_requested():
            return _finish(state, messages, "interrupted")
        if state.turns >= state.config.max_turns:
            return _finish(state, messages, "max_turns")
        try:
            turn = llm.complete(system, messages, tool_schemas)
        except Exception as exc:  # 网络、鉴权、脚本耗尽：结构化地停下，不让调用方去猜
            return _finish(state, messages, "llm_error", f"{type(exc).__name__}: {exc}")
        state.turns += 1
        state.last_text = turn.text
        _add_usage(state.usage, turn.usage)

        step = next_step(turn, state)
        if step != "run_tools":
            _record_without_tools(messages, turn)
            if step in ("continue", "max_tokens"):
                state.truncated = True
            if step != "continue":
                return _finish(state, messages, step)  # completed / max_tokens / refusal 同名即停止原因
            state.continuations += 1
            messages.append(user(CONTINUE_PROMPT))
            continue

        messages.append(assistant_from_turn(turn))
        results, interrupted = execute_tools(turn.tool_calls, tools, stop_requested)
        messages.append(tool_results(results))  # 一批调用的全部结果放进同一条 user 消息
        if interrupted:
            return _finish(state, messages, "interrupted")
        _update_error_streak(state, results)
        if state.consecutive_errors >= state.config.max_consecutive_errors:
            return _finish(state, messages, "error_budget")


def _record_without_tools(messages: list[dict], turn: AssistantTurn) -> None:
    """不执行工具的回复只留安全前缀：去掉 tool_use 块，去完为空就不入史。

    截断/拒答时 tool_use 的参数可能只有半截，而且没有结果的 tool_use 会让下一次请求被拒；
    空的 assistant 消息同样会被 API 拒绝。按块过滤而不只是清空 tool_calls，
    是因为适配器可能把厂商原始块（含 tool_use）放在 turn.blocks 里原样写回。
    """
    message = assistant_from_turn(replace(turn, tool_calls=[]))
    message["content"] = [b for b in message["content"] if b.get("type") != "tool_use"]
    if message["content"]:
        messages.append(message)


def _update_error_streak(state: LoopState, results: list[tuple[str, str, bool]]) -> None:
    """按调用顺序累计连续失败；任何一次成功都清零。"""
    for _, _, is_error in results:
        state.consecutive_errors = state.consecutive_errors + 1 if is_error else 0


def _add_usage(total: dict[str, int], usage: dict | None) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def _finish(
    state: LoopState,
    messages: list[dict],
    stop: StopReason,
    error: str | None = None,
) -> LoopResult:
    return LoopResult(
        final_text=state.last_text,
        stop=stop,
        turns=state.turns,
        messages=messages,
        truncated=state.truncated,
        usage=dict(state.usage),
        error=error,
    )
