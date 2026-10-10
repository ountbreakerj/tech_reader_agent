"""labs/harness/llm.py 的测试。全部离线：真实适配器的 _post 被替换，绝不发网络请求。"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest import mock

from harness.llm import (
    AnthropicLLM,
    FakeLLM,
    LLMHTTPError,
    MissingAPIKey,
    OpenAICompatLLM,
    ScriptExhausted,
    from_env,
    parse_openai_response,
    to_openai_messages,
)
from harness.messages import AssistantTurn, ToolCall, assistant_from_turn, tool_results, user

TOOLS = [
    {
        "name": "run_tests",
        "description": "运行测试",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


class Recorder:
    """替身 _post：记录请求，返回预设响应。"""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((url, headers, json.loads(json.dumps(body))))
        return self.response


class FakeLLMTest(unittest.TestCase):
    def test_returns_script_in_order(self) -> None:
        llm = FakeLLM([AssistantTurn(text="一"), AssistantTurn(text="二")])
        self.assertEqual(llm.complete("", [user("a")], []).text, "一")
        self.assertEqual(llm.complete("", [user("b")], []).text, "二")

    def test_callable_item_sees_messages(self) -> None:
        llm = FakeLLM([lambda msgs: AssistantTurn(text=f"收到 {len(msgs)} 条")])
        self.assertEqual(llm.complete("", [user("a"), user("b")], []).text, "收到 2 条")

    def test_script_exhausted(self) -> None:
        llm = FakeLLM([AssistantTurn(text="only")])
        llm.complete("", [user("a")], [])
        with self.assertRaises(ScriptExhausted):
            llm.complete("", [user("a")], [])

    def test_calls_are_snapshots(self) -> None:
        llm = FakeLLM([AssistantTurn(text="x"), AssistantTurn(text="y")])
        history = [user("修复失败的测试")]
        llm.complete("", history, [])
        history.append({"role": "assistant", "content": "x"})
        llm.complete("", history, [])
        self.assertEqual(len(llm.calls[0]), 1)  # 后来的 append 不影响第一次的记录
        self.assertEqual(len(llm.calls[1]), 2)


class MessagesTest(unittest.TestCase):
    def test_assistant_from_turn_builds_blocks(self) -> None:
        turn = AssistantTurn(text="先跑测试", tool_calls=[ToolCall("t1", "run_tests", {"path": "tests"})])
        message = assistant_from_turn(turn)
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"][0], {"type": "text", "text": "先跑测试"})
        self.assertEqual(message["content"][1]["type"], "tool_use")
        self.assertEqual(message["content"][1]["input"], {"path": "tests"})

    def test_assistant_from_turn_prefers_raw_blocks(self) -> None:
        raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "好"}]
        message = assistant_from_turn(AssistantTurn(text="好", blocks=raw))
        self.assertEqual(message["content"], raw)

    def test_tool_results_single_message(self) -> None:
        message = tool_results([("t1", "ok", False), ("t2", "boom", True)])
        self.assertEqual(message["role"], "user")
        self.assertEqual([b["tool_use_id"] for b in message["content"]], ["t1", "t2"])
        self.assertTrue(message["content"][1]["is_error"])


class AnthropicLLMTest(unittest.TestCase):
    RESPONSE = {
        "content": [
            {"type": "thinking", "thinking": "", "signature": "abc"},
            {"type": "text", "text": "我先运行测试。"},
            {"type": "tool_use", "id": "toolu_1", "name": "run_tests", "input": {"path": "tests/test_x.py"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 12, "output_tokens": 34},
    }

    def make(self, response: dict[str, Any] | None = None) -> tuple[AnthropicLLM, Recorder]:
        llm = AnthropicLLM(api_key="test-key")
        recorder = Recorder(response or self.RESPONSE)
        llm._post = recorder  # type: ignore[method-assign]
        return llm, recorder

    def test_request_shape(self) -> None:
        llm, recorder = self.make()
        llm.complete("你是编码助手", [user("修复失败的测试")], TOOLS)
        url, headers, body = recorder.requests[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["max_tokens"], 16000)
        self.assertEqual(body["system"], "你是编码助手")
        self.assertEqual(body["tools"], TOOLS)
        self.assertEqual(body["messages"], [user("修复失败的测试")])
        self.assertNotIn("stream", body)

    def test_response_parsing(self) -> None:
        llm, _ = self.make()
        turn = llm.complete("", [user("x")], TOOLS)
        self.assertEqual(turn.text, "我先运行测试。")
        self.assertEqual(turn.tool_calls, [ToolCall("toolu_1", "run_tests", {"path": "tests/test_x.py"})])
        self.assertEqual(turn.stop_reason, "tool_use")
        self.assertEqual(turn.usage["output_tokens"], 34)
        # 原始块（含 thinking）原样写回历史
        self.assertEqual(assistant_from_turn(turn)["content"], self.RESPONSE["content"])

    def test_stop_reason_kept_verbatim(self) -> None:
        for reason in ["end_turn", "max_tokens", "stop_sequence", "pause_turn", "refusal"]:
            llm, _ = self.make({"content": [], "stop_reason": reason, "usage": {}})
            self.assertEqual(llm.complete("", [user("x")], []).stop_reason, reason)

    def test_optional_fields_omitted(self) -> None:
        llm = AnthropicLLM(api_key="k", effort=None, fallbacks=None)
        recorder = Recorder({"content": [], "stop_reason": "end_turn"})
        llm._post = recorder  # type: ignore[method-assign]
        llm.complete("", [user("x")], [])
        _, headers, body = recorder.requests[0]
        for key in ("system", "tools", "output_config", "fallbacks"):
            self.assertNotIn(key, body)
        self.assertNotIn("anthropic-beta", headers)

    def test_missing_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingAPIKey) as ctx:
                AnthropicLLM()
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_http_error_propagates(self) -> None:
        llm = AnthropicLLM(api_key="k")

        def failing(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
            raise LLMHTTPError(429, '{"type":"error","error":{"type":"rate_limit_error"}}')

        llm._post = failing  # type: ignore[method-assign]
        with self.assertRaises(LLMHTTPError) as ctx:
            llm.complete("", [user("x")], [])
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("rate_limit_error", ctx.exception.body)


class OpenAIConversionTest(unittest.TestCase):
    HISTORY = [
        user("修复失败的测试"),
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "s"},
                {"type": "text", "text": "先看看。"},
                {"type": "tool_use", "id": "c1", "name": "run_tests", "input": {"path": "tests"}},
            ],
        },
        tool_results([("c1", "1 failed", True)]),
    ]

    def test_history_to_openai(self) -> None:
        out = to_openai_messages("sys", self.HISTORY)
        self.assertEqual(out[0], {"role": "system", "content": "sys"})
        self.assertEqual(out[1], {"role": "user", "content": "修复失败的测试"})
        assistant = out[2]
        self.assertEqual(assistant["content"], "先看看。")
        self.assertEqual(assistant["tool_calls"][0]["id"], "c1")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"path": "tests"})
        self.assertEqual(out[3], {"role": "tool", "tool_call_id": "c1", "content": "[error] 1 failed"})
        self.assertEqual(len(out), 4)  # thinking 块被丢弃，不产生额外消息

    def test_tool_results_precede_user_text(self) -> None:
        mixed = {
            "role": "user",
            "content": [
                {"type": "text", "text": "另外注意别改公共接口"},
                {"type": "tool_result", "tool_use_id": "c1", "content": "ok"},
            ],
        }
        out = to_openai_messages("", [mixed])
        self.assertEqual([m["role"] for m in out], ["tool", "user"])

    def test_response_to_turn(self) -> None:
        data = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c2", "type": "function",
                             "function": {"name": "run_tests", "arguments": "{\"path\": \"tests\"}"}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
        turn = parse_openai_response(data)
        self.assertEqual(turn.text, "")
        self.assertEqual(turn.tool_calls, [ToolCall("c2", "run_tests", {"path": "tests"})])
        self.assertEqual(turn.stop_reason, "tool_use")
        self.assertEqual(turn.usage["input_tokens"], 5)
        self.assertEqual(turn.usage["output_tokens"], 7)

    def test_finish_reason_mapping(self) -> None:
        cases = {"stop": "end_turn", "length": "max_tokens", "content_filter": "content_filter"}
        for finish, expected in cases.items():
            data = {"choices": [{"message": {"content": "x"}, "finish_reason": finish}]}
            self.assertEqual(parse_openai_response(data).stop_reason, expected)

    def test_stop_with_tool_calls_is_corrected(self) -> None:
        data = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [{"id": "c3", "function": {"name": "run_tests", "arguments": "{}"}}],
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        self.assertEqual(parse_openai_response(data).stop_reason, "tool_use")

    def test_bad_arguments_kept_raw(self) -> None:
        data = {
            "choices": [
                {
                    "message": {"tool_calls": [{"id": "c4", "function": {"name": "run_tests", "arguments": "{oops"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        }
        self.assertEqual(parse_openai_response(data).tool_calls[0].args, {"__raw_arguments__": "{oops"})


class OpenAICompatLLMTest(unittest.TestCase):
    def test_request_shape(self) -> None:
        llm = OpenAICompatLLM(api_key="ds-key")
        recorder = Recorder({"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}]})
        llm._post = recorder  # type: ignore[method-assign]
        turn = llm.complete("sys", [user("hi")], TOOLS)
        url, headers, body = recorder.requests[0]
        self.assertEqual(url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer ds-key")
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["tools"][0]["function"]["name"], "run_tests")
        self.assertEqual(body["tools"][0]["function"]["parameters"], TOOLS[0]["input_schema"])
        self.assertEqual(turn.text, "好")
        self.assertEqual(turn.stop_reason, "end_turn")

    def test_missing_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingAPIKey) as ctx:
                OpenAICompatLLM()
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))


class FromEnvTest(unittest.TestCase):
    def test_default_is_fake(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            llm = from_env()
        self.assertIsInstance(llm, FakeLLM)
        self.assertIn("HARNESS_LLM", llm.complete("", [user("x")], []).text)

    def test_anthropic_requires_key(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_LLM": "anthropic"}, clear=True):
            with self.assertRaises(MissingAPIKey):
                from_env()

    def test_deepseek_with_key(self) -> None:
        env = {"HARNESS_LLM": "deepseek", "DEEPSEEK_API_KEY": "k", "HARNESS_MODEL": "deepseek-reasoner"}
        with mock.patch.dict(os.environ, env, clear=True):
            llm = from_env()
        self.assertIsInstance(llm, OpenAICompatLLM)
        self.assertEqual(llm.model, "deepseek-reasoner")

    def test_unknown_kind(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_LLM": "gpt"}, clear=True):
            with self.assertRaises(ValueError):
                from_env()


if __name__ == "__main__":
    unittest.main()
