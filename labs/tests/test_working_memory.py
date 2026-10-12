"""working_memory.py 的测试：待办校验、渲染稳定、diff、replay、陈旧提醒、计划闸门。"""

from __future__ import annotations

import unittest

from harness.working_memory import (
    MAX_TODOS,
    REMINDER_TEXT,
    PlanGate,
    TodoError,
    TodoItem,
    TodoList,
    diff_todos,
    render_todos,
    should_remind,
    todo_context,
    validate_todos,
)


def todo(i, content, status="pending"):
    return {"id": i, "content": content, "status": status}


class ValidateTest(unittest.TestCase):
    def test_valid_list_is_normalized(self):
        items = validate_todos([todo("1", "  读源码  ", "in_progress"), todo("2", "写测试")])
        self.assertEqual(items[0], TodoItem("1", "读源码", "in_progress"))
        self.assertEqual(len(items), 2)

    def test_rejects_non_list(self):
        with self.assertRaises(TodoError):
            validate_todos({"id": "1"})

    def test_rejects_duplicate_id(self):
        with self.assertRaisesRegex(TodoError, "重复"):
            validate_todos([todo("1", "a"), todo("1", "b")])

    def test_rejects_unknown_status(self):
        with self.assertRaisesRegex(TodoError, "不合法"):
            validate_todos([todo("1", "a", "done")])

    def test_rejects_missing_content(self):
        with self.assertRaisesRegex(TodoError, "缺少"):
            validate_todos([todo("1", "   ")])

    def test_rejects_two_in_progress_by_default(self):
        with self.assertRaisesRegex(TodoError, "只能有一项"):
            validate_todos([todo("1", "a", "in_progress"), todo("2", "b", "in_progress")])

    def test_parallel_mode_allows_several_in_progress(self):
        items = validate_todos(
            [todo("1", "a", "in_progress"), todo("2", "b", "in_progress")],
            allow_parallel=True,
        )
        self.assertEqual(len(items), 2)

    def test_rejects_too_many(self):
        with self.assertRaisesRegex(TodoError, "最多"):
            validate_todos([todo(str(n), "x") for n in range(MAX_TODOS + 1)])


class RenderTest(unittest.TestCase):
    def test_render_shape(self):
        items = validate_todos([todo("1", "读源码", "completed"), todo("2", "写测试", "in_progress"), todo("3", "构建")])
        self.assertEqual(
            render_todos(items),
            "[x] 1. 读源码\n[>] 2. 写测试\n[ ] 3. 构建\n(1/3 completed)",
        )

    def test_render_is_byte_stable(self):
        raw = [todo("1", "a", "in_progress"), todo("2", "b")]
        self.assertEqual(render_todos(validate_todos(raw)), render_todos(validate_todos(list(raw))))

    def test_render_empty(self):
        self.assertEqual(render_todos([]), "(no todos)")


class DiffTest(unittest.TestCase):
    def test_added_removed_changed(self):
        old = validate_todos([todo("1", "a", "in_progress"), todo("2", "b")])
        new = validate_todos([todo("1", "a", "completed"), todo("3", "c", "in_progress")])
        change = diff_todos(old, new)
        self.assertEqual(change.added, ("3",))
        self.assertEqual(change.removed, ("2",))
        self.assertEqual(change.changed, (("1", "in_progress", "completed"),))
        self.assertEqual(change.describe(), "+3; -2; 1: in_progress -> completed")

    def test_rewording_is_not_add_remove(self):
        old = validate_todos([todo("1", "读源码")])
        new = validate_todos([todo("1", "读 dsh 源码")])
        self.assertTrue(diff_todos(old, new).is_empty())


class TodoListTest(unittest.TestCase):
    def test_write_returns_diff_and_full_list(self):
        todos = TodoList()
        todos.write([todo("1", "a", "in_progress")], round_no=1)
        result = todos.write([todo("1", "a", "completed")], round_no=2)
        self.assertTrue(result.startswith("Updated todos (1: in_progress -> completed)\n"))
        self.assertIn("[x] 1. a", result)
        self.assertEqual(todos.last_write_round, 2)

    def test_failed_write_changes_nothing(self):
        todos = TodoList()
        todos.write([todo("1", "a", "in_progress")], round_no=1)
        with self.assertRaises(TodoError):
            todos.write([todo("1", "a"), todo("1", "b")], round_no=5)
        self.assertEqual(todos.items, [TodoItem("1", "a", "in_progress")])
        self.assertEqual(todos.last_write_round, 1)

    def test_replay_is_last_write_wins(self):
        events = [
            {"type": "user", "text": "hi"},
            {"type": "todo/write", "round": 1, "todos": [todo("1", "a", "in_progress")]},
            {"type": "compaction/summary", "text": "..."},
            {"type": "todo/write", "round": 4, "todos": [todo("1", "a", "completed"), todo("2", "b")]},
        ]
        todos = TodoList.replay(events)
        self.assertEqual(todos.snapshot(), [todo("1", "a", "completed"), todo("2", "b")])
        self.assertEqual(todos.last_write_round, 4)

    def test_context_only_when_open_items(self):
        todos = TodoList()
        todos.write([todo("1", "a", "in_progress")], round_no=1)
        self.assertEqual(todo_context(todos), "<current_todos>\n[>] 1. a\n(0/1 completed)\n</current_todos>")
        todos.write([todo("1", "a", "completed")], round_no=2)
        self.assertEqual(todo_context(todos), "")


class RemindTest(unittest.TestCase):
    def test_never_written_never_reminds(self):
        self.assertFalse(should_remind(None, 50, True))

    def test_no_open_items_no_reminder(self):
        self.assertFalse(should_remind(1, 50, False))

    def test_threshold(self):
        self.assertFalse(should_remind(1, 3, True))
        self.assertTrue(should_remind(1, 4, True))

    def test_reminder_resets_the_clock(self):
        self.assertFalse(should_remind(1, 5, True, last_reminded_round=4))
        self.assertTrue(should_remind(1, 7, True, last_reminded_round=4))

    def test_reminder_text_names_the_tool(self):
        self.assertIn("todo_write", REMINDER_TEXT)


class PlanGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = PlanGate(read_only_tools=frozenset({"read_file", "grep"}))

    def test_inactive_gate_allows_everything(self):
        self.assertTrue(self.gate.check("write_file").allowed)

    def test_plan_mode_allows_read_only_and_exit(self):
        self.gate.enter()
        self.assertTrue(self.gate.check("grep").allowed)
        self.assertTrue(self.gate.check("exit_plan_mode").allowed)

    def test_plan_mode_denies_write_with_reason(self):
        self.gate.enter()
        decision = self.gate.check("write_file")
        self.assertFalse(decision.allowed)
        self.assertIn("exit_plan_mode", decision.reason)

    def test_exit_requires_user_approval(self):
        self.gate.enter()
        self.assertFalse(self.gate.exit("# 计划\n1. 改 a", approved_by_user=False).allowed)
        self.assertTrue(self.gate.active)
        self.assertTrue(self.gate.exit("# 计划\n1. 改 a", approved_by_user=True).allowed)
        self.assertFalse(self.gate.active)
        self.assertTrue(self.gate.check("write_file").allowed)

    def test_exit_requires_heading(self):
        self.gate.enter()
        decision = self.gate.exit("先改 a 再改 b", approved_by_user=True)
        self.assertFalse(decision.allowed)
        self.assertTrue(self.gate.active)

    def test_exit_outside_plan_mode(self):
        self.assertFalse(self.gate.exit("# 计划", approved_by_user=True).allowed)


if __name__ == "__main__":
    unittest.main()
