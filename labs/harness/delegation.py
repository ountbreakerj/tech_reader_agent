"""子代理与 PTC：把一部分工作交给另一个 agent，或者交给一段程序。

对照正文 hx-23。主 agent 的上下文是最贵的资源：每一轮都要重发、每一个 token
都在稀释注意力。这一章的两种手段都在做同一件事——**把中间过程留在主上下文之外，
只把结果带回来**。

- **子代理**：启动一个新的 agent 循环，给它一个任务描述和一组（通常更少的）工具，
  它在自己的上下文里跑完，只把最终结论交回主 agent。两种启动方式：
  spawn（全新上下文，只带任务描述）和 fork（继承父代理的历史，前缀缓存可以复用）。
- **PTC**（programmatic tool calling）：让模型写一段程序，程序在沙箱里批量调用工具，
  只把程序的输出交回上下文。读 50 个文件、各取一行，用工具调用要 50 轮往返和
  50 份完整结果进上下文；用一段程序只要一轮、一份汇总。

两者都要回答同样的问题：子任务能用哪些工具（权限只能收窄不能扩大）、
最多跑多久、花多少、嵌套多深，以及失败时父级收到什么。

只用标准库。子代理复用 hx-01 的 run_agent 和 FakeLLM；PTC 的「程序」用一个
受限的 Python 子集演示，真实系统里它应该在 hx-15 那样的沙箱进程里跑。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import LLM
from .loop import LoopConfig, LoopResult, run_agent

Tool = Callable[[dict[str, Any]], str]


class DelegationError(Exception):
    """委派的配置不合法（工具越权、深度超限）。"""


@dataclass(frozen=True)
class SubagentSpec:
    """一次委派的全部约束。

    tools        子代理可用的工具名；必须是父级工具的子集
    max_turns    子代理的模型调用上限（比父级小，教学示意）
    mode         spawn：只带任务描述；fork：带上父级历史
    """

    task: str
    tools: tuple[str, ...]
    max_turns: int = 8
    mode: str = "spawn"
    system: str = "你是一个子代理。只完成交给你的任务，最后用一段简洁的文字汇报结论和依据。"


@dataclass
class SubagentReport:
    """子代理交回给父级的东西：结论，加上足够判断它可信不可信的元信息。"""

    ok: bool
    summary: str
    stop: str
    turns: int
    tool_calls: int
    depth: int

    def as_tool_result(self) -> str:
        status = "完成" if self.ok else f"未完成（{self.stop}）"
        return (f"[子代理{status}，用了 {self.turns} 轮、{self.tool_calls} 次工具调用]\n"
                f"{self.summary or '（子代理没有给出结论）'}")


class Delegator:
    """父级持有的委派器。负责收窄工具、限制深度、裁剪结果。"""

    def __init__(self, llm_factory: Callable[[], LLM], parent_tools: dict[str, Tool], *,
                 depth: int = 0, max_depth: int = 1, max_summary_chars: int = 4000) -> None:
        self.llm_factory = llm_factory
        self.parent_tools = parent_tools
        self.depth = depth
        self.max_depth = max_depth
        self.max_summary_chars = max_summary_chars
        self.reports: list[SubagentReport] = []

    def narrowed_tools(self, spec: SubagentSpec) -> dict[str, Tool]:
        """子代理的工具只能是父级工具的子集。请求了父级没有的工具，直接报错而不是忽略。"""
        missing = [name for name in spec.tools if name not in self.parent_tools]
        if missing:
            raise DelegationError(f"子代理请求了父级没有的工具：{missing}；权限只能收窄，不能扩大")
        tools = {name: self.parent_tools[name] for name in spec.tools}
        # 默认不让子代理再委派：嵌套深度失控是最常见的成本事故
        if "delegate" in tools and self.depth + 1 >= self.max_depth:
            del tools["delegate"]
        return tools

    def run(self, spec: SubagentSpec, parent_messages: list[dict] | None = None) -> SubagentReport:
        if self.depth >= self.max_depth:
            raise DelegationError(f"委派深度已到上限 {self.max_depth}")
        tools = self.narrowed_tools(spec)
        task = spec.task
        if spec.mode == "fork" and parent_messages:
            # fork：把父级历史的文字部分带过去。真实实现会原样复用历史前缀以命中缓存（hx-06），
            # 这里为了让子代理的第一条消息是 user，把父级历史压成一段背景说明。
            background = "\n".join(
                m["content"] if isinstance(m["content"], str) else
                " ".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
                for m in parent_messages)
            task = f"背景（来自父代理的对话）：\n{background}\n\n你的任务：{spec.task}"
        schemas = [{"name": n, "description": f"父级授予的工具 {n}", "input_schema": {"type": "object"}}
                   for n in tools]
        result: LoopResult = run_agent(self.llm_factory(), spec.system, task, tools, schemas,
                                       LoopConfig(max_turns=spec.max_turns))
        tool_calls = sum(1 for m in result.messages if m["role"] == "assistant"
                         for b in (m["content"] if isinstance(m["content"], list) else [])
                         if b.get("type") == "tool_use")
        summary = result.final_text.strip()
        if len(summary) > self.max_summary_chars:
            summary = summary[: self.max_summary_chars] + f"\n（结论超过 {self.max_summary_chars} 字符，已截断）"
        report = SubagentReport(result.stop == "completed", summary, result.stop, result.turns,
                                tool_calls, self.depth + 1)
        self.reports.append(report)
        return report


def delegate_tool(delegator: Delegator, allowed: tuple[str, ...], max_turns: int = 8) -> Tool:
    """把委派包装成父级的一个工具：模型调用 delegate({"task": ...})。

    子代理可用的工具由父级预先定好（allowed），模型不能在调用时自己挑——
    否则模型可以给子代理授予它自己本来不该用的组合。
    """

    def handler(args: dict[str, Any]) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return "Error: delegate 需要一个非空的 task"
        try:
            report = delegator.run(SubagentSpec(task=task, tools=allowed, max_turns=max_turns))
        except DelegationError as exc:
            return f"Error: {exc}"
        return report.as_tool_result()

    return handler


# ---------- PTC：让模型写程序批量调用工具 ----------

ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.Call, ast.Attribute, ast.Subscript, ast.Slice, ast.List, ast.Tuple, ast.Dict, ast.ListComp,
    ast.comprehension, ast.For, ast.If, ast.Compare, ast.BinOp, ast.BoolOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Mod, ast.Div, ast.FloorDiv, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.And, ast.Or, ast.Not, ast.USub, ast.keyword,
    ast.JoinedStr, ast.FormattedValue, ast.Is, ast.IsNot, ast.Pass, ast.Break, ast.Continue,
)
SAFE_BUILTINS = {"len": len, "range": range, "sorted": sorted, "min": min, "max": max, "sum": sum,
                 "str": str, "int": int, "enumerate": enumerate, "zip": zip, "list": list, "dict": dict}


class ProgramRejected(Exception):
    """程序用了不允许的语法或名字。"""


@dataclass
class ProgramResult:
    ok: bool
    output: str
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: str = ""

    def as_tool_result(self) -> str:
        head = f"[程序调用了 {len(self.calls)} 次工具]"
        return f"{head}\n{self.output}" if self.ok else f"Error: 程序失败：{self.error}\n{head}\n{self.output}"


def check_program(source: str) -> ast.Module:
    """静态检查：只允许一个很小的 Python 子集。没有 import、没有 def、没有属性私有名、没有 while。

    这不是安全边界（真实的 PTC 必须在沙箱进程里跑，见 hx-15）；它的作用是让教学版
    在同一个进程里运行时，不至于一段程序就能读写任意文件。
    """
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise ProgramRejected(f"语法错误：{exc.msg}（第 {exc.lineno} 行）") from exc
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ProgramRejected(f"不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ProgramRejected(f"不允许访问私有属性 {node.attr}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ProgramRejected(f"不允许的名字 {node.id}")
    return tree


def run_program(source: str, tools: dict[str, Tool], *, max_calls: int = 50,
                max_output: int = 4000) -> ProgramResult:
    """运行一段 PTC 程序。程序里通过 call(name, **args) 调用工具，通过 emit(text) 输出。

    只有 emit 的内容回到模型的上下文；每次工具调用的完整结果只留在程序里。
    工具调用次数有上限，输出有上限——两者都是成本保护。
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    out: list[str] = []

    def call(name: str, **args: Any) -> str:
        if name not in tools:
            raise ProgramRejected(f"程序调用了未授权的工具 {name!r}")
        if len(calls) >= max_calls:
            raise ProgramRejected(f"工具调用超过上限 {max_calls} 次")
        calls.append((name, args))
        return tools[name](args)

    def emit(*parts: Any) -> None:
        out.append(" ".join(str(p) for p in parts))

    try:
        tree = check_program(source)
        exec(compile(tree, "<ptc>", "exec"), {"__builtins__": SAFE_BUILTINS, "call": call, "emit": emit})
    except ProgramRejected as exc:
        return ProgramResult(False, "\n".join(out)[:max_output], calls, str(exc))
    except Exception as exc:  # noqa: BLE001 — 程序自己的 bug
        return ProgramResult(False, "\n".join(out)[:max_output], calls, f"{type(exc).__name__}: {exc}")
    text = "\n".join(out)
    if len(text) > max_output:
        text = text[:max_output] + f"\n（输出超过 {max_output} 字符，已截断）"
    return ProgramResult(True, text, calls)


def context_cost(results: list[str]) -> int:
    """粗略的上下文开销：进入上下文的字符总数。用来对比逐个工具调用与一段程序。"""
    return sum(len(r) for r in results)
