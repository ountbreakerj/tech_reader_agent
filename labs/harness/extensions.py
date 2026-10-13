"""Skills 与 MCP：两种把「别人写的能力」接进 harness 的方式。

对照正文 hx-22。两者经常被放在一起讲，但解决的问题不同：

- **Skill** 是一份按需加载的说明（外加可选的脚本和资料）。它扩展的是模型的
  「知识」：系统提示里只放一行名字和描述，模型判断需要时调用 load_skill 读全文。
  这叫渐进式披露：一百个 skill 的全文塞进系统提示会吃掉大半个窗口，
  一百行描述只要几千个 token。
- **MCP** 是一个协议：外部进程（MCP server）通过 JSON-RPC 声明自己有哪些工具，
  harness 把它们注册进自己的工具表。它扩展的是模型的「能力」。

两者共同的难点在边界上：第三方写的描述和工具名会进入你的上下文和工具表。
所以这一层要做：名字加命名空间防撞名、描述长度设上限、外部工具默认按最严的
权限处理、MCP server 挂掉时注销它的工具而不是让调用一直失败。

本模块用一个进程内的假 MCP server 演示协议往返（tools/list、tools/call），
不依赖任何第三方库，只用标准库。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SKILL_FILE = "SKILL.md"
MAX_DESCRIPTION = 300          # 单个 skill 描述的字符上限（进系统提示的部分）
MAX_INDEX_CHARS = 6000         # 全部 skill 索引的字符上限（教学示意）
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SkillError(Exception):
    """skill 文件不合法。"""


class McpError(Exception):
    """MCP 协议错误或 server 不可用。"""


# ---------- Skills ----------


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str
    source: str = "project"        # bundled / user / project：同名时优先级从低到高


def parse_skill(text: str, path: str, source: str = "project") -> Skill:
    """解析 SKILL.md：开头是 --- 包住的 name / description，后面是正文。

    只认这两个键，其余忽略；frontmatter 解析失败直接报错，不猜。
    """
    if not text.startswith("---"):
        raise SkillError(f"{path}: 缺少开头的 --- frontmatter")
    try:
        _, header, body = text.split("---", 2)
    except ValueError as exc:
        raise SkillError(f"{path}: frontmatter 没有闭合") from exc
    meta: dict[str, str] = {}
    for line in header.strip().splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"').strip("'")
    name, description = meta.get("name", ""), meta.get("description", "")
    if not NAME_PATTERN.match(name):
        raise SkillError(f"{path}: name {name!r} 只能用小写字母、数字和连字符")
    if not description:
        raise SkillError(f"{path}: 缺少 description；模型靠它判断什么时候加载这个 skill")
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 1] + "…"
    return Skill(name, description, body.strip(), path, source)


SOURCE_RANK = {"bundled": 0, "user": 1, "project": 2}


def discover_skills(roots: list[tuple[str, str]]) -> tuple[dict[str, Skill], list[str]]:
    """在若干目录里找 */SKILL.md。roots 是 [(目录, 来源)]。

    同名时来源优先级高的覆盖低的（项目 > 用户 > 内置），并记录被覆盖的那一个；
    同一来源里同名直接报告冲突，两个都不加载——静默选一个会让行为取决于目录遍历顺序。
    """
    found: dict[str, Skill] = {}
    notes: list[str] = []
    same_source: dict[tuple[str, str], list[str]] = {}
    for root, source in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for file in sorted(base.glob(f"*/{SKILL_FILE}")):
            try:
                skill = parse_skill(file.read_text(encoding="utf-8"), str(file), source)
            except SkillError as exc:
                notes.append(f"跳过：{exc}")
                continue
            same_source.setdefault((skill.name, source), []).append(str(file))
            current = found.get(skill.name)
            if current is None or SOURCE_RANK[source] > SOURCE_RANK[current.source]:
                if current is not None:
                    notes.append(f"{skill.name}: {source} 版本覆盖了 {current.source} 版本")
                found[skill.name] = skill
    for (name, source), paths in same_source.items():
        if len(paths) > 1:
            notes.append(f"冲突：{source} 里有 {len(paths)} 个名为 {name} 的 skill，均不加载")
            if found.get(name) and found[name].source == source:
                del found[name]
    return found, notes


def skill_index(skills: dict[str, Skill], budget: int = MAX_INDEX_CHARS) -> str:
    """进系统提示的那一段：每个 skill 一行。按名字排序，保证前缀稳定（hx-06）。"""
    lines = ["可用的 skill（需要时调用 load_skill 读取全文）："]
    used = len(lines[0])
    for name in sorted(skills):
        line = f"- {name}: {skills[name].description}"
        if used + len(line) + 1 > budget:
            lines.append(f"- ……还有 {len(skills) - (len(lines) - 1)} 个 skill 因长度限制未列出")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def load_skill_tool(skills: dict[str, Skill]) -> Callable[[dict[str, Any]], str]:
    """给模型的 load_skill 工具。全文作为工具结果返回，而不是写进系统提示。

    这样系统提示保持稳定（缓存不失效），skill 全文只在需要的那一轮出现，
    之后可以被压缩（hx-07）当作普通历史处理。
    """

    def handler(args: dict[str, Any]) -> str:
        name = str(args.get("name", ""))
        skill = skills.get(name)
        if skill is None:
            return f"Error: 没有名为 {name!r} 的 skill；可用：{', '.join(sorted(skills)) or '（无）'}"
        return f"<skill name=\"{skill.name}\" source=\"{skill.source}\">\n{skill.body}\n</skill>"

    return handler


# ---------- MCP ----------


class FakeMcpServer:
    """进程内的 MCP server 替身：只实现 initialize、tools/list、tools/call 三个方法。

    真实的 MCP server 是一个子进程（stdio）或一个 HTTP 端点，消息格式都是 JSON-RPC 2.0。
    这里直接处理 JSON 字符串，保留协议的形状，省掉传输层。
    """

    def __init__(self, name: str, tools: dict[str, tuple[str, dict[str, Any], Callable[[dict], str]]]) -> None:
        self.name = name
        self.tools = tools
        self.alive = True
        self.calls: list[str] = []

    def handle(self, raw: str) -> str:
        if not self.alive:
            raise McpError(f"MCP server {self.name} 已断开")
        request = json.loads(raw)
        method, rid = request.get("method"), request.get("id")
        self.calls.append(method)
        if method == "initialize":
            result: Any = {"protocolVersion": "2025-06-18", "serverInfo": {"name": self.name}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, (d, s, _) in self.tools.items()
            ]}
        elif method == "tools/call":
            params = request.get("params") or {}
            entry = self.tools.get(params.get("name"))
            if entry is None:
                return json.dumps({"jsonrpc": "2.0", "id": rid,
                                   "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}})
            try:
                text, is_error = entry[2](params.get("arguments") or {}), False
            except Exception as exc:  # noqa: BLE001
                text, is_error = f"{type(exc).__name__}: {exc}", True
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
        return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False)


@dataclass
class McpTool:
    """注册进 harness 工具表的 MCP 工具。name 带命名空间，original 是 server 里的原名。"""

    name: str
    original: str
    server: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False        # 外部工具默认不是只读：并发与权限都按最严处理


@dataclass
class McpClient:
    """一个 server 一个客户端。负责握手、列工具、调用、断线处理。"""

    server: FakeMcpServer
    max_description: int = MAX_DESCRIPTION
    _next_id: int = 0
    tools: dict[str, McpTool] = field(default_factory=dict)
    connected: bool = False

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        raw = json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}})
        reply = json.loads(self.server.handle(raw))
        if reply.get("id") != self._next_id:
            raise McpError(f"响应 id {reply.get('id')} 与请求 {self._next_id} 不匹配")
        if "error" in reply:
            raise McpError(f"{method} 失败：{reply['error'].get('message')}")
        return reply["result"]

    def connect(self) -> list[McpTool]:
        """握手并拉取工具清单。名字加 mcp__server__ 前缀，描述截断。"""
        self._request("initialize", {"clientInfo": {"name": "labs-harness"}})
        listed = self._request("tools/list")["tools"]
        self.tools = {}
        for entry in listed:
            namespaced = mcp_tool_name(self.server.name, entry["name"])
            description = str(entry.get("description", ""))
            if len(description) > self.max_description:
                description = description[: self.max_description - 1] + "…"
            self.tools[namespaced] = McpTool(namespaced, entry["name"], self.server.name,
                                             description, entry.get("inputSchema") or {"type": "object"})
        self.connected = True
        return list(self.tools.values())

    def call(self, namespaced: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """调用一个工具，返回 (文本, 是否错误)。断线时标记为未连接，并返回错误而不是抛异常。"""
        tool = self.tools.get(namespaced)
        if tool is None:
            return f"Error: 未知的 MCP 工具 {namespaced!r}", True
        try:
            result = self._request("tools/call", {"name": tool.original, "arguments": arguments})
        except McpError as exc:
            if not self.server.alive:
                self.connected = False
            return f"Error: MCP server {tool.server} 调用失败：{exc}", True
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return text, bool(result.get("isError"))


def mcp_tool_name(server: str, tool: str) -> str:
    """命名空间：mcp__{server}__{tool}。两个 server 都有 search 时不会撞名。"""
    clean = lambda s: re.sub(r"[^A-Za-z0-9_-]", "_", s)
    return f"mcp__{clean(server)}__{clean(tool)}"


def to_tool_schemas(tools: list[McpTool]) -> list[dict[str, Any]]:
    """投影成发给模型的工具定义（与 hx-02 的三件套同形）。按名字排序，前缀稳定。"""
    return [{"name": t.name, "description": f"[MCP:{t.server}] {t.description}", "input_schema": t.input_schema}
            for t in sorted(tools, key=lambda t: t.name)]
