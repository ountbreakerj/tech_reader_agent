"""组装 mini-harness 与长任务连续性：把前面 24 章的部件接成一个能跑的整体。

对照正文 hx-25。前面每一章都只实现了一个部件，并且尽量让它能单独读懂。
这一章做两件事：

1. **组装**：一个 MiniHarness 类，把循环（01）、工具注册与校验（02）、会话日志（04）、
   权限（13）、hooks（21）、重试（18）、超时（19）、熔断（20）、可观测性（24）
   按正确的顺序串起来。重点是**顺序**：每一层放在哪、谁包着谁。
2. **长任务连续性**：一个任务跨越多个会话时，靠什么接上。答案是仓库里的工件——
   进度文件和决策文件——而不是上一个会话的对话历史。会话结束前写「交接」，
   新会话开始时读「交接」。

组装版刻意保持小：同步执行、顺序跑工具、不支持插话。它的价值是让你看清
各部件之间的接口，而不是替代某个真实的 harness。只用标准库。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .circuit import BreakerConfig, CircuitBreaker, CircuitOpen
from .hooks import POST_TOOL, PRE_TOOL, STOP, USER_PROMPT, HookBus
from .llm import LLM
from .messages import AssistantTurn, assistant_from_turn, tool_results, user
from .observability import Tracer
from .permissions import ALLOW, DENY, PermissionEngine
from .retry import Failure, RetryPolicy, retry_call
from .session_log import SessionLog
from .timeouts import Deadline, Timeout
from .tools import ToolRegistry


@dataclass
class HarnessConfig:
    max_turns: int = 20
    turn_budget: float = 600.0            # 整轮的时间预算（秒，教学示意）
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_retries=2, initial_delay=0.01, max_delay=0.05))
    breaker: BreakerConfig = field(default_factory=lambda: BreakerConfig(failure_threshold=3, cooldown=30))
    max_consecutive_errors: int = 3


@dataclass
class RunReport:
    stop: str
    final_text: str
    turns: int
    messages: list[dict]
    notes: list[str] = field(default_factory=list)


class MiniHarness:
    """把各章的部件按顺序接起来的最小实现。

    一次工具调用依次经过：hooks(pre) → 参数校验 → 权限规则 → 执行 → hooks(post)；
    一次模型调用依次经过：整轮截止时刻 → 熔断器 → 重试 → 模型。
    每一步都写会话日志，每一段都开一个 span。
    """

    def __init__(self, llm: LLM, registry: ToolRegistry, *, system: str = "你是一个编码助手。",
                 permissions: PermissionEngine | None = None, hooks: HookBus | None = None,
                 config: HarnessConfig | None = None, log: SessionLog | None = None,
                 tracer: Tracer | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.llm = llm
        self.registry = registry
        self.system = system
        self.permissions = permissions
        self.hooks = hooks or HookBus()
        self.config = config or HarnessConfig()
        self.log = log or SessionLog("mini")
        self.tracer = tracer or Tracer(clock)
        self.clock = clock
        self.sleep = sleep
        self.breaker = CircuitBreaker("model", self.config.breaker, clock)

    # ---- 一次模型调用：截止时刻 → 熔断 → 重试 → 模型 ----

    def _call_model(self, messages: list[dict], deadline: Deadline) -> AssistantTurn:
        """熔断器包在重试外面（hx-20 第 4 节）：断开时连重试都不做；
        闭合时，一次「带重试的调用」作为一次结果计入熔断器。"""
        deadline.check()

        def attempt() -> AssistantTurn:
            deadline.check()
            try:
                return self.llm.complete(self.system, messages, self.registry.schemas())
            except Failure:
                raise
            except Exception as exc:  # 适配器没分类的异常：按传输错误处理，交给重试判断
                raise Failure("transport", f"{type(exc).__name__}: {exc}") from exc

        def log_retry(a) -> None:
            self.log.append("llm/retry", {"attempt": a.number, "kind": a.kind, "delay": a.delay})

        def with_retry() -> AssistantTurn:
            outcome = retry_call(attempt, self.config.retry, sleep=self.sleep, on_attempt=log_retry)
            if not outcome.ok:
                raise outcome.failure or Failure("unknown")
            return outcome.value  # type: ignore[return-value]

        with self.tracer.span("model.call"):
            return self.breaker.call(with_retry)      # 断开时抛 CircuitOpen，不进入重试

    # ---- 一次工具调用：hooks(pre) → 校验 → 权限 → 执行 → hooks(post) ----

    def _run_tool(self, call_id: str, name: str, args: dict[str, Any]) -> tuple[str, bool, list[str]]:
        with self.tracer.span(f"tool.{name}", tool=name):
            pre = self.hooks.dispatch(PRE_TOOL, {"tool_name": name, "tool_input": args})
            if pre.blocked:
                return f"Error: 被 hook {pre.blocked_by} 拒绝：{pre.reason}", True, pre.contexts
            args = pre.payload.get("tool_input", args)
            if self.permissions is not None:
                decision = self.permissions.decide(name, args)
                if decision.outcome != ALLOW:
                    # 组装版没有审批界面：ask 按无通道处理（hx-16 的 fail-closed）
                    verb = "拒绝" if decision.outcome == DENY else "需要人工确认，当前无审批通道，按拒绝处理"
                    return f"Error: 权限规则 {decision.rule or '默认策略'} {verb}：{decision.reason}", True, pre.contexts
            output = self.registry.call(name, args)      # 内部做参数校验，失败返回 Error 文本
            is_error = output.startswith("Error:")
            post = self.hooks.dispatch(POST_TOOL, {"tool_name": name, "tool_input": args,
                                                   "tool_output": output, "is_error": is_error})
            return str(post.payload.get("tool_output", output)), is_error, pre.contexts + post.contexts

    # ---- 主循环 ----

    def run(self, task: str) -> RunReport:
        cfg = self.config
        deadline = Deadline("turn", cfg.turn_budget, self.clock)
        notes: list[str] = []
        self.hooks.reset_turn()
        prompt = self.hooks.dispatch(USER_PROMPT, {"text": task})
        if prompt.blocked:
            return RunReport("blocked", prompt.reason, 0, [], [f"用户输入被 {prompt.blocked_by} 拦截"])
        messages: list[dict] = [user(prompt.payload.get("text", task))]
        self.log.append("user/message", {"text": messages[0]["content"]})
        turns = errors = 0
        final = ""
        with self.tracer.span("turn"):
            while True:
                if turns >= cfg.max_turns:
                    return RunReport("max_turns", final, turns, messages, notes)
                try:
                    turn = self._call_model(messages, deadline)
                except Timeout as exc:
                    notes.append(str(exc))
                    return RunReport("timeout", final, turns, messages, notes)
                except CircuitOpen as exc:
                    notes.append(str(exc))
                    return RunReport("circuit_open", final, turns, messages, notes)
                except Failure as exc:
                    notes.append(f"模型调用失败：{exc.kind}")
                    return RunReport("llm_error", final, turns, messages, notes)
                turns += 1
                final = turn.text or final
                if turn.stop_reason in ("max_tokens", "refusal") or not turn.tool_calls:
                    if turn.text:
                        messages.append({"role": "assistant", "content": [{"type": "text", "text": turn.text}]})
                    self.log.append("assistant/message", {"text": turn.text, "stop": turn.stop_reason})
                    if turn.stop_reason in ("max_tokens", "refusal"):
                        return RunReport(turn.stop_reason, final, turns, messages, notes)
                    stop = self.hooks.dispatch(STOP, {"final_text": turn.text})
                    if stop.keep_going:
                        messages.append(user(f"（来自 hook 的要求）{stop.continue_reason}"))
                        notes.append(f"stop hook 要求继续：{stop.continue_reason}")
                        continue
                    return RunReport("completed", final, turns, messages, notes)
                messages.append(assistant_from_turn(turn))
                self.log.append("assistant/message", {"text": turn.text, "tool_calls": len(turn.tool_calls)})
                results, extra = [], []
                for call in turn.tool_calls:
                    output, is_error, contexts = self._run_tool(call.id, call.name, call.args)
                    self.log.append("tool/result", {"name": call.name, "is_error": is_error, "chars": len(output)})
                    results.append((call.id, output, is_error))
                    extra.extend(contexts)
                    errors = errors + 1 if is_error else 0
                message = tool_results(results)
                for text in extra:      # hooks 追加的上下文排在工具结果之后，同一条消息里（hx-01）
                    message["content"].append({"type": "text", "text": text})
                messages.append(message)
                if errors >= cfg.max_consecutive_errors:
                    return RunReport("error_budget", final, turns, messages, notes)


# ---------- 长任务连续性：进度文件与交接 ----------

PROGRESS_FILE = "PROGRESS.md"
DECISIONS_FILE = "DECISIONS.md"
HANDOFF_FILE = ".harness/handoff.json"


@dataclass
class Handoff:
    """一次会话结束时留给下一次会话的东西。字段刻意少：多了没人写，也没人读。"""

    goal: str
    done: list[str]
    next_steps: list[str]
    open_questions: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)        # 下一次开工前先跑的验证命令
    updated_at: str = ""

    def validate(self) -> list[str]:
        problems = []
        if not self.goal.strip():
            problems.append("缺少目标")
        if not self.next_steps:
            problems.append("没有写下一步：下一个会话会从头摸索")
        if not self.verify:
            problems.append("没有写验证命令：下一个会话无法确认当前状态是不是好的")
        return problems


def write_handoff(workdir: str, handoff: Handoff, *, now: str | None = None) -> list[str]:
    """会话结束前（「下班」）调用：写交接文件，同时把进度追加到 PROGRESS.md。

    交接写进仓库而不是只存在会话日志里：下一次可能是另一个 harness、另一个模型，
    甚至是一个人来接手。返回校验问题，空列表表示合格。
    """
    problems = handoff.validate()
    handoff.updated_at = now or time.strftime("%Y-%m-%d %H:%M")
    root = Path(workdir)
    target = root / HANDOFF_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(handoff.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)      # 原子替换：进程中途被杀，也不会留下半个交接文件
    lines = [f"\n## {handoff.updated_at}", f"目标：{handoff.goal}"]
    lines += [f"- [x] {item}" for item in handoff.done]
    lines += [f"- [ ] {item}" for item in handoff.next_steps]
    with open(root / PROGRESS_FILE, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return problems


def read_handoff(workdir: str) -> Handoff | None:
    """新会话开始时（「上班」）调用。没有交接文件返回 None：这是一个新任务。"""
    path = Path(workdir) / HANDOFF_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Handoff(**data)


def resume_prompt(handoff: Handoff | None) -> str:
    """把交接渲染成新会话的第一段上下文。先验证，再干活。"""
    if handoff is None:
        return "这是一个新任务，没有上一次的交接记录。"
    parts = [f"上一次会话（{handoff.updated_at}）留下的交接：", f"目标：{handoff.goal}"]
    if handoff.done:
        parts.append("已完成：" + "；".join(handoff.done))
    parts.append("下一步：" + "；".join(handoff.next_steps))
    if handoff.open_questions:
        parts.append("未决问题：" + "；".join(handoff.open_questions))
    if handoff.verify:
        parts.append("开工前先运行这些命令，确认当前状态和交接描述一致：" + "；".join(handoff.verify))
    return "\n".join(parts)


def record_decision(workdir: str, title: str, decision: str, why: str, *, now: str | None = None) -> None:
    """把一个设计决策追加到 DECISIONS.md。压缩会丢掉「为什么」，决策文件不会。"""
    stamp = now or time.strftime("%Y-%m-%d")
    with open(Path(workdir) / DECISIONS_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"\n## {stamp} {title}\n决定：{decision}\n原因：{why}\n")
