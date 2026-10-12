"""记忆的生命周期：过期、衰减、替代与陈旧引用。

记忆会「变坏」，而且变坏的方式不止一种，对应的手段也不同：

* 事实过时（项目状态变了）     —— TTL 硬过期：:func:`is_expired`
* 用不上了（检索里一直排不上） —— 分数衰减 + 访问强化：:func:`decayed_score`、:func:`touch`
* 被更正了（偏好改了、旧结论错了）—— 显式替代链：:func:`resolve_supersedes`
* 指向的东西没了（文件被删/改名）—— 陈旧引用检测：:func:`find_stale_references`

:func:`sweep` 把四件事串成一次清理，输出「保留 / 降权 / 删除」三组并写明原因。

时间统一用 epoch 秒（float），省掉时区换算；真实系统里用带时区的 datetime。
本文件里的半衰期、TTL、阈值全是教学示意，不是任何仓库的取值。

只用标准库，Python 3.10+。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

DAY = 86_400.0

# 按类型的寿命（秒）。None 表示这一类不按时间过期。教学示意。
# 项目状态最容易过时，给得最短；用户偏好和反馈不靠时间判断，靠替代链更正。
DEFAULT_TTL_BY_TYPE: Mapping[str, float | None] = MappingProxyType({
    "project": 14 * DAY,
    "reference": 90 * DAY,
    "feedback": None,
    "user": None,
})


@dataclass(frozen=True)
class MemoryRecord:
    """一条记忆。不可变：衰减和强化都返回新对象，读路径不写状态。"""

    id: str
    type: str
    text: str
    created_at: float
    # 衰减时钟的起点。strength 是「在这个时刻」的强度，现在的强度靠计算得出。
    last_accessed_at: float
    strength: float = 1.0
    # 单条覆盖类型的寿命；None 表示按类型走。
    ttl: float | None = None
    # 本条替代了哪些旧记忆。可以多条（合并时一条新记忆替代几条旧的）。
    supersedes: tuple[str, ...] = ()


def decayed_score(record: MemoryRecord, now: float, half_life: float) -> float:
    """``strength · e^(−λΔt)``，λ = ln2 / 半衰期，Δt 从最近一次访问算起。

    纯函数：只算不写。把衰减结果写回记录（并顺手更新时钟）会让
    「最近访问时间」不再表示访问，见本章坑 2。
    """
    if half_life <= 0:
        raise ValueError(f"半衰期必须为正数，收到 {half_life}")
    # 时钟回拨（多机、手改文件）时 Δt 为负，不允许分数因此变大。
    elapsed = max(0.0, now - record.last_accessed_at)
    return record.strength * math.exp(-math.log(2) / half_life * elapsed)


def touch(
    record: MemoryRecord,
    now: float,
    *,
    half_life: float,
    boost: float = 0.3,
    cap: float = 1.0,
) -> MemoryRecord:
    """一次访问：先把衰减结算到 now，再加强化量，封顶 cap，并重置衰减时钟。

    顺序不能反：直接在旧 strength 上加 boost，等于把这段时间的衰减一笔勾销。
    """
    settled = decayed_score(record, now, half_life)
    return replace(record, strength=min(cap, settled + boost), last_accessed_at=now)


def is_expired(
    record: MemoryRecord,
    now: float,
    ttl_by_type: Mapping[str, float | None] = DEFAULT_TTL_BY_TYPE,
) -> bool:
    """TTL 从创建时刻算，不从访问时刻算：被频繁读到不代表这条事实还对。"""
    if record.type not in ttl_by_type:
        # 拼错的类型如果默默按「永不过期」处理，就成了最难发现的那种泄漏。
        raise ValueError(f"记忆 {record.id} 的类型 {record.type!r} 不在 TTL 表里")
    ttl = record.ttl if record.ttl is not None else ttl_by_type[record.type]
    if ttl is None:
        return False
    if ttl <= 0:
        raise ValueError(f"记忆 {record.id} 的 ttl 必须为正数；不过期请用 None")
    return now >= record.created_at + ttl


class SupersedeError(ValueError):
    """替代关系本身不合法：重复 id、成环、分叉，或（严格模式下）悬空引用。"""


@dataclass(frozen=True)
class Resolution:
    """替代链解析结果。"""

    # 每条记录 id → 它所在链的链尾 id（链尾就是它自己时表示没被替代）。
    heads: Mapping[str, str]
    # 没被任何记录替代的 id，按输入顺序。读取时只取这些。
    current: tuple[str, ...]
    # (声明方 id, 不存在的目标 id)。旧记录被删之后，这种引用会自然出现。
    dangling: tuple[tuple[str, str], ...]


def resolve_supersedes(
    records: Sequence[MemoryRecord], *, allow_dangling: bool = False
) -> Resolution:
    """构建替代链，返回每条链的链尾；成环、分叉都给出可读错误而不是死循环。"""
    successor, dangling = _successor_map(records)
    if dangling and not allow_dangling:
        who, target = dangling[0]
        raise SupersedeError(f"{who} 声明替代 {target}，但 {target} 不存在")
    heads: dict[str, str] = {}
    for record in records:
        _walk_to_head(record.id, successor, heads)
    current = tuple(r.id for r in records if r.id not in successor)
    return Resolution(heads=heads, current=current, dangling=tuple(dangling))


def _successor_map(
    records: Sequence[MemoryRecord],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """旧 id → 替代它的新 id。每条旧记录最多一个继任者，否则就是分叉。"""
    ids = [r.id for r in records]
    if len(ids) != len(set(ids)):
        dup = next(i for i in ids if ids.count(i) > 1)
        raise SupersedeError(f"记忆 id {dup} 出现了不止一次")
    known = set(ids)
    successor: dict[str, str] = {}
    dangling: list[tuple[str, str]] = []
    for record in records:
        for target in record.supersedes:
            if target not in known:
                dangling.append((record.id, target))
            elif target in successor:
                raise SupersedeError(
                    f"{target} 同时被 {successor[target]} 和 {record.id} 替代：分叉了，"
                    "该由一条新记忆合并两者"
                )
            else:
                successor[target] = record.id
    return successor, dangling


def _walk_to_head(start: str, successor: Mapping[str, str], heads: dict[str, str]) -> str:
    """沿继任者走到链尾，并把途经的每个节点都记进 heads（下次直接命中）。"""
    path: list[str] = []
    on_path: set[str] = set()
    node = start
    while node not in heads:
        if node in on_path:
            ring = path[path.index(node):] + [node]
            raise SupersedeError("替代链成环：" + " → ".join(ring) + "（箭头读作「被替代为」）")
        path.append(node)
        on_path.add(node)
        nxt = successor.get(node)
        if nxt is None:
            heads[node] = node
            break
        node = nxt
    head = heads[node]
    for visited in path:
        heads[visited] = head
    return head


# 只认这些扩展名。宁可漏报也别误报：总在喊「已失效」的检查很快会被所有人忽略。
PATH_EXTENSIONS = frozenset({
    "py", "ipynb", "md", "txt", "json", "yaml", "yml", "toml", "ini", "cfg",
    "js", "mjs", "ts", "tsx", "html", "css", "sh", "ps1", "rs", "go", "java",
})
_TOKEN_SPLIT = re.compile(r"[\s`'\"()\[\]{}<>,;，。；：、（）【】「」《》！？]+")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


def path_candidates(text: str) -> list[str]:
    """从正文里抽出「看起来像仓库内路径」的片段，去重、保序、统一成正斜杠。

    必须带目录分隔符：裸文件名（build.mjs）和 Node.js 这种词在字面上分不开，
    一律不查。代价是根目录下的文件被删了查不出来。
    """
    found: list[str] = []
    for token in _TOKEN_SPLIT.split(text):
        token = _LINE_SUFFIX.sub("", token.replace("\\", "/").rstrip(".:!?"))
        token = token.removeprefix("./")
        if "/" not in token or "://" in token:
            continue
        last = token.rstrip("/").rsplit("/", 1)[-1]
        ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
        if (token.endswith("/") or ext in PATH_EXTENSIONS) and token not in found:
            found.append(token)
    return found


def find_stale_references(record_text: str, repo_root: str | Path) -> list[str]:
    """返回正文里提到、但在 repo_root 下已经不存在的路径。只查路径，不查符号。

    绝对路径和跳出 repo_root 的路径（``../``）不检查：记忆正文是不可信数据，
    不该拿它去探测仓库外的文件系统。
    """
    root = Path(repo_root).resolve()
    missing: list[str] = []
    for candidate in path_candidates(record_text):
        if candidate.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", candidate):
            continue
        target = (root / candidate).resolve()
        if not target.is_relative_to(root):
            continue
        if not target.exists():
            missing.append(candidate)
    return missing


@dataclass(frozen=True)
class SweepPolicy:
    """一次清理的策略。所有数字都是教学示意。"""

    half_life: float = 30 * DAY
    ttl_by_type: Mapping[str, float | None] = DEFAULT_TTL_BY_TYPE
    expired_action: Literal["delete", "demote"] = "delete"
    demote_below: float = 0.25
    delete_below: float = 0.05
    # 这些类型低频但关键（「永远不要推 main」），衰减最多把它们降权，不删。
    never_decay_delete: frozenset[str] = frozenset({"user", "feedback"})
    # 给了就做陈旧路径检查。
    repo_root: Path | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.delete_below < self.demote_below:
            raise ValueError("需要 0 ≤ delete_below < demote_below")


@dataclass(frozen=True)
class Verdict:
    record: MemoryRecord
    reason: str


@dataclass(frozen=True)
class SweepResult:
    keep: tuple[Verdict, ...]
    demote: tuple[Verdict, ...]
    delete: tuple[Verdict, ...]
    # 不归属某条记录的问题，例如悬空的替代引用。
    issues: tuple[str, ...]


def sweep(records: Sequence[MemoryRecord], now: float, policy: SweepPolicy) -> SweepResult:
    """一次清理。判定顺序：被替代 → TTL 过期 → 陈旧路径 → 衰减分数，先命中先算。

    被替代的旧记录只降权不删：读取时不再返回它，但「为什么改」还能追溯。
    """
    resolution = resolve_supersedes(records, allow_dangling=True)
    groups: dict[str, list[Verdict]] = {"keep": [], "demote": [], "delete": []}
    issues = [f"{who} 替代的 {target} 已不存在（多半是被清理了）"
              for who, target in resolution.dangling]
    for record in records:
        try:
            group, reason = _judge(record, now, policy, resolution)
        except ValueError as error:
            # 一条坏数据不该让整次清理中止：原样保留，问题单独报出来。
            group, reason = "keep", f"未判定：{error}"
            issues.append(str(error))
        groups[group].append(Verdict(record, reason))
    return SweepResult(
        keep=tuple(groups["keep"]),
        demote=tuple(groups["demote"]),
        delete=tuple(groups["delete"]),
        issues=tuple(issues),
    )


def _judge(
    record: MemoryRecord, now: float, policy: SweepPolicy, resolution: Resolution
) -> tuple[str, str]:
    head = resolution.heads[record.id]
    if head != record.id:
        return "demote", f"已被替代，当前版本是 {head}"
    if is_expired(record, now, policy.ttl_by_type):
        return policy.expired_action, f"{record.type} 类型的 TTL 已到"
    if policy.repo_root is not None:
        missing = find_stale_references(record.text, policy.repo_root)
        if missing:
            return "demote", "引用的路径已不存在：" + ", ".join(missing)
    score = decayed_score(record, now, policy.half_life)
    if score < policy.delete_below and record.type not in policy.never_decay_delete:
        return "delete", f"衰减后得分 {score:.3f} 低于删除线"
    if score < policy.demote_below:
        return "demote", f"衰减后得分 {score:.3f} 低于降权线"
    return "keep", f"得分 {score:.3f}"
