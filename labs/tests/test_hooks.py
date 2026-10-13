"""21 Hooks 的测试。"""
from __future__ import annotations

import unittest

from harness.hooks import (
    POST_TOOL,
    PRE_TOOL,
    SESSION_START,
    STOP,
    USER_PROMPT,
    HookBus,
    HookError,
    HookResult,
    command_hook,
    python_command,
    run_tool_with_hooks,
)


class RegistrationTests(unittest.TestCase):
    def test_unknown_event_rejected(self) -> None:
        with self.assertRaises(HookError):
            HookBus().on("before_lunch", "x", lambda e, p: None)

    def test_duplicate_name_on_same_event_rejected(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "guard", lambda e, p: None)
        with self.assertRaises(HookError):
            bus.on(PRE_TOOL, "guard", lambda e, p: None)

    def test_block_requires_reason(self) -> None:
        with self.assertRaises(HookError):
            HookResult(block=True)
        with self.assertRaises(HookError):
            HookResult(keep_going=True)


class DispatchTests(unittest.TestCase):
    def test_modifications_chain_in_order(self) -> None:
        bus = HookBus()
        bus.on(USER_PROMPT, "a", lambda e, p: HookResult(payload={**p, "text": p["text"] + " A"}))
        bus.on(USER_PROMPT, "b", lambda e, p: HookResult(payload={**p, "text": p["text"] + " B"}))
        self.assertEqual(bus.dispatch(USER_PROMPT, {"text": "hi"}).payload["text"], "hi A B")

    def test_first_block_wins_and_stops_chain(self) -> None:
        calls = []
        bus = HookBus()
        bus.on(PRE_TOOL, "deny", lambda e, p: HookResult(block=True, reason="不许部署"))
        bus.on(PRE_TOOL, "later", lambda e, p: calls.append("later"))
        outcome = bus.dispatch(PRE_TOOL, {"tool_name": "deploy"})
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.blocked_by, "deny")
        self.assertEqual(calls, [])

    def test_post_tool_cannot_block(self) -> None:
        bus = HookBus()
        bus.on(POST_TOOL, "late-veto", lambda e, p: HookResult(block=True, reason="太晚了"))
        outcome = bus.dispatch(POST_TOOL, {"tool_name": "write_file", "tool_output": "ok"})
        self.assertFalse(outcome.blocked)
        self.assertTrue(any("不能拦截" in msg for msg in outcome.ignored))

    def test_matcher_limits_to_tools(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "edits-only", lambda e, p: HookResult(block=True, reason="只读模式"), matcher="edit_*")
        self.assertFalse(bus.dispatch(PRE_TOOL, {"tool_name": "read_file"}).blocked)
        self.assertTrue(bus.dispatch(PRE_TOOL, {"tool_name": "edit_file"}).blocked)

    def test_pre_tool_error_fails_closed(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "broken-guard", lambda e, p: 1 / 0)
        outcome = bus.dispatch(PRE_TOOL, {"tool_name": "rm"})
        self.assertTrue(outcome.blocked)
        self.assertIn("自身出错", outcome.reason)

    def test_observer_error_is_ignored(self) -> None:
        bus = HookBus()
        bus.on(POST_TOOL, "broken-logger", lambda e, p: 1 / 0)
        outcome = bus.dispatch(POST_TOOL, {"tool_name": "x", "tool_output": "ok"})
        self.assertFalse(outcome.blocked)
        self.assertEqual(len(outcome.errors), 1)

    def test_session_start_context(self) -> None:
        bus = HookBus()
        bus.on(SESSION_START, "branch", lambda e, p: HookResult(context="当前分支：feature/x"))
        self.assertEqual(bus.dispatch(SESSION_START, {}).contexts, ["当前分支：feature/x"])


class StopTests(unittest.TestCase):
    def test_continue_is_capped(self) -> None:
        bus = HookBus(max_forced_continues=2)
        bus.on(STOP, "never-done", lambda e, p: HookResult(keep_going=True, reason="测试还没过"))
        results = [bus.dispatch(STOP, {}).keep_going for _ in range(4)]
        self.assertEqual(results, [True, True, False, False])

    def test_reset_turn_restores_budget(self) -> None:
        bus = HookBus(max_forced_continues=1)
        bus.on(STOP, "once", lambda e, p: HookResult(keep_going=True, reason="再检查一次"))
        self.assertTrue(bus.dispatch(STOP, {}).keep_going)
        self.assertFalse(bus.dispatch(STOP, {}).keep_going)
        bus.reset_turn()
        self.assertTrue(bus.dispatch(STOP, {}).keep_going)

    def test_only_stop_can_continue(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "confused", lambda e, p: HookResult(keep_going=True, reason="继续"))
        outcome = bus.dispatch(PRE_TOOL, {"tool_name": "x"})
        self.assertFalse(outcome.keep_going)
        self.assertTrue(outcome.ignored)


class ToolIntegrationTests(unittest.TestCase):
    def test_pre_modifies_args_and_post_appends_context(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "normalize", lambda e, p: HookResult(
            payload={**p, "tool_input": {**p["tool_input"], "path": p["tool_input"]["path"].strip()}}))
        bus.on(POST_TOOL, "formatter", lambda e, p: HookResult(context="black: 1 file reformatted"),)
        seen = {}

        def execute(args):
            seen.update(args)
            return "written"

        output, is_error, contexts = run_tool_with_hooks(bus, "write_file", {"path": "  a.py "}, execute)
        self.assertEqual(seen["path"], "a.py")
        self.assertEqual(output, "written")
        self.assertFalse(is_error)
        self.assertEqual(contexts, ["black: 1 file reformatted"])

    def test_blocked_tool_is_not_executed(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "freeze", lambda e, p: HookResult(block=True, reason="发布冻结期，禁止部署"), matcher="deploy")
        calls = []
        output, is_error, _ = run_tool_with_hooks(bus, "deploy", {}, lambda a: calls.append(a))
        self.assertTrue(is_error)
        self.assertIn("发布冻结期", output)
        self.assertEqual(calls, [])


class CommandHookTests(unittest.TestCase):
    def test_exit_2_blocks_with_stderr(self) -> None:
        hook = command_hook(python_command("import sys; sys.stderr.write('值班群未确认'); sys.exit(2)"))
        result = hook(PRE_TOOL, {"tool_name": "deploy"})
        self.assertTrue(result.block)
        self.assertEqual(result.reason, "值班群未确认")

    def test_json_stdout_becomes_context(self) -> None:
        code = "import sys, json; d = json.load(sys.stdin); print(json.dumps({'context': 'saw ' + d['tool_name']}))"
        result = command_hook(python_command(code))(POST_TOOL, {"tool_name": "edit_file"})
        self.assertEqual(result.context, "saw edit_file")

    def test_other_exit_code_is_error(self) -> None:
        bus = HookBus()
        bus.on(PRE_TOOL, "crashy", command_hook(python_command("import sys; sys.exit(1)")))
        self.assertTrue(bus.dispatch(PRE_TOOL, {"tool_name": "x"}).blocked)   # 失败关闭


if __name__ == "__main__":
    unittest.main()
