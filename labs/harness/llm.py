"""模型适配层：循环只依赖 LLM 协议和 AssistantTurn，厂商差异全部关在这个文件里。"""

from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol, Union

from .messages import AssistantTurn, ToolCall

Messages = list[dict[str, Any]]
ScriptItem = Union[AssistantTurn, Callable[[Messages], AssistantTurn]]


class LLM(Protocol):
    def complete(self, system: str, messages: Messages, tools: list[dict[str, Any]]) -> AssistantTurn:
        """给定系统提示、历史和工具定义，返回一次模型回复。"""
        ...


class ScriptExhausted(RuntimeError):
    """FakeLLM 的脚本用完了：通常说明循环多调了一次模型。"""


class MissingAPIKey(RuntimeError):
    def __init__(self, env_var: str) -> None:
        super().__init__(f"缺少 API key：请设置环境变量 {env_var}，或改用 HARNESS_LLM=fake")
        self.env_var = env_var


class LLMHTTPError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class FakeLLM:
    """按脚本顺序回放回复的假模型：测试可复现、零成本，能精确构造失败场景。"""

    def __init__(self, script: list[ScriptItem]) -> None:
        self._script = list(script)
        self.calls: list[Messages] = []

    def complete(self, system: str, messages: Messages, tools: list[dict[str, Any]]) -> AssistantTurn:
        # 存深拷贝：循环随后还会往 messages 里追加，断言要看的是模型“当时”看到的历史
        snapshot = copy.deepcopy(messages)
        self.calls.append(snapshot)
        step = len(self.calls)
        if step > len(self._script):
            raise ScriptExhausted(f"脚本只有 {len(self._script)} 步，第 {step} 次调用没有可回放的回复")
        item = self._script[step - 1]
        return item(snapshot) if callable(item) else item


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 状态码和响应体原样上抛：429 该退避重试、400 该修请求，交给上层决定
        raise LLMHTTPError(exc.code, exc.read().decode("utf-8", "replace")) from exc


def parse_anthropic_response(data: dict[str, Any]) -> AssistantTurn:
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    calls = [
        ToolCall(id=b["id"], name=b["name"], args=b.get("input") or {})
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    # stop_reason 原样保留（end_turn / tool_use / max_tokens / stop_sequence / pause_turn / refusal）；
    # 原始块整体留在 blocks 里，thinking 块必须原封不动地回传
    return AssistantTurn(
        text=text,
        tool_calls=calls,
        stop_reason=data.get("stop_reason") or "",
        usage=data.get("usage") or {},
        blocks=blocks,
    )


class AnthropicLLM:
    """直接调 Anthropic Messages API（非流式，只用标准库）。"""

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        max_tokens: int = 16000,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        effort: str | None = "high",
        fallbacks: str | None = "default",
        timeout: float = 600.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise MissingAPIKey("ANTHROPIC_API_KEY")
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self.effort = effort
        self.fallbacks = fallbacks
        self.timeout = timeout

    def complete(self, system: str, messages: Messages, tools: list[dict[str, Any]]) -> AssistantTurn:
        url = f"{self.base_url}/v1/messages"
        return parse_anthropic_response(self._post(url, self._headers(), self._body(system, messages, tools)))

    def _headers(self) -> dict[str, str]:
        headers = {
            "x-api-key": str(self.api_key),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self.fallbacks:
            # 安全分类器拒答时由服务端换模型重跑，需要这个 beta 头
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        return headers

    def _body(self, system: str, messages: Messages, tools: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": messages}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if self.effort:
            # 这个模型的 effort 默认是 medium，显式写出来，免得换模型时行为悄悄变化
            body["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            body["fallbacks"] = self.fallbacks
        return body

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        return _post_json(url, headers, body, self.timeout)


# OpenAI 的 finish_reason → 本库统一的 stop_reason；未列出的值原样透出，让循环看得见
_FINISH_TO_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def to_openai_messages(system: str, messages: Messages) -> Messages:
    out: Messages = [{"role": "system", "content": system}] if system else []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
        elif message["role"] == "assistant":
            out.append(_openai_assistant(content))
        else:
            out.extend(_openai_user(content))
    return out


def _openai_assistant(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    # thinking 等 Anthropic 专有块在 OpenAI 格式里没有位置，只转 text 和 tool_use
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    calls = [
        {
            "id": b["id"],
            "type": "function",
            "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
        }
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    if calls:
        message["tool_calls"] = calls
    return message


def _openai_user(blocks: list[dict[str, Any]]) -> Messages:
    # role=tool 必须紧跟带 tool_calls 的 assistant，所以先放工具结果，再放用户文本
    out: Messages = [
        {"role": "tool", "tool_call_id": b["tool_use_id"], "content": _tool_result_text(b)}
        for b in blocks
        if b.get("type") == "tool_result"
    ]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if text:
        out.append({"role": "user", "content": text})
    return out


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content", "")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if p.get("type") == "text")
    # OpenAI 格式没有 is_error 字段，只能写进正文，否则模型分不清成功和失败
    return f"[error] {content}" if block.get("is_error") else str(content)


def _openai_tool_call(raw_call: dict[str, Any]) -> ToolCall:
    raw = raw_call["function"].get("arguments") or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = None
    if not isinstance(args, dict):
        # 不静默改成 {}：保留原文，让执行管线把“参数不是合法 JSON”报回给模型
        args = {"__raw_arguments__": raw}
    return ToolCall(id=raw_call["id"], name=raw_call["function"]["name"], args=args)


def parse_openai_response(data: dict[str, Any]) -> AssistantTurn:
    choice = data["choices"][0]
    message = choice.get("message") or {}
    calls = [_openai_tool_call(c) for c in message.get("tool_calls") or []]
    finish = choice.get("finish_reason") or ""
    stop_reason = _FINISH_TO_STOP.get(finish, finish)
    if calls and stop_reason == "end_turn":
        # 有的兼容服务带着工具调用却报 stop；在适配器里纠正，循环只看统一后的值
        stop_reason = "tool_use"
    usage = data.get("usage") or {}
    return AssistantTurn(
        text=message.get("content") or "",
        tool_calls=calls,
        stop_reason=stop_reason,
        usage={
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "raw": usage,
        },
    )


class OpenAICompatLLM:
    """OpenAI Chat Completions 兼容接口，默认指向 DeepSeek。"""

    def __init__(
        self,
        model: str = "deepseek-chat",
        max_tokens: int | None = None,
        *,
        api_key: str | None = None,
        api_key_env: str = "DEEPSEEK_API_KEY",
        base_url: str = "https://api.deepseek.com",
        timeout: float = 600.0,
    ) -> None:
        self.api_key = api_key or os.environ.get(api_key_env)
        if not self.api_key:
            raise MissingAPIKey(api_key_env)
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, system: str, messages: Messages, tools: list[dict[str, Any]]) -> AssistantTurn:
        body: dict[str, Any] = {"model": self.model, "messages": to_openai_messages(system, messages)}
        if tools:
            body["tools"] = to_openai_tools(tools)
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        headers = {"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        return parse_openai_response(self._post(f"{self.base_url}/chat/completions", headers, body))

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        return _post_json(url, headers, body, self.timeout)


def from_env() -> LLM:
    """按 HARNESS_LLM 选择模型：fake（默认）| anthropic | deepseek；HARNESS_MODEL 可覆盖模型名。"""
    kind = os.environ.get("HARNESS_LLM", "fake").strip().lower()
    model = os.environ.get("HARNESS_MODEL")
    if kind == "fake":
        hint = "（FakeLLM）设置 HARNESS_LLM=anthropic 或 HARNESS_LLM=deepseek 以接入真实模型。"
        return FakeLLM([AssistantTurn(text=hint)])
    if kind == "anthropic":
        return AnthropicLLM(model=model) if model else AnthropicLLM()
    if kind == "deepseek":
        return OpenAICompatLLM(model=model) if model else OpenAICompatLLM()
    raise ValueError(f"HARNESS_LLM={kind!r} 无法识别，可选 fake / anthropic / deepseek")
