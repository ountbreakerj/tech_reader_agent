"""prompt_cache 的单元测试：前缀稳定性这件事必须能被离线证明。

运行：``py -m unittest discover -s labs/tests -t labs -p "test_prompt_cache.py"``
"""

from __future__ import annotations

import unittest

from harness.prompt_cache import (
    Message,
    PrefixCache,
    approx_tokens,
    build_request,
    canonical_json,
    canonical_message,
    canonical_tools,
    common_byte_prefix,
    common_unit_count,
    demo_stabilization,
    diff_prefix,
    render_request,
    reusable_bytes,
)

TOOLS = [
    {"name": "read", "description": "读取文件", "parameters": {"path": {"type": "string"}}},
    {"name": "bash", "description": "执行命令", "parameters": {"cmd": {"type": "string"}}},
]
SYSTEM = "You are a coding agent."


def history() -> list[Message]:
    return [Message(role="user", text="修一下登录的 bug"), Message(role="assistant", text="好的")]


class TestCanonicalRendering(unittest.TestCase):
    def test_key_order_does_not_change_bytes(self) -> None:
        left = canonical_json({"a": 1, "b": {"c": 2, "d": [3, 4]}})
        right = canonical_json({"b": {"d": [3, 4], "c": 2}, "a": 1})
        self.assertEqual(left, right, "键顺序不同但内容相同，渲染必须逐字节一致")

    def test_tool_order_is_normalized(self) -> None:
        reversed_tools = list(reversed(TOOLS))
        self.assertEqual(canonical_tools(TOOLS), canonical_tools(reversed_tools))
        self.assertEqual(
            render_request(TOOLS, SYSTEM, history()).stream(),
            render_request(reversed_tools, SYSTEM, history()).stream(),
            "只改了注册顺序的两次请求必须渲染成同一串字节",
        )

    def test_message_canonical_ignores_dict_insertion_order(self) -> None:
        message = Message(role="user", text="hello")
        self.assertEqual(message.canonical, canonical_message(message))
        rebuilt = Message(text="hello", role="user")
        self.assertEqual(message.canonical, rebuilt.canonical)

    def test_identical_requests_render_identically(self) -> None:
        first = render_request(TOOLS, SYSTEM, history())
        second = render_request(TOOLS, SYSTEM, history())
        self.assertEqual(first.stream(), second.stream())
        self.assertEqual(first.hashes, second.hashes)
        self.assertEqual(diff_prefix(first, second).common_units, len(first.units))


class TestFirstDifferenceLocation(unittest.TestCase):
    def test_timestamp_in_system_breaks_at_system(self) -> None:
        first = build_request(
            system=SYSTEM, tools=TOOLS, messages=history(),
            runtime_context="Current time: 10:00", context_in_system=True,
        )
        second = build_request(
            system=SYSTEM, tools=TOOLS, messages=history(),
            runtime_context="Current time: 10:01", context_in_system=True,
        )
        diff = diff_prefix(first, second)
        self.assertIsNotNone(diff.first_change)
        assert diff.first_change is not None
        self.assertEqual(diff.first_change.kind, "system")
        # 工具声明排在系统提示词之前，所以只有第一个单位还能复用。
        self.assertEqual(diff.common_units, 1)
        self.assertIn("10:00", diff.first_change.before)

    def test_timestamp_appended_as_message_reuses_whole_prefix(self) -> None:
        snapshots: list[Message] = []
        previous = build_request(system=SYSTEM, tools=TOOLS, messages=history())
        for index, stamp in enumerate(["10:00", "10:01", "10:02"]):
            snapshots.append(Message(role="user", text=f"Current time: {stamp}", source="runtime-context"))
            current = build_request(system=SYSTEM, tools=TOOLS, messages=[*history(), *snapshots])
            diff = diff_prefix(previous, current)
            self.assertTrue(diff.reuse_all, f"第 {index + 1} 次追加应当完整复用前缀")
            self.assertEqual(diff.reusable_bytes, diff.prev_bytes)
            self.assertIsNotNone(diff.first_change)
            assert diff.first_change is not None
            self.assertEqual(diff.first_change.field, "新增单位")
            previous = current

    def test_appending_one_message_does_not_invalidate_earlier_messages(self) -> None:
        base = build_request(system=SYSTEM, tools=TOOLS, messages=history())
        grown = build_request(
            system=SYSTEM, tools=TOOLS,
            messages=[*history(), Message(role="assistant", text="我看看")],
        )
        diff = diff_prefix(base, grown)
        self.assertEqual(diff.common_units, len(base.units))
        self.assertGreater(diff.reusable_bytes, 0)

    def test_rewriting_a_middle_message_breaks_from_that_message(self) -> None:
        base = build_request(system=SYSTEM, tools=TOOLS, messages=history())
        rewritten = build_request(
            system=SYSTEM, tools=TOOLS,
            messages=[history()[0], Message(role="assistant", text="收到")],
        )
        diff = diff_prefix(base, rewritten)
        assert diff.first_change is not None
        self.assertEqual(diff.first_change.kind, "message")
        self.assertEqual(diff.first_change.message_index, 1)
        # tools + system + 第 0 条消息
        self.assertEqual(diff.common_units, 3)
        self.assertFalse(diff.reuse_all)


class TestToolSetChanges(unittest.TestCase):
    def test_tool_reorder_does_not_invalidate(self) -> None:
        first = render_request(TOOLS, SYSTEM, history())
        second = render_request(list(reversed(TOOLS)), SYSTEM, history())
        diff = diff_prefix(first, second)
        self.assertIsNone(diff.first_change, "排序后重排工具不应产生差异")

    def test_tool_addition_breaks_at_tools(self) -> None:
        grown = [*TOOLS, {"name": "edit", "description": "改文件", "parameters": {}}]
        diff = diff_prefix(render_request(TOOLS, SYSTEM, history()), render_request(grown, SYSTEM, history()))
        assert diff.first_change is not None
        self.assertEqual(diff.first_change.kind, "tools")
        self.assertEqual(diff.common_units, 0, "工具是第一个单位，它变了就整条未命中")
        self.assertEqual(diff.first_change.field, "工具集合")
        self.assertIn("+edit", diff.first_change.after)

    def test_tool_removal_is_reported(self) -> None:
        smaller = TOOLS[:1]
        diff = diff_prefix(render_request(TOOLS, SYSTEM, history()), render_request(smaller, SYSTEM, history()))
        assert diff.first_change is not None
        self.assertEqual(diff.first_change.field, "工具集合")
        self.assertIn("-bash", diff.first_change.before)

    def test_tool_description_change_is_reported(self) -> None:
        edited = [dict(TOOLS[0], description="换个说法"), TOOLS[1]]
        diff = diff_prefix(render_request(TOOLS, SYSTEM, history()), render_request(edited, SYSTEM, history()))
        assert diff.first_change is not None
        self.assertEqual(diff.first_change.field, "工具定义")

    def test_unregistered_tool_is_not_silently_sorted_away(self) -> None:
        empty = render_request([], SYSTEM, history())
        diff = diff_prefix(empty, render_request(TOOLS, SYSTEM, history()))
        assert diff.first_change is not None
        self.assertEqual(diff.first_change.field, "工具集合")
        self.assertIn("read", diff.first_change.after)


class TestPrefixCache(unittest.TestCase):
    def test_first_call_never_hits(self) -> None:
        cache = PrefixCache()
        request = render_request(TOOLS, SYSTEM, history())
        self.assertEqual(cache.call(request), 0)
        self.assertEqual(cache.misses, 1)

    def test_second_identical_call_hits_every_unit(self) -> None:
        cache = PrefixCache()
        request = render_request(TOOLS, SYSTEM, history())
        cache.call(request)
        self.assertEqual(cache.call(request), len(request.units))

    def test_appended_message_still_hits_the_old_prefix(self) -> None:
        cache = PrefixCache()
        base = render_request(TOOLS, SYSTEM, history())
        cache.call(base)
        grown = render_request(
            TOOLS, SYSTEM, [*history(), Message(role="user", text="Current time: 10:01", source="runtime-context")]
        )
        self.assertEqual(cache.call(grown), len(base.units))

    def test_changed_system_stops_hitting_after_tools(self) -> None:
        cache = PrefixCache()
        cache.call(render_request(TOOLS, SYSTEM, history()))
        changed = render_request(TOOLS, SYSTEM + " Be terse.", history())
        self.assertEqual(cache.call(changed), 1)

    def test_changed_tool_set_hits_nothing(self) -> None:
        cache = PrefixCache()
        cache.call(render_request(TOOLS, SYSTEM, history()))
        grown = [*TOOLS, {"name": "edit", "description": "改文件", "parameters": {}}]
        self.assertEqual(cache.call(render_request(grown, SYSTEM, history())), 0)
        self.assertEqual(cache.hits, 0)

    def test_known_prefix_hashes_grow_monotonically(self) -> None:
        cache = PrefixCache()
        self.assertEqual(cache.known_prefix_hashes(), frozenset())
        request = render_request(TOOLS, SYSTEM, history())
        cache.call(request)
        self.assertEqual(cache.known_prefix_hashes(), frozenset(request.hashes))
        grown = render_request(
            TOOLS, SYSTEM, [*history(), Message(role="user", text="多一条")]
        )
        cache.call(grown)
        self.assertTrue(set(request.hashes) <= cache.known_prefix_hashes())


class TestDemoAndHelpers(unittest.TestCase):
    def test_good_layout_beats_bad_layout(self) -> None:
        result = demo_stabilization(
            ["Current time: 10:00", "Current time: 10:01", "Current time: 10:02", "Current time: 10:03"],
            tools=TOOLS,
            system=SYSTEM,
            history=history(),
        )
        self.assertGreater(
            result["good"]["hit_ratio"],
            result["bad"]["hit_ratio"],
            "把易变内容移出系统提示词，命中率必须更高",
        )
        self.assertEqual(result["bad"]["first_change_kinds"], ["system.文本", "system.文本", "system.文本"])
        self.assertEqual(
            result["good"]["first_change_kinds"],
            ["尾部追加（复用率 100%）"] * 3,
        )
        self.assertEqual(result["good"]["reused_whole_prefix"], [True, True, True])
        self.assertEqual(result["bad"]["reused_whole_prefix"], [False, False, False])
        self.assertIn("教学示意", result["note"])

    def test_bad_layout_never_reuses_more_than_the_tools_unit(self) -> None:
        result = demo_stabilization(["t=0", "t=1", "t=2"], tools=TOOLS, system=SYSTEM, history=history())
        self.assertLess(result["bad"]["hit_ratio"], 0.32)

    def test_common_byte_prefix(self) -> None:
        self.assertEqual(common_byte_prefix(b"abc", b"abd"), 2)
        self.assertEqual(common_byte_prefix(b"abc", b"abc"), 3)
        self.assertEqual(common_byte_prefix(b"", b"abc"), 0)

    def test_common_unit_count_is_prefix_not_set_overlap(self) -> None:
        first = render_request(TOOLS, SYSTEM, history())
        second = render_request(TOOLS, SYSTEM + "!", history())
        self.assertEqual(common_unit_count(first, second), 1)
        reused = reusable_bytes(first, second)
        self.assertGreaterEqual(reused, first.framed_prefix(1), "整段的复用必须算进去")
        self.assertLess(reused, first.framed_prefix(2), "系统提示词已经变了，不能算作命中")

    def test_approx_tokens_is_labelled_teaching(self) -> None:
        self.assertEqual(approx_tokens(400), 100)
        self.assertEqual(approx_tokens(0), 0)

    def test_empty_system_prompt_still_occupies_a_unit(self) -> None:
        request = render_request(TOOLS, "", history())
        self.assertEqual(request.units[1].kind, "system")
        self.assertEqual(request.units[1].payload, b"")
        self.assertEqual(len(request.units), 2 + len(history()))


if __name__ == "__main__":
    unittest.main()
