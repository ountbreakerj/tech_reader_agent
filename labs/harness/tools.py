"""工具定义与注册：一份 schema 既喂给模型，也用来校验模型的参数。

设计要点（对照源码见正文 hx-02）：
- 工具的“真相”只有一个：ToolSpec。模型看到的是它的投影（name/description/input_schema），
  harness 拿到的是它的 handler。两者不能各写一份，否则漂移迟早出事。
- 参数校验由 harness 做，不假设模型遵守 schema。校验失败返回可读错误，不抛异常：
  抛异常会打断整个循环，而“模型参数写错”是正常事件，不是程序故障。
- 执行前钩子是列表，按注册顺序跑，任一拒绝即拒绝。钩子只能拒绝，不能强制放行——
  单调性（DSH 的 guard 语义）是安全策略能被推理的前提。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

# JSON Schema 的 type 关键字到 Python 类型的映射。integer 单独处理：
# 在 JSON 里 3 和 3.0 是同一个数，但 Python 的 isinstance(3.0, int) 是 False，
# 反过来 bool 又是 int 的子类，两处都得挡。
_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}

SUPPORTED_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "enum", "items", "description"}
)


class ToolError(Exception):
    """工具层错误：注册冲突、未知工具、schema 不合法。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的全部定义。模型只看得到前三个字段，handler 永远不出圈。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]
    # 只读工具可以并发；写工具是屏障。默认发 false 是 fail-safe：
    # 忘了标就退化成串行，不会把两个写操作并到一起。
    read_only: bool = False
    # 与 read_only 分开：只读也可能不可并发（例如共享一个游标）。
    parallel_safe: bool = False
    # 秒。None 表示不设时限，交给上层钩子处理。
    timeout: float | None = None

    def schema(self) -> dict[str, Any]:
        """投影成模型可见的定义。执行相关字段一律不进这个字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _check_scalar_type(value: Any, expected: str, path: str) -> list[str]:
    """单值类型检查。返回问题列表，空列表表示通过。"""
    if expected == "integer":
        # bool 是 int 的子类，得先排掉；3.0 在 JSON 里等价于 3，按整数收下。
        if isinstance(value, bool):
            return [f"{path}: 期望 integer，收到 boolean"]
        if isinstance(value, float) and not value.is_integer():
            return [f"{path}: 期望 integer，收到带小数的 {value!r}"]
        if not isinstance(value, (int, float)):
            return [f"{path}: 期望 integer，收到 {type(value).__name__}"]
        return []
    checker = _TYPE_CHECKS.get(expected)
    if checker is None:
        return [f"{path}: 不支持的 type {expected!r}"]
    if expected in ("number",) and isinstance(value, bool):
        return [f"{path}: 期望 number，收到 boolean"]
    if not isinstance(value, checker):
        return [f"{path}: 期望 {expected}，收到 {type(value).__name__}"]
    return []


def validate_value(schema: Mapping[str, Any], value: Any, path: str = "arguments") -> list[str]:
    """按 schema 校验一个值，返回全部问题（不是遇到第一个就返回）。

    一次报全，模型一次就能改对；只报第一个问题往往要来回好几轮。
    """
    problems: list[str] = []

    if "enum" in schema:
        allowed = schema["enum"]
        if value not in allowed:
            problems.append(f"{path}: 取值 {value!r} 不在 enum {allowed!r} 中")
        # enum 已经限定了取值，不再叠加类型检查，避免同一处报两条。
        return problems

    expected = schema.get("type")
    if expected is None:
        # 没写 type 就只做结构性检查，不做类型约束（JSON Schema 的宽松语义）。
        if isinstance(value, dict) and "properties" in schema:
            expected = "object"
        else:
            return problems

    if expected == "object":
        if not isinstance(value, dict):
            return _check_scalar_type(value, "object", path)
        properties: dict[str, Any] = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{path}.{key}: 缺少必填字段")
        for key, sub in properties.items():
            if key in value:
                problems.extend(validate_value(sub, value[key], f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    problems.append(f"{path}.{key}: 不是已声明的字段（additionalProperties 为 false）")
        return problems

    if expected == "array":
        if not isinstance(value, list):
            return _check_scalar_type(value, "array", path)
        items = schema.get("items")
        if isinstance(items, dict):
            for index, entry in enumerate(value):
                problems.extend(validate_value(items, entry, f"{path}[{index}]"))
        return problems

    return _check_scalar_type(value, expected, path)


def check_schema(schema: Mapping[str, Any], path: str = "schema") -> list[str]:
    """注册时检查 schema 本身是否合法：写了不支持的 keyword 要当场炸，别拖到运行期。"""
    problems: list[str] = []
    for key in schema:
        if key not in SUPPORTED_KEYWORDS:
            problems.append(
                f"{path}.{key}: 不支持的 keyword（支持 {'/'.join(sorted(SUPPORTED_KEYWORDS))}）"
            )
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            problems.append(f"{path}.required: 必须是字符串数组")
        else:
            declared = set((schema.get("properties") or {}).keys())
            for key in required:
                if key not in declared:
                    problems.append(f"{path}.required: 字段 {key!r} 没有出现在 properties 里")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        problems.append(f"{path}.additionalProperties: 必须是布尔值")
    for key, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict):
            problems.extend(check_schema(sub, f"{path}.properties.{key}"))
    if isinstance(schema.get("items"), dict):
        problems.extend(check_schema(schema["items"], f"{path}.items"))
    return problems


@dataclass(frozen=True)
class HookDecision:
    """执行前钩子的返回值。allowed=False 时必须给 reason：拒绝理由会原样喂回模型。"""

    allowed: bool
    reason: str = ""

    @staticmethod
    def allow() -> "HookDecision":
        return HookDecision(True)

    @staticmethod
    def deny(reason: str) -> "HookDecision":
        if not reason:
            raise ToolError("拒绝工具调用必须给出理由：模型看不到理由就只会重试同样的调用")
        return HookDecision(False, reason)


PreExecuteHook = Callable[[str, dict[str, Any]], HookDecision]


class ToolRegistry:
    """工具的唯一入口：注册、查表、投影、校验、钩子、执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._hooks: list[PreExecuteHook] = []

    # ---- 注册与查表 ----

    def register(self, tool: ToolSpec) -> ToolSpec:
        """注册一个工具。重名直接报错，不覆盖。"""
        if tool.name in self._tools:
            raise ToolError(
                f"工具 {tool.name!r} 已经注册过了。"
                f"不能静默覆盖：覆盖会让运行时行为取决于插件的安装顺序，"
                f"而排错时看到的日志和实际生效的实现对不上。"
                f"要换实现请先 unregister，或者换一个名字。"
            )
        problems = check_schema(tool.input_schema)
        if problems:
            raise ToolError(f"工具 {tool.name!r} 的 input_schema 不合法：{'；'.join(problems)}")
        if not callable(tool.handler):
            raise ToolError(f"工具 {tool.name!r} 的 handler 不可调用")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """导出给模型的 schema 列表，按注册顺序。执行字段一个都不出去。"""
        return [tool.schema() for tool in self._tools.values()]

    # ---- 执行前钩子 ----

    def add_hook(self, hook: PreExecuteHook) -> None:
        """挂一个 pre-execute 钩子。任一拒绝即拒绝，后面的钩子不再跑。"""
        self._hooks.append(hook)

    def run_hooks(self, name: str, args: dict[str, Any]) -> HookDecision:
        for hook in self._hooks:
            decision = hook(name, args)
            if not decision.allowed:
                return decision
        return HookDecision.allow()

    # ---- 调用 ----

    def call(self, name: str, args: Any) -> str:
        """执行一次模型请求的调用。任何失败都返回字符串错误，不抛异常。"""
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "（一个都没有）"
            return f"Error: 未知工具 {name!r}；可用工具：{known}"

        if not isinstance(args, dict):
            return f"Error: 工具 {name!r} 的参数必须是对象，收到 {type(args).__name__}"

        problems = validate_value(tool.input_schema, args)
        if problems:
            return f"Error: 工具 {name!r} 的参数不合法：\n- " + "\n- ".join(problems)

        decision = self.run_hooks(name, args)
        if not decision.allowed:
            return f"Error: 工具 {name!r} 被拒绝：{decision.reason}"

        try:
            return str(tool.handler(args))
        except Exception as exc:  # noqa: BLE001 — 工具是用户代码，什么异常都可能有
            # 异常转成错误文本而不是上抛：一次工具失败不该结束整个循环。
            return f"Error: 工具 {name!r} 执行失败：{type(exc).__name__}: {exc}"


# ---- 调度辅助：只读/写决定谁能并发 ----

def execution_mode(tool: ToolSpec | None) -> str:
    """按工具的并行标记返回 'parallel' 或 'exclusive'。

    未知工具按 exclusive 处理：宁可让一个坏名字挡住后面的调用，
    也不要让它混进一批并发调用里。
    """
    return "parallel" if tool is not None and tool.parallel_safe else "exclusive"


def group_calls(registry: ToolRegistry, calls: Iterable[tuple[str, Any]]) -> list[list[tuple[str, Any]]]:
    """把一次模型回复里的多个调用切成执行组：连续可并发的合成一组，独占的各自成组。"""
    groups: list[list[tuple[str, Any]]] = []
    current: list[tuple[str, Any]] = []
    for name, args in calls:
        if execution_mode(registry.get(name)) == "parallel":
            current.append((name, args))
        else:
            if current:
                groups.append(current)
                current = []
            groups.append([(name, args)])
    if current:
        groups.append(current)
    return groups


# ---- 示例工具 ----

def _read_file(args: dict[str, Any]) -> str:
    with open(args["path"], encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return text[:4000] if text else "(空文件)"


def _write_file(args: dict[str, Any]) -> str:
    with open(args["path"], "w", encoding="utf-8") as handle:
        handle.write(args["content"])
    return f"已写入 {args['path']}（{len(args['content'])} 字符）"


def default_registry() -> ToolRegistry:
    """一个最小的可用注册表，方便正文和测试直接引用。"""
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="read_file",
        description="读取一个 UTF-8 文本文件，返回前 4000 个字符。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "limit": {"type": "integer", "description": "最多返回的字符数"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_read_file,
        read_only=True,
        parallel_safe=True,
    ))
    registry.register(ToolSpec(
        name="write_file",
        description="把内容写入文件，覆盖原有内容。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=_write_file,
        read_only=False,
        parallel_safe=False,
        timeout=30.0,
    ))
    return registry
