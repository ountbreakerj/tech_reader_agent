"""最小消息模型。

历史统一用 Anthropic 风格的 dict 列表：{"role": ..., "content": str | list[block]}，
block 只用 text / tool_use / tool_result 三种。选它是因为 tool_use 与 tool_result
天然按 id 成对；OpenAI 格式只在适配器发请求时临时转换，不进入历史。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """模型想调用的一次工具；id 用来把结果配对回去。"""

    id: str
    name: str
    args: dict[str, Any]


@dataclass
class AssistantTurn:
    """一次模型回复，已抹平厂商差异。循环只认这个类型。"""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, Any] = field(default_factory=dict)
    # 厂商返回的原始 content 块（例如 thinking）。非空时原样写回历史，适配器不改写。
    blocks: list[dict[str, Any]] = field(default_factory=list)


def user(text: str) -> dict[str, Any]:
    """一条纯文本用户消息。"""
    return {"role": "user", "content": text}


def assistant_from_turn(turn: AssistantTurn) -> dict[str, Any]:
    """把模型回复写回历史；有原始块就用原始块，保证下次请求与厂商当初返回的一致。"""
    if turn.blocks:
        return {"role": "assistant", "content": copy.deepcopy(turn.blocks)}
    content: list[dict[str, Any]] = []
    if turn.text:
        content.append({"type": "text", "text": turn.text})
    for call in turn.tool_calls:
        content.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": copy.deepcopy(call.args)}
        )
    return {"role": "assistant", "content": content}


def tool_results(results: list[tuple[str, str, bool]]) -> dict[str, Any]:
    """一轮的全部工具结果放进同一条 user 消息：每个 tool_use 都要有对应的 tool_result。"""
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": is_error}
            for call_id, output, is_error in results
        ],
    }
