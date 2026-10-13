"""15 沙箱的测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from harness.sandbox import (
    SAFE_ENV_KEYS,
    SECRET_HINTS,
    Sandbox,
    SandboxPolicy,
    SandboxViolation,
    describe_limits,
    readonly_policy,
    writable_policy,
)

PY = sys.executable


class PolicyTests(unittest.TestCase):
    def test_readonly_policy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            policy = readonly_policy(root)
            self.assertFalse(policy.allow_write)
            self.assertFalse(policy.shell)
            self.assertEqual(policy.timeout, 10.0)

    def test_describe_limits_mentions_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            lines = describe_limits(readonly_policy(root))
            self.assertTrue(any(str(Path(root).resolve()) in line for line in lines))

    def test_missing_root_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            Sandbox(SandboxPolicy(root=str(Path(tempfile.gettempdir()) / "definitely-not-here-xyz")))


class PathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.sandbox = Sandbox(readonly_policy(str(self.root)))

    def test_inside_path_ok(self) -> None:
        target = self.sandbox.resolve("sub/file.txt")
        self.assertTrue(str(target).startswith(str(self.root.resolve())))

    def test_escape_rejected(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.sandbox.resolve("../outside.txt")

    def test_write_rejected_in_readonly_sandbox(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.sandbox.resolve("new.txt", write=True)

    def test_symlink_escape_rejected(self) -> None:
        outside = Path(self.tmp.name).parent / "escape-target.txt"
        outside.write_text("x", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台不允许创建符号链接")
        with self.assertRaises(SandboxViolation):
            self.sandbox.resolve("link.txt")

    def test_argv_paths_checked(self) -> None:
        with self.assertRaises(SandboxViolation):
            self.sandbox.check_argv_paths(["cat", "../secret.txt"])

    def test_argv_flags_skipped(self) -> None:
        self.sandbox.check_argv_paths(["grep", "-n", "todo", "src/app.py"])


class EnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_secrets_are_stripped(self) -> None:
        sandbox = Sandbox(readonly_policy(self.tmp.name))
        fake = {
            "PATH": "/usr/bin",
            "HOME": "/home/dev",
            "OPENAI_API_KEY": "sk-secret",
            "GITHUB_TOKEN": "ghp-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "DATABASE_PASSWORD": "hunter2",
            "MY_CREDENTIALS": "x",
            "SSH_AUTH_SOCK": "/tmp/sock",
            "PROXY_URL": "http://proxy",  # 会撞上 "PROXY" 而不是敏感词
        }
        env = sandbox.build_env(fake)
        self.assertEqual(env.get("PATH"), "/usr/bin")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("DATABASE_PASSWORD", env)
        self.assertNotIn("MY_CREDENTIALS", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)

    def test_extra_env_rejects_secret_names(self) -> None:
        sandbox = Sandbox(readonly_policy(self.tmp.name, extra_env={"MY_TOKEN": "x", "SAFE_FLAG": "1"}))
        env = sandbox.build_env({})
        self.assertNotIn("MY_TOKEN", env)
        self.assertEqual(env.get("SAFE_FLAG"), "1")

    def test_safe_env_keys_contain_no_secret_hint(self) -> None:
        for key in SAFE_ENV_KEYS:
            self.assertFalse(
                any(hint in key.upper() for hint in SECRET_HINTS),
                f"{key} 看起来像凭据变量，不该进白名单",
            )


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.sandbox = Sandbox(writable_policy(str(self.root), timeout=5.0))

    def test_echo_runs(self) -> None:
        result = self.sandbox.run([PY, "-c", "print('hello')"])
        self.assertTrue(result.ok, result.as_tool_result())
        self.assertIn("hello", result.stdout)

    def test_cwd_is_sandbox_root(self) -> None:
        result = self.sandbox.run([PY, "-c", "import os; print(os.getcwd())"])
        self.assertEqual(Path(result.stdout.strip()).resolve(), self.root.resolve())

    def test_timeout_kills_and_reports(self) -> None:
        result = self.sandbox.run([PY, "-c", "import time; time.sleep(30)"], timeout=0.6)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertIn("超时", result.as_tool_result())

    def test_missing_command_is_error_not_exception(self) -> None:
        result = self.sandbox.run(["definitely-not-a-real-command-xyz"])
        self.assertFalse(result.ok)
        self.assertIn("命令不存在", result.as_tool_result())

    def test_long_output_spills_to_disk(self) -> None:
        sandbox = Sandbox(writable_policy(str(self.root), max_output=200))
        result = sandbox.run([PY, "-c", "print('x' * 5000)"])
        self.assertTrue(result.truncated)
        self.assertIsNotNone(result.spill_path)
        spilled = (self.root / result.spill_path).read_text(encoding="utf-8")
        self.assertGreater(len(spilled), 5000)
        self.assertIn("已落盘", result.as_tool_result())

    def test_argv_escape_is_reported_as_violation(self) -> None:
        result = self.sandbox.run(["cat", "../outside.txt"])
        self.assertFalse(result.ok)
        self.assertIn("沙箱拒绝执行", result.as_tool_result())

    def test_write_argv_in_readonly_sandbox(self) -> None:
        readonly = Sandbox(readonly_policy(str(self.root)))
        result = readonly.run(["cp", "a.txt", "b.txt"])
        self.assertFalse(result.ok)
        self.assertIn("只读", result.as_tool_result())

    def test_empty_argv_is_violation(self) -> None:
        result = self.sandbox.run([])
        self.assertFalse(result.ok)

    def test_secret_not_visible_to_child(self) -> None:
        import os

        os.environ["LEAK_TEST_TOKEN"] = "should-not-leak"
        self.addCleanup(lambda: os.environ.pop("LEAK_TEST_TOKEN", None))
        result = self.sandbox.run([PY, "-c", "import os; print(os.environ.get('LEAK_TEST_TOKEN', 'absent'))"])
        self.assertIn("absent", result.stdout)


class LimitTests(unittest.TestCase):
    """记录教学版沙箱挡不住的东西。这些测试断言的是「局限存在」，换成真隔离后应当改写。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_readonly_flag_does_not_stop_child_writes(self) -> None:
        sandbox = Sandbox(readonly_policy(str(self.root)))
        result = sandbox.run([PY, "-c", "open('made-by-child.txt', 'w').write('x')"])
        self.assertTrue(result.ok)
        self.assertTrue((self.root / "made-by-child.txt").exists())

    def test_write_programs_matched_by_name_not_substring(self) -> None:
        # format / transform 里含 rm，但它们不是 rm；按子串匹配会把它们当成写命令
        from harness.sandbox import WRITE_PROGRAMS
        self.assertNotIn("format", WRITE_PROGRAMS)
        sandbox = Sandbox(readonly_policy(str(self.root)))
        result = sandbox.run(["format-not-a-real-cmd", "a/b.txt"])
        self.assertNotIn("只读", result.as_tool_result())


if __name__ == "__main__":
    unittest.main()
