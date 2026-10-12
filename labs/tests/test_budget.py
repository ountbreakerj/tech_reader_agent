"""budget.py 的测试：估算、窗口切分、分级阈值、用量归一化、成本。"""

from __future__ import annotations

import unittest

from harness.budget import (
    BLOCK_OVERHEAD,
    ROLE_OVERHEAD,
    Budget,
    HeuristicCounter,
    Prices,
    UsageLedger,
    count_messages,
    count_tools,
    normalize_usage,
)


class HeuristicCounterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = HeuristicCounter()

    def test_empty_text_is_zero(self):
        self.assertEqual(self.counter.count(""), 0)

    def test_ascii_density(self):
        # 教学示意系数：4 个 ASCII 字符 1 token
        self.assertEqual(self.counter.count("a" * 4), 1)
        self.assertEqual(self.counter.count("a" * 8), 2)
        self.assertEqual(self.counter.count("a" * 5), 2)  # 向上取整

    def test_cjk_is_denser_than_ascii(self):
        # 同样的字符数，中日韩文字估出来的 token 更多，这是估算最容易被低估的地方
        cjk = self.counter.count("上下文窗口")
        ascii_same_length = self.counter.count("abcdef")
        self.assertGreater(cjk, ascii_same_length)

    def test_monotonic_in_prefix(self):
        # 单调性：a 是 b 的前缀时 count(a) <= count(b)。预算决策依赖这一条
        base = "def run_agent(llm, task):"
        for cut in range(len(base) + 1):
            self.assertLessEqual(self.counter.count(base[:cut]), self.counter.count(base))

    def test_monotonic_in_repetition(self):
        previous = 0
        for repeat in range(1, 20):
            current = self.counter.count("token " * repeat)
            self.assertGreaterEqual(current, previous)
            previous = current

    def test_count_is_not_identity(self):
        # 计数器不是字符数：混排文本的估计值必须和 len() 不同
        text = "上下文 context 窗口 window"
        self.assertNotEqual(self.counter.count(text), len(text))


class MessageAndToolCountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = HeuristicCounter()

    def test_role_overhead_per_message(self):
        messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(count_messages(self.counter, messages), ROLE_OVERHEAD + self.counter.count("hi"))

    def test_blocks_pay_structural_overhead(self):
        messages = [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
        expected = ROLE_OVERHEAD + BLOCK_OVERHEAD + self.counter.count("ok")
        self.assertEqual(count_messages(self.counter, messages), expected)

    def test_empty_inputs_are_zero(self):
        self.assertEqual(count_messages(self.counter, []), 0)
        self.assertEqual(count_tools(self.counter, []), 0)

    def test_tool_schemas_cost_more_than_nothing(self):
        schemas = [{"name": "bash", "description": "run a command", "input_schema": {"type": "object"}}]
        self.assertGreater(count_tools(self.counter, schemas), 0)


class BudgetTest(unittest.TestCase):
    def test_window_split(self):
        budget = Budget(context_window=200_000, output_reserve=8_192, safety_margin=8_192)
        self.assertEqual(budget.message_budget, 191_808)
        self.assertEqual(budget.pressure_budget, 183_616)

    def test_available_input_subtracts_every_consumer(self):
        budget = Budget(context_window=200_000, output_reserve=8_192, safety_margin=8_192)
        plan = budget.plan(
            HeuristicCounter(),
            system_prompt="你是助手",
            tool_schemas=[{"name": "bash"}],
            messages=[{"role": "user", "content": "hello"}],
        )
        self.assertEqual(plan.used, plan.system_prompt + plan.tool_schemas + plan.history)
        self.assertEqual(plan.available_input, plan.pressure_budget - plan.used)
        self.assertEqual(plan.level, "ok")
        self.assertFalse(plan.should_compact)

    def test_empty_plan_leaves_full_budget(self):
        budget = Budget(context_window=1_000, output_reserve=200, safety_margin=100)
        plan = budget.plan(HeuristicCounter())
        self.assertEqual(plan.used, 0)
        self.assertEqual(plan.available_input, 700)

    def test_graded_levels(self):
        budget = Budget(
            context_window=1_000, output_reserve=0, safety_margin=0,
            near_limit_ratio=0.8, hard_ratio=0.95,
        )
        self.assertEqual(budget.level_for(0), "ok")
        self.assertEqual(budget.level_for(799), "ok")
        self.assertEqual(budget.level_for(800), "near_limit")   # 阈值闭区间
        self.assertEqual(budget.level_for(949), "near_limit")
        self.assertEqual(budget.level_for(950), "must_compact")
        self.assertEqual(budget.level_for(1_000), "must_compact")
        self.assertEqual(budget.level_for(1_001), "overflow")

    def test_level_reflects_real_messages(self):
        budget = Budget(
            context_window=1_000, output_reserve=0, safety_margin=0,
            near_limit_ratio=0.8, hard_ratio=0.95,
        )
        small = budget.plan(HeuristicCounter(), messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(small.level, "ok")
        huge = budget.plan(
            HeuristicCounter(),
            messages=[{"role": "user", "content": "a" * 4_000}],
        )
        self.assertEqual(huge.level, "overflow")
        self.assertTrue(huge.should_compact)
        self.assertLess(huge.available_input, 0)  # 负数是「还差多少」，不是无意义值

    def test_thresholds_are_parameters(self):
        strict = Budget(context_window=1_000, output_reserve=0, safety_margin=0,
                        near_limit_ratio=0.5, hard_ratio=0.6)
        loose = Budget(context_window=1_000, output_reserve=0, safety_margin=0,
                       near_limit_ratio=0.9, hard_ratio=0.99)
        self.assertEqual(strict.level_for(600), "must_compact")
        self.assertEqual(loose.level_for(600), "ok")

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            Budget(context_window=0)
        with self.assertRaises(ValueError):
            Budget(context_window=1_000, output_reserve=800, safety_margin=300)  # 切不出预算
        with self.assertRaises(ValueError):
            Budget(context_window=1_000, near_limit_ratio=0.9, hard_ratio=0.5)   # 档位反了


class NormalizeUsageTest(unittest.TestCase):
    def test_anthropic_shape(self):
        buckets = normalize_usage({
            "input_tokens": 1_000,
            "output_tokens": 200,
            "cache_read_input_tokens": 5_000,
            "cache_creation_input_tokens": 300,
        })
        self.assertEqual(buckets, {"input": 1_000, "output": 200, "cache_read": 5_000, "cache_write": 300})

    def test_openai_shape_subtracts_cache_from_prompt(self):
        # prompt_tokens 含命中缓存的量，不减掉会重复计价
        buckets = normalize_usage({
            "prompt_tokens": 1_500,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 500},
        })
        self.assertEqual(buckets, {"input": 1_000, "output": 200, "cache_read": 500, "cache_write": 0})

    def test_openai_without_details(self):
        buckets = normalize_usage({"prompt_tokens": 700, "completion_tokens": 100})
        self.assertEqual(buckets, {"input": 700, "output": 100, "cache_read": 0, "cache_write": 0})

    def test_missing_usage_counts_as_zero(self):
        self.assertEqual(normalize_usage({}), {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})


class UsageLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = UsageLedger()

    def test_accumulates_across_turns(self):
        self.ledger.record_usage({"input_tokens": 1_000, "output_tokens": 100})
        self.ledger.new_turn()
        self.ledger.record_usage({"input_tokens": 2_000, "output_tokens": 200})
        self.assertEqual(self.ledger.total["input"], 3_000)
        self.assertEqual(self.ledger.total["output"], 300)
        self.assertEqual(self.ledger.tokens_at_turn(1)["input"], 1_000)
        self.assertEqual(self.ledger.tokens_at_turn(2)["input"], 2_000)

    def test_retries_count_as_separate_billed_calls(self):
        # 一次响应里若含两次计费调用，用量要乘倍，否则账本会比账单小
        self.ledger.record_usage({"input_tokens": 1_000, "output_tokens": 100}, calls=2)
        self.assertEqual(self.ledger.total["input"], 2_000)
        self.assertEqual(self.ledger.attempts[0]["calls"], 2)

    def test_mixed_usage_formats_normalize_to_one_total(self):
        self.ledger.record_usage({"input_tokens": 100, "output_tokens": 10})
        self.ledger.new_turn()
        self.ledger.record_usage({"prompt_tokens": 200, "completion_tokens": 20})
        self.assertEqual(self.ledger.total["input"], 300)
        self.assertEqual(self.ledger.total["output"], 30)

    def test_cache_buckets_are_tracked_separately(self):
        self.ledger.record_usage({
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_input_tokens": 9_000,
            "cache_creation_input_tokens": 1_000,
        })
        self.assertEqual(self.ledger.total["cache_read"], 9_000)
        self.assertEqual(self.ledger.total["cache_write"], 1_000)

    def test_cost_uses_four_separate_prices(self):
        ledger = UsageLedger(prices=Prices(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75))
        ledger.record_usage({
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
        })
        self.assertAlmostEqual(ledger.cost(), 3.0 + 15.0 + 0.3 + 3.75)

    def test_cost_per_turn(self):
        ledger = UsageLedger(prices=Prices(input=2.0, output=10.0, cache_read=0.0, cache_write=0.0))
        ledger.record_usage({"input_tokens": 500_000, "output_tokens": 0})
        ledger.new_turn()
        ledger.record_usage({"input_tokens": 1_000_000, "output_tokens": 0})
        self.assertAlmostEqual(ledger.cost(1), 1.0)
        self.assertAlmostEqual(ledger.cost(2), 2.0)
        self.assertAlmostEqual(ledger.cost(), 3.0)

    def test_cost_without_prices_raises(self):
        self.ledger.record_usage({"input_tokens": 10, "output_tokens": 1})
        with self.assertRaises(ValueError):
            self.ledger.cost()


if __name__ == "__main__":
    unittest.main()
