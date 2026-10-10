"""Tech Reader Harness 模块的配套实验库（只用标准库）。"""

from .llm import (
    LLM,
    AnthropicLLM,
    FakeLLM,
    LLMHTTPError,
    MissingAPIKey,
    OpenAICompatLLM,
    ScriptExhausted,
    from_env,
)
from .messages import AssistantTurn, ToolCall, assistant_from_turn, tool_results, user

__all__ = [
    "LLM",
    "AnthropicLLM",
    "AssistantTurn",
    "FakeLLM",
    "LLMHTTPError",
    "MissingAPIKey",
    "OpenAICompatLLM",
    "ScriptExhausted",
    "ToolCall",
    "assistant_from_turn",
    "from_env",
    "tool_results",
    "user",
]
