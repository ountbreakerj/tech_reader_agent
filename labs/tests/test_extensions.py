"""22 Skills 与 MCP 的测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.extensions import (
    MAX_DESCRIPTION,
    FakeMcpServer,
    McpClient,
    SkillError,
    discover_skills,
    load_skill_tool,
    mcp_tool_name,
    parse_skill,
    skill_index,
    to_tool_schemas,
)

GOOD = "---\nname: lint-rules\ndescription: 团队的代码规范检查流程与常见违规\n---\n\n# 规范\n1. 先跑 ruff\n"


def write_skill(root: Path, folder: str, text: str) -> None:
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / "SKILL.md").write_text(text, encoding="utf-8")


class ParseTests(unittest.TestCase):
    def test_parse_good(self) -> None:
        skill = parse_skill(GOOD, "x/SKILL.md")
        self.assertEqual(skill.name, "lint-rules")
        self.assertIn("ruff", skill.body)

    def test_missing_frontmatter(self) -> None:
        with self.assertRaises(SkillError):
            parse_skill("# no header", "x")

    def test_bad_name(self) -> None:
        with self.assertRaises(SkillError):
            parse_skill("---\nname: Lint Rules\ndescription: d\n---\nbody", "x")

    def test_missing_description(self) -> None:
        with self.assertRaises(SkillError):
            parse_skill("---\nname: a\n---\nbody", "x")

    def test_long_description_truncated(self) -> None:
        skill = parse_skill(f"---\nname: a\ndescription: {'长' * 1000}\n---\nbody", "x")
        self.assertEqual(len(skill.description), MAX_DESCRIPTION)


class DiscoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_project_overrides_user(self) -> None:
        write_skill(self.root / "user", "lint", GOOD.replace("团队的", "个人的"))
        write_skill(self.root / "project", "lint", GOOD)
        skills, notes = discover_skills([(str(self.root / "user"), "user"), (str(self.root / "project"), "project")])
        self.assertEqual(skills["lint-rules"].source, "project")
        self.assertTrue(any("覆盖" in n for n in notes))

    def test_same_source_conflict_loads_neither(self) -> None:
        write_skill(self.root / "project", "a", GOOD)
        write_skill(self.root / "project", "b", GOOD)
        skills, notes = discover_skills([(str(self.root / "project"), "project")])
        self.assertNotIn("lint-rules", skills)
        self.assertTrue(any("冲突" in n for n in notes))

    def test_broken_skill_is_skipped_not_fatal(self) -> None:
        write_skill(self.root / "project", "ok", GOOD)
        write_skill(self.root / "project", "bad", "no frontmatter")
        skills, notes = discover_skills([(str(self.root / "project"), "project")])
        self.assertIn("lint-rules", skills)
        self.assertTrue(any("跳过" in n for n in notes))

    def test_missing_root_is_fine(self) -> None:
        skills, notes = discover_skills([(str(self.root / "nope"), "user")])
        self.assertEqual(skills, {})


class IndexTests(unittest.TestCase):
    def test_index_sorted_and_one_line_each(self) -> None:
        skills = {n: parse_skill(f"---\nname: {n}\ndescription: d-{n}\n---\nbody", n) for n in ["zeta", "alpha"]}
        index = skill_index(skills)
        self.assertLess(index.index("alpha"), index.index("zeta"))
        self.assertNotIn("body", index)

    def test_index_budget(self) -> None:
        skills = {f"s{i:03d}": parse_skill(f"---\nname: s{i:03d}\ndescription: {'x' * 100}\n---\nb", "p")
                  for i in range(200)}
        index = skill_index(skills, budget=2000)
        self.assertLessEqual(len(index), 2200)
        self.assertIn("未列出", index)

    def test_load_skill_tool(self) -> None:
        skills = {"lint-rules": parse_skill(GOOD, "p")}
        tool = load_skill_tool(skills)
        self.assertIn("先跑 ruff", tool({"name": "lint-rules"}))
        self.assertIn("可用：lint-rules", tool({"name": "nope"}))


def make_server(name: str = "docs") -> FakeMcpServer:
    return FakeMcpServer(name, {
        "search": ("在内部文档里搜索", {"type": "object", "properties": {"q": {"type": "string"}}},
                   lambda a: f"找到 3 条关于 {a.get('q')} 的结果"),
        "boom": ("总是失败", {"type": "object"}, lambda a: 1 / 0),
    })


class McpTests(unittest.TestCase):
    def test_connect_namespaces_tools(self) -> None:
        client = McpClient(make_server())
        names = sorted(t.name for t in client.connect())
        self.assertEqual(names, ["mcp__docs__boom", "mcp__docs__search"])
        self.assertEqual(client.server.calls[:2], ["initialize", "tools/list"])

    def test_two_servers_same_tool_name_do_not_collide(self) -> None:
        a, b = McpClient(make_server("docs")), McpClient(make_server("wiki"))
        names = {t.name for t in a.connect()} | {t.name for t in b.connect()}
        self.assertIn("mcp__docs__search", names)
        self.assertIn("mcp__wiki__search", names)

    def test_call_roundtrip(self) -> None:
        client = McpClient(make_server())
        client.connect()
        text, is_error = client.call("mcp__docs__search", {"q": "timeout"})
        self.assertFalse(is_error)
        self.assertIn("timeout", text)

    def test_tool_error_is_flagged(self) -> None:
        client = McpClient(make_server())
        client.connect()
        text, is_error = client.call("mcp__docs__boom", {})
        self.assertTrue(is_error)
        self.assertIn("ZeroDivisionError", text)

    def test_server_down_returns_error_and_marks_disconnected(self) -> None:
        server = make_server()
        client = McpClient(server)
        client.connect()
        server.alive = False
        text, is_error = client.call("mcp__docs__search", {"q": "x"})
        self.assertTrue(is_error)
        self.assertFalse(client.connected)

    def test_external_tools_default_not_read_only(self) -> None:
        tools = McpClient(make_server()).connect()
        self.assertTrue(all(not t.read_only for t in tools))

    def test_long_description_truncated(self) -> None:
        server = FakeMcpServer("x", {"t": ("长" * 2000, {"type": "object"}, lambda a: "")})
        tool = McpClient(server).connect()[0]
        self.assertEqual(len(tool.description), MAX_DESCRIPTION)

    def test_schemas_marked_and_sorted(self) -> None:
        schemas = to_tool_schemas(McpClient(make_server()).connect())
        self.assertEqual([s["name"] for s in schemas], ["mcp__docs__boom", "mcp__docs__search"])
        self.assertTrue(schemas[0]["description"].startswith("[MCP:docs]"))

    def test_name_sanitized(self) -> None:
        self.assertEqual(mcp_tool_name("my server", "do.thing"), "mcp__my_server__do_thing")


if __name__ == "__main__":
    unittest.main()
