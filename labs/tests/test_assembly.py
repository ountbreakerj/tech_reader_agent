"""25 组装 mini-harness 与长任务连续性的测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.assembly import (
    DECISIONS_FILE,
    HANDOFF_FILE,
    PROGRESS_FILE,
    Handoff,
    HarnessConfig,
    MiniHarness,
    read_handoff,
    record_decision,
    resume_prompt,
    write_handoff,
)
from harness.circuit import BreakerConfig
from harness.hooks import POST_TOOL, PRE_TOOL, STOP, HookBus, HookResult
from harness.llm import FakeLLM
from harness.messages import AssistantTurn, ToolCall
from harness.permissions import PermissionEngine, default_rules
from harness.retry import Failure, RetryPolicy
from harness.tools import ToolRegistry, ToolSpec


def make_registry(root: str) -> ToolRegistry:
    files: dict[str, str] = {"a.py": "print('a')"}
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="read_file", description="读文件",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=lambda a: files.get(a["path"], f"Error: 没有 {a['path']}"), read_only=True, parallel_safe=True))
    registry.register(ToolSpec(
        name="write_file", description="写文件",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]},
        handler=lambda a: files.__setitem__(a["path"], a["content"]) or "written"))
    return registry


def call(name, **args):
    return AssistantTurn(text="", tool_calls=[ToolCall(f"c-{name}", name, args)], stop_reason="tool_use")


def done(text="完成"):
    return AssistantTurn(text=text, stop_reason="end_turn")


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def harness(self, script, **kwargs) -> MiniHarness:
        return MiniHarness(FakeLLM(script), make_registry(self.root), sleep=lambda s: None, **kwargs)

    def test_happy_path_logs_and_traces(self) -> None:
        h = self.harness([call("read_file", path="a.py"), done("a.py 打印 a")])
        report = h.run("看看 a.py")
        self.assertEqual(report.stop, "completed")
        self.assertEqual(report.turns, 2)
        kinds = [e.kind for e in h.log.events]
        self.assertEqual(kinds, ["user/message", "assistant/message", "tool/result", "assistant/message"])
        self.assertIn("tool.read_file", [s.name for s in h.tracer.spans])

    def test_permission_denies_path_escape(self) -> None:
        engine = PermissionEngine(default_rules(self.root))
        h = self.harness([call("read_file", path="../../etc/passwd"), done()], permissions=engine)
        report = h.run("读密码文件")
        tool_result = report.messages[2]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertIn("越出了工作区", tool_result["content"])

    def test_ask_without_channel_fails_closed(self) -> None:
        engine = PermissionEngine(default_rules(self.root))
        h = self.harness([call("write_file", path="b.py", content="x"), done()], permissions=engine)
        report = h.run("写个文件")
        self.assertIn("无审批通道", report.messages[2]["content"][0]["content"])

    def test_hooks_wrap_tools_and_append_context(self) -> None:
        bus = HookBus()
        bus.on(POST_TOOL, "formatter", lambda e, p: HookResult(context="formatter: ok"), matcher="write_file")
        h = self.harness([call("write_file", path="b.py", content="x"), done()], hooks=bus)
        report = h.run("写个文件")
        content = report.messages[2]["content"]
        self.assertEqual(content[0]["type"], "tool_result")
        self.assertEqual(content[-1], {"type": "text", "text": "formatter: ok"})

    def test_pre_hook_block_prevents_execution(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "freeze", lambda e, p: HookResult(block=True, reason="冻结期"), matcher="write_file")
        h = self.harness([call("write_file", path="b.py", content="x"), done()], hooks=bus)
        report = h.run("写")
        self.assertIn("冻结期", report.messages[2]["content"][0]["content"])

    def test_stop_hook_can_continue_once(self) -> None:
        bus = HookBus(max_forced_continues=1)
        bus.on(STOP, "verify", lambda e, p: HookResult(keep_going=True, reason="先跑测试再结束"))
        h = self.harness([done("第一次说完成"), done("跑完了")], hooks=bus)
        report = h.run("做事")
        self.assertEqual(report.stop, "completed")
        self.assertEqual(report.turns, 2)
        self.assertIn("先跑测试", report.notes[0])

    def test_transient_model_failure_is_retried(self) -> None:
        state = {"n": 0}

        def flaky(messages):
            state["n"] += 1
            if state["n"] == 1:
                raise Failure("server", "503")
            return done("恢复了")

        h = self.harness([flaky, flaky])
        report = h.run("做事")
        self.assertEqual(report.stop, "completed")
        self.assertEqual([e.kind for e in h.log.events].count("llm/retry"), 1)

    def test_breaker_opens_after_repeated_failures(self) -> None:
        def down(messages):
            raise Failure("server", "503")

        cfg = HarnessConfig(retry=RetryPolicy(max_retries=0, initial_delay=0.01, max_delay=0.02),
                            breaker=BreakerConfig(failure_threshold=2, cooldown=60))
        h = self.harness([down] * 5, config=cfg)
        self.assertEqual(h.run("一").stop, "llm_error")
        self.assertEqual(h.run("二").stop, "llm_error")
        self.assertEqual(h.run("三").stop, "circuit_open")    # 第三次连模型都不调了

    def test_error_budget(self) -> None:
        script = [call("read_file", path=f"missing{i}.py") for i in range(5)]
        h = self.harness(script, config=HarnessConfig(max_consecutive_errors=2))
        # registry.call 对不存在的文件返回 "Error: 没有 ..."，计入连续错误
        self.assertEqual(h.run("读一堆不存在的文件").stop, "error_budget")


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_roundtrip_and_progress_file(self) -> None:
        handoff = Handoff(goal="把 50 个测试迁移到 pytest", done=["tests/a.py", "tests/b.py"],
                          next_steps=["tests/c.py"], verify=["py -m pytest tests/a.py tests/b.py"])
        problems = write_handoff(self.root, handoff, now="2026-10-13 18:00")
        self.assertEqual(problems, [])
        loaded = read_handoff(self.root)
        self.assertEqual(loaded.next_steps, ["tests/c.py"])
        progress = (Path(self.root) / PROGRESS_FILE).read_text(encoding="utf-8")
        self.assertIn("- [x] tests/a.py", progress)
        self.assertIn("- [ ] tests/c.py", progress)

    def test_validation_flags_missing_fields(self) -> None:
        problems = Handoff(goal="x", done=[], next_steps=[]).validate()
        self.assertTrue(any("下一步" in p for p in problems))
        self.assertTrue(any("验证命令" in p for p in problems))

    def test_handoff_written_atomically(self) -> None:
        write_handoff(self.root, Handoff("g", [], ["n"], verify=["v"]))
        files = [p.name for p in (Path(self.root) / ".harness").iterdir()]
        self.assertEqual(files, ["handoff.json"])     # 没有残留的 .tmp
        json.loads((Path(self.root) / HANDOFF_FILE).read_text(encoding="utf-8"))

    def test_resume_prompt(self) -> None:
        self.assertIn("新任务", resume_prompt(None))
        text = resume_prompt(Handoff("g", ["a"], ["b"], verify=["pytest"], updated_at="t"))
        self.assertIn("开工前先运行", text)
        self.assertIn("下一步：b", text)

    def test_decisions_append(self) -> None:
        record_decision(self.root, "测试框架", "用 pytest", "团队已有 fixture", now="2026-10-13")
        record_decision(self.root, "目录", "tests/ 平铺", "和 CI 配置一致", now="2026-10-13")
        text = (Path(self.root) / DECISIONS_FILE).read_text(encoding="utf-8")
        self.assertEqual(text.count("## 2026-10-13"), 2)
        self.assertIn("原因：团队已有 fixture", text)


if __name__ == "__main__":
    unittest.main()
