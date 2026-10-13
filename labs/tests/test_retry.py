"""18 重试与退避的测试。不真的睡觉：sleep 和随机数都是注入的。"""
from __future__ import annotations

import unittest

from harness.retry import (
    AUTH,
    CANCELLED,
    INVALID,
    RATE_LIMIT,
    SERVER,
    TIMEOUT,
    TOO_LONG,
    UNKNOWN,
    Failure,
    RetryPolicy,
    backoff_delay,
    classify_status,
    describe_for_model,
    herd_spread,
    parse_retry_after,
    plan_wait,
    retry_call,
)


class Script:
    """按脚本依次失败或成功的假调用。"""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class FakeSleep:
    def __init__(self):
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def mid(): return 0.5  # 抖动因子恰好为 1


class ClassifyTests(unittest.TestCase):
    def test_status_classes(self) -> None:
        self.assertEqual(classify_status(429), RATE_LIMIT)
        self.assertEqual(classify_status(529), RATE_LIMIT)
        self.assertEqual(classify_status(503), SERVER)
        self.assertEqual(classify_status(504), TIMEOUT)
        self.assertEqual(classify_status(401), AUTH)
        self.assertEqual(classify_status(400), INVALID)
        self.assertEqual(classify_status(None), UNKNOWN)

    def test_overloaded_body_is_rate_limit(self) -> None:
        self.assertEqual(classify_status(500, '{"type":"overloaded_error"}'), RATE_LIMIT)

    def test_too_long_is_not_retryable_server_error(self) -> None:
        self.assertEqual(classify_status(400, "prompt is too long: 210000 tokens"), TOO_LONG)

    def test_word_timeout_in_400_is_not_a_timeout(self) -> None:
        # 按状态码分类：错误文本里出现 timeout 不代表请求超时
        self.assertEqual(classify_status(400, "invalid parameter: timeout must be positive"), INVALID)

    def test_parse_retry_after(self) -> None:
        self.assertEqual(parse_retry_after("3"), 3.0)
        self.assertEqual(parse_retry_after(" 1.5 "), 1.5)
        self.assertIsNone(parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT"))
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("-1"))


class BackoffTests(unittest.TestCase):
    def test_exponential_growth_without_jitter(self) -> None:
        policy = RetryPolicy(initial_delay=0.5, max_delay=10, jitter=0)
        self.assertEqual([backoff_delay(policy, n) for n in range(6)], [0.5, 1, 2, 4, 8, 10])

    def test_jitter_bounds(self) -> None:
        policy = RetryPolicy(initial_delay=0.5, max_delay=10, jitter=0.1)
        self.assertAlmostEqual(backoff_delay(policy, 0, rand=lambda: 0.0), 0.45)
        self.assertAlmostEqual(backoff_delay(policy, 1, rand=lambda: 1.0), 1.1)

    def test_jitter_never_exceeds_cap(self) -> None:
        policy = RetryPolicy(initial_delay=1, max_delay=4, jitter=0.5)
        self.assertLessEqual(backoff_delay(policy, 10, rand=lambda: 0.999), 4)

    def test_jitter_spreads_a_herd(self) -> None:
        import random as _random
        gen = _random.Random(7)
        policy = RetryPolicy(initial_delay=1, max_delay=10, jitter=0.5)
        no_jitter = RetryPolicy(initial_delay=1, max_delay=10, jitter=0)
        spread = herd_spread(backoff_delay(policy, 2, rand=gen.random) for _ in range(100))
        flat = herd_spread(backoff_delay(no_jitter, 2) for _ in range(100))
        self.assertEqual(flat, 0)
        self.assertGreater(spread, 2)

    def test_bad_policy_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(max_retries=-1)
        with self.assertRaises(ValueError):
            RetryPolicy(initial_delay=5, max_delay=1)
        with self.assertRaises(ValueError):
            RetryPolicy(jitter=1.5)


class PlanTests(unittest.TestCase):
    def test_non_retryable_gives_up_immediately(self) -> None:
        delay, _, why = plan_wait(RetryPolicy(), Failure(AUTH), 0, 0)
        self.assertIsNone(delay)
        self.assertIn("不可重试", why)

    def test_retry_after_is_honored(self) -> None:
        delay, source, _ = plan_wait(RetryPolicy(), Failure(RATE_LIMIT, retry_after=3), 0, 0)
        self.assertEqual((delay, source), (3, "retry-after"))

    def test_retry_after_over_cap_gives_up(self) -> None:
        delay, _, why = plan_wait(RetryPolicy(max_delay=10), Failure(RATE_LIMIT, retry_after=3600), 0, 0)
        self.assertIsNone(delay)
        self.assertIn("3600", why)

    def test_total_budget(self) -> None:
        policy = RetryPolicy(max_total=5, jitter=0)
        delay, _, why = plan_wait(policy, Failure(SERVER), 3, waited=1.0)  # 想再等 4s，累计 5s 刚好
        self.assertEqual(delay, 4)
        delay, _, why = plan_wait(policy, Failure(SERVER), 3, waited=1.5)
        self.assertIsNone(delay)
        self.assertIn("总上限", why)


class RetryCallTests(unittest.TestCase):
    def test_success_first_try(self) -> None:
        sleep = FakeSleep()
        outcome = retry_call(Script("ok"), sleep=sleep)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, "ok")
        self.assertEqual(sleep.waits, [])

    def test_transient_then_success(self) -> None:
        sleep = FakeSleep()
        fn = Script(Failure(SERVER), Failure(TIMEOUT), "ok")
        outcome = retry_call(fn, RetryPolicy(jitter=0), sleep=sleep)
        self.assertTrue(outcome.ok)
        self.assertEqual(fn.calls, 3)
        self.assertEqual(sleep.waits, [0.5, 1.0])
        self.assertEqual([a.kind for a in outcome.attempts], [SERVER, TIMEOUT, None])

    def test_budget_exhausted(self) -> None:
        sleep = FakeSleep()
        fn = Script(*[Failure(SERVER)] * 4)
        outcome = retry_call(fn, RetryPolicy(max_retries=3, jitter=0), sleep=sleep)
        self.assertFalse(outcome.ok)
        self.assertEqual(fn.calls, 4)          # 1 次首发 + 3 次重试
        self.assertEqual(len(sleep.waits), 3)
        self.assertIn("上限", outcome.gave_up_because)

    def test_auth_is_not_retried(self) -> None:
        fn = Script(Failure(AUTH, "invalid x-api-key"), "never")
        outcome = retry_call(fn, sleep=FakeSleep())
        self.assertFalse(outcome.ok)
        self.assertEqual(fn.calls, 1)

    def test_too_long_is_not_retried(self) -> None:
        fn = Script(Failure(TOO_LONG), "never")
        outcome = retry_call(fn, sleep=FakeSleep())
        self.assertEqual(fn.calls, 1)
        self.assertEqual(outcome.failure.kind, TOO_LONG)

    def test_unclassified_exception_not_retried(self) -> None:
        fn = Script(KeyError("x"), "never")
        outcome = retry_call(fn, sleep=FakeSleep())
        self.assertEqual(fn.calls, 1)
        self.assertEqual(outcome.failure.kind, UNKNOWN)

    def test_on_attempt_called_before_sleep(self) -> None:
        events: list[str] = []
        fn = Script(Failure(SERVER), "ok")
        retry_call(fn, RetryPolicy(jitter=0),
                   sleep=lambda s: events.append(f"sleep {s}"),
                   on_attempt=lambda a: events.append(f"log {a.number} {a.kind}"))
        self.assertEqual(events, ["log 1 server", "sleep 0.5"])

    def test_cancellation_between_attempts(self) -> None:
        state = {"calls": 0}

        def stop() -> bool:
            return state["calls"] >= 1

        def fn():
            state["calls"] += 1
            raise Failure(SERVER)

        outcome = retry_call(fn, sleep=FakeSleep(), should_stop=stop)
        self.assertEqual(outcome.failure.kind, CANCELLED)
        self.assertEqual(state["calls"], 1)

    def test_describe_for_model(self) -> None:
        outcome = retry_call(Script(*[Failure(RATE_LIMIT)] * 3),
                             RetryPolicy(max_retries=2, jitter=0), sleep=FakeSleep())
        text = describe_for_model(outcome)
        self.assertIn("rate_limit", text)
        self.assertIn("3 次", text)
        self.assertEqual(describe_for_model(retry_call(Script("ok"), sleep=FakeSleep())), "")


if __name__ == "__main__":
    unittest.main()
