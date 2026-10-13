"""Hooks：在 harness 的固定时点上，让外部代码观察、修改或拒绝正在发生的事。

对照正文 hx-21。到第 20 章为止，harness 能做的每件事都写在它自己的代码里。
hooks 是第一种「不改 harness 代码就能改变它行为」的扩展方式：
在循环的几个固定时点（收到用户输入、工具执行前、工具执行后、准备停下）
调用外部注册的函数。

设计上的四个决定：

1. **事件是一张固定的词表**。时点由 harness 定义，外部不能发明新的时点。
   词表就是 harness 对外承诺的扩展接口，改它等于改 API。
2. **三种能力分开**：观察（只读）、修改（返回新的载荷）、拦截（返回拒绝）。
   每个事件声明自己允许哪几种，例如工具执行后的结果可以被修改，但不能被「拦截」——
   工具已经执行过了。
3. **拦截是单调的**（和 hx-13 同一条规则）：任何一个 hook 可以拒绝，
   没有 hook 能强行放行别的 hook 已经拒绝的事。
4. **hook 自己的失败不能拖垮循环**：抛异常、超时都按事件声明的策略处理
   （观察类忽略并记录；拦截类按失败关闭处理）。

另外两条防护：Stop 类 hook 可以要求「继续」，但次数必须有上限；
外部命令形式的 hook 通过标准输入拿 JSON、通过标准输出和退出码回话。

只用标准库。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

# ---- 事件词表 ----

USER_PROMPT = "user_prompt_submit"   # 用户输入进入上下文之前：可修改、可拦截
PRE_TOOL = "pre_tool_use"            # 工具执行之前：可修改参数、可拦截
POST_TOOL = "post_tool_use"          # 工具执行之后：可修改结果（追加上下文），不可拦截
STOP = "stop"                        # 准备停下之前：可要求继续（有上限）
SESSION_START = "session_start"      # 会话开始：只观察（也可以追加上下文）

# 每个事件允许的能力。不在表里的能力即使 hook 返回了也会被忽略并记录。
CAPABILITIES: dict[str, frozenset[str]] = {
    USER_PROMPT: frozenset({"observe", "modify", "block"}),
    PRE_TOOL: frozenset({"observe", "modify", "block"}),
    POST_TOOL: frozenset({"observe", "modify"}),
    STOP: frozenset({"observe", "continue"}),
    SESSION_START: frozenset({"observe", "modify"}),
}

# hook 自身失败时怎么办。拦截类事件失败关闭：一个坏掉的安全 hook 不能变成放行。
ON_ERROR: dict[str, str] = {
    USER_PROMPT: "ignore",
    PRE_TOOL: "block",
    POST_TOOL: "ignore",
    STOP: "ignore",
    SESSION_START: "ignore",
}


class HookError(Exception):
    """hook 注册或返回值不合法。"""


@dataclass(frozen=True)
class HookResult:
    """hook 的返回值。字段都可选，含义由事件决定。

    block      拒绝这件事（必须带 reason，会喂回模型）
    payload    修改后的载荷（替换原载荷传给下一个 hook）
    context    追加给模型的一段上下文（例如格式化脚本的输出）
    keep_going Stop 事件里要求继续，并给出继续的理由
    """

    block: bool = False
    reason: str = ""
    payload: dict[str, Any] | None = None
    context: str = ""
    keep_going: bool = False

    def __post_init__(self) -> None:
        if self.block and not self.reason:
            raise HookError("拦截必须给出理由：模型看不到理由就只会原样重试")
        if self.keep_going and not self.reason:
            raise HookError("要求继续必须给出理由：它会作为下一轮的输入")


Hook = Callable[[str, dict[str, Any]], "HookResult | None"]


@dataclass
class Registration:
    name: str
    event: str
    fn: Hook
    matcher: str | None = None     # 只对某些工具生效（fnmatch 通配）；None 表示全部


@dataclass
class DispatchOutcome:
    """一次事件分发的合并结果。"""

    event: str
    payload: dict[str, Any]
    blocked: bool = False
    reason: str = ""
    blocked_by: str = ""
    contexts: list[str] = field(default_factory=list)
    keep_going: bool = False
    continue_reason: str = ""
    errors: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)   # 返回了事件不允许的能力


class HookBus:
    """hook 注册表与分发器。"""

    def __init__(self, *, max_forced_continues: int = 3) -> None:
        self._hooks: list[Registration] = []
        self.max_forced_continues = max_forced_continues
        self._forced_continues = 0
        self.log: list[tuple[str, str, str]] = []   # (event, hook, 发生了什么)

    def on(self, event: str, name: str, fn: Hook, *, matcher: str | None = None) -> None:
        if event not in CAPABILITIES:
            raise HookError(f"未知事件 {event!r}；可用事件：{', '.join(CAPABILITIES)}")
        if any(r.name == name and r.event == event for r in self._hooks):
            raise HookError(f"事件 {event} 上已经有名为 {name!r} 的 hook")
        self._hooks.append(Registration(name, event, fn, matcher))

    def reset_turn(self) -> None:
        """用户有新输入时调用：强制继续的计数重新开始。"""
        self._forced_continues = 0

    def dispatch(self, event: str, payload: dict[str, Any]) -> DispatchOutcome:
        """按注册顺序调用 hook。修改会传递给下一个；拦截立刻结束；继续有上限。"""
        import fnmatch

        allowed = CAPABILITIES[event]
        outcome = DispatchOutcome(event, dict(payload))
        for reg in [r for r in self._hooks if r.event == event]:
            tool = str(outcome.payload.get("tool_name", ""))
            if reg.matcher and not fnmatch.fnmatchcase(tool, reg.matcher):
                continue
            try:
                result = reg.fn(event, dict(outcome.payload))
            except Exception as exc:  # noqa: BLE001 — 外部代码，什么都可能抛
                message = f"{reg.name}: {type(exc).__name__}: {exc}"
                outcome.errors.append(message)
                self.log.append((event, reg.name, f"error {exc}"))
                if ON_ERROR[event] == "block":
                    outcome.blocked = True
                    outcome.blocked_by = reg.name
                    outcome.reason = f"安全检查 {reg.name} 自身出错，按拒绝处理：{exc}"
                    return outcome
                continue
            if result is None:
                continue
            if result.block:
                if "block" not in allowed:
                    outcome.ignored.append(f"{reg.name}: {event} 不能拦截（事情已经发生了）")
                else:
                    outcome.blocked, outcome.reason, outcome.blocked_by = True, result.reason, reg.name
                    self.log.append((event, reg.name, f"block {result.reason}"))
                    return outcome
            if result.payload is not None:
                if "modify" in allowed:
                    outcome.payload = dict(result.payload)
                    self.log.append((event, reg.name, "modify"))
                else:
                    outcome.ignored.append(f"{reg.name}: {event} 不能修改载荷")
            if result.context:
                if "modify" in allowed or event == SESSION_START:
                    outcome.contexts.append(result.context)
                else:
                    outcome.ignored.append(f"{reg.name}: {event} 不能追加上下文")
            if result.keep_going:
                if "continue" not in allowed:
                    outcome.ignored.append(f"{reg.name}: 只有 stop 事件能要求继续")
                elif self._forced_continues >= self.max_forced_continues:
                    outcome.ignored.append(
                        f"{reg.name}: 已经连续强制继续 {self._forced_continues} 次，达到上限，这次按停止处理")
                    self.log.append((event, reg.name, "continue refused (cap)"))
                elif not outcome.keep_going:
                    self._forced_continues += 1
                    outcome.keep_going, outcome.continue_reason = True, result.reason
                    self.log.append((event, reg.name, f"continue {result.reason}"))
        return outcome


# ---- 外部命令形式的 hook ----


def command_hook(argv: Sequence[str], *, timeout: float = 10.0) -> Hook:
    """把一条外部命令包装成 hook：载荷以 JSON 写进 stdin。

    约定（与常见的 CLI agent 一致的简化版）：
    - 退出码 0：通过。stdout 若是 JSON，可带 {"context": ...} 或 {"payload": ...}
    - 退出码 2：拦截，stderr 作为理由
    - 其他退出码或超时：视为 hook 自身出错（由事件的 ON_ERROR 策略处理）
    """

    def hook(event: str, payload: dict[str, Any]) -> HookResult | None:
        data = json.dumps({"event": event, **payload}, ensure_ascii=False)
        proc = subprocess.run(
            list(argv), input=data, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if proc.returncode == 2:
            return HookResult(block=True, reason=proc.stderr.strip() or f"{argv[0]} 拒绝了这次操作")
        if proc.returncode != 0:
            raise HookError(f"hook 命令退出码 {proc.returncode}: {proc.stderr.strip()[:200]}")
        out = proc.stdout.strip()
        if not out:
            return None
        try:
            reply = json.loads(out)
        except json.JSONDecodeError:
            return HookResult(context=out)
        return HookResult(context=str(reply.get("context", "")), payload=reply.get("payload"))

    return hook


# ---- 把 hooks 接到一次工具调用上 ----


def run_tool_with_hooks(bus: HookBus, tool_name: str, args: dict[str, Any],
                        execute: Callable[[dict[str, Any]], str]) -> tuple[str, bool, list[str]]:
    """pre → 执行 → post。返回 (给模型的结果, 是否错误, 要追加的上下文)。"""
    pre = bus.dispatch(PRE_TOOL, {"tool_name": tool_name, "tool_input": args})
    if pre.blocked:
        return f"Error: 工具 {tool_name} 被 hook {pre.blocked_by} 拒绝：{pre.reason}", True, pre.contexts
    final_args = pre.payload.get("tool_input", args)
    started = time.monotonic()
    try:
        output, is_error = execute(final_args), False
    except Exception as exc:  # noqa: BLE001
        output, is_error = f"Error: {type(exc).__name__}: {exc}", True
    post = bus.dispatch(POST_TOOL, {
        "tool_name": tool_name, "tool_input": final_args, "tool_output": output,
        "is_error": is_error, "duration": time.monotonic() - started,
    })
    return str(post.payload.get("tool_output", output)), is_error, pre.contexts + post.contexts


def python_command(code: str) -> list[str]:
    """测试与示例用：用当前解释器跑一段代码，充当外部 hook 命令。"""
    return [sys.executable, "-c", code]
