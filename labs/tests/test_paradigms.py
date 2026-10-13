"""26 Agent 范式的测试。全部用 FakeLLM 回放：测的是控制流，不是模型能力。"""
from __future__ import annotations

import unittest

from harness.llm import FakeLLM
from harness.messages import AssistantTurn, ToolCall
from harness.paradigms import (
    CycleError,
    DagTask,
    Worker,
    divide_and_conquer,
    orchestrate,
    parse_json_list,
    plan_and_execute,
    react,
    reflexion,
    run_dag,
    topo_layers,
)


def text(t):
    return AssistantTurn(text=t, stop_reason="end_turn")


def tool(name, thought="", **args):
    return AssistantTurn(text=thought, tool_calls=[ToolCall(f"c-{name}", name, args)], stop_reason="tool_use")


TOOLS = {"search": lambda a: f"关于 {a.get('q')} 的 2 条结果", "calc": lambda a: str(eval(a["expr"], {}, {}))}
SCHEMAS = [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in TOOLS]


class ParseTests(unittest.TestCase):
    def test_extracts_array_from_chatter(self) -> None:
        self.assertEqual(parse_json_list('好的，计划如下：\n["a", "b"]\n希望有帮助'), ["a", "b"])

    def test_no_array_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_json_list("我觉得不用拆")


class ReactTests(unittest.TestCase):
    def test_thoughts_and_actions_interleave(self) -> None:
        llm = FakeLLM([
            tool("search", "思考：先查一下默认超时是多少。", q="timeout"),
            tool("calc", "思考：文档说 30 秒，换算成毫秒。", expr="30*1000"),
            text("默认超时是 30000 毫秒。"),
        ])
        trace = react(llm, "默认超时是多少毫秒？", TOOLS, SCHEMAS)
        self.assertEqual(trace.actions, ["search", "calc"])
        self.assertEqual(len(trace.thoughts), 3)
        self.assertTrue(trace.thoughts[0].startswith("思考"))
        self.assertEqual(trace.answer, "默认超时是 30000 毫秒。")
        self.assertEqual(trace.stop, "completed")

    def test_react_is_bounded(self) -> None:
        llm = FakeLLM([tool("search", "思考：再查一次。", q="x") for _ in range(10)])
        self.assertEqual(react(llm, "t", TOOLS, SCHEMAS, max_turns=3).stop, "max_turns")


class PlanExecuteTests(unittest.TestCase):
    def test_plan_then_execute_with_replan(self) -> None:
        planner = FakeLLM([
            text('["列出文件", "统计行数", "写报告"]'),
            text('["统计行数", "写报告"]'),
            text('["写报告"]'),          # 规划者可以根据结果修改剩余计划
            text("[]"),
        ])
        executor_outputs = iter(["共 3 个文件", "共 120 行", "报告已写好"])
        run = plan_and_execute(planner, lambda: FakeLLM([text(next(executor_outputs))]), "做个统计", TOOLS, SCHEMAS)
        self.assertEqual(run.stop, "completed")
        self.assertEqual([s for s, _ in run.steps], ["列出文件", "统计行数", "写报告"])
        self.assertEqual(len(run.plans), 4)

    def test_replanner_can_finish_early(self) -> None:
        planner = FakeLLM([text('["a", "b", "c"]'), text("[]")])
        run = plan_and_execute(planner, lambda: FakeLLM([text("a 做完，任务其实已经完成")]), "t", TOOLS, SCHEMAS)
        self.assertEqual(len(run.steps), 1)

    def test_step_cap(self) -> None:
        planner = FakeLLM([text('["s"]')] + [text('["s"]')] * 20)
        run = plan_and_execute(planner, lambda: FakeLLM([text("ok")]), "t", TOOLS, SCHEMAS, max_steps=3)
        self.assertEqual(run.stop, "max_steps")


class DacTests(unittest.TestCase):
    def test_split_solve_merge(self) -> None:
        llm = FakeLLM([
            text('["总结第一章", "总结第二章"]'),   # 拆分根
            text("[]"), text("第一章讲循环"),        # 子任务 1：不再拆，直接解
            text("[]"), text("第二章讲工具"),        # 子任务 2
            text("全书：先讲循环，再讲工具"),         # 合并
        ])
        root = divide_and_conquer(llm, "总结这本书")
        self.assertEqual(root.count(), 3)
        self.assertEqual(root.result, "全书：先讲循环，再讲工具")
        self.assertEqual([c.result for c in root.children], ["第一章讲循环", "第二章讲工具"])

    def test_depth_limit_stops_splitting(self) -> None:
        llm = FakeLLM([text('["a", "b"]'), text("A"), text("B"), text("AB")])
        root = divide_and_conquer(llm, "t", max_depth=1)   # 第 1 层不再询问是否拆分
        self.assertEqual(root.result, "AB")

    def test_branch_limit(self) -> None:
        llm = FakeLLM([text('["1","2","3","4","5","6"]')] + [text("[]"), text("x")] * 2 + [text("merged")])
        root = divide_and_conquer(llm, "t", max_children=2)
        self.assertEqual(len(root.children), 2)


class DagTests(unittest.TestCase):
    TASKS = [
        DagTask("schema", "设计表结构"),
        DagTask("api", "写接口", ("schema",)),
        DagTask("ui", "写页面", ("schema",)),
        DagTask("e2e", "端到端测试", ("api", "ui")),
    ]

    def test_layers(self) -> None:
        self.assertEqual(topo_layers(self.TASKS), [["schema"], ["api", "ui"], ["e2e"]])

    def test_each_task_sees_only_its_deps(self) -> None:
        seen = {}

        def solve(task, inputs):
            seen[task.id] = sorted(inputs)
            return f"{task.id}-done"

        results, _ = run_dag(self.TASKS, solve)
        self.assertEqual(seen["e2e"], ["api", "ui"])
        self.assertEqual(seen["api"], ["schema"])
        self.assertEqual(results["e2e"], "e2e-done")

    def test_cycle_detected(self) -> None:
        with self.assertRaises(CycleError):
            topo_layers([DagTask("a", "", ("b",)), DagTask("b", "", ("a",))])

    def test_unknown_dependency(self) -> None:
        with self.assertRaises(ValueError):
            topo_layers([DagTask("a", "", ("ghost",))])

    def test_duplicate_ids(self) -> None:
        with self.assertRaises(ValueError):
            topo_layers([DagTask("a", ""), DagTask("a", "")])


class ReflexionTests(unittest.TestCase):
    def test_reflection_feeds_next_attempt(self) -> None:
        prompts = []

        def attempt_two(messages):
            prompts.append(messages[0]["content"])
            return text("def add(a, b): return a + b")

        llm = FakeLLM([
            text("def add(a, b): return a - b"),
            text("把减号写成了加号的反面；下次检查运算符。"),
            attempt_two,
        ])

        def evaluate(code):
            return ("a + b" in code, "add(1, 2) 期望 3，得到 -1")

        attempts = reflexion(llm, "写一个 add 函数", evaluate)
        self.assertEqual([a.passed for a in attempts], [False, True])
        self.assertIn("检查运算符", prompts[0])

    def test_gives_up_after_max_attempts(self) -> None:
        llm = FakeLLM([text("wrong"), text("反思")] * 3)
        attempts = reflexion(llm, "t", lambda o: (False, "还是不对"), max_attempts=3)
        self.assertEqual(len(attempts), 3)
        self.assertFalse(attempts[-1].passed)


class OrchestrateTests(unittest.TestCase):
    def test_route_run_and_synthesize(self) -> None:
        coordinator = FakeLLM([
            text('[{"role": "reviewer", "task": "审查 diff"}, {"role": "tester", "task": "补测试"}, '
                 '{"role": "hacker", "task": "绕过权限"}]'),
            text("审查发现 1 个问题，测试已补 2 条。"),
        ])
        workers = {
            "reviewer": Worker("reviewer", "你是代码审查者", lambda: FakeLLM([text("发现 1 个空指针风险")])),
            "tester": Worker("tester", "你是测试工程师", lambda: FakeLLM([text("补了 2 条测试")])),
        }
        run = orchestrate(coordinator, workers, "处理这个 PR")
        self.assertEqual([r for r, _ in run.assignments], ["reviewer", "tester"])
        self.assertEqual(len(run.rejected), 1)          # 不存在的角色被拒绝
        self.assertIn("hacker", run.rejected[0])
        self.assertIn("测试已补", run.answer)


if __name__ == "__main__":
    unittest.main()
