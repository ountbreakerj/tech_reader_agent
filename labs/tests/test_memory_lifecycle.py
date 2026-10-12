"""memory_lifecycle 的测试：衰减、访问强化、TTL、替代链、陈旧路径、sweep 分组。"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from harness.memory_lifecycle import (
    DAY,
    MemoryRecord,
    SupersedeError,
    SweepPolicy,
    decayed_score,
    find_stale_references,
    is_expired,
    path_candidates,
    resolve_supersedes,
    sweep,
    touch,
)

T0 = 1_700_000_000.0


def rec(id: str, type: str = "project", *, age_days: float = 0.0, idle_days: float | None = None,
        strength: float = 1.0, ttl: float | None = None, supersedes: tuple[str, ...] = (),
        text: str = "") -> MemoryRecord:
    """以 T0 为「现在」，按天数倒推创建与访问时间。"""
    idle = age_days if idle_days is None else idle_days
    return MemoryRecord(id=id, type=type, text=text or id, created_at=T0 - age_days * DAY,
                        last_accessed_at=T0 - idle * DAY, strength=strength, ttl=ttl,
                        supersedes=supersedes)


class DecayTests(unittest.TestCase):
    def test_half_life_halves_the_score(self) -> None:
        r = rec("a", idle_days=7)
        self.assertAlmostEqual(decayed_score(r, T0, 7 * DAY), 0.5)
        self.assertAlmostEqual(decayed_score(r, T0 + 7 * DAY, 7 * DAY), 0.25)

    def test_monotonic_and_positive(self) -> None:
        r = rec("a")
        scores = [decayed_score(r, T0 + d * DAY, 10 * DAY) for d in range(0, 200, 10)]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(s > 0 for s in scores))

    def test_clock_skew_never_increases_score(self) -> None:
        r = rec("a", strength=0.6)
        self.assertEqual(decayed_score(r, T0 - 5 * DAY, DAY), 0.6)

    def test_rejects_non_positive_half_life(self) -> None:
        with self.assertRaises(ValueError):
            decayed_score(rec("a"), T0, 0)


class TouchTests(unittest.TestCase):
    def test_touch_settles_decay_before_boost(self) -> None:
        r = rec("a", idle_days=7)
        t = touch(r, T0, half_life=7 * DAY, boost=0.3)
        self.assertAlmostEqual(t.strength, 0.8)  # 0.5 + 0.3，而不是 1.0 + 0.3 封顶
        self.assertEqual(t.last_accessed_at, T0)
        self.assertEqual(r.strength, 1.0)  # 原对象不变

    def test_touch_respects_cap(self) -> None:
        t = touch(rec("a"), T0, half_life=DAY, boost=0.5, cap=1.2)
        self.assertAlmostEqual(t.strength, 1.2)

    def test_touch_resets_decay_clock(self) -> None:
        t = touch(rec("a", idle_days=30), T0, half_life=10 * DAY, boost=1.0)
        self.assertAlmostEqual(decayed_score(t, T0 + 10 * DAY, 10 * DAY), t.strength / 2)


class TtlTests(unittest.TestCase):
    def test_project_expires_preference_does_not(self) -> None:
        self.assertTrue(is_expired(rec("p", "project", age_days=15), T0))
        self.assertFalse(is_expired(rec("p", "project", age_days=13), T0))
        self.assertFalse(is_expired(rec("u", "user", age_days=3650), T0))

    def test_per_record_ttl_overrides_type(self) -> None:
        self.assertTrue(is_expired(rec("u", "user", age_days=2, ttl=DAY), T0))

    def test_ttl_counts_from_creation_not_access(self) -> None:
        r = rec("p", "project", age_days=20, idle_days=0)
        self.assertTrue(is_expired(r, T0))

    def test_unknown_type_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "不在 TTL 表里"):
            is_expired(rec("x", "projcet"), T0)

    def test_zero_ttl_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            is_expired(rec("p", ttl=0), T0)


class SupersedeTests(unittest.TestCase):
    def test_chain_resolves_to_tail(self) -> None:
        a, b, c = rec("a"), rec("b", supersedes=("a",)), rec("c", supersedes=("b",))
        res = resolve_supersedes([c, a, b])
        self.assertEqual(res.heads, {"a": "c", "b": "c", "c": "c"})
        self.assertEqual(res.current, ("c",))

    def test_merge_supersedes_several(self) -> None:
        res = resolve_supersedes([rec("a"), rec("b"), rec("m", supersedes=("a", "b")), rec("x")])
        self.assertEqual(res.current, ("m", "x"))
        self.assertEqual(res.heads["a"], "m")

    def test_cycle_is_reported_not_looped(self) -> None:
        with self.assertRaisesRegex(SupersedeError, "成环"):
            resolve_supersedes([rec("a", supersedes=("b",)), rec("b", supersedes=("a",))])

    def test_self_supersede_is_a_cycle(self) -> None:
        with self.assertRaisesRegex(SupersedeError, "a → a"):
            resolve_supersedes([rec("a", supersedes=("a",))])

    def test_dangling_strict_and_lenient(self) -> None:
        records = [rec("b", supersedes=("gone",))]
        with self.assertRaisesRegex(SupersedeError, "gone 不存在"):
            resolve_supersedes(records)
        res = resolve_supersedes(records, allow_dangling=True)
        self.assertEqual(res.dangling, (("b", "gone"),))
        self.assertEqual(res.current, ("b",))

    def test_fork_is_rejected(self) -> None:
        with self.assertRaisesRegex(SupersedeError, "分叉"):
            resolve_supersedes([rec("a"), rec("b", supersedes=("a",)), rec("c", supersedes=("a",))])

    def test_duplicate_ids_rejected(self) -> None:
        with self.assertRaisesRegex(SupersedeError, "不止一次"):
            resolve_supersedes([rec("a"), rec("a")])

    def test_long_chain_is_linear(self) -> None:
        records = [rec("n0")] + [rec(f"n{i}", supersedes=(f"n{i-1}",)) for i in range(1, 2000)]
        for order in (records, list(reversed(records))):
            res = resolve_supersedes(order)
            self.assertEqual(res.current, ("n1999",))
            self.assertEqual(res.heads["n0"], "n1999")


class StaleReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("x", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_candidates_need_separator_and_known_extension(self) -> None:
        text = "改了 `src/app.py:12` 和 src\\old.py，见 https://x.io/a.py；Node.js 和 build.mjs 不算"
        self.assertEqual(path_candidates(text), ["src/app.py", "src/old.py"])

    def test_missing_paths_reported(self) -> None:
        text = "入口在 src/app.py，配置在 config/settings.toml，目录 docs/"
        self.assertEqual(find_stale_references(text, self.root), ["config/settings.toml", "docs/"])

    def test_paths_outside_root_are_not_probed(self) -> None:
        text = "../secret/key.txt 和 /etc/hosts.txt 和 C:/Windows/win.ini"
        self.assertEqual(find_stale_references(text, self.root), [])


class SweepTests(unittest.TestCase):
    def test_groups_and_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "live.py").write_text("x", encoding="utf-8")
            policy = SweepPolicy(half_life=10 * DAY, repo_root=root)
            records = [
                rec("old-pref", "user", age_days=40),
                rec("new-pref", "user", supersedes=("old-pref",)),
                rec("status", "project", age_days=20),
                rec("moved", "reference", text="见 src/gone.py"),
                rec("alive", "reference", text="见 src/live.py"),
                rec("faded", "reference", age_days=50),
                rec("faded-pref", "feedback", age_days=50),
                rec("cooling", "reference", age_days=25),
                rec("typo", "projcet"),
                rec("orphan", "reference", supersedes=("deleted-long-ago",)),
            ]
            result = sweep(records, T0, policy)

        def ids(group):  # noqa: ANN001
            return {v.record.id for v in group}

        self.assertEqual(ids(result.keep), {"new-pref", "alive", "typo", "orphan"})
        self.assertEqual(ids(result.demote), {"old-pref", "moved", "faded-pref", "cooling"})
        self.assertEqual(ids(result.delete), {"status", "faded"})
        reasons = {v.record.id: v.reason for v in result.demote + result.delete}
        self.assertIn("new-pref", reasons["old-pref"])
        self.assertIn("TTL", reasons["status"])
        self.assertIn("src/gone.py", reasons["moved"])
        self.assertEqual(len(result.issues), 2)

    def test_expired_can_demote_instead(self) -> None:
        result = sweep([rec("s", "project", age_days=20)], T0, SweepPolicy(expired_action="demote"))
        self.assertEqual([v.record.id for v in result.demote], ["s"])

    def test_cycle_aborts_sweep(self) -> None:
        with self.assertRaises(SupersedeError):
            sweep([rec("a", supersedes=("b",)), rec("b", supersedes=("a",))], T0, SweepPolicy())

    def test_policy_validates_thresholds(self) -> None:
        with self.assertRaises(ValueError):
            SweepPolicy(demote_below=0.1, delete_below=0.2)

    def test_scores_match_formula(self) -> None:
        r = rec("a", "reference", idle_days=3)
        expected = math.exp(-math.log(2) / (30 * DAY) * 3 * DAY)
        self.assertAlmostEqual(decayed_score(r, T0, 30 * DAY), expected)


if __name__ == "__main__":
    unittest.main()
