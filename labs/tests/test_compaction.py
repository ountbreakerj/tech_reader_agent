"""compaction 的单元测试：切分点不拆配对、分级、保留最近 K 条、熔断。

运行：py -m unittest discover -s labs/tests -t labs -p "test_compaction.py"
"""

from __future__ import annotations

import json
import unittest

from harness.compaction import (
    MAX_CONSECUTIVE_FAILURES,
    CompactionError,
    Compactor,
    CircuitBreaker,
    balance_flags,
    history_is_valid,
    pairing_complete,
    prune_tool_results,
    safe_cut_index,
    summarize_prefix,
)
from harness.llm import AssistantTurn, FakeLLM, ScriptExhausted, ToolCall
from harness.messages import assistant_from_turn, tool_results, user


# ---------------------------------------------------------------- 测试用历史

def turn_with_tools(index: int, size: int = 400) -> list[dict]:
    """一轮「助手发起两个工具调用 + 结果」的历史，共两条消息。"""
    call_a = ToolCall(id=f"c{index}a", name="read", args={"path": f"f{index}.py"})
    call_b = ToolCall(id=f"c{index}b", name="grep", args={"pattern": "x"})
    assistant = assistant_from_turn(AssistantTurn(text=f"第 {index} 轮", tool_calls=[call_a, call_b]))
    results = tool_results([(call_a.id, "A" * size, False), (call_b.id, "B" * size, False)])
    return [assistant, results]


def conversation(rounds: int = 4, size: int = 400) -> list[dict]:
    """一段合法历史：以 user 开头，每轮配对完整。长度 = 2 + 2*rounds。"""
    messages = [user("把 f1.py 里的 bug 修掉，不要改公开接口")]
    for index in range(rounds):
        messages.extend(turn_with_tools(index, size))
    messages.append(user("继续"))
    return messages


def counted_count(messages: list[dict]) -> int:
    """按序列化长度计费的假计数器：确定性，测试不依赖真实 tokenizer。"""
    return len(json.dumps(messages, ensure_ascii=False))


def summarize_budget(messages: list[dict], keep_results: int = 1) -> int:
    """选一个「裁剪之后仍然要求摘要」的预算。

    第 1 级之后控制器会重新量一次，所以预算必须紧到让裁剪后的历史
    仍然落在摘要档（>= 0.95），第 2 级才真的会被触发。
    """
    pruned, _ = prune_tool_results(messages, keep_recent=keep_results)
    return int(counted_count(pruned) / 0.97)


def fake_summarizer(text: str = "摘要：目标是修 bug，已读 f1.py，未完成的是改代码。"):
    """记录被调用过的历史，并返回固定摘要。"""
    calls: list[list[dict]] = []

    def summarize(messages: list[dict]) -> str:
        calls.append(messages)
        return text

    summarize.calls = calls  # type: ignore[attr-defined]
    return summarize


# ---------------------------------------------------------------- 切分点

class SafeCutTests(unittest.TestCase):
    def test_balance_flags_mark_every_position(self) -> None:
        messages = conversation(rounds=1)  # [user, assistant, results, user]
        flags = balance_flags(messages)
        self.assertEqual(len(flags), len(messages) + 1)
        # flags[1]：user 之后，平衡；flags[2]：助手发起了两个调用，不平衡；
        # flags[3]：结果都到齐，平衡；flags[4]：末尾，平衡。
        self.assertEqual(flags, [True, True, False, True, True])

    def test_safe_cut_moves_back_to_a_balanced_position(self) -> None:
        messages = conversation(rounds=2)  # 长度 6
        flags = balance_flags(messages)
        desired = 2  # 落在助手消息与其结果之间
        self.assertFalse(flags[desired])
        cut = safe_cut_index(messages, desired)
        self.assertEqual(cut, 1)
        self.assertTrue(flags[cut])

    def test_safe_cut_never_returns_an_unbalanced_position(self) -> None:
        messages = conversation(rounds=3)
        flags = balance_flags(messages)
        for desired in range(len(messages) + 1):
            self.assertTrue(flags[safe_cut_index(messages, desired)], f"desired={desired}")

    def test_safe_cut_rejects_history_with_orphan_result(self) -> None:
        broken = [user("hi"), tool_results([("nope", "out", False)])]
        with self.assertRaises(CompactionError):
            balance_flags(broken)
        self.assertFalse(pairing_complete(broken))
        self.assertFalse(history_is_valid(broken))

    def test_safe_cut_falls_back_to_zero_when_nothing_balanced_before_desired(self) -> None:
        # 第一段就是一对未配对的调用：desired 落在配对内部时没有可用的平衡点。
        messages = [*turn_with_tools(0), user("继续")]
        self.assertEqual(safe_cut_index(messages, 1), 0)
        # 整段历史的末尾总是平衡的，所以 desired 落到末尾就一定可切。
        self.assertEqual(safe_cut_index(messages, len(messages)), len(messages))


# ---------------------------------------------------------------- 第 1 级

class PruneTests(unittest.TestCase):
    def test_prune_only_rewrites_result_content(self) -> None:
        messages = conversation(rounds=4)
        pruned, saved = prune_tool_results(messages, keep_recent=2)
        self.assertGreater(saved, 0)
        self.assertEqual(len(pruned), len(messages))
        for before, after in zip(messages, pruned):
            self.assertEqual(before["role"], after["role"])
            if isinstance(before["content"], str):
                self.assertEqual(before, after)  # 纯文本消息一字不改
                continue
            self.assertEqual(len(before["content"]), len(after["content"]))
            for block_before, block_after in zip(before["content"], after["content"]):
                self.assertEqual(block_before.get("type"), block_after.get("type"))
                if block_before.get("type") == "tool_result":
                    self.assertEqual(block_before["tool_use_id"], block_after["tool_use_id"])
                    self.assertEqual(block_before["is_error"], block_after["is_error"])
                else:
                    self.assertEqual(block_before, block_after)
        self.assertTrue(pairing_complete(pruned))
        self.assertTrue(history_is_valid(pruned))

    def test_prune_keeps_the_most_recent_results_intact(self) -> None:
        messages = conversation(rounds=4)
        pruned, _ = prune_tool_results(messages, keep_recent=2)
        blocks = [block for message in pruned for block in message["content"]
                  if isinstance(block, dict) and block.get("type") == "tool_result"]
        self.assertEqual(len(blocks), 8)
        for block in blocks[-2:]:
            self.assertIn(block["content"][0], "AB")
        for block in blocks[:-2]:
            self.assertIn("已裁剪", block["content"])

    def test_prune_does_not_mutate_the_input_history(self) -> None:
        messages = conversation(rounds=3)
        before = json.dumps(messages, ensure_ascii=False)
        prune_tool_results(messages, keep_recent=1)
        self.assertEqual(json.dumps(messages, ensure_ascii=False), before)

    def test_prune_skips_error_results(self) -> None:
        call = ToolCall(id="e1", name="bash", args={})
        assistant = assistant_from_turn(AssistantTurn(text="", tool_calls=[call]))
        messages = [
            user("跑一下"),
            assistant,
            tool_results([(call.id, "E" * 500, True)]),
            *turn_with_tools(9, size=500),
            user("再跑一次"),
        ]
        pruned, _ = prune_tool_results(messages, keep_recent=1)
        self.assertEqual(pruned[2]["content"][0]["content"], "E" * 500)

    def test_prune_is_a_noop_when_there_is_nothing_worth_pruning(self) -> None:
        messages = conversation(rounds=1, size=10)
        pruned, saved = prune_tool_results(messages, keep_recent=1)
        self.assertEqual(saved, 0)
        self.assertIs(pruned, messages)


# ---------------------------------------------------------------- 第 2 级

class SummarizeTests(unittest.TestCase):
    def test_summarize_keeps_recent_messages_verbatim(self) -> None:
        messages = conversation(rounds=4)  # 长度 10
        summarize = fake_summarizer()
        folded = summarize_prefix(messages, summarize, keep_recent=4)
        # desired=6 落在配对内部，退回 5；于是保留 5 条原样 + 1 条摘要 = 6。
        self.assertEqual(len(folded), 6)
        self.assertEqual(folded[1:], messages[5:])
        self.assertIn("【自动检查点】", folded[0]["content"])

    def test_summarize_output_is_a_valid_history(self) -> None:
        messages = conversation(rounds=4)
        folded = summarize_prefix(messages, fake_summarizer(), keep_recent=4)
        self.assertTrue(history_is_valid(folded))
        self.assertEqual(folded[0]["role"], "user")

    def test_summarizer_never_sees_the_preserved_tail(self) -> None:
        messages = conversation(rounds=4)
        summarize = fake_summarizer()
        summarize_prefix(messages, summarize, keep_recent=4)
        captured = summarize.calls[0]  # type: ignore[attr-defined]
        self.assertEqual(captured, messages[:5])

    def test_summarizer_failure_is_reported(self) -> None:
        messages = conversation(rounds=4)

        def boom(_: list[dict]) -> str:
            raise RuntimeError("模型超时")

        with self.assertRaises(CompactionError):
            summarize_prefix(messages, boom, keep_recent=4)

    def test_empty_summary_is_a_failure(self) -> None:
        messages = conversation(rounds=4)
        with self.assertRaises(CompactionError):
            summarize_prefix(messages, lambda _: "   ", keep_recent=4)

    def test_nothing_to_fold_returns_the_input(self) -> None:
        messages = conversation(rounds=1)
        summarize = fake_summarizer()
        folded = summarize_prefix(messages, summarize, keep_recent=10)
        self.assertIs(folded, messages)
        self.assertEqual(summarize.calls, [])  # type: ignore[attr-defined]

    def test_fake_llm_can_back_the_summarizer(self) -> None:
        """摘要提示词能被 FakeLLM 消费：它就多一次可见的模型调用。"""
        llm = FakeLLM([AssistantTurn(text="摘要：目标是修 f1.py 的 bug。")])

        def summarize(messages: list[dict]) -> str:
            return llm.complete("你是压缩引擎", messages, []).text

        messages = conversation(rounds=3)
        folded = summarize_prefix(messages, summarize, keep_recent=2)
        self.assertIn("修 f1.py", folded[0]["content"])
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(history_is_valid(folded))


# ---------------------------------------------------------------- 分级

class LevelTests(unittest.TestCase):
    def test_thresholds_pick_the_level(self) -> None:
        compactor = Compactor(counted_count, budget=1_000, keep_recent=2, keep_results=1)
        self.assertEqual(compactor.level_for(700), "none")
        self.assertEqual(compactor.level_for(800), "prune")
        self.assertEqual(compactor.level_for(949), "prune")
        self.assertEqual(compactor.level_for(950), "summarize")

    def test_invalid_thresholds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Compactor(counted_count, budget=1_000, prune_ratio=0.9, summarize_ratio=0.5)
        with self.assertRaises(ValueError):
            Compactor(counted_count, budget=0)

    def test_prune_level_does_not_call_the_summarizer(self) -> None:
        messages = conversation(rounds=4)
        used = counted_count(messages)
        summarize = fake_summarizer()
        compactor = Compactor(
            counted_count,
            budget=int(used / 0.85),  # 落在 prune 档
            summarize=summarize,
            keep_recent=2,
            keep_results=1,
        )
        result = compactor.compact_once(messages)
        self.assertEqual(result.level, "prune")
        self.assertEqual(summarize.calls, [])  # type: ignore[attr-defined]
        self.assertLess(result.chars_after, result.chars_before)

    def test_summarize_level_runs_when_prune_is_not_enough(self) -> None:
        messages = conversation(rounds=4)
        used = counted_count(messages)
        summarize = fake_summarizer()
        compactor = Compactor(
            counted_count,
            budget=summarize_budget(messages),
            summarize=summarize,
            keep_recent=4,
            keep_results=1,
        )
        result = compactor.compact_once(messages)
        self.assertEqual(result.level, "summarize")
        self.assertEqual(len(summarize.calls), 1)  # type: ignore[attr-defined]
        self.assertTrue(history_is_valid(result.messages))

    def test_summarize_level_stops_after_prune_is_enough(self) -> None:
        """贵的那一级只在便宜的那一级不够用时才动。"""
        messages = conversation(rounds=4)
        pruned, _ = prune_tool_results(messages, keep_recent=1)
        budget = int(counted_count(pruned) / 0.9)  # 裁剪后落到 0.9 < 0.95
        self.assertGreater(counted_count(messages) / budget, 0.95)  # 未压缩时确实在摘要档
        summarize = fake_summarizer()
        compactor = Compactor(
            counted_count,
            budget=budget,
            summarize=summarize,
            keep_recent=4,
            keep_results=1,
        )
        result = compactor.compact_once(messages)
        self.assertEqual(result.level, "prune")
        self.assertEqual(summarize.calls, [])  # type: ignore[attr-defined]

    def test_below_threshold_is_a_noop(self) -> None:
        messages = conversation(rounds=1)
        compactor = Compactor(counted_count, budget=10 ** 7)
        result = compactor.compact_once(messages)
        self.assertEqual(result.level, "none")
        self.assertFalse(result.changed)
        self.assertIs(result.messages, messages)


# ---------------------------------------------------------------- 熔断

class BreakerTests(unittest.TestCase):
    def _failing_compactor(self, messages: list[dict]) -> Compactor:
        def boom(_: list[dict]) -> str:
            raise RuntimeError("配额用尽")

        return Compactor(
            counted_count,
            budget=summarize_budget(messages),
            summarize=boom,
            keep_recent=4,
            keep_results=1,
        )

    def test_consecutive_failures_open_the_breaker(self) -> None:
        messages = conversation(rounds=4)
        compactor = self._failing_compactor(messages)
        levels = [compactor.compact_once(messages).level for _ in range(MAX_CONSECUTIVE_FAILURES)]
        # 第 2 级失败，但第 1 级已经生效，所以每次都退到 prune。
        self.assertEqual(levels, ["prune"] * MAX_CONSECUTIVE_FAILURES)
        self.assertTrue(compactor.breaker.open)
        blocked = compactor.compact_once(messages)
        self.assertEqual(blocked.level, "none")
        self.assertIn("熔断", " ".join(blocked.notes))
        self.assertIs(blocked.messages, messages)

    def test_breaker_resets_after_a_success(self) -> None:
        breaker = CircuitBreaker()
        breaker.record_failure("x")
        breaker.record_failure("y")
        self.assertFalse(breaker.open)
        breaker.record_success()
        self.assertEqual(breaker.failures, 0)
        self.assertFalse(breaker.open)

    def test_summarizer_is_not_called_repeatedly_while_open(self) -> None:
        messages = conversation(rounds=4)
        calls: list[int] = []

        def boom(_: list[dict]) -> str:
            calls.append(1)
            raise RuntimeError("还是不行")

        compactor = Compactor(
            counted_count,
            budget=summarize_budget(messages),
            summarize=boom,
            keep_recent=4,
            keep_results=1,
        )
        for _ in range(6):
            compactor.compact_once(messages)
        self.assertEqual(len(calls), MAX_CONSECUTIVE_FAILURES)
        self.assertTrue(compactor.breaker.open)

    def test_no_progress_counts_as_a_failure(self) -> None:
        """第 2 级跑完却没缩短历史：算失败，否则每轮都会重复同一次无用功。"""
        messages = [user("hi"), user("只有用户消息，没有可裁的工具结果")]
        compactor = Compactor(
            counted_count,
            budget=max(1, counted_count(messages) // 100),
            summarize=fake_summarizer(),
            keep_recent=6,   # 比历史还长：切分点退回 0，第 2 级无从下手
            keep_results=4,
        )
        result = compactor.compact_once(messages)
        self.assertEqual(result.level, "none")
        self.assertEqual(compactor.breaker.failures, 1)
        self.assertTrue(any("没有缩短" in note for note in result.notes))


# ---------------------------------------------------------------- 组合场景

class CascadeTests(unittest.TestCase):
    def test_compaction_preserves_a_sendable_history_end_to_end(self) -> None:
        messages = conversation(rounds=5)
        compactor = Compactor(
            counted_count,
            budget=summarize_budget(messages),
            summarize=fake_summarizer(),
            keep_recent=4,
            keep_results=1,
        )
        result = compactor.compact_once(messages)
        self.assertTrue(history_is_valid(result.messages))
        self.assertEqual(result.messages[0]["role"], "user")
        self.assertLess(result.chars_after, result.chars_before)
        json.dumps(result.messages, ensure_ascii=False)

    def test_script_exhausted_degrades_to_prune_without_crashing(self) -> None:
        """真实适配器抛的错要浮上来记一笔，但不能让整个 turn 崩掉。"""
        llm = FakeLLM([])  # 空脚本：一调用就抛 ScriptExhausted

        def summarize(messages: list[dict]) -> str:
            return llm.complete("sys", messages, []).text

        messages = conversation(rounds=4)
        compactor = Compactor(
            counted_count,
            budget=summarize_budget(messages),
            summarize=summarize,
            keep_recent=4,
            keep_results=1,
        )
        result = compactor.compact_once(messages)
        self.assertEqual(result.level, "prune")
        self.assertTrue(any("第 2 级失败" in note for note in result.notes))
        self.assertTrue(history_is_valid(result.messages))
        self.assertIsInstance(ScriptExhausted("x"), RuntimeError)


if __name__ == "__main__":
    unittest.main()
