"""harness.loop 的行为测试：每一种停止原因、工具结果合并、错误回填、截断处理。"""
from __future__ import annotations

import unittest

from harness.llm import FakeLLM
from harness.loop import (
    CONTINUE_PROMPT,
    INTERRUPTED_RESULT,
    LoopConfig,
    LoopState,
    next_step,
    run_agent,
)
from harness.messages import AssistantTurn, ToolCall

SYSTEM = "You are a test agent."
SCHEMAS = [{"name": "echo", "description": "echo", "input_schema": {"type": "object"}}]


def turn(text: str = "", calls: list[ToolCall] | None = None, stop: str | None = None) -> AssistantTurn:
    calls = calls or []
    return AssistantTurn(
        text=text,
        tool_calls=calls,
        stop_reason=stop or ("tool_use" if calls else "end_turn"),
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def call(cid: str, name: str = "echo", **args: object) -> ToolCall:
    return ToolCall(id=cid, name=name, args=dict(args))


def blocks(message: dict) -> list[dict]:
    content = message["content"]
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def of_type(message: dict, kind: str) -> list[dict]:
    return [b for b in blocks(message) if b.get("type") == kind]


def result_text(block: dict) -> str:
    content = block.get("content", "")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content)
    return str(content)


def all_tool_results(messages: list[dict]) -> list[dict]:
    return [b for m in messages if m["role"] == "user" for b in of_type(m, "tool_result")]


def echo(args: dict) -> str:
    return f"echo:{args.get('text', '')}"


def boom(args: dict) -> str:
    raise RuntimeError("disk on fire")


class HistoryInvariant(unittest.TestCase):
    """每个返回点上的历史都必须能直接再发给 API。"""

    def assertHistoryValid(self, messages: list[dict]) -> None:
        self.assertEqual(messages[0]["role"], "user")
        for index, message in enumerate(messages):
            if message["role"] != "assistant":
                continue
            self.assertTrue(blocks(message), "assistant 消息不能为空")
            use_ids = [b["id"] for b in of_type(message, "tool_use")]
            if not use_ids:
                continue
            self.assertLess(index + 1, len(messages), "tool_use 之后必须紧跟结果消息")
            follower = messages[index + 1]
            self.assertEqual(follower["role"], "user")
            result_ids = [b["tool_use_id"] for b in of_type(follower, "tool_result")]
            self.assertEqual(result_ids, use_ids, "结果要齐全、同序、在同一条消息里")


class StopReasonTests(HistoryInvariant):
    def test_completed_when_no_tool_calls(self) -> None:
        llm = FakeLLM([turn("all done")])
        result = run_agent(llm, SYSTEM, "hi", {"echo": echo}, SCHEMAS)
        self.assertEqual(result.stop, "completed")
        self.assertEqual(result.turns, 1)
        self.assertEqual(result.final_text, "all done")
        self.assertEqual([m["role"] for m in result.messages], ["user", "assistant"])
        self.assertHistoryValid(result.messages)

    def test_tool_round_trip_then_completed(self) -> None:
        llm = FakeLLM([turn("calling", [call("c1", text="x")]), turn("the tool said echo:x")])
        result = run_agent(llm, SYSTEM, "go", {"echo": echo}, SCHEMAS)
        self.assertEqual(result.stop, "completed")
        self.assertEqual(result.turns, 2)
        self.assertEqual([m["role"] for m in result.messages], ["user", "assistant", "user", "assistant"])
        [only] = all_tool_results(result.messages)
        self.assertEqual(result_text(only), "echo:x")
        self.assertFalse(only.get("is_error", False))
        self.assertEqual(result.usage, {"input_tokens": 20, "output_tokens": 10})
        self.assertHistoryValid(result.messages)

    def test_max_turns_stops_with_complete_history(self) -> None:
        script = [turn("again", [call(f"c{i}", text=str(i))]) for i in range(10)]
        result = run_agent(FakeLLM(script), SYSTEM, "loop", {"echo": echo}, SCHEMAS, LoopConfig(max_turns=3))
        self.assertEqual(result.stop, "max_turns")
        self.assertEqual(result.turns, 3)
        # 停在采样之前：最后一条是工具结果，历史完整，可以直接续跑
        self.assertEqual(result.messages[-1]["role"], "user")
        self.assertEqual(len(all_tool_results(result.messages)), 3)
        self.assertHistoryValid(result.messages)

    def test_refusal_skips_that_turns_tools(self) -> None:
        ran: list[dict] = []
        llm = FakeLLM([turn("I can't help with that", [call("c1", text="x")], stop="refusal")])
        result = run_agent(llm, SYSTEM, "bad", {"echo": lambda a: ran.append(a) or "ran"}, SCHEMAS)
        self.assertEqual(result.stop, "refusal")
        self.assertEqual(ran, [])
        self.assertEqual(all_tool_results(result.messages), [])
        self.assertHistoryValid(result.messages)

    def test_error_budget_counts_consecutive_failures(self) -> None:
        llm = FakeLLM([turn("", [call("c1", "boom"), call("c2", "boom")]), turn("never reached")])
        config = LoopConfig(max_consecutive_errors=2)
        result = run_agent(llm, SYSTEM, "go", {"boom": boom}, SCHEMAS, config)
        self.assertEqual(result.stop, "error_budget")
        self.assertEqual(result.turns, 1)
        self.assertHistoryValid(result.messages)

    def test_success_resets_error_streak(self) -> None:
        llm = FakeLLM([
            turn("", [call("c1", "boom")]),
            turn("", [call("c2", "echo", text="ok")]),
            turn("", [call("c3", "boom")]),
            turn("finished"),
        ])
        config = LoopConfig(max_consecutive_errors=2)
        result = run_agent(llm, SYSTEM, "go", {"boom": boom, "echo": echo}, SCHEMAS, config)
        self.assertEqual(result.stop, "completed")
        self.assertEqual(result.turns, 4)

    def test_interrupted_before_first_call(self) -> None:
        result = run_agent(FakeLLM([turn("unused")]), SYSTEM, "go", {}, SCHEMAS, should_stop=lambda: True)
        self.assertEqual(result.stop, "interrupted")
        self.assertEqual(result.turns, 0)
        self.assertHistoryValid(result.messages)

    def test_interrupt_mid_batch_still_answers_every_tool_use(self) -> None:
        flag = {"stop": False}

        def first(args: dict) -> str:
            flag["stop"] = True  # 模拟用户在第一个工具执行期间按了中断
            return "first ran"

        ran: list[str] = []
        tools = {"first": first, "echo": lambda a: ran.append("echo") or "ran"}
        llm = FakeLLM([turn("", [call("c1", "first"), call("c2", "echo"), call("c3", "echo")])])
        result = run_agent(llm, SYSTEM, "go", tools, SCHEMAS, should_stop=lambda: flag["stop"])
        self.assertEqual(result.stop, "interrupted")
        self.assertEqual(ran, [])
        r1, r2, r3 = all_tool_results(result.messages)
        self.assertEqual(result_text(r1), "first ran")
        self.assertEqual([result_text(r2), result_text(r3)], [INTERRUPTED_RESULT] * 2)
        self.assertTrue(r2.get("is_error") and r3.get("is_error"))
        self.assertHistoryValid(result.messages)

    def test_llm_error_is_structured(self) -> None:
        llm = FakeLLM([turn("", [call("c1", text="x")])])  # 第二次采样时脚本耗尽
        result = run_agent(llm, SYSTEM, "go", {"echo": echo}, SCHEMAS)
        self.assertEqual(result.stop, "llm_error")
        self.assertEqual(result.turns, 1)
        self.assertIn("ScriptExhausted", result.error or "")
        self.assertHistoryValid(result.messages)


class ToolBatchTests(HistoryInvariant):
    def test_parallel_results_share_one_user_message_in_model_order(self) -> None:
        calls = [call("a", text="1"), call("b", text="2"), call("c", text="3")]
        llm = FakeLLM([turn("three at once", calls), turn("done")])
        result = run_agent(llm, SYSTEM, "go", {"echo": echo}, SCHEMAS)
        result_messages = [m for m in result.messages if of_type(m, "tool_result")]
        self.assertEqual(len(result_messages), 1)
        ids = [b["tool_use_id"] for b in of_type(result_messages[0], "tool_result")]
        self.assertEqual(ids, ["a", "b", "c"])
        self.assertHistoryValid(result.messages)

    def test_unknown_tool_becomes_error_result(self) -> None:
        llm = FakeLLM([turn("", [call("c1", "no_such_tool")]), turn("sorry, retrying differently")])
        result = run_agent(llm, SYSTEM, "go", {"echo": echo}, SCHEMAS)
        self.assertEqual(result.stop, "completed")
        [only] = all_tool_results(result.messages)
        self.assertTrue(only.get("is_error"))
        self.assertIn("unknown tool", result_text(only))

    def test_tool_exception_becomes_error_result(self) -> None:
        llm = FakeLLM([turn("", [call("c1", "boom"), call("c2", text="ok")]), turn("recovered")])
        result = run_agent(llm, SYSTEM, "go", {"boom": boom, "echo": echo}, SCHEMAS)
        self.assertEqual(result.stop, "completed")
        failed, ok = all_tool_results(result.messages)
        self.assertTrue(failed.get("is_error"))
        self.assertIn("RuntimeError: disk on fire", result_text(failed))
        self.assertEqual(result_text(ok), "echo:ok")  # 兄弟调用不受影响
        self.assertHistoryValid(result.messages)


class MaxTokensTests(HistoryInvariant):
    def test_truncated_tool_call_is_dropped_not_executed(self) -> None:
        ran: list[dict] = []
        half = turn("partial text", [call("c1", text="unfinish")], stop="max_tokens")
        result = run_agent(FakeLLM([half]), SYSTEM, "go", {"echo": lambda a: ran.append(a) or "ran"}, SCHEMAS)
        self.assertEqual(result.stop, "max_tokens")
        self.assertTrue(result.truncated)
        self.assertEqual(ran, [])
        # 安全的文本前缀保留，半截 tool_use 不入史
        self.assertEqual(of_type(result.messages[-1], "tool_use"), [])
        self.assertEqual(of_type(result.messages[-1], "text")[0]["text"], "partial text")
        self.assertHistoryValid(result.messages)

    def test_empty_truncated_turn_leaves_no_empty_assistant(self) -> None:
        half = turn("", [call("c1", text="x")], stop="max_tokens")
        result = run_agent(FakeLLM([half]), SYSTEM, "go", {"echo": echo}, SCHEMAS)
        self.assertEqual(result.stop, "max_tokens")
        self.assertEqual([m["role"] for m in result.messages], ["user"])

    def test_raw_blocks_from_adapter_are_filtered_too(self) -> None:
        # 真实适配器会把厂商原始块放进 turn.blocks，原样写回；截断时也必须把其中的 tool_use 去掉
        half = turn("kept", [call("c1", text="x")], stop="max_tokens")
        half.blocks = [
            {"type": "text", "text": "kept"},
            {"type": "tool_use", "id": "c1", "name": "echo", "input": {"text": "x"}},
        ]
        result = run_agent(FakeLLM([half]), SYSTEM, "go", {"echo": echo}, SCHEMAS)
        self.assertEqual(result.messages[-1]["content"], [{"type": "text", "text": "kept"}])
        self.assertHistoryValid(result.messages)

    def test_continuation_is_sticky(self) -> None:
        llm = FakeLLM([turn("first half", stop="max_tokens"), turn("second half")])
        result = run_agent(llm, SYSTEM, "go", {}, SCHEMAS, LoopConfig(max_continuations=1))
        self.assertEqual(result.stop, "completed")
        self.assertTrue(result.truncated)  # 后面的正常结束不会把“截断过”抹掉
        self.assertEqual(result.turns, 2)
        texts = [b["text"] for m in result.messages if m["role"] == "user" for b in of_type(m, "text")]
        self.assertIn(CONTINUE_PROMPT, texts)
        self.assertHistoryValid(result.messages)

    def test_continuation_budget_runs_out(self) -> None:
        llm = FakeLLM([turn("a", stop="max_tokens"), turn("b", stop="max_tokens"), turn("c")])
        result = run_agent(llm, SYSTEM, "go", {}, SCHEMAS, LoopConfig(max_continuations=1))
        self.assertEqual(result.stop, "max_tokens")
        self.assertEqual(result.turns, 2)


class NextStepTests(unittest.TestCase):
    def state(self, **config: int) -> LoopState:
        return LoopState(LoopConfig(**config))

    def test_decision_table(self) -> None:
        tool = [call("c1")]
        cases = [
            (turn("x"), self.state(), "completed"),
            (turn("x", tool), self.state(), "run_tools"),
            (turn("x", tool, stop="max_tokens"), self.state(), "max_tokens"),
            (turn("x", tool, stop="max_tokens"), self.state(max_continuations=1), "continue"),
            (turn("x", tool, stop="refusal"), self.state(), "refusal"),
            (turn("x", stop="stop_sequence"), self.state(), "completed"),
        ]
        for sampled, state, expected in cases:
            with self.subTest(stop=sampled.stop_reason, calls=len(sampled.tool_calls), expected=expected):
                self.assertEqual(next_step(sampled, state), expected)

    def test_continuation_budget_is_read_not_spent(self) -> None:
        state = self.state(max_continuations=1)
        state.continuations = 1
        truncated = turn("x", stop="max_tokens")
        self.assertEqual(next_step(truncated, state), "max_tokens")
        self.assertEqual(state.continuations, 1)


if __name__ == "__main__":
    unittest.main()
