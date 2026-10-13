"""23 子代理与 PTC 的测试。子代理用 FakeLLM 回放，不调用真实模型。"""
from __future__ import annotations

import unittest

from harness.delegation import (
    DelegationError,
    Delegator,
    ProgramRejected,
    SubagentSpec,
    check_program,
    context_cost,
    delegate_tool,
    run_program,
)
from harness.llm import FakeLLM
from harness.messages import AssistantTurn, ToolCall

FILES = {f"src/m{i}.py": f"# module {i}\nVERSION = '{i}.0'\n" + "x = 1\n" * 200 for i in range(20)}


def read_file(args):
    return FILES.get(args.get("path", ""), "Error: not found")


def list_files(args):
    return "\n".join(sorted(FILES))


def write_file(args):
    return "written"


PARENT_TOOLS = {"read_file": read_file, "list_files": list_files, "write_file": write_file}


def scripted(*turns):
    return lambda: FakeLLM(list(turns))


class NarrowingTests(unittest.TestCase):
    def test_subset_is_allowed(self) -> None:
        d = Delegator(scripted(), PARENT_TOOLS)
        self.assertEqual(set(d.narrowed_tools(SubagentSpec("t", ("read_file",)))), {"read_file"})

    def test_cannot_request_tools_parent_lacks(self) -> None:
        d = Delegator(scripted(), {"read_file": read_file})
        with self.assertRaises(DelegationError):
            d.narrowed_tools(SubagentSpec("t", ("read_file", "deploy")))

    def test_depth_limit(self) -> None:
        d = Delegator(scripted(), PARENT_TOOLS, depth=1, max_depth=1)
        with self.assertRaises(DelegationError):
            d.run(SubagentSpec("t", ("read_file",)))

    def test_delegate_tool_removed_at_last_level(self) -> None:
        tools = dict(PARENT_TOOLS, delegate=lambda a: "")
        d = Delegator(scripted(), tools, depth=0, max_depth=1)
        self.assertNotIn("delegate", d.narrowed_tools(SubagentSpec("t", ("read_file", "delegate"))))


class SubagentRunTests(unittest.TestCase):
    def test_only_summary_returns_to_parent(self) -> None:
        llm = scripted(
            AssistantTurn(text="", tool_calls=[ToolCall("c1", "read_file", {"path": "src/m3.py"})],
                          stop_reason="tool_use"),
            AssistantTurn(text="m3 的版本是 3.0。", stop_reason="end_turn"),
        )
        d = Delegator(llm, PARENT_TOOLS)
        report = d.run(SubagentSpec("查 m3 的版本", ("read_file",)))
        self.assertTrue(report.ok)
        self.assertEqual(report.tool_calls, 1)
        result = report.as_tool_result()
        self.assertIn("3.0", result)
        self.assertNotIn("x = 1", result)     # 文件全文留在子代理的上下文里

    def test_turn_limit_reports_incomplete(self) -> None:
        loop_forever = [AssistantTurn(text="", tool_calls=[ToolCall(f"c{i}", "list_files", {})],
                                      stop_reason="tool_use") for i in range(10)]
        d = Delegator(lambda: FakeLLM(loop_forever), PARENT_TOOLS)
        report = d.run(SubagentSpec("无尽任务", ("list_files",), max_turns=3))
        self.assertFalse(report.ok)
        self.assertEqual(report.stop, "max_turns")
        self.assertIn("未完成", report.as_tool_result())

    def test_long_summary_truncated(self) -> None:
        d = Delegator(scripted(AssistantTurn(text="长" * 10000)), PARENT_TOOLS, max_summary_chars=100)
        report = d.run(SubagentSpec("t", ("read_file",)))
        self.assertIn("已截断", report.summary)

    def test_fork_carries_background(self) -> None:
        seen = {}

        def capture(messages):
            seen["first"] = messages[0]["content"]
            return AssistantTurn(text="ok")

        d = Delegator(lambda: FakeLLM([capture]), PARENT_TOOLS)
        d.run(SubagentSpec("继续", ("read_file",), mode="fork"),
              parent_messages=[{"role": "user", "content": "我们在迁移到 pytest"}])
        self.assertIn("迁移到 pytest", seen["first"])

    def test_delegate_tool_end_to_end(self) -> None:
        d = Delegator(scripted(AssistantTurn(text="共 20 个文件。")), PARENT_TOOLS)
        tool = delegate_tool(d, ("list_files",))
        self.assertIn("共 20 个文件", tool({"task": "数一下文件"}))
        self.assertIn("非空", tool({"task": " "}))


class ProgramTests(unittest.TestCase):
    def test_batch_reads_return_only_summary(self) -> None:
        program = (
            "names = call('list_files').split('\\n')\n"
            "for n in names:\n"
            "    text = call('read_file', path=n)\n"
            "    line = [l for l in text.split('\\n') if l.startswith('VERSION')][0]\n"
            "    emit(n, line)\n"
        )
        result = run_program(program, {"list_files": list_files, "read_file": read_file})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.calls), 21)
        self.assertEqual(result.output.count("VERSION"), 20)
        # 对比：逐个工具调用时，20 份完整文件都会进上下文
        per_call = context_cost([read_file({"path": p}) for p in FILES])
        self.assertLess(len(result.output) * 10, per_call)

    def test_unauthorized_tool_rejected(self) -> None:
        result = run_program("call('write_file', path='a')", {"read_file": read_file})
        self.assertFalse(result.ok)
        self.assertIn("未授权", result.error)

    def test_call_budget(self) -> None:
        result = run_program("for i in range(100):\n    call('list_files')", {"list_files": list_files}, max_calls=5)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.calls), 5)

    def test_import_rejected(self) -> None:
        with self.assertRaises(ProgramRejected):
            check_program("import os\nos.remove('x')")

    def test_function_def_and_while_rejected(self) -> None:
        with self.assertRaises(ProgramRejected):
            check_program("def f():\n    pass")
        with self.assertRaises(ProgramRejected):
            check_program("while True:\n    pass")

    def test_dunder_escape_rejected(self) -> None:
        with self.assertRaises(ProgramRejected):
            check_program("x = ().__class__")

    def test_program_bug_is_reported_with_partial_output(self) -> None:
        result = run_program("emit('before')\nx = 1 / 0", {})
        self.assertFalse(result.ok)
        self.assertIn("ZeroDivisionError", result.error)
        self.assertIn("before", result.output)

    def test_output_capped(self) -> None:
        result = run_program("for i in range(1000):\n    emit('line', i)", {}, max_output=200)
        self.assertIn("已截断", result.output)


if __name__ == "__main__":
    unittest.main()
