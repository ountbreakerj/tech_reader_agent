"""13 权限规则引擎的测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.permissions import (
    ALLOW,
    ASK,
    DENY,
    Decision,
    PathEscape,
    PermissionEngine,
    Rule,
    RuleError,
    default_rules,
    escapes_root,
    resolve_within,
)


class ResolveTests(unittest.TestCase):
    def test_relative_path_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = resolve_within(root, "src/app.py")
            self.assertTrue(str(target).startswith(str(Path(root).resolve())))

    def test_parent_traversal_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PathEscape):
                resolve_within(root, "../secret.txt")

    def test_sibling_prefix_is_not_inside(self) -> None:
        # /tmp/work 与 /tmp/work2 共用前缀，不能靠字符串前缀判断
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "work"
            root.mkdir()
            sibling = Path(parent) / "work2"
            sibling.mkdir()
            with self.assertRaises(PathEscape):
                resolve_within(str(root), str(sibling / "x.txt"))

    def test_null_byte_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PathEscape):
                resolve_within(root, "src/\x00evil")

    def test_absolute_path_outside_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            outside = Path(tempfile.gettempdir()) / "elsewhere.txt"
            with self.assertRaises(PathEscape):
                resolve_within(root, str(outside))


class RuleTests(unittest.TestCase):
    def test_deny_requires_reason(self) -> None:
        with self.assertRaises(RuleError):
            Rule(name="r", mode=DENY)
        with self.assertRaises(RuleError):
            Rule(name="r", mode=ASK)

    def test_bad_mode_rejected(self) -> None:
        with self.assertRaises(RuleError):
            Rule(name="r", mode="maybe")

    def test_predicate_exception_counts_as_miss(self) -> None:
        def boom(tool: str, args: dict) -> bool:
            raise RuntimeError("坏规则")

        rule = Rule(name="boom", mode=DENY, matches=boom, reason="不该命中")
        self.assertFalse(rule.hit("read_file", {}))

    def test_tool_pattern_is_glob(self) -> None:
        rule = Rule(name="writes", mode=ASK, tool="write_*", reason="写操作")
        self.assertTrue(rule.hit("write_file", {}))
        self.assertFalse(rule.hit("read_file", {}))


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def engine(self, rules=None, **kwargs) -> PermissionEngine:
        return PermissionEngine(rules or default_rules(self.root), **kwargs)

    def test_deny_wins_over_allow(self) -> None:
        rules = [
            Rule(name="allow-all", mode=ALLOW, tool="*"),
            Rule(name="deny-rm", mode=DENY, tool="run_bash",
                 matches=lambda t, a: "rm " in str(a.get("command", "")), reason="不许删"),
        ]
        engine = PermissionEngine(rules)
        self.assertEqual(engine.decide("run_bash", {"command": "rm -rf /"}).outcome, DENY)
        self.assertEqual(engine.decide("run_bash", {"command": "ls ."}).outcome, ALLOW)

    def test_deny_wins_regardless_of_rule_order(self) -> None:
        allow = Rule(name="allow", mode=ALLOW, tool="*")
        deny = Rule(name="deny", mode=DENY, tool="*", reason="一律拒绝")
        self.assertEqual(PermissionEngine([allow, deny]).decide("x", {}).outcome, DENY)
        self.assertEqual(PermissionEngine([deny, allow]).decide("x", {}).outcome, DENY)

    def test_path_escape_is_denied(self) -> None:
        engine = self.engine()
        self.assertEqual(engine.decide("read_file", {"path": "../../etc/passwd"}).outcome, DENY)

    def test_credentials_are_denied(self) -> None:
        engine = self.engine()
        decision = engine.decide("read_file", {"path": "config/credentials.json"})
        self.assertEqual(decision.outcome, DENY)
        self.assertIn("凭据", decision.reason)

    def test_default_is_ask_and_fail_closed_without_channel(self) -> None:
        engine = PermissionEngine([], has_approval_channel=True)
        self.assertEqual(engine.decide("unknown_tool", {}).outcome, ASK)

        closed = PermissionEngine([], has_approval_channel=False)
        decision = closed.decide("unknown_tool", {})
        self.assertEqual(decision.outcome, DENY)
        self.assertIn("没有审批通道", decision.reason)

    def test_reason_is_present_on_deny(self) -> None:
        engine = self.engine()
        decision = engine.decide("write_file", {"path": "a.txt", "content": "x"})
        self.assertEqual(decision.outcome, ASK)
        self.assertTrue(decision.reason)

    def test_audit_records_every_call(self) -> None:
        engine = self.engine()
        engine.decide("read_file", {"path": "a.txt"})
        engine.decide("run_bash", {"command": "rm -rf build"})
        self.assertEqual([entry.outcome for entry in engine.audit], [ALLOW, DENY])
        self.assertEqual(engine.audit[1].rule, "deny-shell-write")

    def test_explain_lists_hits_in_severity_order(self) -> None:
        rules = [
            Rule(name="allow-all", mode=ALLOW, tool="read_file"),
            Rule(name="ask-files", mode=ASK, tool="read_file", reason="看一眼"),
        ]
        engine = PermissionEngine(rules)
        self.assertEqual(engine.explain("read_file", {}), ["ask-files[ask]", "allow-all[allow]"])

    def test_escapes_root_predicate(self) -> None:
        predicate = escapes_root(self.root)
        self.assertTrue(predicate("read_file", {"path": "../x"}))
        self.assertFalse(predicate("read_file", {"path": "x"}))

    def test_decision_describe_includes_rule(self) -> None:
        self.assertIn("deny", Decision(DENY, "deny-x", "不许").describe())

    def test_allowed_property(self) -> None:
        self.assertTrue(Decision(ALLOW).allowed)
        self.assertFalse(Decision(ASK, "a", "b").allowed)


if __name__ == "__main__":
    unittest.main()
