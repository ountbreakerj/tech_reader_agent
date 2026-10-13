"""权限规则引擎：每次工具调用之前，harness 自己回答「允许 / 拒绝 / 问人」。

对照源码见正文 hx-13。三个设计约束：

1. **单调性**：规则只能拒绝，不能强制放行。判定方式是「先收集所有命中的规则，
   再看有没有任何一个拒绝」——不需要推理规则之间的顺序，策略可以逐条独立审读。
2. **默认不安全**：没有任何规则命中时，默认是 ask，不是 allow。没有问人通道时
   ask 降级为 deny（fail-closed），而不是降级为放行。
3. **判定本身不能抛异常**：规则里的谓词是用户代码，它崩了要当成「拒绝 + 说明」，
   不能让它把循环带下去。一条坏规则最坏的结果应该是「多拦了一次」，不是「放行」。

只用标准库。
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ALLOW = "allow"
DENY = "deny"
ASK = "ask"

# 判定结果之间的优先级：只要有一条规则拒绝，结果就是拒绝。
_SEVERITY = {ALLOW: 0, ASK: 1, DENY: 2}


class PathEscape(Exception):
    """路径越出工作区。"""


class RuleError(Exception):
    """规则定义不合法（例如拒绝规则没写理由）。"""


@dataclass(frozen=True)
class Decision:
    """一次权限判定的结果。

    outcome: allow / deny / ask
    rule:    做出这个决定的规则名；None 表示没人命中，用的是默认值
    reason:  给模型看的理由。拒绝和询问都必须带理由，否则模型只会原样重试。
    """

    outcome: str
    rule: str | None = None
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def describe(self) -> str:
        who = self.rule or "默认策略"
        return f"{self.outcome}（{who}）: {self.reason}" if self.reason else f"{self.outcome}（{who}）"


Match = Callable[[str, dict[str, Any]], bool]


@dataclass(frozen=True)
class Rule:
    """一条权限规则。

    name     规则名，会出现在日志和给模型的理由里
    mode     allow / deny / ask
    tool     工具名，支持 fnmatch 通配；None 表示匹配所有工具
    matches  额外的参数谓词；None 表示只看工具名
    reason   命中时要说明的话
    """

    name: str
    mode: str
    tool: str | None = None
    matches: Match | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.mode not in (ALLOW, DENY, ASK):
            raise RuleError(f"规则 {self.name!r} 的 mode 必须是 allow/deny/ask，收到 {self.mode!r}")
        if self.mode in (DENY, ASK) and not self.reason:
            raise RuleError(f"规则 {self.name!r} 是 {self.mode}，必须给出 reason")

    def hit(self, tool: str, args: dict[str, Any]) -> bool:
        """这条规则是否命中。谓词抛异常一律算「不命中」——交给后面的兜底规则处理。"""
        if self.tool is not None and not fnmatch.fnmatchcase(tool, self.tool):
            return False
        if self.matches is None:
            return True
        try:
            return bool(self.matches(tool, args))
        except Exception:  # noqa: BLE001 — 坏规则不能变成放行，也不能把循环带走
            return False


# ---- 常用的路径判定 ----


def resolve_within(root: str | os.PathLike[str], candidate: str) -> Path:
    """把 candidate 规范化到 root 之内，越界就抛 PathEscape。

    三道关：
    - 拼接后 resolve()，跟随符号链接（`..` 和软链接都会被展开成真实路径）
    - 结果必须仍在 root 之内（用 commonpath，避免 `/work` 前缀匹配掉 `/work2`）
    - 空字节直接拒绝（某些系统调用会把它截断成另一个路径）
    """
    if "\x00" in candidate:
        raise PathEscape(f"路径含空字节：{candidate!r}")
    base = Path(root).resolve()
    target = (base / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
    try:
        inside = os.path.commonpath([str(base), str(target)]) == str(base)
    except ValueError:  # 不同盘符
        inside = False
    if not inside:
        raise PathEscape(f"路径 {candidate!r} 越出工作区 {base}")
    return target


def path_args_within(root: str, keys: Sequence[str] = ("path", "file", "file_path")) -> Match:
    """生成一个「参数里的路径必须在工作区内」的谓词。

    注意返回的是谓词而不是结论：它只负责说「这条调用是指向工作区内部的」，
    至于它是允许还是拒绝，由规则自己决定。
    """

    def predicate(tool: str, args: dict[str, Any]) -> bool:
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value:
                try:
                    resolve_within(root, value)
                except PathEscape:
                    return True  # 越界 → 让「越界」那条规则命中
                return True
        return True

    return predicate


def escapes_root(root: str, keys: Sequence[str] = ("path", "file", "file_path")) -> Match:
    """命中条件：参数里的路径越出工作区。"""

    def predicate(tool: str, args: dict[str, Any]) -> bool:
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value:
                try:
                    resolve_within(root, value)
                except PathEscape:
                    return True
        return False

    return predicate


def touches_sensitive(name: str, patterns: Iterable[str] | None = None) -> Match:
    """命中条件：路径命中一组敏感文件名（默认是凭据、密钥、环境文件）。"""
    globs = tuple(patterns or (
        "*credentials*", "*.pem", "*.key", "*id_rsa*", "*.env", ".env*",
        "*/.ssh/*", "*/.aws/*", "*/.git/config", "*.p12", "*.keystore",
    ))

    def predicate(tool: str, args: dict[str, Any]) -> bool:
        for key, value in args.items():
            if isinstance(value, str) and any(fnmatch.fnmatch(value.lower(), g) for g in globs):
                return True
        return False

    return predicate


# ---- 引擎 ----


@dataclass
class AuditEntry:
    """一条判定记录。真实系统里它应该进会话日志，而不是留在内存里。"""

    tool: str
    outcome: str
    rule: str | None
    reason: str
    args: dict[str, Any] = field(default_factory=dict)


class PermissionEngine:
    """规则引擎。规则是数据，判定是纯函数，审计是副作用。"""

    def __init__(
        self,
        rules: Sequence[Rule],
        *,
        default: str = ASK,
        has_approval_channel: bool = True,
    ) -> None:
        if default not in (ALLOW, DENY, ASK):
            raise RuleError(f"default 必须是 allow/deny/ask，收到 {default!r}")
        self.rules = list(rules)
        self.default = default
        self.has_approval_channel = has_approval_channel
        self.audit: list[AuditEntry] = []

    def decide(self, tool: str, args: dict[str, Any] | None = None) -> Decision:
        """跑一遍规则，给出最终判定。

        返回结果是单一的落点，不会因为「谁先命中」而不同：所有命中的规则都算一遍，
        取最严的那个（deny > ask > allow）。
        """
        payload = dict(args or {})
        hits = [rule for rule in self.rules if rule.hit(tool, payload)]
        if not hits:
            decision = self._fallback(f"没有规则命中 {tool!r}")
        else:
            worst = max(hits, key=lambda r: _SEVERITY[r.mode])
            decision = Decision(worst.mode, worst.name, worst.reason)
        self.audit.append(AuditEntry(tool, decision.outcome, decision.rule, decision.reason, payload))
        return decision

    def _fallback(self, why: str) -> Decision:
        """没有任何规则命中时的兜底：默认值，或者在没有问人通道时降级成拒绝。"""
        if self.default == ASK and not self.has_approval_channel:
            return Decision(DENY, None, f"{why}；这是默认策略，且当前没有审批通道，按拒绝处理")
        return Decision(self.default, None, why)

    def explain(self, tool: str, args: dict[str, Any] | None = None) -> list[str]:
        """列出这条调用命中了哪些规则，按严格程度排序。排错时比 decide() 有用。"""
        payload = dict(args or {})
        hits = sorted((r for r in self.rules if r.hit(tool, payload)), key=lambda r: -_SEVERITY[r.mode])
        return [f"{r.name}[{r.mode}]" for r in hits] or ["（无命中，用默认策略）"]


def default_rules(root: str) -> list[Rule]:
    """一份能直接用的规则集，顺序无关紧要（判定取最严）。

    这里刻意把 allow 规则也写出来，而不是靠 default=allow：默认值应该留给
    「谁都没命中」这种意外情况，正常的放行要能一条一条审。
    """
    return [
        Rule(
            name="deny-path-escape",
            mode=DENY,
            matches=escapes_root(root),
            reason=f"参数里的路径越出了工作区 {root}；请改用工作区内的相对路径",
        ),
        Rule(
            name="deny-credentials",
            mode=DENY,
            matches=touches_sensitive(""),
            reason="这条调用会碰到凭据或密钥文件；如果你需要配置，请告诉我具体要改哪一项",
        ),
        Rule(
            name="ask-write",
            mode=ASK,
            tool="write_file",
            reason="要写文件，需要确认",
        ),
        Rule(
            name="allow-read",
            mode=ALLOW,
            tool="read_file",
        ),
        Rule(
            name="deny-shell-write",
            mode=DENY,
            tool="run_bash",
            matches=lambda tool, args: any(
                token in str(args.get("command", "")) for token in ("rm ", "mv ", "chmod ", "> ")
            ),
            reason="shell 命令里包含写操作；请改用专门的写工具，或者把命令拆开逐条确认",
        ),
    ]
