"""常见的 Agent 范式：ReAct、Plan-and-Execute、分治 / DAG、Reflexion、多 agent 编排。

对照正文 hx-26。前 25 章造的是 harness 的部件；范式回答的是另一个问题：
**用这些部件，按什么样的控制流去解一个任务**。

每个范式都写成一个小函数，模型都通过 hx-00 的 LLM 协议调用，测试里用 FakeLLM 回放。
它们刻意写得短，目的是看清控制流的差别，而不是做一个可用的框架：

- ReAct：思考、行动、观察交替进行。就是 hx-01 的 agent loop，只是要求模型把「思考」写出来。
- Plan-and-Execute：先让模型列出完整计划，再逐步执行；执行中可以重新规划。
- 分治（Divide and Conquer）：把任务拆成独立的子任务，分别求解，再合并。
- DAG 规划：子任务之间有依赖时，按拓扑顺序执行，同一层的可以并行。
- Reflexion：做完之后由评审（测试、规则或另一个模型）打分，失败就把反馈写进下一次尝试。
- 多 agent 编排：一个协调者把任务分派给不同角色的 worker，再汇总。

只用标准库。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import LLM
from .loop import LoopConfig, run_agent
from .messages import user

Tool = Callable[[dict[str, Any]], str]


def ask(llm: LLM, system: str, prompt: str) -> str:
    """不带工具的一次问答。范式里的规划、合并、评审都用它。"""
    return llm.complete(system, [user(prompt)], []).text.strip()


def parse_json_list(text: str) -> list[Any]:
    """从模型回复里取出第一个 JSON 数组。模型常在数组前后加说明文字，所以不能直接 json.loads。"""
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ValueError(f"回复里没有 JSON 数组：{text[:80]!r}")
    value = json.loads(match.group(0))
    if not isinstance(value, list):
        raise ValueError("不是数组")
    return value


# ---------- 1. ReAct ----------

REACT_SYSTEM = (
    "每一步先用一两句话写出你的思考（以「思考：」开头），再决定调用哪个工具；"
    "看到工具结果后继续思考。确定答案后，不再调用工具，直接给出最终回答。"
)


@dataclass
class Trace:
    """一次运行的思考与行动轨迹，便于检查模型是不是在「边想边做」。"""

    thoughts: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    answer: str = ""
    stop: str = ""


def react(llm: LLM, task: str, tools: dict[str, Tool], schemas: list[dict], *, max_turns: int = 10) -> Trace:
    """ReAct = hx-01 的 run_agent + 一段要求「先写思考」的系统提示。

    这里没有任何新的控制流：ReAct 的价值在于让推理过程出现在上下文里，
    后续每一步都能看到前面「为什么这么做」，而不是只看到一串工具调用。
    """
    result = run_agent(llm, REACT_SYSTEM, task, tools, schemas, LoopConfig(max_turns=max_turns))
    trace = Trace(answer=result.final_text, stop=result.stop)
    for message in result.messages:
        if message["role"] != "assistant" or not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "text" and block.get("text", "").strip():
                trace.thoughts.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                trace.actions.append(block["name"])
    return trace


# ---------- 2. Plan-and-Execute ----------

PLANNER_SYSTEM = "你是规划者。把任务拆成 2 到 6 个有序步骤，只输出一个 JSON 字符串数组，不要别的内容。"
EXECUTOR_SYSTEM = "你是执行者。只完成给你的这一步，最后用一句话汇报结果。"
REPLANNER_SYSTEM = (
    "你是规划者。根据已完成步骤的结果，输出剩余步骤的 JSON 字符串数组；"
    "如果任务已经完成，输出空数组 []。"
)


@dataclass
class PlanRun:
    plans: list[list[str]] = field(default_factory=list)      # 每次（重新）规划的结果
    steps: list[tuple[str, str]] = field(default_factory=list)  # (步骤, 结果)
    stop: str = ""


def plan_and_execute(planner: LLM, executor_factory: Callable[[], LLM], task: str,
                     tools: dict[str, Tool], schemas: list[dict], *,
                     max_steps: int = 10, replan: bool = True) -> PlanRun:
    """先规划、再逐步执行。每执行完一步，可以让规划者根据结果修改剩余计划。

    和 ReAct 的区别：计划是显式的数据，可以在执行前给人看、可以校验、可以在第 n 步时修改；
    代价是多了规划调用，而且计划可能在第一步就被现实推翻。
    """
    run = PlanRun()
    plan = [str(s) for s in parse_json_list(ask(planner, PLANNER_SYSTEM, task))]
    run.plans.append(list(plan))
    while plan:
        if len(run.steps) >= max_steps:
            run.stop = "max_steps"
            return run
        step = plan.pop(0)
        done = "\n".join(f"- {s}：{r}" for s, r in run.steps)
        prompt = f"总任务：{task}\n已完成：\n{done or '（无）'}\n现在执行这一步：{step}"
        result = run_agent(executor_factory(), EXECUTOR_SYSTEM, prompt, tools, schemas, LoopConfig(max_turns=6))
        run.steps.append((step, result.final_text.strip() or f"（未完成：{result.stop}）"))
        if replan:
            done = "\n".join(f"- {s}：{r}" for s, r in run.steps)
            plan = [str(s) for s in parse_json_list(ask(
                planner, REPLANNER_SYSTEM, f"总任务：{task}\n已完成：\n{done}\n原剩余计划：{json.dumps(plan, ensure_ascii=False)}"))]
            run.plans.append(list(plan))
    run.stop = "completed"
    return run


# ---------- 3. 分治（Divide and Conquer） ----------

SPLIT_SYSTEM = ("判断任务是否需要拆分。如果足够小，可以直接完成，输出 []；"
                "否则拆成 2 到 4 个彼此独立的子任务，输出 JSON 字符串数组。")
SOLVE_SYSTEM = "直接完成这个子任务，给出结果。"
MERGE_SYSTEM = "把子任务的结果合并成对总任务的一个完整回答。"


@dataclass
class DacNode:
    task: str
    depth: int
    children: list["DacNode"] = field(default_factory=list)
    result: str = ""

    def count(self) -> int:
        return 1 + sum(c.count() for c in self.children)


def divide_and_conquer(llm: LLM, task: str, *, max_depth: int = 2, max_children: int = 4,
                       depth: int = 0) -> DacNode:
    """递归地拆分、求解、合并。

    分治适合「子任务彼此独立」的情况：总结一份很长的文档（按章节）、审查一个大 PR（按文件）、
    调研一个问题（按方向）。每个子任务在自己的上下文里完成，主上下文只看到子结果。
    两个必要的上限：递归深度和分支数，否则拆分本身就会失控。
    """
    node = DacNode(task, depth)
    parts: list[str] = []
    if depth < max_depth:
        parts = [str(p) for p in parse_json_list(ask(llm, SPLIT_SYSTEM, task))][:max_children]
    if not parts:
        node.result = ask(llm, SOLVE_SYSTEM, task)
        return node
    node.children = [divide_and_conquer(llm, p, max_depth=max_depth, max_children=max_children, depth=depth + 1)
                     for p in parts]
    pieces = "\n".join(f"[{c.task}]\n{c.result}" for c in node.children)
    node.result = ask(llm, MERGE_SYSTEM, f"总任务：{task}\n子任务结果：\n{pieces}")
    return node


# ---------- 4. DAG 规划 ----------


class CycleError(ValueError):
    """依赖图有环。"""


@dataclass(frozen=True)
class DagTask:
    id: str
    prompt: str
    deps: tuple[str, ...] = ()


def topo_layers(tasks: list[DagTask]) -> list[list[str]]:
    """按依赖关系分层：同一层的任务互不依赖，可以并行；下一层依赖上一层。

    规划结果来自模型，必须校验：依赖不存在的任务、重复 id、有环，都在执行前报错。
    """
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("任务 id 重复")
    known = set(ids)
    for t in tasks:
        missing = [d for d in t.deps if d not in known]
        if missing:
            raise ValueError(f"任务 {t.id} 依赖了不存在的任务 {missing}")
    remaining = {t.id: set(t.deps) for t in tasks}
    layers: list[list[str]] = []
    while remaining:
        ready = sorted(i for i, deps in remaining.items() if not deps)
        if not ready:
            raise CycleError(f"依赖有环：{sorted(remaining)}")
        layers.append(ready)
        for i in ready:
            del remaining[i]
        for deps in remaining.values():
            deps.difference_update(ready)
    return layers


def run_dag(tasks: list[DagTask], solve: Callable[[DagTask, dict[str, str]], str]) -> tuple[dict[str, str], list[list[str]]]:
    """按层执行。每个任务拿到它依赖的那些任务的结果（只给依赖的，不给全部）。

    教学版逐层顺序执行；同一层里的任务可以交给 hx-03 的执行管线并行跑。
    """
    by_id = {t.id: t for t in tasks}
    layers = topo_layers(tasks)
    results: dict[str, str] = {}
    for layer in layers:
        for task_id in layer:
            task = by_id[task_id]
            results[task_id] = solve(task, {d: results[d] for d in task.deps})
    return results, layers


# ---------- 5. Reflexion ----------

ATTEMPT_SYSTEM = "完成任务。如果给了你之前失败的反思，务必针对反思修改做法。"
REFLECT_SYSTEM = "根据失败信息，用两三句话写出这次为什么失败、下次具体要改什么。不要写代码。"


@dataclass
class Attempt:
    output: str
    passed: bool
    feedback: str
    reflection: str = ""


def reflexion(llm: LLM, task: str, evaluate: Callable[[str], tuple[bool, str]], *,
              max_attempts: int = 3) -> list[Attempt]:
    """尝试 → 评审 → 反思 → 带着反思再尝试。

    关键在评审器 evaluate：它必须是**外部的、可验证的**信号（测试是否通过、规则是否满足），
    而不是让同一个模型自己说「我觉得对了」。没有外部信号的反思，常常只是把错误换个说法重复一遍。
    反思只保留最近几条，避免它们自己把上下文撑满。
    """
    attempts: list[Attempt] = []
    reflections: list[str] = []
    for _ in range(max_attempts):
        memo = "\n".join(f"- {r}" for r in reflections[-3:])
        prompt = task if not memo else f"{task}\n\n之前失败的反思：\n{memo}"
        output = ask(llm, ATTEMPT_SYSTEM, prompt)
        passed, feedback = evaluate(output)
        attempt = Attempt(output, passed, feedback)
        attempts.append(attempt)
        if passed:
            break
        attempt.reflection = ask(llm, REFLECT_SYSTEM, f"任务：{task}\n你的输出：{output}\n评审结果：{feedback}")
        reflections.append(attempt.reflection)
    return attempts


# ---------- 6. 多 agent 编排（协调者 + 角色 worker） ----------

ROUTER_SYSTEM = ("你是协调者。把任务分派给合适的角色。只输出 JSON 数组，"
                 "每项是 {\"role\": 角色名, \"task\": 子任务}。可用角色：{roles}")
SYNTH_SYSTEM = "你是协调者。把各角色的结果整理成给用户的最终回答，指出它们之间的分歧（如果有）。"


@dataclass
class Worker:
    role: str
    system: str
    llm_factory: Callable[[], LLM]
    tools: dict[str, Tool] = field(default_factory=dict)


@dataclass
class OrchestrationRun:
    assignments: list[tuple[str, str]] = field(default_factory=list)
    results: list[tuple[str, str]] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    answer: str = ""


def orchestrate(coordinator: LLM, workers: dict[str, Worker], task: str, *, max_assignments: int = 6) -> OrchestrationRun:
    """协调者分派、worker 各自完成、协调者汇总。

    每个 worker 有自己的系统提示和工具集（权限按角色收窄，hx-23）。
    协调者给出的角色名必须在名单里，不在的拒绝并记录——不要让模型临时发明一个角色。
    """
    run = OrchestrationRun()
    roles = ", ".join(sorted(workers))
    plan = parse_json_list(ask(coordinator, ROUTER_SYSTEM.replace("{roles}", roles), task))
    for item in plan[:max_assignments]:
        role, sub = str(item.get("role", "")), str(item.get("task", ""))
        worker = workers.get(role)
        if worker is None or not sub:
            run.rejected.append(f"{role or '（空）'}: {sub[:40]}")
            continue
        run.assignments.append((role, sub))
        schemas = [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in worker.tools]
        result = run_agent(worker.llm_factory(), worker.system, sub, worker.tools, schemas, LoopConfig(max_turns=6))
        run.results.append((role, result.final_text.strip()))
    pieces = "\n".join(f"[{r}] {t}" for r, t in run.results)
    run.answer = ask(coordinator, SYNTH_SYSTEM, f"用户任务：{task}\n各角色结果：\n{pieces}")
    return run
