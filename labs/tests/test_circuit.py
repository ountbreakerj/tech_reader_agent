"""20 熔断与降级的测试。"""
from __future__ import annotations

import unittest

from harness.circuit import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    BreakerConfig,
    CircuitBreaker,
    CircuitOpen,
    Route,
    describe_open,
    with_fallback,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def fail():
    raise ConnectionError("down")


def ok():
    return "ok"


class BreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.breaker = CircuitBreaker("model-a", BreakerConfig(failure_threshold=3, cooldown=30), self.clock)

    def trip(self) -> None:
        for _ in range(3):
            with self.assertRaises(ConnectionError):
                self.breaker.call(fail)

    def test_opens_after_consecutive_failures(self) -> None:
        self.trip()
        self.assertEqual(self.breaker.state, OPEN)

    def test_open_rejects_without_calling(self) -> None:
        self.trip()
        calls = []
        with self.assertRaises(CircuitOpen) as ctx:
            self.breaker.call(lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertAlmostEqual(ctx.exception.retry_in, 30)

    def test_success_resets_consecutive_count(self) -> None:
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                self.breaker.call(fail)
        self.breaker.call(ok)
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                self.breaker.call(fail)
        self.assertEqual(self.breaker.state, CLOSED)

    def test_half_open_after_cooldown(self) -> None:
        self.trip()
        self.clock.advance(29)
        self.assertEqual(self.breaker.current_state(), OPEN)
        self.clock.advance(1)
        self.assertEqual(self.breaker.current_state(), HALF_OPEN)

    def test_probe_success_closes(self) -> None:
        self.trip()
        self.clock.advance(30)
        self.assertEqual(self.breaker.call(ok), "ok")
        self.assertEqual(self.breaker.state, CLOSED)

    def test_probe_failure_reopens_with_fresh_cooldown(self) -> None:
        self.trip()
        self.clock.advance(30)
        with self.assertRaises(ConnectionError):
            self.breaker.call(fail)
        self.assertEqual(self.breaker.state, OPEN)
        self.clock.advance(29)
        self.assertEqual(self.breaker.current_state(), OPEN)

    def test_half_open_limits_concurrent_probes(self) -> None:
        self.trip()
        self.clock.advance(30)
        self.breaker.current_state()
        self.breaker.in_flight_probes = 1     # 模拟已有一个试探在途
        with self.assertRaises(CircuitOpen):
            self.breaker.call(ok)

    def test_failure_rate_trips_even_with_interleaved_success(self) -> None:
        clock = FakeClock()
        cfg = BreakerConfig(failure_threshold=100, window=10, failure_rate=0.6, min_calls=10)
        breaker = CircuitBreaker("flaky", cfg, clock)
        pattern = [fail, fail, ok, fail, fail, ok, fail, fail, ok, fail]   # 7/10 失败，连续数从不超过 2
        for fn in pattern:
            try:
                breaker.call(fn)
            except ConnectionError:
                pass
        self.assertEqual(breaker.state, OPEN)
        self.assertIn("失败率", breaker.transitions[-1].reason)

    def test_min_calls_prevents_cold_start_trip(self) -> None:
        cfg = BreakerConfig(failure_threshold=100, window=10, failure_rate=0.5, min_calls=10)
        breaker = CircuitBreaker("cold", cfg, FakeClock())
        with self.assertRaises(ConnectionError):
            breaker.call(fail)
        self.assertEqual(breaker.state, CLOSED)   # 1/1 = 100%，但样本太少

    def test_client_errors_do_not_count(self) -> None:
        cfg = BreakerConfig(failure_threshold=2, counts_as_failure=lambda e: not isinstance(e, ValueError))
        breaker = CircuitBreaker("api", cfg, FakeClock())

        def bad_request():
            raise ValueError("400 bad request")

        for _ in range(5):
            with self.assertRaises(ValueError):
                breaker.call(bad_request)
        self.assertEqual(breaker.state, CLOSED)

    def test_transitions_are_recorded(self) -> None:
        self.trip()
        self.clock.advance(30)
        self.breaker.call(ok)
        self.assertEqual([(t.old, t.new) for t in self.breaker.transitions],
                         [(CLOSED, OPEN), (OPEN, HALF_OPEN), (HALF_OPEN, CLOSED)])


class FallbackTests(unittest.TestCase):
    def test_primary_success_is_not_degraded(self) -> None:
        result = with_fallback([Route("primary", ok), Route("backup", ok)])
        self.assertTrue(result.ok)
        self.assertFalse(result.degraded)
        self.assertEqual(result.route, "primary")

    def test_falls_back_and_says_so(self) -> None:
        result = with_fallback([Route("primary", fail), Route("backup", ok, note="已切换到备用模型")])
        self.assertTrue(result.degraded)
        self.assertEqual(result.route, "backup")
        self.assertEqual(result.note, "已切换到备用模型")
        self.assertEqual(result.skipped[0][0], "primary")

    def test_open_breaker_skips_route_without_calling(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker("primary", BreakerConfig(failure_threshold=1), clock)
        with self.assertRaises(ConnectionError):
            breaker.call(fail)
        calls = []
        result = with_fallback(
            [Route("primary", lambda: calls.append("p")), Route("backup", ok)],
            {"primary": breaker},
        )
        self.assertEqual(calls, [])
        self.assertIn("熔断中", result.skipped[0][1])
        self.assertEqual(result.route, "backup")

    def test_all_routes_fail(self) -> None:
        result = with_fallback([Route("a", fail), Route("b", fail)])
        self.assertFalse(result.ok)
        self.assertIn("所有路线都不可用", result.note)

    def test_describe_open(self) -> None:
        text = describe_open(CircuitOpen("model-a", 25))
        self.assertIn("不要在这一轮里重复调用", text)


if __name__ == "__main__":
    unittest.main()
