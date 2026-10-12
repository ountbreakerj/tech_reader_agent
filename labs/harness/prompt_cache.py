"""Prompt cache 稳定性：把一次请求渲染成确定的字节序列，并诊断前缀在哪一段、哪个字节断掉。

只用标准库，兼容 Python 3.10+。设计目标是本章最想讲的那件事：
prompt 缓存按**前缀**命中，所以「这次请求能复用多少上一次的缓存」是一个可以离线算出来的
纯函数问题——不需要真的调 API，也不需要 provider 配合。

渲染单位（unit）刻意切得比「段」更细：工具声明一个单位、系统提示词一个单位、**每条消息各算
一个单位**。这样「追加一条消息」只会让前缀变长（前面的单位哈希不变），而「改写第 2 条消息」
会让它和它之后的一切都失效。这两种情况在真实缓存里的后果完全不同，切粗了就看不出区别。

对应 DSH 的机制（正文里有对应的 file:line）：
  - 工具列表先规范化（按名字排序）再进提示词：``system-prompt/orderTools``；
  - 运行时上下文不写进系统提示词，而是作为一条独立消息追加在历史之后：
    ``agent-loop/runtime-context.ts`` 的 ``RuntimeContextProjection.project``；
  - 系统提示词渲染结果相同就不产生任何事件：``SystemPromptProjection.project``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "Message",
    "Unit",
    "RenderedRequest",
    "FieldChange",
    "RequestDiff",
    "canonical_json",
    "canonical_tools",
    "canonical_message",
    "render_request",
    "unit_hashes",
    "diff_prefix",
    "common_unit_count",
    "common_byte_prefix",
    "reusable_bytes",
    "PrefixCache",
    "approx_tokens",
    "build_request",
    "demo_stabilization",
]

# 渲染顺序必须是 tools → system → messages，前缀复用完全依赖它：
# 任何一部分提前，它之后的内容都跟着换位置，缓存也就整条断掉。
_SEPARATOR = b"\x00"
_TAIL = b"\x01"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Message:
    """一条模型可见的消息。source 用来模拟 DSH 消息上的来源标记。"""

    role: str
    text: str
    source: str = "user"

    @property
    def canonical(self) -> bytes:
        """规范字节。字段名固定，不依赖 dict 的插入顺序。"""
        return canonical_json(
            {
                "content": [{"type": "text", "text": self.text}],
                "role": self.role,
                "source": self.source,
            }
        )


@dataclass(frozen=True)
class Unit:
    """前缀里的一个可复用单位：``kind`` 是类别，``label`` 是给人看的位置。"""

    kind: str  # 'tools' | 'system' | 'message'
    label: str  # 'tools' | 'system' | 'messages[2]'
    payload: bytes

    def framed(self) -> bytes:
        """带边界标记的字节。标记里含 label，避免「同样字节出现在不同位置」被等同看待。"""
        return self.label.encode("utf-8") + _SEPARATOR + self.payload + _TAIL

    @property
    def framed_length(self) -> int:
        """线上这单位占多少字节（含边界标记）。"""
        return len(self.label.encode("utf-8")) + 1 + len(self.payload) + 1


@dataclass(frozen=True)
class RenderedRequest:
    """一次请求「在线上会出现的那串字节」的本地表示。

    ``hashes[i]`` 是从第 0 个单位到第 i 个单位的累积哈希——缓存命中的边界就落在单位上。
    """

    units: tuple[Unit, ...]
    hashes: tuple[str, ...]

    def units_of(self, kind: str) -> tuple[Unit, ...]:
        return tuple(unit for unit in self.units if unit.kind == kind)

    def segment(self, kind: str) -> bytes | None:
        """取某一类的字节（工具 / 系统提示词 / 整段消息）。多单位时按顺序拼起来。"""
        parts = [unit.payload for unit in self.units_of(kind)]
        return None if not parts else b"".join(parts)

    def total_bytes(self) -> int:
        return sum(len(unit.payload) for unit in self.units)

    def stream(self) -> bytes:
        """整条请求拼成一串字节——这就是 provider 眼里的前缀。"""
        return b"".join(unit.framed() for unit in self.units)

    def framed_prefix(self, unit_count: int) -> int:
        """前 ``unit_count`` 个单位在线上占多少字节。"""
        return sum(unit.framed_length for unit in self.units[:unit_count])


@dataclass(frozen=True)
class FieldChange:
    """第一个差异的位置：落在哪个单位、第几条消息、单位内第几个字节。"""

    kind: str
    label: str
    message_index: int | None
    field: str
    char_offset: int
    before: str
    after: str

    def describe(self) -> str:
        where = self.label if self.message_index is None else f"{self.kind}[{self.message_index}]"
        return (
            f"{where} 的 {self.field}（单位内偏移 {self.char_offset}）："
            f"{self.before!r} → {self.after!r}"
        )


@dataclass(frozen=True)
class RequestDiff:
    """两次请求的差异，以及由此推出的「这次最多能复用多少」。"""

    first_change: FieldChange | None
    common_units: int
    reusable_bytes: int
    prev_bytes: int
    curr_bytes: int

    @property
    def reuse_all(self) -> bool:
        """上一次请求的整条前缀都能复用（差异只出现在这次新增的尾部）。"""
        return self.reusable_bytes >= self.prev_bytes

    def hit_ratio(self) -> float:
        """教学示意：可复用字节占这次请求的比例。"""
        return round(self.reusable_bytes / self.curr_bytes, 4) if self.curr_bytes else 0.0

    def summary(self) -> str:
        if self.first_change is None:
            return f"两次请求逐字节一致；可复用 {self.reusable_bytes}/{self.prev_bytes} 字节"
        return (
            f"首个差异在 {self.first_change.describe()}；"
            f"可复用 {self.reusable_bytes}/{self.prev_bytes} 字节（共用 {self.common_units} 个单位）"
        )


# --------------------------------------------------------------------------- #
# 规范化渲染
# --------------------------------------------------------------------------- #

def canonical_json(value: Any) -> bytes:
    """确定性的 JSON 字节序列：键排序、无多余空白、非 ASCII 不转义。

    sort_keys 解决「同一个 dict 两次 dumps 出不同字节」这类静默失效：
    Python 的 dict 保持插入顺序，而插入顺序经常取决于上游并发任务的完成顺序。
    """
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def canonical_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """工具集规范化：按名字排序。

    注册顺序是插件加载顺序的产物（DSH README 的原话是 registration order is a
    plugin-load artifact），它跟模型的判断无关，却会改变提示前缀。排序把它消掉。
    """
    return [dict(tool) for tool in sorted(tools, key=lambda tool: str(tool["name"]))]


def canonical_message(message: Message) -> bytes:
    """一条消息的规范字节。``Message.canonical`` 的模块级别名，方便按需导入。"""
    return message.canonical


def render_request(
    tools: Sequence[Mapping[str, Any]],
    system: str,
    messages: Sequence[Message],
) -> RenderedRequest:
    """把一次请求渲染成一条前缀，并按单位算出累积哈希。

    顺序是 tools → system → messages。空系统提示词仍然占一个单位（长度 0），
    因为「有没有这一部分」本身也是前缀的一部分。
    """
    units: list[Unit] = [
        Unit(kind="tools", label="tools", payload=canonical_json(canonical_tools(tools))),
        Unit(kind="system", label="system", payload=system.encode("utf-8")),
    ]
    for index, message in enumerate(messages):
        units.append(
            Unit(kind="message", label=f"messages[{index}]", payload=message.canonical)
        )
    frozen = tuple(units)
    return RenderedRequest(units=frozen, hashes=unit_hashes(frozen))


def unit_hashes(units: Sequence[Unit]) -> tuple[str, ...]:
    """各单位的前缀哈希（第 i 项 = 前 i+1 个单位累积）。"""
    accumulator = hashlib.sha256()
    hashes: list[str] = []
    for unit in units:
        accumulator.update(unit.framed())
        hashes.append(accumulator.hexdigest()[:16])
    return tuple(hashes)


# --------------------------------------------------------------------------- #
# 差异诊断：本章最该讲的纯函数
# --------------------------------------------------------------------------- #

def common_byte_prefix(before: bytes, after: bytes) -> int:
    """两串字节从头开始有多少字节完全相同。"""
    limit = min(len(before), len(after))
    index = 0
    while index < limit and before[index] == after[index]:
        index += 1
    return index


def common_unit_count(prev: RenderedRequest, curr: RenderedRequest) -> int:
    """两个前缀哈希列表从前往后有多少个单位相等。"""
    count = 0
    for left, right in zip(prev.hashes, curr.hashes):
        if left != right:
            break
        count += 1
    return count


def reusable_bytes(prev: RenderedRequest, curr: RenderedRequest) -> int:
    """这次请求里能复用上一段前缀的字节数。"""
    return _diff(prev, curr).reusable_bytes


def _window(blob: bytes, offset: int, span: int = 24) -> str:
    """从 offset 前后取一小段可读文本，便于印在报告里。"""
    lo = max(0, offset - 6)
    return blob[lo:lo + span].decode("utf-8", "replace")


def _tool_change(before: bytes, after: bytes) -> tuple[str, str, str]:
    """工具单位的差异：尽量翻译成「加了哪些工具、去掉了哪些、还是只改了顺序或描述」。

    只报告第一个差异所在的单位，但工具集往往一次变化多项，所以这里把集合差异全列出来。
    """
    def names(blob: bytes) -> list[str]:
        try:
            return [str(tool.get("name")) for tool in json.loads(blob.decode("utf-8"))]
        except (ValueError, AttributeError, TypeError):
            return []

    before_names, after_names = names(before), names(after)
    added = [name for name in after_names if name not in before_names]
    removed = [name for name in before_names if name not in after_names]
    if added or removed:
        return "工具集合", "-" + ",".join(removed) if removed else "(无)", "+" + ",".join(added) if added else "(无)"
    if before_names != after_names:
        return "工具顺序", ",".join(before_names), ",".join(after_names)
    return "工具定义", "旧定义", "新定义"


def _diff(prev: RenderedRequest, curr: RenderedRequest) -> RequestDiff:
    prev_stream, curr_stream = prev.stream(), curr.stream()
    shared = common_byte_prefix(prev_stream, curr_stream)
    common = common_unit_count(prev, curr)
    count = min(len(prev.units), len(curr.units))
    change: FieldChange | None
    if common < count:
        before_unit, after_unit = prev.units[common], curr.units[common]
        offset = common_byte_prefix(before_unit.payload, after_unit.payload)
        message_index: int | None = None
        if before_unit.kind == "tools":
            field, before_text, after_text = _tool_change(before_unit.payload, after_unit.payload)
        elif before_unit.kind == "system":
            field, before_text, after_text = "文本", _window(before_unit.payload, offset), _window(after_unit.payload, offset)
        else:
            message_index = sum(1 for unit in prev.units[:common] if unit.kind == "message")
            field = "内容"
            before_text, after_text = _window(before_unit.payload, offset), _window(after_unit.payload, offset)
        change = FieldChange(
            kind=before_unit.kind,
            label=before_unit.label,
            message_index=message_index,
            field=field,
            char_offset=offset,
            before=before_text,
            after=after_text,
        )
    elif len(prev.units) == len(curr.units):
        change = None
    else:
        longer_is_curr = len(curr.units) > len(prev.units)
        extra = curr.units[common] if longer_is_curr else prev.units[common]
        extra_index = (
            sum(1 for unit in prev.units[:common] if unit.kind == "message")
            if extra.kind == "message"
            else None
        )
        change = FieldChange(
            kind=extra.kind,
            label=extra.label,
            message_index=extra_index,
            field="新增单位" if longer_is_curr else "被移除的单位",
            char_offset=shared,
            before=f"{len(prev.units)} 个单位",
            after=f"{len(curr.units)} 个单位",
        )
    boundary = prev.framed_prefix(common)
    if change is not None and common < count:
        # 首个不同的单位内部，前缀还可能有一段相同的字节（例如系统提示词只是尾部多了一句
        # 提醒）。真实缓存按 token 块命中，这里保守地只算到段内第一个不同的字节。
        boundary += common_byte_prefix(
            prev.units[common].framed(), curr.units[common].framed()
        )
    return RequestDiff(
        first_change=change,
        common_units=common,
        reusable_bytes=min(boundary, len(curr_stream)),
        prev_bytes=len(prev_stream),
        curr_bytes=len(curr_stream),
    )


def diff_prefix(prev: RenderedRequest, curr: RenderedRequest) -> RequestDiff:
    """找出两次请求的第一个差异，并据此判断这次最多能复用多少缓存。

    这是整个模块的核心纯函数：给定两次渲染结果，回答「缓存从哪个单位、哪个字节开始失效」。
    三种形态的后果差别很大，是本章反复要讲的：
      1. 工具或系统提示词变了 —— 命中在它之前，之后全废；
      2. 历史里第 k 条消息被改写 —— 命中到 k-1，之后全废；
      3. 尾部追加 —— 前缀完整命中，只有新增部分要重新计算。
    """
    return _diff(prev, curr)


def approx_tokens(byte_count: int, bytes_per_token: float = 4.0) -> int:
    """教学示意：按 4 字节 1 token 折算。

    真实实现必须读 provider 返回的 usage；这里折算只是为了让「命中率」这个比例
    在离线演示里有直观的数字。
    """
    return int(byte_count / bytes_per_token)


# --------------------------------------------------------------------------- #
# 模拟缓存：只记前缀哈希，不调真实 API
# --------------------------------------------------------------------------- #

class PrefixCache:
    """模拟 provider 的前缀缓存：记住见过的每一个单位前缀哈希。

    真实缓存还有 TTL、最小可缓存长度（例如 64 / 1024 token 对齐）、按模型区分等维度；
    这里只保留「前缀必须逐字节相同」这一条本质规则，用来离线验证稳定化改动的效果。
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.hits = 0
        self.misses = 0
        self.hit_bytes = 0
        self.sent_bytes = 0

    def lookup(self, request: RenderedRequest) -> int:
        """返回这次请求命中的**单位数**（0 表示连工具声明都没命中）。"""
        hit = 0
        for digest in request.hashes:
            if digest not in self._seen:
                break
            hit += 1
        return hit

    def store(self, request: RenderedRequest) -> None:
        """记下这次请求的每个单位前缀。

        只记前缀、不记整串：下次请求多一条消息时，它前面的单位哈希仍然没变，
        所以照样命中——这正是追加式历史天然对缓存友好的原因。
        """
        self._seen.update(request.hashes)

    def call(self, request: RenderedRequest) -> int:
        """一次完整调用：先查命中，再写回。返回命中的单位数。"""
        hit = self.lookup(request)
        self.hit_bytes += sum(len(unit.payload) for unit in request.units[:hit])
        self.sent_bytes += request.total_bytes()
        if hit == 0:
            self.misses += 1
        else:
            self.hits += 1
        self.store(request)
        return hit

    def hit_ratio(self) -> float:
        """教学示意：整段过程中的命中字节占比（分母含首次请求的全量）。"""
        return round(self.hit_bytes / self.sent_bytes, 4) if self.sent_bytes else 0.0

    def known_prefix_hashes(self) -> frozenset[str]:
        """已经见过的全部单位前缀哈希。"""
        return frozenset(self._seen)


# --------------------------------------------------------------------------- #
# 构造请求：稳定系统提示词 + 运行时上下文快照
# --------------------------------------------------------------------------- #

def build_request(
    *,
    system: str,
    tools: Sequence[Mapping[str, Any]],
    messages: Sequence[Message] = (),
    runtime_context: str | None = None,
    context_in_system: bool = False,
) -> RenderedRequest:
    """构造一次请求。

    ``context_in_system=False``（推荐）：易变的运行时事实（时间、cwd、git 状态、todo）
    作为一条独立消息追加在历史**之后**——它只是前缀的一条尾巴，前缀本身不受影响。
    这对应 DSH 的 runtime-context 快照：渲染成一条 user 消息，而不是拼进系统提示词。

    ``context_in_system=True``（反例）：把同样的内容拼进系统提示词。系统提示词排在第二个
    单位，它一变，整条请求从那里就开始偏离，后面全部失效——而模型在这种配置下看到的内容
    和「好」版本完全一样，所以失效没有任何可见症状。
    """
    history: list[Message] = list(messages)
    rendered_system = system
    if runtime_context is not None:
        if context_in_system:
            rendered_system = f"{system}\n\n{runtime_context}"
        else:
            history.append(Message(role="user", text=runtime_context, source="runtime-context"))
    return render_request(tools, rendered_system, history)


def demo_stabilization(
    turns: Sequence[str],
    *,
    tools: Sequence[Mapping[str, Any]] | None = None,
    system: str = "You are a coding agent. Use the tools when needed.",
    history: Sequence[Message] = (),
) -> dict[str, Any]:
    """坏版本（时间戳拼进系统提示词）与好版本（追加为消息）的命中差异。

    ``turns`` 是每轮新产生的运行时上下文字符串。两版渲染出的模型可见内容**完全一样**，
    区别只在它落在哪个单位：坏版本重写 system（第二个单位），好版本追加在尾部。
    返回教学示意用的命中率，不调真实 API。
    """
    tool_list = list(tools) if tools is not None else [
        {"name": "read", "description": "读取文件", "parameters": {"path": {"type": "string"}}},
        {"name": "bash", "description": "执行命令", "parameters": {"cmd": {"type": "string"}}},
    ]
    result: dict[str, Any] = {}
    for label, in_system in (("bad", True), ("good", False)):
        cache = PrefixCache()
        first_change_kinds: list[str] = []
        reused_whole_prefix: list[bool] = []
        previous: RenderedRequest | None = None
        snapshots: list[Message] = []
        for turn in turns:
            if in_system:
                request = build_request(
                    system=system, tools=tool_list, messages=history,
                    runtime_context=turn, context_in_system=True,
                )
            else:
                snapshots.append(Message(role="user", text=turn, source="runtime-context"))
                request = build_request(system=system, tools=tool_list, messages=[*history, *snapshots])
            cache.call(request)
            if previous is not None:
                diff = diff_prefix(previous, request)
                change = diff.first_change
                if change is None:
                    verdict = "无差异（逐字节一致）"
                elif change.field == "新增单位":
                    verdict = "尾部追加（复用率 100%）"
                elif change.kind == "message":
                    verdict = f"messages[{change.message_index}].{change.field}"
                else:
                    verdict = f"{change.kind}.{change.field}"
                first_change_kinds.append(verdict)
                reused_whole_prefix.append(diff.reuse_all)
            previous = request
        result[label] = {
            "hit_ratio": cache.hit_ratio(),
            "first_change_kinds": first_change_kinds,
            "reused_whole_prefix": reused_whole_prefix,
        }
    result["note"] = "命中按字节算；token 与价格是教学示意"
    return result
