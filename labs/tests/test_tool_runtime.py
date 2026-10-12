"""tool_runtime 的批切分、并发上限、顺序提交、超时/异常/取消测试。

全部用真实的线程跑（不 mock），睡眠都在几十毫秒级，整套控制在 1 秒内。
"""
from __future__ import annotations

import threading
import time
import unittest

from harness.tool_runtime import (
    DEFAULT_MAX_PARALLEL,
    CallResult,
    ToolCall,
    ToolCallTimeout,
    ToolSpec,
    cancelled_result,
    execute_batch,
    plan_batches,
    run_pipeline,
    summarize,
)


def make_read(delay: float = 0.0, is_parallel: bool = True,
              timeout: float | None = None) -> ToolSpec:
    """一个可并行的探针工具：睡 delay 后返回 'read:<id>'。"""
    def fn(args: dict) -> str:
        time.sleep(delay)
        return f"read:{args.get('id')}"
    return ToolSpec(fn=fn, is_parallel=is_parallel, timeout=timeout)


def make_write(delay: float = 0.0) -> ToolSpec:
    """一个独占的探针工具（写操作）：默认不带 is_parallel，就是屏障。"""
    def fn(args: dict) -> str:
        time.sleep(delay)
        return f"write:{args.get('id')}"
    return ToolSpec(fn=fn)


def plan_of(*triples: tuple[str, str]) -> list[ToolCall]:
    """按 (call_id, tool_name) 建一批调用。"""
    return [ToolCall(call_id=cid, name=name, args={"id": cid}) for cid, name in triples]


def by_id(calls: list[ToolCall]) -> dict[str, ToolCall]:
    return {c.call_id: c for c in calls}


READ_ONLY = {"read": make_read(), "write": make_write()}


class TestPlanBatches(unittest.TestCase):
    """批切分是纯函数，也是这章最该钉死的行为。"""

    def test_all_parallel_collapses_into_one_batch(self):
        plan = plan_of(("c1", "read"), ("c2", "read"))
        self.assertEqual(plan_batches(plan, READ_ONLY), [["c1", "c2"]])

    def test_middle_exclusive_splits_into_three_batches(self):
        """对应 DSH tool-calls.spec.ts:126「独占调用形成屏障（3 组）」。"""
        plan = plan_of(("c1", "read"), ("c2", "write"), ("c3", "read"))
        self.assertEqual(plan_batches(plan, READ_ONLY), [["c1"], ["c2"], ["c3"]])

    def test_consecutive_writes_each_form_their_own_batch(self):
        plan = plan_of(("c1", "write"), ("c2", "write"))
        self.assertEqual(plan_batches(plan, READ_ONLY), [["c1"], ["c2"]])

    def test_leading_and_trailing_writes_do_not_merge_with_reads(self):
        plan = plan_of(("c1", "write"), ("c2", "read"), ("c3", "read"),
                       ("c4", "write"), ("c5", "read"))
        self.assertEqual(plan_batches(plan, READ_ONLY),
                         [["c1"], ["c2", "c3"], ["c4"], ["c5"]])

    def test_undeclared_tool_fails_closed_to_exclusive(self):
        """没声明 is_parallel 就是屏障——DSH 的 executionMode() 也是这个默认。"""
        plan = plan_of(("c1", "read"), ("c2", "unknown-tool"))
        self.assertEqual(plan_batches(plan, READ_ONLY), [["c1"], ["c2"]])

    def test_empty_plan_is_empty(self):
        self.assertEqual(plan_batches([], READ_ONLY), [])

    def test_default_cap_is_the_real_dsh_value(self):
        """DSH constants.ts:6 的真实值就是 10，不是教学示意。"""
        self.assertEqual(DEFAULT_MAX_PARALLEL, 10)


class TestExecuteBatchOrder(unittest.TestCase):
    """提交顺序 == 输入顺序，这是整章最重要的不变量。"""

    def test_reversed_completion_order_still_submits_in_input_order(self):
        delays = {"c1": 0.06, "c2": 0.03, "c3": 0.005}

        def fn(args: dict) -> str:
            time.sleep(delays[args["id"]])
            return f"read:{args['id']}"

        specs = {"read": ToolSpec(fn=fn, is_parallel=True)}
        plan = plan_of(("c1", "read"), ("c2", "read"), ("c3", "read"))
        results = execute_batch(["c1", "c2", "c3"], by_id(plan), specs, max_parallel=3)
        self.assertEqual([r.call_id for r in results], ["c1", "c2", "c3"])
        self.assertEqual([r.content for r in results], ["read:c1", "read:c2", "read:c3"])
        # 完成顺序确实反了过来：c3 先结束，但它排在最后
        self.assertLess(results[2].finished_at, results[0].finished_at)

    def test_fast_later_call_does_not_jump_ahead_of_a_slow_earlier_one(self):
        specs = {
            "slow": ToolSpec(fn=lambda a: (time.sleep(0.06), "slow")[1], is_parallel=True),
            "fast": make_read(),
        }
        plan = [ToolCall("c1", "slow", {"id": "1"}), ToolCall("c2", "fast", {"id": "2"})]
        results = execute_batch(["c1", "c2"], by_id(plan), specs, max_parallel=2)
        self.assertEqual(summarize(results), [("c1", False), ("c2", False)])

    def test_summarize_reports_error_positions(self):
        """工具没注册时，两条结果都在原位变成错误，长度不变。"""
        plan = plan_of(("c1", "read"), ("c2", "read"))
        results = execute_batch(["c1", "c2"], by_id(plan), {}, max_parallel=2)
        self.assertEqual(summarize(results), [("c1", True), ("c2", True)])


class TestConcurrencyCap(unittest.TestCase):
    """并发上限必须真被遵守，而不是只写进配置。"""

    def test_peak_concurrency_never_exceeds_the_cap(self):
        lock = threading.Lock()
        active = {"now": 0, "peak": 0}

        def fn(args: dict) -> str:
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.03)
            with lock:
                active["now"] -= 1
            return "ok"

        specs = {"read": ToolSpec(fn=fn, is_parallel=True)}
        plan = [ToolCall(f"c{i}", "read", {"id": str(i)}) for i in range(1, 7)]
        started = time.monotonic()
        results = execute_batch([c.call_id for c in plan], by_id(plan), specs, max_parallel=3)
        elapsed = time.monotonic() - started

        self.assertEqual(active["peak"], 3)                 # 上限被用满
        self.assertLessEqual(active["peak"], 3)              # 且绝不被超过
        self.assertGreaterEqual(elapsed, 0.055)              # 6 个 0.03s 按 3 并发 => 至少两波
        self.assertEqual([r.call_id for r in results], [f"c{i}" for i in range(1, 7)])
        self.assertTrue(all(not r.is_error for r in results))

    def test_cap_of_one_is_fully_serial(self):
        lock = threading.Lock()
        active = {"now": 0, "peak": 0}

        def fn(args: dict) -> str:
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.02)
            with lock:
                active["now"] -= 1
            return "ok"

        specs = {"read": ToolSpec(fn=fn, is_parallel=True)}
        plan = plan_of(("c1", "read"), ("c2", "read"))
        execute_batch(["c1", "c2"], by_id(plan), specs, max_parallel=1)
        self.assertEqual(active["peak"], 1)

    def test_cap_larger_than_batch_does_not_over_allocate(self):
        plan = plan_of(("c1", "read"), ("c2", "read"))
        results = execute_batch(["c1", "c2"], by_id(plan), READ_ONLY,
                                max_parallel=DEFAULT_MAX_PARALLEL)
        self.assertEqual(len(results), 2)


class TestBarrierInPipeline(unittest.TestCase):
    """独占调用构成屏障：它开始前前面的批必须提交完，它结束前后面不许开始。"""

    def test_write_barrier_serializes_around_itself(self):
        lock = threading.Lock()
        record: list[str] = []

        def probe(tag: str, delay: float, parallel: bool) -> ToolSpec:
            def fn(args: dict) -> str:
                with lock:
                    record.append(f"{tag}-{args['id']}")
                time.sleep(delay)
                return f"{tag}:{args['id']}"
            return ToolSpec(fn=fn, is_parallel=parallel)

        specs = {"read": probe("read", 0.02, True), "write": probe("write", 0.02, False)}
        plan = plan_of(("A", "read"), ("B", "read"), ("W", "write"), ("C", "read"))
        results = run_pipeline(plan, specs, max_parallel=4)
        # 两个读都结束后才轮到写，写结束后才轮到最后一个读
        self.assertEqual(record, ["read-A", "read-B", "write-W", "read-C"])
        self.assertEqual([r.call_id for r in results], ["A", "B", "W", "C"])

    def test_parallel_reads_really_overlap(self):
        """并行段的意义：两个调用确实同时在跑，而不是被排成串行。"""
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def fn(args: dict) -> str:
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.04)
            with lock:
                state["now"] -= 1
            return "ok"

        specs = {"read": ToolSpec(fn=fn, is_parallel=True)}
        started = time.monotonic()
        run_pipeline(plan_of(("c1", "read"), ("c2", "read")), specs)
        self.assertEqual(state["peak"], 2)
        self.assertLess(time.monotonic() - started, 0.075)   # 串行会是 ~0.08s


class TestFailuresArePerCall(unittest.TestCase):
    """一个工具失败不拖累同批其他调用，且失败能归因到具体调用。"""

    def test_one_raising_call_does_not_affect_siblings(self):
        def boom(args: dict) -> str:
            raise ValueError("disk on fire")

        specs = {"read": make_read(), "boom": ToolSpec(fn=boom, is_parallel=True)}
        plan = plan_of(("c1", "read"), ("c2", "boom"), ("c3", "read"))
        results = run_pipeline(plan, specs, max_parallel=3)

        self.assertEqual(summarize(results), [("c1", False), ("c2", True), ("c3", False)])
        self.assertIn("ValueError", results[1].content)
        self.assertIn("disk on fire", results[1].content)
        self.assertEqual(results[1].call_id, "c2")     # 归因：就是 c2 失败
        self.assertEqual(results[0].content, "read:c1")
        self.assertEqual(results[2].content, "read:c3")

    def test_unknown_tool_becomes_an_error_result_in_place(self):
        plan = plan_of(("c1", "read"), ("c2", "nonexistent"), ("c3", "read"))
        results = run_pipeline(plan, {"read": make_read()})
        self.assertEqual([r.call_id for r in results], ["c1", "c2", "c3"])
        self.assertTrue(results[1].is_error)
        self.assertIn("unknown tool", results[1].content)
        self.assertFalse(results[0].is_error)

    def test_timeout_is_per_call_and_flagged(self):
        specs = {"slow": make_read(delay=0.06, timeout=0.02), "read": make_read()}
        plan = plan_of(("c1", "slow"), ("c2", "read"))
        results = run_pipeline(plan, specs, max_parallel=2, timeout=5.0)
        self.assertTrue(results[0].timed_out)
        self.assertTrue(results[0].is_error)
        self.assertFalse(results[1].timed_out)
        self.assertEqual(results[1].content, "read:c2")

    def test_batch_level_timeout_covers_tools_without_their_own(self):
        specs = {"read": make_read(delay=0.06)}
        results = run_pipeline(plan_of(("c1", "read")), specs, timeout=0.02)
        self.assertTrue(results[0].timed_out)

    def test_tool_budget_overrides_the_batch_budget(self):
        """工具自己声明的预算优先，不会被整批预算误杀。"""
        specs = {"read": make_read(delay=0.05, timeout=1.0)}
        results = run_pipeline(plan_of(("c1", "read")), specs, timeout=0.01)
        self.assertFalse(results[0].is_error)

    def test_tool_timeout_exception_is_flagged_as_timeout(self):
        def fn(args: dict) -> str:
            raise ToolCallTimeout("x")

        specs = {"read": ToolSpec(fn=fn, is_parallel=True)}
        results = run_pipeline(plan_of(("c1", "read")), specs)
        self.assertTrue(results[0].timed_out)

    def test_a_tools_own_timeout_error_is_not_misattributed(self):
        """工具体自己抛 TimeoutError 是它的实现细节，不该被算成调度器超时。"""
        def fn(args: dict) -> str:
            raise TimeoutError("socket timeout")

        specs = {"read": ToolSpec(fn=fn, is_parallel=True)}
        results = run_pipeline(plan_of(("c1", "read")), specs)
        self.assertTrue(results[0].is_error)
        self.assertFalse(results[0].timed_out)


class TestCancellation(unittest.TestCase):
    """取消之后，不能留下没有 tool_result 的 tool_use。"""

    def test_cancel_before_dispatch_backfills_every_remaining_call(self):
        plan = plan_of(("c1", "read"), ("c2", "read"), ("c3", "write"), ("c4", "write"))
        results = run_pipeline(plan, READ_ONLY, on_cancel=lambda: True)
        self.assertEqual(len(results), len(plan))
        self.assertEqual([r.call_id for r in results], ["c1", "c2", "c3", "c4"])
        self.assertTrue(all(r.cancelled and r.is_error for r in results))

    def test_cancel_mid_pipeline_commits_started_and_backfills_the_tail(self):
        state = {"cancelled": False}

        def body(args: dict) -> str:
            state["cancelled"] = True     # 第一个调用执行期间用户按下中断
            return "ok"

        specs = {"read": make_read(), "write": ToolSpec(fn=body)}
        # c1 是独占调用（自己一批），它跑的时候设下取消标记；后面的批全部不再派发
        plan = plan_of(("c1", "write"), ("c2", "read"), ("c3", "read"))
        results = run_pipeline(plan, specs, on_cancel=lambda: state["cancelled"])

        self.assertEqual(len(results), 3)
        self.assertEqual([r.call_id for r in results], ["c1", "c2", "c3"])
        self.assertFalse(results[0].cancelled)   # 已派发的照常提交，结果不被丢掉
        self.assertEqual(results[0].content, "ok")
        self.assertTrue(results[1].cancelled)    # 之后的调用补一条，配对不掉队
        self.assertTrue(results[2].cancelled)
        self.assertTrue(results[1].is_error)

    def test_cancelled_result_shape(self):
        r = cancelled_result(ToolCall("c9", "write", {}))
        self.assertTrue(r.is_error)
        self.assertTrue(r.cancelled)
        self.assertFalse(r.timed_out)
        self.assertIn("cancelled before dispatch", r.content)


class TestCallResultShape(unittest.TestCase):
    def test_result_defaults(self):
        r = CallResult(call_id="c1", name="read", content="ok")
        self.assertFalse(r.is_error)
        self.assertFalse(r.cancelled)
        self.assertFalse(r.timed_out)
        self.assertIsNone(r.started_at)
        self.assertIsNone(r.finished_at)

    def test_successful_result_carries_timestamps(self):
        results = run_pipeline(plan_of(("c1", "read")), {"read": make_read()})
        self.assertIsNotNone(results[0].started_at)
        self.assertIsNotNone(results[0].finished_at)
        self.assertLessEqual(results[0].started_at, results[0].finished_at)


class TestBudgetGuard(unittest.TestCase):
    def test_pipeline_of_eight_calls_stays_far_below_the_budget(self):
        """防止后续加测试时把整套拖慢。"""
        started = time.monotonic()
        plan = [ToolCall(f"c{i}", "read", {"id": str(i)}) for i in range(1, 9)]
        results = run_pipeline(plan, {"read": make_read(delay=0.01)}, max_parallel=8)
        self.assertEqual(len(results), 8)
        self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
