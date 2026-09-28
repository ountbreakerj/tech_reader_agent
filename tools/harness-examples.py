"""Validate the Python examples readers copy from the Codex Harness HTML."""

import ast
import asyncio
from contextlib import redirect_stdout
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


PROJECT = Path(__file__).resolve().parents[1]
CONTENT = PROJECT / "src/modules/codex-harness/content.html"


class ExamplesParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.examples = {}
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "code" and attrs.get("data-language") == "python":
            name = attrs["data-example"]
            if name in self.examples:
                raise ValueError(f"Duplicate example: {name}")
            self.current = name
            self.examples[name] = {
                "runnable": attrs["data-runnable"] == "true", "code": ""
            }

    def handle_data(self, data):
        if self.current is not None:
            self.examples[self.current]["code"] += data

    def handle_endtag(self, tag):
        if tag == "code":
            self.current = None


parser = ExamplesParser()
parser.feed(CONTENT.read_text(encoding="utf-8"))
EXAMPLES = parser.examples


def definitions(name):
    # Load functions and constants without rerunning an example's main program.
    tree = ast.parse(EXAMPLES[name]["code"])
    tree.body = [
        node for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef))
        or (isinstance(node, ast.Assign)
            and all(isinstance(target, ast.Name) and target.id.isupper()
                    for target in node.targets))
    ]
    namespace = {"__name__": "harness_example"}
    exec(compile(tree, f"<example:{name}>", "exec"), namespace)
    return namespace


class ExampleTests(unittest.TestCase):
    def test_python_310_grammar_and_standalone_output(self):
        expected = {
            "loop-state": ["compact", "sample", "finish"],
            "context": ["model-v1"],
            "tools": ["a"],
            "tool-errors": ["items 必须是列表"],
            "history": ["目标测试通过"],
            "history-version": ["3 1", "False"],
            "extensions": ["目标测试还没通过，需要继续检查"],
            "complete": ["c1 run_tests => IndexError", "c3 read_file",
                         "c5 apply_fix", "c7 run_tests => 2 个测试通过",
                         "最终回答： 目标测试通过。"],
            "stream-order": ["完成顺序： ['B', 'A']", "历史顺序： ['A', 'B']"],
            "process": ["'status': 'running'", "'exit_code': 0",
                        "'output': 'finished'"],
            "retry": ["重试等待： 0.1", "恢复成功",
                      "[['用户任务'], ['用户任务', '已收集的工具结果']]"],
            "lab": ["['item_done', 'tool_started', 'tool_result', 'next_request']"],
        }
        self.assertEqual(len(EXAMPLES), 15)
        self.assertEqual({name for name, item in EXAMPLES.items()
                          if item["runnable"]}, set(expected))
        for name, item in EXAMPLES.items():
            with self.subTest(example=name):
                ast.parse(item["code"], feature_version=(3, 10))
                if not item["runnable"]:
                    continue
                result = subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", "-c", item["code"]],
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                for fragment in expected[name]:
                    self.assertIn(fragment, result.stdout)

    def test_post_sampling_decisions(self):
        choose = definitions("loop-state")["next_action"]
        def unexpected_hook():
            self.fail("Stop hook ran while follow-up work was pending")
        self.assertEqual(choose(True, False, True, unexpected_hook), "compact")
        self.assertEqual(choose(False, True, False, unexpected_hook), "sample")
        self.assertEqual(choose(False, False, False,
                                lambda: {"block": True, "prompt": "check"}),
                         "record_hook_then_sample")
        self.assertEqual(choose(False, False, True,
                                lambda: {"block": True, "prompt": ""}), "finish")

    def test_tool_parameter_failures(self):
        dispatch = definitions("tool-errors")["dispatch"]
        for args, key, value in [
            ("{", "error", "参数不是合法 JSON"),
            ("[]", "error", "只接受 items 参数"),
            ('{"items": [], "extra": 1}', "error", "只接受 items 参数"),
            ('{"items": null}', "error", "items 必须是列表"),
            ('{"items": []}', "value", None),
            ('{"items": [0, 1]}', "value", 0),
        ]:
            with self.subTest(arguments=args):
                result = dispatch({"id": "check", "name": "first_item",
                                   "arguments": args})
                self.assertEqual(result, {"call_id": "check", key: value})
        self.assertEqual(dispatch({"id": "x", "name": "unknown", "arguments": "{}"}),
                         {"call_id": "x", "error": "未知工具"})

    def test_approval_precedes_environment_and_execution(self):
        execute = definitions("permissions")["execute_with_policy"]
        calls = []
        def choose(call):
            calls.append("environment")
            return "restricted"
        def run(call, environment):
            calls.append(environment)
            return "done"
        self.assertEqual(execute({}, lambda call: "ask", choose, run),
                         {"status": "waiting_for_approval"})
        for decision in ["deny", "unknown"]:
            with self.assertRaises(PermissionError):
                execute({}, lambda call: decision, choose, run)
        self.assertEqual(calls, [])
        self.assertEqual(execute({}, lambda call: "allow", choose, run), "done")
        self.assertEqual(calls, ["environment", "restricted"])

    def test_stop_hook(self):
        hook = definitions("extensions")["stop_hook"]
        self.assertFalse(hook({"tests_passed": True})["continue"])
        result = hook({"tests_passed": False})
        self.assertTrue(result["continue"])
        self.assertTrue(result["reason"])


class AsyncExampleTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_result_feedback_and_step_limit(self):
        run_turn = definitions("loop")["run_turn"]
        seen = []
        async def model(history):
            seen.append(list(history))
            if len(history) == 1:
                return {"role": "assistant", "tool_call": {"id": "a"}}
            self.assertEqual(history[-1], {"role": "tool", "call_id": "a",
                                           "content": "real result"})
            return {"role": "assistant", "content": "finished"}
        async def tool(call):
            self.assertEqual(call["id"], "a")
            return "real result"
        self.assertEqual(await run_turn("task", model, tool), "finished")
        self.assertEqual(len(seen), 2)
        calls = 0
        async def endless(history):
            nonlocal calls
            calls += 1
            return {"role": "assistant", "tool_call": {"id": "a"}}
        with self.assertRaises(RuntimeError):
            await run_turn("task", endless, tool)
        self.assertEqual(calls, 20)

    async def test_compaction_retains_recent_steps_and_propagates_failure(self):
        compact = definitions("compact")["compact_context"]
        older = [{"call": "a", "result": "ok"}]
        recent = [{"user": "new constraint"}]
        async def summarize(steps):
            self.assertEqual(steps, older)
            return "progress"
        self.assertEqual(await compact(older, recent, summarize),
                         [{"kind": "summary", "text": "progress"}] + recent)
        async def failing(steps):
            raise ConnectionError("summary unavailable")
        with self.assertRaises(ConnectionError):
            await compact(older, recent, failing)
        self.assertEqual(recent, [{"user": "new constraint"}])

    async def test_repair_success_already_fixed_and_failed_retest(self):
        example = definitions("complete")
        with TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            source = Path(folder) / "sample.py"
            for initial, replacement, expected, tool_names in [
                (example["BROKEN"], example["FIXED"], "目标测试通过。",
                 ["run_tests", "read_file", "apply_fix", "run_tests"]),
                (example["FIXED"], example["FIXED"], "目标测试通过。", ["run_tests"]),
                (example["BROKEN"], example["BROKEN"], "复测仍失败。",
                 ["run_tests", "read_file", "apply_fix", "run_tests"]),
            ]:
                source.write_text(initial, encoding="utf-8")
                example["FIXED"] = replacement
                answer, history = await example["run_turn"]("repair", source)
                self.assertEqual(answer, expected)
                outputs = [item for item in history if item["role"] == "tool"]
                self.assertEqual([item["tool_name"] for item in outputs], tool_names)
                self.assertEqual([item["call_id"] for item in outputs],
                                 [f"c{2 * index + 1}" for index in range(len(outputs))])
            source.write_text("# changed by another writer\n", encoding="utf-8")
            result = example["execute_tool"]({"name": "apply_fix"}, source)
            self.assertFalse(result["ok"])
            self.assertEqual(source.read_text(encoding="utf-8"),
                             "# changed by another writer\n")

    async def test_retry_budget_nonretryable_error_and_cancellation(self):
        retry = definitions("retry")["request_with_retry"]
        attempts, delays = [], []
        async def failing(prompt):
            attempts.append(prompt)
            raise ConnectionError("offline")
        async def sleep(seconds):
            delays.append(seconds)
        with self.assertRaises(ConnectionError):
            await retry(failing, ["task"], sleep, max_retries=2)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [0.1, 0.2])
        for error in [PermissionError, asyncio.CancelledError]:
            async def reject(prompt):
                raise error()
            delays.clear()
            with self.assertRaises(error):
                await retry(reject, [], sleep)
            self.assertEqual(delays, [])

    async def test_poll_timeout_keeps_collector_alive(self):
        poll = definitions("process")["poll"]
        release = asyncio.Event()
        async def collect():
            await release.wait()
            return {"exit_code": 0}
        task = asyncio.create_task(collect())
        try:
            self.assertEqual(await poll({"result": task}, 0.001),
                             {"session_id": "p1", "status": "running"})
            self.assertFalse(task.done())
            release.set()
            self.assertEqual(await poll({"result": task}, 1), {"exit_code": 0})
        finally:
            release.set()
            await task


if __name__ == "__main__":
    result = unittest.main(exit=False)
    report = {
        "sourceSha256": hashlib.sha256(CONTENT.read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
        "grammar": "3.10",
        "examples": len(EXAMPLES),
        "standaloneExamples": sum(item["runnable"] for item in EXAMPLES.values()),
        "testGroups": result.result.testsRun,
        "passed": result.result.wasSuccessful(),
        "failures": [str(test) for test, _ in result.result.failures],
        "errors": [str(test) for test, _ in result.result.errors],
    }
    report_path = PROJECT / "reports/harness-examples.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if report["passed"] else 1)
