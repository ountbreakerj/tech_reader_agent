"""上下文压缩：多级级联 + 安全切分点 + 熔断。

三个层次从便宜到贵，对应三种不同量级的损失：

1. ``prune_tool_results``  —— 不调模型。只把**旧工具结果的正文**换成一行占位符，
   保留 tool_use/tool_result 的配对与 id，结构一个字节都不动。信息损失最小，
   收益也最小（只清掉最占地方的那部分）。
2. ``summarize_prefix``    —— 调一次模型。把较早的若干轮折成一条摘要消息，
   最近 K 条原样保留。损失大，但腾出的空间也大。
3. 第 0 级（本文件不实现）—— 让大结果**根本不进历史**。
   DSH 用 spill 承担这一层：超大工具结果在落地时就转存到磁盘，
   正文只留预览和路径，见 ``labs/harness/tool_runtime.py`` 与 hx-08 章。
   它比这里的两级都便宜，因为它压的是「还没进上下文的东西」。

级联的顺序不能反过来。第 1 级可做、可反复做、不花一分钱；第 2 级要花钱、
要等模型、还可能失败。先做便宜的，只有便宜的不够用才做贵的。

这个文件里最该记住的不是三级，而是 :func:`balance_flags` 和 :func:`safe_cut_index`：
一次切分如果落在 tool_use 和它的 tool_result 之间，发出的请求会被 API 直接拒绝
（400），而且往往是在压缩「成功」之后才炸。

只用标准库，Python 3.10+。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

Messages = list[dict[str, Any]]
Summarizer = Callable[[Messages], str]

# 裁剪后留在原地的占位文本。保留 tool_use_id 与 is_error，只换 content。
PRUNE_PLACEHOLDER = "[已裁剪：原 {n} 字符]"

# 短于这个长度的工具结果不值得裁：省下的空间还不如占位符本身大。
PRUNE_MIN_CHARS = 200

# 这些工具的结果一旦是错误，就不参与裁剪——错误信息通常正是模型下一步的依据。
PRUNE_SKIP_ERRORS = True

# 一级阈值：用量到预算的这个比例才值得动第 1 级。
PRUNE_RATIO = 0.8
# 二级阈值：到这一步第 1 级大概率已经做过了，直接上摘要。
SUMMARY_RATIO = 0.95

# 熔断：连续失败到这个次数就不再尝试压缩，让循环带着原历史跑。
MAX_CONSECUTIVE_FAILURES = 3


class CompactionError(RuntimeError):
    """压缩本身失败。调用方应当捕获它并继续跑，而不是让整个 turn 崩掉。"""


def balance_flags(messages: Sequence[dict[str, Any]]) -> list[bool]:
    """返回每一处切分点是否「配对平衡」。

    结果比消息数多一个：``flags[i]`` 是第 i 条消息**之前**的切分点，
    ``flags[len(messages)]`` 是整段历史末尾的切分点。

    做法是一次前缀扫描：助手消息里的每个 ``tool_use`` 记 +1，每条 ``tool_result``
    记 -1，计数为 0 的位置才算平衡。这和 DSH 的做法一致
    （``packages/compaction/compaction/src/tool-pairing.ts``：它维护的
    ``inProgressToolCalls`` 就是同一个量，只是它按 surface 增量缓存）。

    注意 flags 里出现 False 完全正常：一轮工具调用进行到一半时，
    前缀里就有一个没人回答的 tool_use。只有**整段末尾**那个 flag 必须是 True。
    所以下面两个问题要分开回答：

    * 「哪些位置可以切」——看 :func:`safe_cut_index`；
    * 「这份历史配对完整吗」——只看最后一个 flag，见 :func:`pairing_complete`。

    遇到「结果比调用多」（计数变负）直接抛错：这样的历史本身就不合法，
    压缩只会把坏数据带到更远的地方。
    """
    flags = [True]
    pending = 0
    for message in messages:
        pending += _pending_delta(message)
        if pending < 0:
            raise CompactionError(
                "历史里出现了没有对应 tool_use 的 tool_result：这份历史本身不合法，"
                "先修数据再压缩"
            )
        flags.append(pending == 0)
    return flags


def pairing_complete(messages: Sequence[dict[str, Any]]) -> bool:
    """整段历史的 tool_use 与 tool_result 是否全部配平。

    这就是「能不能直接发给 API」在配对维度上的答案。切分点的 flags 里可以有
    False（正跑到一半的轮次），但末尾的 flag 必须是 True——否则就是
    「助手调了工具，结果没回来」，多数 API 会直接拒这份历史。
    """
    try:
        return balance_flags(messages)[-1]
    except CompactionError:
        return False


def _pending_delta(message: dict[str, Any]) -> int:
    """一条消息让「未配对的工具调用」计数变化多少。"""
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    delta = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "tool_use":
            delta += 1
        elif kind == "tool_result":
            delta -= 1
    return delta


def safe_cut_index(messages: Sequence[dict[str, Any]], desired: int) -> int:
    """把切分点从 ``desired`` 往回挪到最近的平衡位置。

    返回的是「新历史从这里开始」的下标：0 表示不切，``len(messages)`` 表示全切。
    只往后挪不往前挪是刻意的——往后挪会切掉更少的旧内容，但这比拆开配对安全。
    一条 ``desired`` 之前全是未配对调用的历史（例如整段历史都在一对调用之间）
    就退回 0，宁可这次不压。

    DSH 在选范围时做的是同一件事，但方向相反：它从保留区的起点往前退
    （``packages/compaction/compaction-basic/src/region.ts`` 里的
    ``while (keepFromIdx > firstIdx)`` 循环，每退一步查一次
    ``toolPairingBalancedBefore``）。两边都在挪同一个点，差别只是
    「保住最近 K 条」还是「多切一点旧内容」。
    """
    flags = balance_flags(messages)
    index = min(max(desired, 0), len(messages))
    while index > 0 and not flags[index]:
        index -= 1
    return index


def prune_tool_results(
    messages: Messages,
    *,
    keep_recent: int = 4,
    min_chars: int = PRUNE_MIN_CHARS,
) -> tuple[Messages, int]:
    """第 1 级：把较旧的工具结果正文换成占位符。返回 ``(新历史, 省下的字符数)``。

    只动 ``tool_result.content``。``tool_use_id``、``is_error``、消息顺序、
    消息条数全部不变——所以这一步之后的历史一定还是合法的，
    压缩本身不可能成为一次 400 的原因。（工具结果的 ``is_error`` 为真时默认跳过：
    错误信息往往是模型下一步做决定的依据，见「坑与失败案例」。）

    ``keep_recent`` 数的是**工具结果条数**，不是消息条数。
    OpenHarness 的 ``microcompact_messages`` 是同一个口径
    （``all_ids[-keep_recent:]`` 对应这里的最后 N 条结果），并且照样会
    ``keep_recent = max(1, ...)`` 强制至少留一条。我们也保证至少留一条。
    """
    positions: list[tuple[int, int, dict[str, Any]]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                positions.append((message_index, block_index, block))

    keep = max(1, keep_recent)
    if len(positions) <= keep:
        return messages, 0

    pruned = _deep_copy(messages)
    saved = 0
    for message_index, block_index, block in positions[:-keep]:
        text = block.get("content")
        if not isinstance(text, str) or len(text) < min_chars:
            continue
        if PRUNE_SKIP_ERRORS and block.get("is_error"):
            continue
        target = pruned[message_index]["content"][block_index]
        before = len(pruned[message_index]["content"][block_index]["content"])
        target["content"] = PRUNE_PLACEHOLDER.format(n=before)
        saved += before - len(target["content"])
    if saved <= 0:
        return messages, 0
    return pruned, saved


SUMMARY_INSTRUCTION = """把上面的对话折成一份可以继续干活的检查点。用要点罗列，不要散文。
必须覆盖下面每一项，没有内容就写「无」，不要省略任何一节：

- 任务目标：用户最初提出、后来又补充过的目标，逐字保留关键约束
- 已做的决定：选了什么、为什么这么选、被否决的方案
- 改动过的文件：精确路径，以及每个文件为什么重要
- 错误与修复：出过的错、怎么解决的、用户因此提过什么意见
- 未完成的事：用户明确要求但还没做完的
- 下一步：对齐最近一次请求的单个动作

保留精确的文件路径、命令、错误字符串、标识符和数值。
不要提及这次总结动作，也不要复述这份说明本身。"""


def summary_message(summary: str, *, cut: int, total: int) -> dict[str, Any]:
    """把摘要包成一条 user 消息，替换掉被折掉的那段历史。

    必须是 user 消息：多数 API 要求历史以 user 开头，而摘要替换的正是开头那一段。
    措辞明确要求模型把摘要当作背景、不要复述它——否则模型经常会在下一步
    先来一句「我了解到我们正在……」白白花掉一轮。
    """
    return {
        "role": "user",
        "content": (
            f"【自动检查点】以下是较早的 {cut} 条消息（共 {total} 条）的压缩摘要。"
            "把它当成既有背景，直接接着干活，不要复述它。\n\n"
            f"{summary}"
        ),
    }


def summarize_prefix(
    messages: Messages,
    summarize: Summarizer,
    *,
    keep_recent: int = 6,
) -> Messages:
    """第 2 级：把较早的若干条折成一条摘要，最近 ``keep_recent`` 条原样保留。

    切分点由 :func:`safe_cut_index` 决定；``summarize`` 是调用方传进来的函数
    （测试里用普通函数或 FakeLLM，生产里可能就是一次 ``LLM.complete``）。

    摘要失败会把原始异常包成 :class:`CompactionError` 抛出——注意是**抛出**，
    不是「退化成不压缩」。调用方（:class:`Compactor`）知道该怎么记账并继续，
    而这里静默吞掉失败会让上层以为压缩成功、于是每轮都重试。
    """
    cut = safe_cut_index(messages, len(messages) - max(0, keep_recent))
    if cut <= 0:
        return messages
    older, newer = messages[:cut], messages[cut:]
    try:
        summary = summarize(_deep_copy(older))
    except Exception as exc:  # noqa: BLE001 —— 任何失败都要让调用方看到
        raise CompactionError(f"摘要失败：{exc}") from exc
    if not isinstance(summary, str) or not summary.strip():
        raise CompactionError("摘要为空：压缩没有腾出任何信息空间，视为失败")
    return [summary_message(summary, cut=cut, total=len(messages)), *newer]


def _deep_copy(messages: Messages) -> Messages:
    """深拷贝历史。

    压缩会原地改写 block 的 ``content``，而 ``FakeLLM`` 里存的是深拷贝快照——
    两边都要有自己的一份，否则「压缩后历史变了」和「模型当时看到的历史」
    会互相污染。
    """
    return json.loads(json.dumps(messages, ensure_ascii=False))


# ---------------------------------------------------------------- 熔断

@dataclass
class CircuitBreaker:
    """连续失败计数。到阈值就停止尝试压缩，直到有人显式复位。

    这条规则解决的是「压缩一直失败、于是每轮都重试一遍」：一次摘要要花一次
    模型调用，失败还往往是确定性的（历史太大、模型不支持长输入、配额用尽），
    每轮重试等于每轮白花一次钱。

    OpenHarness 的做法是一个计数器加一条短路：
    ``packages/.../compact/__init__.py`` 里 ``should_autocompact`` 的第一行就是
    ``if state.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES: return False``。
    我们在成功时也清零——它那边的清零挂在 turn 结束上，两者都在「环境变了」时
    重新给一次机会。
    """

    max_failures: int = MAX_CONSECUTIVE_FAILURES
    failures: int = 0
    last_error: str = ""

    @property
    def open(self) -> bool:
        """是否已经熔断（停止尝试压缩）。"""
        return self.failures >= self.max_failures

    def record_success(self) -> None:
        self.failures = 0
        self.last_error = ""

    def record_failure(self, error: BaseException | str) -> None:
        self.failures += 1
        self.last_error = str(error)


# ---------------------------------------------------------------- 级联

@dataclass
class CompactionResult:
    """一次压缩尝试的结局。``level`` 是实际做到的**最高**一级。"""

    messages: Messages
    level: str  # "none" | "prune" | "summarize"
    chars_before: int
    chars_after: int
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.level != "none"

    @property
    def saved(self) -> int:
        return self.chars_before - self.chars_after

    def describe(self) -> str:
        return (
            f"level={self.level} {self.chars_before}→{self.chars_after} 字符"
            f"（省 {self.saved}）"
        )


class Compactor:
    """按估算用量和预算决定做第几级的级联控制器。

    估算与环境全部由参数传入：``count`` 是一个 ``messages -> int`` 的函数，
    ``budget`` 是 token 预算。这样这个模块既不依赖 ``budget.py`` 的实现细节，
    也能被测试用「按条数计费」的假计数器精确驱动。
    """

    def __init__(
        self,
        count: Callable[[Messages], int],
        budget: int,
        *,
        summarize: Summarizer | None = None,
        prune_ratio: float = PRUNE_RATIO,
        summarize_ratio: float = SUMMARY_RATIO,
        keep_recent: int = 6,
        keep_results: int = 4,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        if budget <= 0:
            raise ValueError("budget 必须是正整数")
        if not 0.0 < prune_ratio < summarize_ratio:
            raise ValueError("要求 0 < prune_ratio < summarize_ratio")
        self.count = count
        self.budget = budget
        self.summarize = summarize
        self.prune_ratio = prune_ratio
        self.summarize_ratio = summarize_ratio
        self.keep_recent = keep_recent
        self.keep_results = keep_results
        self.breaker = breaker or CircuitBreaker()

    def level_for(self, used: int) -> str:
        """按当前用量决定这次该做哪一级（还没考虑是否能成功）。"""
        ratio = used / self.budget
        if ratio >= self.summarize_ratio:
            return "summarize"
        if ratio >= self.prune_ratio:
            return "prune"
        return "none"

    def compact_once(self, messages: Messages) -> CompactionResult:
        """跑一次级联。失败时原样返回历史，并记账到熔断器。

        关键约定：**返回的历史永远可以直接发给 API**——第一条是 user、
        tool_use 与 tool_result 配对完整。压缩不许把一份合法历史变成非法历史，
        否则错误会从「上下文太长」变成「请求格式错误」，更难查也更难恢复。
        """
        before = _chars(messages)
        if self.breaker.open:
            return CompactionResult(
                messages=messages,
                level="none",
                chars_before=before,
                chars_after=before,
                notes=[f"熔断已打开（连续失败 {self.breaker.failures} 次），本轮跳过压缩"],
            )
        level = self.level_for(self.count(messages))
        if level == "none":
            return CompactionResult(messages, "none", before, before, ["用量在阈值以下"])

        notes: list[str] = []
        failed = False
        try:
            pruned, saved = prune_tool_results(messages, keep_recent=self.keep_results)
        except CompactionError as exc:
            self.breaker.record_failure(exc)
            return CompactionResult(messages, "none", before, before, [f"第 1 级失败：{exc}"])
        if saved > 0:
            notes.append(f"第 1 级裁剪旧工具结果，省 {saved} 字符")
            messages = pruned
            achieved = "prune"
        else:
            notes.append("第 1 级无可裁剪内容")
            achieved = "none"

        # 做第 1 级之后重新量一次：便宜的那一级可能已经够了，
        # 那就不要再花一次模型调用去做贵的那个。DSH 里的顺序也是这个：
        # 先让 toolResultPruner 落一轮（它不调模型），只有重新量完仍然超阈值，
        # 才去选摘要范围（compaction-basic/src/index.ts 的 compactIfNeeded）。
        if level == "summarize" and self.level_for(self.count(messages)) == "summarize":
            if self.summarize is None:
                notes.append("需要第 2 级但没有传入 summarize，止步于第 1 级")
            else:
                try:
                    folded = summarize_prefix(
                        messages, self.summarize, keep_recent=self.keep_recent
                    )
                except CompactionError as exc:
                    self.breaker.record_failure(exc)
                    notes.append(f"第 2 级失败：{exc}")
                    failed = True
                else:
                    if len(folded) < len(messages):
                        notes.append(f"第 2 级摘要，历史 {len(messages)} → {len(folded)} 条")
                        messages = folded
                        achieved = "summarize"
                    else:
                        notes.append("第 2 级没有缩短历史，按失败处理")
                        self.breaker.record_failure("summary did not shrink history")
                        failed = True

        after = _chars(messages)
        if after < before:
            if not failed:
                self.breaker.record_success()
        elif not failed:
            # 做完了却一点没省下来：算「压缩无效」，同样进熔断计数，
            # 否则下一轮会一模一样地再来一次。
            # （已经记过一次的失败不重复计数：阈值说的是「连续失败几次」，
            # 不是「失败有几条理由」。）
            self.breaker.record_failure("compaction did not reduce the history")
        return CompactionResult(messages, achieved, before, after, notes)


def _chars(messages: Messages) -> int:
    """历史的字符数。真实实现应当换成 token 计数（见 budget.py 的 Counter）。"""
    return len(json.dumps(messages, ensure_ascii=False))


def history_is_valid(messages: Messages) -> bool:
    """校验一份历史能不能直接发给 API。

    两条规则，都是客户端可以自己检查、不必等 400 的：

    * 第一条消息必须是 user（多数 API 要求，Anthropic 的 Messages 协议尤其如此）；
    * 整段历史的 tool_use / tool_result 必须配平（见 :func:`pairing_complete`），
      配对完整即可——中间某一轮跑到一半不算问题。

    这个函数是本章的「断言」：任何压缩步骤之后都该能过。写测试时先拿它过滤，
    比拿真 API 试错便宜得多。
    """
    if not messages:
        return False
    if messages[0].get("role") != "user":
        return False
    return pairing_complete(messages) and _starts_with_real_user_content(messages)


def _starts_with_real_user_content(messages: Messages) -> bool:
    """第一条 user 消息不能是纯 tool_result：那说明它的 tool_use 被切掉了。"""
    content = messages[0].get("content")
    if not isinstance(content, list):
        return True
    return any(
        isinstance(block, dict) and block.get("type") != "tool_result" for block in content
    )
