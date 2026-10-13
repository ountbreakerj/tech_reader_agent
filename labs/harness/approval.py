"""审批与升档：从「自动放行」到「停下来问人」，以及提问本身怎么设计。

对照源码见正文 hx-16。权限引擎给出 allow / deny / ask 三种判定，真正难的是
ask 之后发生的事：谁来问、问什么、用户批准的范围有多大、批准过一次之后还要不要
再问、用户不同意时模型收到什么。这四件事决定了「审批」是一个真闸门还是走过场。

本模块给出一个能跑的审批层，关键设计：

- **升档**：调用先过规则；规则说 allow 就放行，说 ask 才进入审批队列。
  审批不是「每次都问」，而是「规则判断它需要人看」。
- **批准的生命周期**：一次批准只对「这一个工具 + 这个作用范围」有效，默认只对
  当前这一轮有效，可以选择扩展成会话内有效。范围能扩展，但必须显式扩展。
- **拒绝也是结果**：用户拒绝之后，模型收到的是一句话说明 + 可选的替代方案，
  而不是一个异常。没有替代方案时它就只能重试，这正是它反复请求的原因。
- **fail-closed**：没有审批通道（无人值守、非交互运行）时，ask 降级为 deny，
  而不是降级为 allow。

只用标准库。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .permissions import ASK, Decision, PermissionEngine

APPROVED = "approved"
REJECTED = "rejected"
NO_CHANNEL = "no_channel"


class ApprovalError(Exception):
    """审批层用法错误。"""


@dataclass(frozen=True)
class ApprovalRequest:
    """一次要向人展示的请求。字段是给人看的，不是给模型看的。"""

    tool: str
    args: dict
    reason: str
    risk: str = "write"          # read / write / destructive / network
    preview: str = ""            # 命令原文、diff 摘要等
    alternatives: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [f"工具：{self.tool}", f"风险：{self.risk}", f"原因：{self.reason}"]
        if self.preview:
            lines.append("内容：")
            lines.extend(f"  {line}" for line in self.preview.splitlines()[:20])
        if self.alternatives:
            lines.append("可以选择：" + " / ".join(self.alternatives))
        return "\n".join(lines)


@dataclass
class Grant:
    """一次批准授予的权限范围。范围越小越好。

    三种范围：
    - once：只对刚批准的那一次调用有效，之后的调用一律重新走审批。它存在的意义
      是让 `check()` 能返回「已批准」，而不是让后续调用白白通过。
    - turn：本轮之内对同一工具有效，新一轮开始时作废。
    - session：整个会话有效，直到用户显式撤销。范围最大，只在用户明确选择时授予。
    """

    tool: str
    scope: str                   # once / turn / session
    args_key: str = ""           # 精确到参数（例如命令原文）；空表示该工具任意参数
    active: bool = True

    def covers(self, tool: str, args: dict) -> bool:
        if not self.active or self.scope == "once":
            return False
        if self.tool != tool:
            return False
        if not self.args_key:
            return True
        return _args_key(args) == self.args_key

    def spend(self) -> None:
        """一次授权用掉一次。once 用掉即失效；turn / session 不受影响。"""
        if self.scope == "once":
            self.active = False


def _args_key(args: dict) -> str:
    """把参数压成一个稳定的键，用于「只批准这一条命令」这种精确授权。"""
    return "|".join(f"{k}={args[k]!r}" for k in sorted(args))


@dataclass
class ApprovalOutcome:
    """审批的结局。status 是三选一，message 是要喂回模型的话。"""

    status: str
    decision: Decision
    message: str = ""
    grant: Grant | None = None

    @property
    def allowed(self) -> bool:
        # 规则直接放行（status=allow）和人工批准（status=approved）都算通过；
        # 两者的区别留在 status 里，调用方要区分时仍然看得到。
        return self.status in (APPROVED, "allow")


# 审批回调：收到一个请求，返回 (是否批准, 作用范围, 用户备注)。
Approver = Callable[[ApprovalRequest], tuple[bool, str, str]]


def auto_deny(request: ApprovalRequest) -> tuple[bool, str, str]:
    """无人值守时的默认审批人：全部拒绝，并说明原因。"""
    return False, "once", "当前是非交互运行，没有人可以批准这次操作"


def scripted_approver(answers: Sequence[bool], scopes: Sequence[str] | None = None) -> Approver:
    """测试用：按脚本依次回答。用完之后一律拒绝（fail-closed）。"""
    state = {"index": 0}

    def approver(request: ApprovalRequest) -> tuple[bool, str, str]:
        index = state["index"]
        state["index"] += 1
        if index >= len(answers):
            return False, "once", "脚本已经用完了，按拒绝处理"
        scope = scopes[index] if scopes and index < len(scopes) else "once"
        return answers[index], scope, "（脚本回答）"

    return approver


class ApprovalGate:
    """把权限判定和人工审批接起来的一层。"""

    def __init__(
        self,
        engine: PermissionEngine,
        approver: Approver | None = None,
    ) -> None:
        self.engine = engine
        self.approver = approver or auto_deny
        self.grants: list[Grant] = []
        self.history: list[tuple[ApprovalRequest, ApprovalOutcome]] = []
        self._turn = 0

    # ---- 会话与轮次 ----

    def begin_turn(self) -> None:
        """新的一轮开始：本轮之前授予的 turn 级授权全部作废。

        once 授权在 check() 时就已经用掉（active=False），这里顺手清掉；
        session 级授权保留，直到用户显式撤销。
        """
        self._turn += 1
        self.grants = [g for g in self.grants if g.scope == "session"]

    def revoke(self, tool: str | None = None) -> int:
        """撤销授权：不给 tool 就撤销全部，给了就只撤销这个工具的。返回撤掉了多少条。"""
        before = len(self.grants)
        if tool is None:
            self.grants = []
        else:
            self.grants = [g for g in self.grants if g.tool != tool]
        return before - len(self.grants)

    # ---- 主路径 ----

    def check(self, tool: str, args: dict) -> ApprovalOutcome:
        """判定一次调用。返回的 message 会作为错误或说明返回给模型。"""
        decision = self.engine.decide(tool, args)
        if decision.outcome != ASK:
            return ApprovalOutcome(decision.outcome, decision, _describe(decision))

        risk = _risk_of(tool, args)
        # 升档：高危操作不吃之前的授权，每次都重新问。批准过一次 write_file
        # 不代表批准了 `rm -rf`，哪怕它们走的是同一个工具。
        if risk != "destructive":
            for grant in self.grants:
                if grant.covers(tool, args):
                    grant.spend()
                    return ApprovalOutcome(APPROVED, decision, f"已按之前的批准放行（范围：{grant.scope}）")

        if not self.engine.has_approval_channel:
            return ApprovalOutcome(NO_CHANNEL, decision, _fail_closed(decision))

        request = ApprovalRequest(
            tool=tool,
            args=args,
            reason=decision.reason or "这条调用需要人工确认",
            risk=risk,
            preview=_preview_of(tool, args),
            alternatives=_alternatives_of(tool),
        )
        approved, scope, note = self.approver(request)
        if not approved:
            outcome = ApprovalOutcome(REJECTED, decision, _rejected_message(request, note))
            self.history.append((request, outcome))
            return outcome

        # session 授权覆盖这个工具的任意参数——这是一次范围扩大，只在用户明确选择时授予；
        # 高危操作即使用户选了 session，也只记成 once（见上面的升档）。
        if risk == "destructive" and scope != "once":
            scope = "once"
        grant = Grant(tool=tool, scope=scope, args_key=_args_key(args) if scope != "session" else "")
        self.grants.append(grant)
        outcome = ApprovalOutcome(APPROVED, decision, f"用户批准了这次 {tool}（范围：{scope}）", grant)
        self.history.append((request, outcome))
        return outcome


def _describe(decision: Decision) -> str:
    if decision.outcome == "allow":
        return ""
    return f"Error: 调用被拒绝：{decision.reason}"


def _fail_closed(decision: Decision) -> str:
    return (
        "Error: 这次调用需要人工确认，但当前没有审批通道，按拒绝处理。"
        f"原因：{decision.reason}。"
        "如果你认为不必做这一步，请换一种不需要确认的做法。"
    )


def _rejected_message(request: ApprovalRequest, note: str) -> str:
    parts = [f"Error: 用户拒绝了这个操作（{request.tool}）。"]
    if note:
        parts.append(f"用户的说明：{note}")
    parts.append("不要原样重试这个调用。")
    if request.alternatives:
        parts.append("可以改用的做法：" + "；".join(request.alternatives))
    else:
        parts.append("如果需要继续，请先说明你想达成什么，让用户决定怎么做。")
    return " ".join(parts)


def _risk_of(tool: str, args: dict) -> str:
    if tool == "run_bash":
        command = str(args.get("command", ""))
        if any(token in command for token in ("rm ", "rmdir", "dd ", "mkfs", ":(){", "shutdown", "curl ", "wget ")):
            return "destructive"
        return "write"
    if tool in ("read_file", "list_dir", "grep"):
        return "read"
    return "write"


def _preview_of(tool: str, args: dict) -> str:
    if tool == "run_bash":
        return str(args.get("command", ""))
    if tool == "write_file":
        return f"path={args.get('path')}\n" + str(args.get("content", ""))[:400]
    return "\n".join(f"{key}={value!r}" for key, value in sorted(args.items()))[:400]


def _alternatives_of(tool: str) -> tuple[str, ...]:
    table = {
        "run_bash": ("改用 read_file / write_file 这类明确声明的工具", "把命令拆成只读的部分先跑"),
        "write_file": ("先给出要改的内容让用户确认", "改成在临时目录里试写"),
        "delete_file": ("先移动到 .harness/trash 而不是直接删除",),
    }
    return table.get(tool, ())


def summarize(history: Iterable[tuple[ApprovalRequest, ApprovalOutcome]]) -> dict[str, int]:
    """审批历史的小结，适合写进会话日志或给用户看。"""
    counts = {APPROVED: 0, REJECTED: 0, NO_CHANNEL: 0}
    for _, outcome in history:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts
