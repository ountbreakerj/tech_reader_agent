"""19 超时的测试。用假时钟，不依赖真实的等待（run_with_timeout 的两条除外，各约 0.1 秒）。"""
from __future__ import annotations

import threading
import time
import unittest

from harness.timeouts import (
    Deadline,
    Timeout,
    TimeoutPlan,
    describe_timeout,
    idle_guard,
    run_with_timeout,
)


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeadlineTests(unittest.TestCase):
    def test_remaining_and_expiry(self) -> None:
        clock = FakeClock()
        d = Deadline("turn", 10, clock)
        clock.advance(4)
        self.assertEqual(d.remaining(), 6)
        clock.advance(6)
        self.assertTrue(d.expired())
        with self.assertRaises(Timeout) as ctx:
            d.check()
        self.assertEqual(ctx.exception.scope, "turn")

    def test_child_cannot_outlive_parent(self) -> None:
        clock = FakeClock()
        turn = Deadline("turn", 10, clock)
        clock.advance(8)
        tool = turn.child("tool:run_tests", 60)
        self.assertEqual(tool.remaining(), 2)    # 声明 60 秒，实际只剩父级的 2 秒

    def test_child_shorter_than_parent_uses_own_budget(self) -> None:
        clock = FakeClock()
        turn = Deadline("turn", 100, clock)
        tool = turn.child("tool:read_file", 5)
        self.assertEqual(tool.remaining(), 5)

    def test_timeout_is_attributed_to_binding_scope(self) -> None:
        clock = FakeClock()
        turn = Deadline("turn", 10, clock)
        clock.advance(8)
        tool = turn.child("tool:run_tests", 60)
        clock.advance(3)
        with self.assertRaises(Timeout) as ctx:
            tool.check()
        self.assertEqual(ctx.exception.scope, "turn")     # 是整轮用完了，不是工具太慢

    def test_own_budget_attribution(self) -> None:
        clock = FakeClock()
        turn = Deadline("turn", 100, clock)
        tool = turn.child("tool:read_file", 5)
        clock.advance(6)
        with self.assertRaises(Timeout) as ctx:
            tool.check()
        self.assertEqual(ctx.exception.scope, "tool:read_file")

    def test_nonpositive_budget_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Deadline("x", 0)


class IdleGuardTests(unittest.TestCase):
    def test_steady_stream_passes_even_if_long(self) -> None:
        clock = FakeClock()

        def chunks():
            for i in range(10):
                clock.advance(5)        # 每 5 秒一块，总共 50 秒
                yield i

        self.assertEqual(list(idle_guard(chunks(), idle=10, clock=clock)), list(range(10)))

    def test_stall_raises_idle_timeout(self) -> None:
        clock = FakeClock()

        def chunks():
            yield 1
            clock.advance(30)
            yield 2

        with self.assertRaises(Timeout) as ctx:
            list(idle_guard(chunks(), idle=10, clock=clock, scope="model"))
        self.assertEqual(ctx.exception.kind, "idle")
        self.assertEqual(ctx.exception.scope, "model")

    def test_total_deadline_still_applies(self) -> None:
        clock = FakeClock()
        total = Deadline("model", 20, clock)

        def chunks():
            for i in range(10):
                clock.advance(5)
                yield i

        got: list[int] = []
        with self.assertRaises(Timeout) as ctx:
            for chunk in idle_guard(chunks(), idle=10, total=total, clock=clock):
                got.append(chunk)
        self.assertEqual(ctx.exception.kind, "total")
        self.assertEqual(got, [0, 1, 2])


class RunWithTimeoutTests(unittest.TestCase):
    def test_returns_value_in_time(self) -> None:
        d = Deadline("tool:x", 2)
        self.assertEqual(run_with_timeout(lambda cancel: 42, d), 42)

    def test_cooperative_cancel_is_signalled(self) -> None:
        stopped = threading.Event()

        def slow(cancel: threading.Event) -> str:
            while not cancel.wait(0.01):
                pass
            stopped.set()
            return "late"

        with self.assertRaises(Timeout):
            run_with_timeout(slow, Deadline("tool:slow", 0.1))
        # 等待要宽松：机器负载高时线程调度会被推迟，1 秒会让这条测试偶发失败
        self.assertTrue(stopped.wait(10.0))   # 工作线程看到了取消信号并退出

    def test_errors_propagate(self) -> None:
        def boom(cancel):
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            run_with_timeout(boom, Deadline("tool:boom", 1))


class PlanAndMessageTests(unittest.TestCase):
    def test_tool_overrides(self) -> None:
        plan = TimeoutPlan()
        self.assertEqual(plan.tool_budget("run_tests"), 600)
        self.assertEqual(plan.tool_budget("read_file"), 10)
        self.assertEqual(plan.tool_budget("anything_else"), 60)

    def test_messages_differ_by_scope(self) -> None:
        tool = describe_timeout(Timeout("tool:run_tests", 120, 121))
        turn = describe_timeout(Timeout("turn", 600, 601))
        idle = describe_timeout(Timeout("model", 60, 61, kind="idle"))
        self.assertIn("不要原样重试", tool)
        self.assertIn("总结已经完成的部分", turn)
        self.assertIn("可以重试", idle)


if __name__ == "__main__":
    unittest.main()
