"""spill 的单元测试：阈值、预览内容、去重、路径穿越、分页边界、不可卸载的情况。

全部用 tempfile.TemporaryDirectory，测试不往仓库里写任何文件。
运行：py -m unittest discover -s labs/tests -t labs -p "test_spill.py"
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.spill import (
    DEFAULT_LIMIT_CHARS,
    SpillPathError,
    SpillStore,
    build_preview,
    is_complete_json,
    looks_binary,
    safe_name,
    should_spill,
)

SESSION = "session-abc"


def make_store(root: Path) -> SpillStore:
    return SpillStore(root, SESSION)


class ThresholdTest(unittest.TestCase):
    """阈值两侧的行为。"""

    def test_under_threshold_returns_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            content = "x" * (DEFAULT_LIMIT_CHARS - 1)
            result = store.spill(tool_name="bash", call_id="c1", content=content)
            self.assertFalse(result.spilled)
            self.assertIsNone(result.path)
            self.assertEqual(result.content, content)
            # 未卸载时不能往磁盘上写任何东西，否则每次小结果都留一个垃圾文件。
            self.assertFalse(store.session_dir.exists())

    def test_exactly_at_threshold_stays_inline(self) -> None:
        """边界是 <=：正好等于阈值不卸载，这样阈值是「允许的最大长度」。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            content = "y" * DEFAULT_LIMIT_CHARS
            result = store.spill(tool_name="bash", call_id="c1", content=content)
            self.assertFalse(result.spilled)

    def test_over_threshold_returns_preview_with_path_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            content = "line\n" * 10_000
            result = store.spill(tool_name="bash", call_id="c1", content=content)

            self.assertTrue(result.spilled)
            self.assertIsNotNone(result.path)
            assert result.path is not None
            # 文件里是完整原文，逐字一致。
            self.assertEqual(result.path.read_text(encoding="utf-8"), content)
            # 预览里三样东西一个都不能少：路径、原始大小、省略量。
            self.assertIn(str(result.path), result.content)
            self.assertIn(f"原始大小：{len(content)} 字符", result.content)
            self.assertEqual(result.original_chars, len(content))
            self.assertEqual(result.omitted_chars, len(content) - 2000 - 2000)
            self.assertIn("已省略", result.content)
            # 替换后的文本必须真的比原文小，否则卸载没有意义。
            self.assertLess(len(result.content), len(content))

    def test_preview_is_marked_as_truncated(self) -> None:
        """模型必须能一眼看出看到的是预览而不是全文。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            result = store.spill(tool_name="bash", call_id="c1", content="z" * 50_000)
            self.assertIn("中间内容已省略", result.content)
            self.assertIn("只是预览，不是完整内容", result.content)
            self.assertIn("read_spill", result.content)


class PreviewTest(unittest.TestCase):
    """预览切分的纯函数边界。"""

    def test_head_and_tail_are_both_kept(self) -> None:
        preview, omitted = build_preview("A" * 100 + "B" * 100, head_chars=10, tail_chars=10)
        self.assertTrue(preview.startswith("A" * 10))
        self.assertTrue(preview.endswith("B" * 10))
        self.assertEqual(omitted, 180)

    def test_short_content_is_not_marked(self) -> None:
        """head+tail 之和大于内容时退化成全文，不能凭空多出省略标记。"""
        preview, omitted = build_preview("short", head_chars=100, tail_chars=100)
        self.assertEqual(preview, "short")
        self.assertEqual(omitted, 0)

    def test_zero_tail_keeps_head_only(self) -> None:
        preview, omitted = build_preview("A" * 100, head_chars=10, tail_chars=0)
        self.assertTrue(preview.startswith("A" * 10))
        self.assertEqual(omitted, 90)


class DedupTest(unittest.TestCase):
    """同一份内容只落盘一次。"""

    def test_same_content_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            content = "q" * 30_000
            first = store.spill(tool_name="bash", call_id="c1", content=content)
            second = store.spill(tool_name="grep", call_id="c2", content=content)
            assert first.path is not None and second.path is not None
            # 文件名以内容哈希开头，所以两次卸载指向同一个文件；后面那段工具名
            # 只是给人看的标签（DSH 的 saveText 收 suggestedName 也是这个定位）。
            self.assertEqual(
                first.path.name.split("-", 1)[0], second.path.name.split("-", 1)[0]
            )
            self.assertFalse(first.deduplicated)
            self.assertTrue(second.deduplicated)
            self.assertEqual(len(list(store.session_dir.glob("*.txt"))), 1)
            self.assertFalse(list(store.session_dir.glob("*.tmp")))

    def test_different_content_writes_two_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            a = store.spill(tool_name="bash", call_id="c1", content="a" * 30_000)
            b = store.spill(tool_name="bash", call_id="c2", content="b" * 30_000)
            self.assertNotEqual(a.path, b.path)
            self.assertEqual(len(list(store.session_dir.glob("*.txt"))), 2)

    def test_dedup_survives_a_new_store_instance(self) -> None:
        """去重靠内容哈希，不靠内存里的集合：进程重启后同一份内容还是同一个文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            content = "r" * 30_000
            first = make_store(Path(tmp)).spill(tool_name="bash", call_id="c1", content=content)
            second = make_store(Path(tmp)).spill(tool_name="bash", call_id="c2", content=content)
            self.assertEqual(first.path, second.path)


class NamingTest(unittest.TestCase):
    """文件名与路径穿越。"""

    def test_safe_name_strips_traversal(self) -> None:
        self.assertEqual(safe_name("../../etc/passwd"), "etc_passwd")
        self.assertNotIn("/", safe_name("a/b"))
        self.assertNotIn("\\", safe_name("a\\b"))
        self.assertEqual(safe_name(""), "tool")
        self.assertEqual(safe_name(".."), "tool")

    def test_spill_stays_inside_session_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            result = store.spill(
                tool_name="../../evil", call_id="c/../../x", content="w" * 30_000
            )
            assert result.path is not None
            self.assertTrue(result.path.resolve().is_relative_to(store.session_dir.resolve()))
            # 会话目录名是会话 id 的哈希，所以会话 id 本身不能出现在路径里。
            self.assertNotIn(SESSION, str(result.path))
            self.assertTrue(result.path.is_file())

    def test_session_id_traversal_is_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SpillStore(Path(tmp), "../../other-session")
            self.assertTrue(store.session_dir.resolve().is_relative_to(Path(tmp).resolve()))
            self.assertEqual(len(store.session_dir.name), len("session-") + 12)


class ReadPageTest(unittest.TestCase):
    """分页取回的边界。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = make_store(Path(self._tmp.name))
        self.lines = [f"line-{index}" for index in range(1, 11)]
        self.result = self.store.spill(
            tool_name="bash", call_id="c1", content="\n".join(self.lines) * 500
        )
        assert self.result.path is not None
        self.path = self.result.path

    def test_first_page_reports_position_and_next_page(self) -> None:
        page = self.store.read_page(self.path, page=0, page_lines=3)
        self.assertIn("第 1-3 行", page)
        self.assertIn("line-1", page)
        self.assertIn("page=1", page)

    def test_last_page_says_it_is_last(self) -> None:
        total = len(self.path.read_text(encoding="utf-8").splitlines())
        page = self.store.read_page(self.path, page=total // 3, page_lines=3)
        self.assertIn("这是最后一页", page)

    def test_page_beyond_end_is_reported_not_silent(self) -> None:
        """越界必须显式回答：返回空字符串会让模型以为文件是空的。"""
        page = self.store.read_page(self.path, page=10_000, page_lines=3)
        self.assertIn("[空页]", page)
        self.assertIn("超出范围", page)

    def test_negative_page_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.read_page(self.path, page=-1)

    def test_missing_file_reports_cleanup(self) -> None:
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.read_page(self.path)

    def test_outside_path_is_blocked(self) -> None:
        """穿越被挡住：读会话目录之外的任何路径都要失败，而不是靠拼字符串判断。"""
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with self.assertRaises(SpillPathError):
            self.store.read_page(outside)

    def test_sibling_session_dir_is_blocked(self) -> None:
        """同一 root 下别的会话目录也不能读——那是别的会话的工具输出。"""
        other = SpillStore(Path(self._tmp.name), "session-other")
        blocked = other.spill(tool_name="bash", call_id="c9", content="s" * 30_000)
        assert blocked.path is not None
        self.assertNotEqual(other.session_dir, self.store.session_dir)
        with self.assertRaises(SpillPathError):
            self.store.read_page(blocked.path)


class NotSpillableTest(unittest.TestCase):
    """不该卸载的三种情况。"""

    def test_error_results_stay_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            content = "Error: boom\n" * 5_000
            result = store.spill(
                tool_name="bash", call_id="c1", content=content, is_error=True
            )
            self.assertFalse(result.spilled)
            self.assertEqual(result.content, content)
            self.assertFalse(store.session_dir.exists())

    def test_binary_content_stays_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            content = "\x00\x01\x02" * 9_000
            result = store.spill(tool_name="bash", call_id="c1", content=content)
            self.assertFalse(result.spilled)

    def test_complete_json_stays_inline(self) -> None:
        """截断的 JSON 比超长的 JSON 更糟：模型会当成完整文档去解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            content = '{"items": ["' + 'a' * 30_000 + '"]}'
            result = store.spill(tool_name="mcp__search", call_id="c1", content=content)
            self.assertFalse(result.spilled)

    def test_json_looking_log_can_still_spill(self) -> None:
        """以 { 开头但结尾不是 } 的内容是日志，可以卸载。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            result = store.spill(tool_name="bash", call_id="c1", content="{start\n" * 5_000)
            self.assertTrue(result.spilled)


class PredicateTest(unittest.TestCase):
    """只读的判定函数。"""

    def test_looks_binary(self) -> None:
        self.assertTrue(looks_binary("a\x00b"))
        self.assertFalse(looks_binary("a\nb"))

    def test_is_complete_json(self) -> None:
        self.assertTrue(is_complete_json('{"a": 1}'))
        self.assertTrue(is_complete_json("[]"))
        self.assertFalse(is_complete_json('{"a": 1'))
        self.assertFalse(is_complete_json("plain text"))
        self.assertFalse(is_complete_json(""))

    def test_should_spill_table(self) -> None:
        self.assertTrue(should_spill(is_error=False, content="ok"))
        self.assertFalse(should_spill(is_error=True, content="ok"))
        self.assertFalse(should_spill(is_error=False, content="a\x00b"))


if __name__ == "__main__":
    unittest.main()
