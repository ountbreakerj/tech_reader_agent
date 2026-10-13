"""24 可观测性与成本的测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from harness.observability import (
    CostLedger,
    Metrics,
    Tracer,
    Usage,
    cost_of,
    jsonl_sink,
    redact,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


class TracerTests(unittest.TestCase):
    def test_nested_spans_form_a_tree(self) -> None:
        clock = FakeClock()
        tracer = Tracer(clock)
        with tracer.span("turn") as turn:
            with tracer.span("model.call", model="big-model"):
                clock.advance(2)
            with tracer.span("tool.run_tests"):
                clock.advance(5)
        self.assertEqual(turn.duration, 7)
        self.assertEqual([s.name for s in tracer.children(turn.span_id)], ["model.call", "tool.run_tests"])

    def test_slowest_leaf_spans(self) -> None:
        clock = FakeClock()
        tracer = Tracer(clock)
        with tracer.span("turn"):
            for name, seconds in [("read_file", 0.1), ("run_tests", 6), ("grep", 0.5)]:
                with tracer.span(f"tool.{name}"):
                    clock.advance(seconds)
        self.assertEqual(tracer.slowest(1)[0].name, "tool.run_tests")

    def test_error_marks_span_and_reraises(self) -> None:
        tracer = Tracer(FakeClock())
        with self.assertRaises(ValueError):
            with tracer.span("tool.broken"):
                raise ValueError("x")
        self.assertEqual(tracer.spans[0].status, "error")
        self.assertEqual(tracer.spans[0].attrs["error"], "ValueError")

    def test_content_redacted_by_default(self) -> None:
        tracer = Tracer(FakeClock())
        with tracer.span("tool.read_file", tool_input={"path": "secret.py"}, output="def x(): ...", tool="read_file"):
            pass
        attrs = tracer.spans[0].attrs
        self.assertNotIn("tool_input", attrs)
        self.assertIn("tool_input_chars", attrs)
        self.assertEqual(attrs["tool"], "read_file")

    def test_keep_content_opt_in(self) -> None:
        self.assertEqual(redact({"output": "abc"}, keep_content=True), {"output": "abc"})

    def test_sink_failure_does_not_break_agent(self) -> None:
        def broken_sink(record):
            raise OSError("disk full")

        tracer = Tracer(FakeClock(), sink=broken_sink)
        with tracer.span("turn"):
            pass
        self.assertEqual(tracer.sink_errors, 1)
        self.assertEqual(len(tracer.spans), 1)

    def test_jsonl_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            tracer = Tracer(FakeClock(), sink=jsonl_sink(path))
            with tracer.span("turn"):
                with tracer.span("model.call"):
                    pass
            with open(path, encoding="utf-8") as handle:
                lines = [json.loads(l) for l in handle]
        self.assertEqual([l["name"] for l in lines], ["model.call", "turn"])
        self.assertEqual(lines[0]["parent_id"], lines[1]["span_id"])
        self.assertEqual(lines[0]["trace_id"], lines[1]["trace_id"])


class CostTests(unittest.TestCase):
    def test_four_buckets_priced_separately(self) -> None:
        usage = Usage(input=1_000_000, output=0, cache_read=1_000_000, cache_write=0)
        # 同样一百万 token，缓存读只要普通输入的十分之一（教学示意价）
        self.assertAlmostEqual(cost_of(usage, "big-model"), 3.00 + 0.30)

    def test_unknown_model_is_an_error(self) -> None:
        with self.assertRaises(KeyError):
            cost_of(Usage(input=1), "mystery-model")

    def test_ledger_groups_hidden_costs(self) -> None:
        ledger = CostLedger()
        ledger.record(1, "big-model", Usage(input=10_000, output=1_000))
        ledger.record(1, "big-model", Usage(input=10_000, output=0), kind="retry")
        ledger.record(2, "small-model", Usage(input=50_000, output=2_000), kind="subagent")
        by_kind = ledger.by("kind")
        self.assertIn("retry", by_kind)
        self.assertIn("subagent", by_kind)
        self.assertIn("retry", ledger.summary())

    def test_cache_hit_rate(self) -> None:
        ledger = CostLedger()
        ledger.record(1, "big-model", Usage(input=1000, cache_write=9000))
        ledger.record(2, "big-model", Usage(input=1000, cache_read=9000))
        self.assertAlmostEqual(ledger.cache_hit_rate(), 9000 / 20000)

    def test_budget(self) -> None:
        ledger = CostLedger(budget=0.01)
        ledger.record(1, "big-model", Usage(input=1_000_000))
        self.assertTrue(ledger.over_budget())
        self.assertIn("已超出", ledger.summary())


class MetricsTests(unittest.TestCase):
    def test_rate(self) -> None:
        metrics = Metrics()
        metrics.inc("sessions", 10)
        metrics.inc("sessions_completed", 7)
        self.assertAlmostEqual(metrics.rate("sessions_completed", "sessions"), 0.7)
        self.assertEqual(metrics.rate("x", "nothing"), 0.0)


if __name__ == "__main__":
    unittest.main()
