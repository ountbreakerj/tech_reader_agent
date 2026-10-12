"""会话日志与回放：把「日志」而不是「内存里的消息数组」当作会话的事实来源。

四条设计不变量（都对应 DSH 的 packages/core/session）：

1. 日志只追加，``seq`` 就是事件在日志里的下标——所以「连续」不是人为约定，
   而是由写入路径本身保证的（DSH：``seq: SessionSeq(this.log.length)``）。
2. 写入前先做一次 JSON 快照，日志里存的是快照，不是调用方之后还会改的那个对象。
3. 模型可见的历史一律从日志推导（``Replayer``）；日志之外不允许有第二份状态，
   否则进程重启后两份状态会分叉。
4. 载入时把两类坏数据分开处理：**提交前缀里**的坏行说明日志真的坏了，拒绝加载并
   报出行号；**没有结尾换行的最后一行**是进程被杀留下的残行，丢弃并把断点报出来。
   DSH 的 JSONL 后端是同一套分工：坏行按 corruption 拒绝，半截尾巴按 recoverable 截断。

事件载荷约定（都是 JSON 可序列化的 dict）：

===========================  =======================================================
kind                         payload
===========================  =======================================================
``system/message``           ``{"content": str}``
``user/message``             ``{"content": str}``
``assistant/message``        ``{"text": str, "tool_calls": [{"id","name","args"}]}``
``tool/call``                ``{"call_id": str, "name": str}``
``tool/result``              ``{"call_id": str, "content": str, "is_error": bool}``
``request/header``           provider / model / toolsFingerprint / toolNames / reason
``turn/start`` 等标记        ``{}``（``turn``/``step`` 单独做字段，不放进 payload）
===========================  =======================================================

只用标准库，Python 3.10+。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

SESSION_FORMAT_VERSION = 1
HEADER_TYPE = "session"
REQUEST_HEADER = "request/header"
EVENT_FIELDS = ("seq", "kind", "turn", "step", "ts", "payload")

# 会产生模型可见消息的事件类型，其余事件（turn/start、request/header…）只改状态。
MESSAGE_KINDS = ("system/message", "user/message", "assistant/message", "tool/result")


class LogCorrupt(ValueError):
    """日志在提交前缀里损坏。带上第几行，因为「第几行坏了」是唯一能拿去修的信息。"""

    def __init__(self, line: int, reason: str) -> None:
        super().__init__(f"session log line {line}: {reason}")
        self.line = line
        self.reason = reason


def _json_snapshot(value: Any) -> Any:
    """把值拷成无损 JSON 快照；拷不动就当场报错，而不是等落盘时才失败。

    走一遍 dumps/loads 同时完成三件事：深拷贝、拒绝非 JSON 类型、拒绝 NaN/Infinity
    （DSH 的 append 也在写入处直接抛错，理由相同：日志是事实来源，坏事件要在写入点暴露）。
    """
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"event payload is not losslessly JSON-serializable: {exc}") from exc


def _safe_segment(value: str) -> str:
    """把会话 id 变成一个安全的路径段：既不越出目录，也不因过滤而互相撞车。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", value)[:64] or "session"
    if safe != value:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}-{digest}"
    return safe


def default_log_path(session_id: str, *, directory: str | Path = "sessions") -> Path:
    """默认落盘路径：目录 + 会话 id + 时间戳，同一会话的多次运行不会互相覆盖。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return Path(directory) / f"{_safe_segment(session_id)}-{stamp}.jsonl"


@dataclass
class Event:
    """一条会话事件。``seq`` 由日志分配，写入之后不再改变。"""

    seq: int
    kind: str
    payload: dict[str, Any]
    ts: float
    turn: int = 0
    step: int = 0

    def to_record(self) -> dict[str, Any]:
        """转成落盘的 JSON 记录，字段顺序固定，方便人肉 diff。"""
        return {
            "seq": self.seq,
            "kind": self.kind,
            "turn": self.turn,
            "step": self.step,
            "ts": self.ts,
            "payload": self.payload,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any], line: int) -> "Event":
        """从记录还原事件，缺字段或类型不对就带上行号报错。"""
        missing = [field for field in EVENT_FIELDS if field not in record]
        if missing:
            raise LogCorrupt(line, f"event is missing required field(s): {', '.join(missing)}")
        for field in ("seq", "turn", "step"):
            if not isinstance(record[field], int) or isinstance(record[field], bool):
                raise LogCorrupt(line, f"field {field!r} must be an integer")
        if not isinstance(record["kind"], str) or not record["kind"]:
            raise LogCorrupt(line, "field 'kind' must be a non-empty string")
        if not isinstance(record["ts"], (int, float)) or isinstance(record["ts"], bool):
            raise LogCorrupt(line, "field 'ts' must be a number")
        if not isinstance(record["payload"], dict):
            raise LogCorrupt(line, "field 'payload' must be a JSON object")
        return cls(
            seq=record["seq"],
            kind=record["kind"],
            payload=record["payload"],
            ts=float(record["ts"]),
            turn=record["turn"],
            step=record["step"],
        )


def tools_fingerprint(tools: Sequence[str]) -> str:
    """工具名列表的指纹。按顺序参与哈希：与 DSH 的 headerEquals 一致，
    工具的增删和改序都算变化——工具列表是提示前缀的一部分，改序一样会打断缓存。"""
    joined = "\n".join(str(name) for name in tools)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def canonical_header(*, provider: str, model: str, tools: Sequence[str]) -> dict[str, Any]:
    """规范化请求头。规范化之后才能用相等判断决定「要不要再写一条 request/header」。"""
    return {
        "provider": str(provider),
        "model": str(model),
        "toolNames": [str(name) for name in tools],
        "toolsFingerprint": tools_fingerprint(tools),
    }


def _header_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("provider", "model", "toolsFingerprint")
    return all(left.get(key) == right.get(key) for key in keys)


class SessionLog:
    """一份只追加的会话日志。写入返回 seq，读取按 seq；除此之外没有别的访问方式。"""

    def __init__(
        self,
        session_id: str = "session",
        *,
        cwd: str | None = None,
        events: Iterable[Event] = (),
        warnings: Iterable[str] = (),
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.created_at = time.time()
        self.warnings = list(warnings)  # 载入时的可恢复问题（例如丢掉的残行）
        self._events: list[Event] = list(events)
        self._lock = threading.Lock()

    # ---- 读 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def next_seq(self) -> int:
        """下一个事件会拿到的 seq。等于日志长度，不是单独维护的计数器。"""
        return len(self._events)

    def event(self, seq: int) -> Event | None:
        """按序号读一条事件；越界返回 None（不抛异常，因为「截断到末尾之后」是常态）。"""
        if 0 <= seq < len(self._events):
            return self._events[seq]
        return None

    def since(self, seq: int) -> list[Event]:
        """返回 seq **之后**（不含 seq）的事件。``since(-1)`` 就是全量。"""
        return [event for event in self._events if event.seq > seq]

    # ---- 写 ---------------------------------------------------------------

    def append(self, kind: str, payload: dict[str, Any] | None = None, *, turn: int = 0, step: int = 0) -> int:
        """追加一条事件并返回它的 seq。

        先做 JSON 快照再入日志：调用方之后改动传入的 dict，不会改变已经写进去的事实。
        seq 的分配和 append 在同一个锁里，所以并发调用拿到的 seq 不会撞车也不会跳号
        （DSH 用的是「seq = 日志下标」这条更简单的路子，代价是同一会话只能有一个写入者）。
        """
        if not isinstance(kind, str) or not kind:
            raise ValueError("event kind must be a non-empty string")
        snapshot = _json_snapshot(payload if payload is not None else {})
        with self._lock:
            event = Event(seq=len(self._events), kind=kind, payload=snapshot, ts=time.time(), turn=turn, step=step)
            self._events.append(event)
        return event.seq

    def log_request_header(self, *, provider: str, model: str, tools: Sequence[str]) -> int | None:
        """把当前请求配置写进日志；与上一条完全相同时不写，返回 None。

        模型每被请求一次就写一条 header 会把日志淹掉，所以只有变化时才落一条
        （DSH 的 agent loop 用同一个 headerEquals 判断，另外还把「工具增删」单独写成
        一条 developer/message 告诉模型，见 tool_set_changes）。
        """
        header = canonical_header(provider=provider, model=model, tools=tools)
        current = self.request_header()
        if current is not None and _header_equal(current, header):
            return None
        reason = "initial" if current is None else "change"
        return self.append(REQUEST_HEADER, {**header, "reason": reason})

    def log_request_context(self, *, provider: str, model: str, context_window: int | None = None) -> int | None:
        """记录这次请求的路由元信息（provider/model/窗口）。与上一条相同就不写。"""
        context = {"provider": str(provider), "model": str(model)}
        if context_window is not None:
            context["contextWindow"] = int(context_window)
        if self.request_context() == context:
            return None
        return self.append("request/context", context)

    # ---- 由日志推导的辅助 --------------------------------------------------

    def request_header(self) -> dict[str, Any] | None:
        """折出当前生效的请求头：取最后一条 request/header。

        常见做法是在内存里再存一份「当前配置」，重启后它就和日志分叉了。
        这里坚持每次从日志折一遍（对应 DSH 的 foldRequestHeader），没有第二份状态可分叉。
        """
        header: dict[str, Any] | None = None
        for event in self._events:
            if event.kind == REQUEST_HEADER:
                header = event.payload
        return header

    def request_context(self) -> dict[str, Any] | None:
        context: dict[str, Any] | None = None
        for event in self._events:
            if event.kind == "request/context":
                context = event.payload
        return context

    def request_headers(self) -> list[dict[str, Any]]:
        """所有 request/header 事件，按写入顺序。用来做「工具集什么时候变的」这类回溯。"""
        return [event.payload for event in self._events if event.kind == REQUEST_HEADER]

    def tool_set_changes(self) -> list[str]:
        """相邻两条 request/header 之间的工具集差异，形如 ``['+read', '-bash']``。

        日志里存了工具指纹，所以「这次运行的工具集和上次不一样」是可以事后检测出来的；
        没有指纹的话，只能看到消息历史莫名其妙变了。
        """
        names = [header.get("toolNames", []) for header in self.request_headers()]
        if len(names) < 2:
            return []
        previous, current = set(names[-2]), set(names[-1])
        added = [f"+{name}" for name in names[-1] if name not in previous]
        removed = [f"-{name}" for name in names[-2] if name not in current]
        return added + removed

    # ---- 落盘与载入 --------------------------------------------------------

    def header_record(self) -> dict[str, Any]:
        """文件第一行的头记录：版本、会话 id、工作目录。"""
        record: dict[str, Any] = {
            "type": HEADER_TYPE,
            "version": SESSION_FORMAT_VERSION,
            "id": self.session_id,
            "createdAt": self.created_at,
        }
        if self.cwd is not None:
            record["cwd"] = self.cwd
        return record

    def save(self, path: str | Path | None = None) -> Path:
        """整份日志落盘：第一行头记录，之后一行一个事件。

        写临时文件再同目录改名，所以读者要么看到完整文件，要么什么都看不到——
        不会读到写了一半的日志（这也是「半行」只可能来自进程被杀的原因）。
        """
        target = Path(path) if path is not None else default_log_path(self.session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(self.header_record(), ensure_ascii=False)]
        lines.extend(json.dumps(event.to_record(), ensure_ascii=False) for event in self._events)
        text = "\n".join(lines) + "\n"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: str | Path, *, strict: bool = False) -> "SessionLog":
        """从 JSONL 载入并校验。

        - 头记录：版本必须是本文件认识的版本，读到更新版本直接拒绝（不要猜）；
        - 事件行：必要字段齐全、seq 从 0 连续；
        - 提交前缀里的坏行 → ``LogCorrupt``，异常里带行号；
        - 最后一行没有结尾换行 → 进程被杀的残行，丢弃并记进 ``warnings``；
          ``strict=True`` 时改为报错，适合「宁可失败也不要丢数据」的场合。
        """
        source = Path(path)
        text = source.read_text(encoding="utf-8")
        warnings: list[str] = []
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # 正常的结尾换行
        elif lines:
            torn = lines.pop()  # 没有结尾换行：这一行从未被提交
            number = len(lines) + 1
            message = f"line {number}: dropped incomplete final line ({len(torn)} bytes, no trailing newline)"
            if strict:
                raise LogCorrupt(number, "incomplete final line (torn tail) and strict loading was requested")
            warnings.append(message)

        header: dict[str, Any] | None = None
        events: list[Event] = []
        for number, raw in enumerate(lines, start=1):
            if not raw.strip():
                warnings.append(f"line {number}: skipped blank line")
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LogCorrupt(number, f"not valid JSON ({exc.msg})") from exc
            if not isinstance(record, dict):
                raise LogCorrupt(number, "record is not a JSON object")
            if record.get("type") == HEADER_TYPE:
                if header is not None:
                    raise LogCorrupt(number, "duplicate session header")
                version = record.get("version")
                if version != SESSION_FORMAT_VERSION:
                    raise LogCorrupt(
                        number,
                        f"unsupported session format version {version!r}; this build reads v{SESSION_FORMAT_VERSION}",
                    )
                header = record
                continue
            if header is None:
                raise LogCorrupt(number, "event appears before the session header")
            event = Event.from_record(record, number)
            if event.seq != len(events):
                raise LogCorrupt(number, f"event seq {event.seq} is not contiguous (expected {len(events)})")
            events.append(event)

        if header is None:
            raise LogCorrupt(1, "no session header; this file is not a session log")
        log = cls(str(header.get("id", "session")), cwd=header.get("cwd"), events=events, warnings=warnings)
        log.created_at = float(header.get("createdAt", log.created_at))
        return log

    def replayer(self) -> "Replayer":
        """绑一份回放器。回放器只读日志，不会往日志里写东西。"""
        return Replayer(self)


class Replayer:
    """把日志重放成模型可见的消息列表。

    只由日志推导，**不另存一份状态**：同一个 seq 调用两次必须得到完全一样的结果。
    这正是确定性测试的基础——失败场景记一份日志，之后不调模型就能复现同一串消息。
    """

    def __init__(self, log: SessionLog) -> None:
        self._log = log
        # 上一次 messages_upto 为保持历史合法而丢掉的悬空 tool_use 数量（诊断用）。
        self.last_dropped_calls = 0

    def messages_upto(self, seq: int | None = None) -> list[dict[str, Any]]:
        """重放 seq（含）为止的事件为消息列表；``None`` 表示整份日志。

        规则：标记事件不产生消息；相邻的 tool/result 合并成一条 user 消息（厂商要求一批
        结果紧跟在一次调用之后）；截断落在 step 中间时，没有结果的 tool_use 会被丢掉——
        否则重放出来的历史不是合法的请求前缀，下一次请求会被厂商直接拒绝。
        """
        messages: list[dict[str, Any]] = []
        pending: dict[str, int] = {}  # tool_call_id -> 它的 tool_use 块所在消息的下标
        results_index: int | None = None  # 当前这条「工具结果」user 消息的下标

        for event in self._log.events:
            if seq is not None and event.seq > seq:
                break
            kind = event.kind
            if kind == "assistant/message":
                message = self._assistant_message(event)
                messages.append(message)
                results_index = None
                for block in message["content"]:
                    if block["type"] == "tool_use":
                        pending[block["id"]] = len(messages) - 1
            elif kind == "tool/result":
                call_id = str(event.payload.get("call_id", ""))
                block = {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": str(event.payload.get("content", "")),
                    "is_error": bool(event.payload.get("is_error", False)),
                }
                if results_index is not None and results_index == len(messages) - 1:
                    messages[results_index]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                    results_index = len(messages) - 1
                pending.pop(call_id, None)
            elif kind in ("system/message", "user/message"):
                messages.append(
                    {
                        "role": "system" if kind == "system/message" else "user",
                        "content": str(event.payload.get("content", "")),
                    }
                )
                results_index = None
            # 其余事件（turn/start、step/start、request/header、developer/message…）不改历史。

        dropped = 0
        for call_id, index in pending.items():
            kept = [
                block
                for block in messages[index]["content"]
                if not (block["type"] == "tool_use" and block["id"] == call_id)
            ]
            messages[index]["content"] = kept
            dropped += 1
        if dropped:
            # 空了内容或只剩空的 assistant 消息不要留在历史里（DSH 也把空 assistant 回复挡在转录之外）。
            messages = [m for m in messages if not (m["role"] == "assistant" and not m["content"])]
        self.last_dropped_calls = dropped
        return messages

    def last_boundary(self, seq: int | None = None) -> int:
        """≤seq 的最后一个安全截断点：一次 turn/end，或一次工具已全部配对的 step/end。

        分片续跑从这个 seq 接着重放，就不会看到半截 step。找不到时返回 -1。
        """
        boundary = -1
        pending: set[str] = set()
        for event in self._log.events:
            if seq is not None and event.seq > seq:
                break
            if event.kind == "assistant/message":
                for call in event.payload.get("tool_calls", []):
                    pending.add(str(call.get("id", "")))
            elif event.kind == "tool/result":
                pending.discard(str(event.payload.get("call_id", "")))
            elif event.kind == "step/end" and not pending:
                boundary = event.seq
            elif event.kind == "turn/end":
                pending.clear()
                boundary = event.seq
        return boundary

    @staticmethod
    def _assistant_message(event: Event) -> dict[str, Any]:
        """一条 assistant/message 事件 → 一条历史消息。"""
        content: list[dict[str, Any]] = []
        text = event.payload.get("text")
        if text:
            content.append({"type": "text", "text": str(text)})
        for call in event.payload.get("tool_calls", []):
            content.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id", "")),
                    "name": str(call.get("name", "")),
                    "input": _json_snapshot(call.get("args", {})),
                }
            )
        return {"role": "assistant", "content": content}
