"""工作记忆：待办列表、陈旧提醒、计划模式闸门。

「短期记忆」在 harness 里有两层：

1. 消息历史本身（hx-01/04/07 已讲）。滑动窗口、token 缓冲这些经典做法，
   在真实 harness 里都对应「消息历史 + 压缩」，不是单独的模块。
2. 模型自己显式维护的结构化工作状态：这次任务拆成哪几步、做到哪一步。
   本文件只管这一层。

三个部件：

- ``TodoList``      —— 整表覆盖写入，带校验；渲染成稳定的紧凑文本；
  和上一版做 diff，告诉模型这次改了什么。
- ``should_remind`` —— 纯函数：好几轮没更新待办、又还有没做完的项，就在本轮附加提醒。
- ``PlanGate``      —— 计划模式下只放行只读工具；退出计划模式必须带用户的显式批准。

只用标准库，Python 3.10+。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUSES = ("pending", "in_progress", "completed")

# 渲染时每种状态的标记。用 ASCII，避免不同终端对符号宽度的理解不一致。
MARKERS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}

# 一张表最多多少项。lcc s05 用的也是 20；超过这个数，多半是模型把子步骤也写进来了。
MAX_TODOS = 20

# 连续多少轮没碰待办就提醒（教学示意；lcc s05 取 3）。
REMIND_AFTER_ROUNDS = 3

REMINDER_TEXT = "<reminder>待办列表已经好几轮没更新了；请用 todo_write 同步进度。</reminder>"


class TodoError(ValueError):
    """模型写入的待办表不合法。错误文本原样回给模型，让它改了再写。"""


@dataclass(frozen=True)
class TodoItem:
    id: str
    content: str
    status: str


def validate_todos(raw: Any, *, allow_parallel: bool = False) -> list[TodoItem]:
    """把模型给的整张表校验成规范形式；任何一处不合法就整体拒绝，不做部分写入。"""
    if not isinstance(raw, list):
        raise TodoError("todos 必须是数组")
    if len(raw) > MAX_TODOS:
        raise TodoError(f"最多 {MAX_TODOS} 项，收到 {len(raw)} 项")
    items: list[TodoItem] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TodoError(f"todos[{index}] 必须是对象")
        item_id = str(entry.get("id", "")).strip()
        content = str(entry.get("content", "")).strip()
        status = entry.get("status")
        if not item_id or not content:
            raise TodoError(f"todos[{index}] 缺少 id 或 content")
        if item_id in seen:
            raise TodoError(f"todos[{index}] 的 id {item_id!r} 重复")
        if status not in STATUSES:
            raise TodoError(f"todos[{index}] 的状态 {status!r} 不合法，只能是 {', '.join(STATUSES)}")
        seen.add(item_id)
        items.append(TodoItem(item_id, content, status))
    active = sum(item.status == "in_progress" for item in items)
    if not allow_parallel and active > 1:
        raise TodoError(f"同时只能有一项 in_progress，收到 {active} 项")
    return items


def render_todos(items: list[TodoItem]) -> str:
    """给模型看的紧凑文本。只取决于表的内容，不带时间戳、不重排：同一张表永远是同一串字节。"""
    if not items:
        return "(no todos)"
    lines = [f"{MARKERS[item.status]} {item.id}. {item.content}" for item in items]
    done = sum(item.status == "completed" for item in items)
    lines.append(f"({done}/{len(items)} completed)")
    return "\n".join(lines)


@dataclass(frozen=True)
class TodoDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    # (id, 旧状态, 新状态)
    changed: tuple[tuple[str, str, str], ...] = ()

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def describe(self) -> str:
        if self.is_empty():
            return "no changes"
        parts = [f"+{i}" for i in self.added] + [f"-{i}" for i in self.removed]
        parts += [f"{i}: {old} -> {new}" for i, old, new in self.changed]
        return "; ".join(parts)


def diff_todos(old: list[TodoItem], new: list[TodoItem]) -> TodoDiff:
    """按 id 对齐两版表。用 id 而不是 content 对齐，模型改写措辞时不会被误判成「删一项加一项」。"""
    before = {item.id: item for item in old}
    after = {item.id: item for item in new}
    added = tuple(i for i in after if i not in before)
    removed = tuple(i for i in before if i not in after)
    changed = tuple(
        (i, before[i].status, after[i].status)
        for i in after
        if i in before and before[i].status != after[i].status
    )
    return TodoDiff(added, removed, changed)


@dataclass
class TodoList:
    """一个会话的待办状态。存在消息历史之外，所以压缩历史不会把它摘丢。"""

    allow_parallel: bool = False
    items: list[TodoItem] = field(default_factory=list)
    last_write_round: int | None = None

    def write(self, raw: Any, *, round_no: int) -> str:
        """todo_write 工具的实现：校验 -> 整表替换 -> 返回工具结果文本。

        校验失败抛 TodoError，表和 last_write_round 都不动：失败的写入不算「更新过」。
        """
        new_items = validate_todos(raw, allow_parallel=self.allow_parallel)
        change = diff_todos(self.items, new_items)
        self.items = new_items
        self.last_write_round = round_no
        return f"Updated todos ({change.describe()})\n{render_todos(new_items)}"

    def has_open_items(self) -> bool:
        return any(item.status != "completed" for item in self.items)

    def snapshot(self) -> list[dict[str, str]]:
        """写进会话日志的形状；replay 用它重建。"""
        return [{"id": i.id, "content": i.content, "status": i.status} for i in self.items]

    @classmethod
    def replay(cls, events: list[dict[str, Any]], *, allow_parallel: bool = False) -> TodoList:
        """从会话日志重建：最后一次写入为准（last-write-wins），与历史有没有被压缩无关。"""
        todo = cls(allow_parallel=allow_parallel)
        for event in events:
            if event.get("type") == "todo/write":
                todo.items = validate_todos(event["todos"], allow_parallel=allow_parallel)
                todo.last_write_round = event["round"]
        return todo


def todo_context(todo: TodoList) -> str:
    """压缩之后由 harness 重新放进运行时上下文的一段。表做完了就不再占位置。"""
    if not todo.has_open_items():
        return ""
    return "<current_todos>\n" + render_todos(todo.items) + "\n</current_todos>"


def should_remind(
    last_write_round: int | None,
    current_round: int,
    has_open_items: bool,
    *,
    last_reminded_round: int | None = None,
    after: int = REMIND_AFTER_ROUNDS,
) -> bool:
    """本轮要不要附加「更新待办」提醒。

    - 从没写过待办：不提醒。单步任务本来就不需要待办，逼它写只会制造噪声。
    - 没有未完成项：不提醒。表已经做完，再催就是在催一份不存在的进度。
    - 否则从「上次写入」和「上次提醒」里较晚的那个算起，满 after 轮才提醒，避免每轮都提醒。
    """
    if last_write_round is None or not has_open_items:
        return False
    since = max(last_write_round, last_reminded_round if last_reminded_round is not None else last_write_round)
    return current_round - since >= after


EXIT_PLAN_TOOL = "exit_plan_mode"


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str


@dataclass
class PlanGate:
    """计划模式闸门：只读阶段先出计划，用户批准后才放开写工具。"""

    read_only_tools: frozenset[str]
    active: bool = False

    def check(self, tool_name: str) -> GateDecision:
        if not self.active:
            return GateDecision(True, "not in plan mode")
        if tool_name in self.read_only_tools:
            return GateDecision(True, f"{tool_name} is read-only")
        if tool_name == EXIT_PLAN_TOOL:
            # 出口本身不是只读工具，但必须放行，否则模型永远提交不了计划
            return GateDecision(True, "the exit tool must stay callable in plan mode")
        return GateDecision(False, f"plan mode blocks {tool_name}; present the plan with {EXIT_PLAN_TOOL} first")

    def enter(self) -> None:
        self.active = True

    def exit(self, plan: str, *, approved_by_user: bool) -> GateDecision:
        """退出需要两样东西：一份像样的计划，和用户的批准。模型自己说「我觉得可以了」不算。"""
        if not self.active:
            return GateDecision(False, "not in plan mode")
        if not plan.strip().startswith("#"):
            return GateDecision(False, "the plan must be markdown starting with a # heading")
        if not approved_by_user:
            return GateDecision(False, "the user chose to keep planning; revise and present again")
        self.active = False
        return GateDecision(True, "plan approved; write tools are allowed from the next step")
