"""Tech Reader Harness 模块的配套实验库（只用标准库）。

各章的代码分模块存放，直接按模块导入最清楚：

    from harness.loop import run_agent            # 01 Agent Loop
    from harness.tools import ToolRegistry        # 02 工具定义与注册
    from harness.tool_runtime import run_pipeline # 03 执行管线与并发
    from harness.session_log import SessionLog    # 04 会话日志与回放

tools 和 tool_runtime 各有一个 ToolSpec（前者描述“工具是什么”，后者描述“怎么调度”），
所以包级只导出不会重名的入口，ToolSpec 请从各自模块导入。
"""

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
from .session_log import LogCorrupt, Replayer, SessionLog
from .tool_runtime import CallResult, plan_batches, run_pipeline
from .tools import HookDecision, ToolError, ToolRegistry, default_registry

__all__ = [
    "LLM",
    "AnthropicLLM",
    "AssistantTurn",
    "CallResult",
    "FakeLLM",
    "HookDecision",
    "LLMHTTPError",
    "LogCorrupt",
    "MissingAPIKey",
    "OpenAICompatLLM",
    "Replayer",
    "ScriptExhausted",
    "SessionLog",
    "ToolCall",
    "ToolError",
    "ToolRegistry",
    "assistant_from_turn",
    "default_registry",
    "from_env",
    "plan_batches",
    "run_pipeline",
    "tool_results",
    "user",
]
