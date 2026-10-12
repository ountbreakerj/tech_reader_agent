"""harness.tools 的行为测试：重名注册、schema 导出、参数校验、钩子、未知工具、分组。"""
from __future__ import annotations

import unittest

from harness.tools import (
    HookDecision,
    ToolError,
    ToolRegistry,
    ToolSpec,
    check_schema,
    default_registry,
    execution_mode,
    group_calls,
    validate_value,
)


def echo_tool(**overrides: object) -> ToolSpec:
    fields: dict[str, object] = {
        "name": "echo",
        "description": "回显一段文本",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "handler": lambda args: args["text"],
    }
    fields.update(overrides)
    return ToolSpec(**fields)  # type: ignore[arg-type]


class RegistrationTest(unittest.TestCase):
    def test_duplicate_name_raises_with_reason(self):
        registry = ToolRegistry()
        registry.register(echo_tool())
        with self.assertRaises(ToolError) as ctx:
            registry.register(echo_tool())
        message = str(ctx.exception)
        self.assertIn("已经注册过了", message)
        self.assertIn("不能静默覆盖", message)

    def test_duplicate_does_not_replace_the_first_handler(self):
        registry = ToolRegistry()
        registry.register(echo_tool())
        with self.assertRaises(ToolError):
            registry.register(echo_tool(handler=lambda args: "第二个"))
        self.assertEqual(registry.call("echo", {"text": "hi"}), "hi")

    def test_unregister_then_register_again(self):
        registry = ToolRegistry()
        registry.register(echo_tool())
        registry.unregister("echo")
        registry.register(echo_tool(handler=lambda args: "新的"))
        self.assertEqual(registry.call("echo", {"text": "hi"}), "新的")

    def test_bad_schema_is_rejected_at_registration(self):
        bad = echo_tool(input_schema={"type": "object", "properties": {"text": {"type": "string", "minLength": 1}}})
        with self.assertRaises(ToolError) as ctx:
            ToolRegistry().register(bad)
        self.assertIn("不支持的 keyword", str(ctx.exception))

    def test_required_must_be_declared(self):
        problems = check_schema({"type": "object", "properties": {}, "required": ["missing"]})
        self.assertTrue(any("没有出现在 properties" in p for p in problems))

    def test_non_callable_handler_rejected(self):
        with self.assertRaises(ToolError):
            ToolRegistry().register(echo_tool(handler="not callable"))


class SchemaExportTest(unittest.TestCase):
    def test_schemas_keep_registration_order(self):
        registry = default_registry()
        self.assertEqual([s["name"] for s in registry.schemas()], ["read_file", "write_file"])

    def test_schema_has_exactly_three_keys(self):
        for schema in default_registry().schemas():
            self.assertEqual(set(schema), {"name", "description", "input_schema"})

    def test_execution_only_fields_never_leak(self):
        registry = ToolRegistry()
        registry.register(echo_tool(read_only=True, parallel_safe=True, timeout=1.5))
        schema = registry.schemas()[0]
        for leaked in ("read_only", "parallel_safe", "timeout", "handler"):
            self.assertNotIn(leaked, schema)

    def test_schema_is_the_same_object_the_validator_uses(self):
        # 模型看到的 schema 和 harness 校验用的是同一份，不能各写一份。
        registry = ToolRegistry()
        tool = echo_tool()
        registry.register(tool)
        self.assertEqual(registry.schemas()[0]["input_schema"], tool.input_schema)


class ValidationTest(unittest.TestCase):
    def test_missing_required_field(self):
        problems = validate_value(echo_tool().input_schema, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("缺少必填字段", problems[0])

    def test_wrong_type(self):
        problems = validate_value(echo_tool().input_schema, {"text": 42})
        self.assertTrue(any("期望 string" in p for p in problems))

    def test_extra_property_rejected(self):
        problems = validate_value(echo_tool().input_schema, {"text": "a", "extra": 1})
        self.assertTrue(any("不是已声明的字段" in p for p in problems))

    def test_all_problems_reported_at_once(self):
        problems = validate_value(echo_tool().input_schema, {"extra": 1})
        self.assertEqual(len(problems), 2)

    def test_integer_rejects_bool(self):
        schema = {"type": "integer"}
        self.assertTrue(validate_value(schema, True))
        self.assertTrue(validate_value(schema, 1.5))
        self.assertEqual(validate_value(schema, 3), [])
        self.assertEqual(validate_value(schema, 3.0), [])

    def test_number_rejects_bool(self):
        self.assertTrue(validate_value({"type": "number"}, True))
        self.assertEqual(validate_value({"type": "number"}, 2), [])

    def test_enum(self):
        schema = {"type": "string", "enum": ["a", "b"]}
        self.assertTrue(validate_value(schema, "c"))
        self.assertEqual(validate_value(schema, "a"), [])

    def test_nested_object_and_array(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}}}},
            },
            "required": ["items"],
        }
        problems = validate_value(schema, {"items": [{"n": 1}, {"n": "x"}]})
        self.assertEqual(len(problems), 1)
        self.assertIn("arguments.items[1].n", problems[0])

    def test_missing_type_only_checks_structure(self):
        self.assertEqual(validate_value({}, {"anything": 1}), [])

    def test_path_is_qualified(self):
        problems = validate_value({"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}}}, {"a": {"b": 1}})
        self.assertEqual(problems, ["arguments.a.b: 期望 string，收到 int"])


class CallTest(unittest.TestCase):
    def test_unknown_tool_returns_error_not_exception(self):
        registry = default_registry()
        out = registry.call("nope", {})
        self.assertTrue(out.startswith("Error: 未知工具"))
        self.assertIn("read_file", out)

    def test_unknown_tool_lists_no_tools(self):
        self.assertIn("（一个都没有）", ToolRegistry().call("nope", {}))

    def test_non_dict_args(self):
        self.assertIn("必须是对象", default_registry().call("read_file", ["path"]))

    def test_invalid_args_are_reported_back(self):
        out = default_registry().call("read_file", {})
        self.assertIn("参数不合法", out)
        self.assertIn("path", out)

    def test_handler_exception_becomes_error_text(self):
        registry = ToolRegistry()
        registry.register(echo_tool(handler=lambda args: 1 / 0))
        out = registry.call("echo", {"text": "hi"})
        self.assertIn("ZeroDivisionError", out)
        self.assertIn("执行失败", out)

    def test_successful_call_returns_str(self):
        self.assertEqual(default_registry().call("read_file", {"path": "nope.txt"})[:6], "Error:")

    def test_echo_roundtrip(self):
        registry = ToolRegistry()
        registry.register(echo_tool())
        self.assertEqual(registry.call("echo", {"text": "你好"}), "你好")


class HookTest(unittest.TestCase):
    def _run(self, registry: ToolRegistry) -> str:
        registry.register(echo_tool())
        return registry.call("echo", {"text": "hi"})

    def test_hook_denies(self):
        registry = ToolRegistry()
        registry.add_hook(lambda name, args: HookDecision.deny("策略禁止回显"))
        out = self._run(registry)
        self.assertIn("被拒绝", out)
        self.assertIn("策略禁止回显", out)

    def test_hooks_are_ordered_and_first_denial_wins(self):
        seen: list[str] = []

        def first(name, args):
            seen.append("first")
            return HookDecision.allow()

        def second(name, args):
            seen.append("second")
            return HookDecision.deny("第二道拦下")

        def third(name, args):
            seen.append("third")
            return HookDecision.allow()

        registry = ToolRegistry()
        for hook in (first, second, third):
            registry.add_hook(hook)
        out = self._run(registry)
        self.assertEqual(seen, ["first", "second"])
        self.assertIn("第二道拦下", out)

    def test_hook_allow_lets_the_call_through(self):
        registry = ToolRegistry()
        registry.add_hook(lambda name, args: HookDecision.allow())
        self.assertEqual(self._run(registry), "hi")

    def test_deny_requires_a_reason(self):
        with self.assertRaises(ToolError):
            HookDecision.deny("")

    def test_hook_runs_after_validation(self):
        # 参数先校验：钩子不该看到一份畸形参数，否则钩子里的路径判断会踩空。
        called: list[str] = []

        def hook(name, args):
            called.append(name)
            return HookDecision.deny("不该被调用")

        registry = ToolRegistry()
        registry.add_hook(hook)
        registry.register(echo_tool())
        out = registry.call("echo", {})
        self.assertEqual(called, [])
        self.assertIn("参数不合法", out)


class GroupingTest(unittest.TestCase):
    def test_read_only_is_parallel_safe_when_declared(self):
        registry = default_registry()
        self.assertEqual(execution_mode(registry.get("read_file")), "parallel")
        self.assertEqual(execution_mode(registry.get("write_file")), "exclusive")

    def test_read_only_without_parallel_flag_is_exclusive(self):
        registry = ToolRegistry()
        registry.register(echo_tool(read_only=True))
        self.assertEqual(execution_mode(registry.get("echo")), "exclusive")

    def test_unknown_tool_is_exclusive(self):
        self.assertEqual(execution_mode(None), "exclusive")

    def test_write_between_reads_forms_a_barrier(self):
        registry = default_registry()
        groups = group_calls(registry, [
            ("read_file", {"path": "a"}),
            ("read_file", {"path": "b"}),
            ("write_file", {"path": "c", "content": "x"}),
            ("read_file", {"path": "d"}),
        ])
        self.assertEqual([len(g) for g in groups], [2, 1, 1])

    def test_consecutive_reads_share_one_group(self):
        groups = group_calls(default_registry(), [("read_file", {}), ("read_file", {})])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)


if __name__ == "__main__":
    unittest.main()
