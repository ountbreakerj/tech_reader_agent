"""session_log 的单元测试：序号单调、落盘往返、坏行报错、截断重放、并发边界。

全部用 tempfile.TemporaryDirectory，测试不往仓库里写任何文件。
运行：py -m unittest discover -s labs/tests -t labs -p "test_session_log.py"
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from harness.session_log import (
    REQUEST_HEADER,
    SESSION_FORMAT_VERSION,
    Event,
    LogCorrupt,
    Replayer,
    SessionLog,
    canonical_header,
    tools_fingerprint,
)


def record_at(path: Path, line: int) -> dict:
    """读回第 line 行（1 起）的 JSON，方便断言落盘内容。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[line - 1])


def make_log(session_id: str = "s-1") -> SessionLog:
    log = SessionLog(session_id, cwd="/work")
    log.append("turn/start", {"n": 1}, turn=1)
    log.append("step/start", {}, turn=1, step=1)
    log.append("system/message", {"content": "you are a coding agent"})
    log.append("user/message", {"content": "fix the bug"})
    log.append(
        "assistant/message",
        {"text": "reading", "tool_calls": [{"id": "c1", "name": "read", "args": {"path": "a.py"}}]},
        turn=1,
        step=1,
    )
    log.append("tool/call", {"call_id": "c1", "name": "read"}, turn=1, step=1)
    log.append(
        "tool/result",
        {"call_id": "c1", "content": "print(1)", "is_error": False},
        turn=1,
        step=1,
    )
    log.append("step/end", {}, turn=1, step=1)
    log.append("assistant/message", {"text": "done", "tool_calls": []}, turn=1, step=1)
    log.append("turn/end", {"reason": {"kind": "completed"}}, turn=1)
    return log


class SeqTest(unittest.TestCase):
    def test_seq_is_monotonic_and_equals_index(self) -> None:
        log = SessionLog("s")
        seqs = [log.append("user/message", {"content": f"m{i}"}) for i in range(5)]
        self.assertEqual(seqs, [0, 1, 2, 3, 4])
        self.assertEqual([event.seq for event in log.events], seqs)
        self.assertEqual(log.next_seq, 5)

    def test_append_returns_seq_and_reads_back_same_event(self) -> None:
        log = SessionLog("s")
        seq = log.append("user/message", {"content": "hello"}, turn=2, step=3)
        event = log.event(seq)
        assert event is not None
        self.assertEqual((event.kind, event.turn, event.step), ("user/message", 2, 3))
        self.assertEqual(event.payload, {"content": "hello"})
        self.assertGreater(event.ts, 0)

    def test_event_returns_none_beyond_the_log(self) -> None:
        log = SessionLog("s")
        log.append("user/message", {"content": "x"})
        self.assertIsNone(log.event(1))
        self.assertIsNone(log.event(-1))

    def test_payload_is_snapshotted_not_aliased(self) -> None:
        log = SessionLog("s")
        payload = {"content": "before", "nested": {"n": 1}}
        seq = log.append("user/message", payload)
        payload["content"] = "after"
        payload["nested"]["n"] = 99
        event = log.event(seq)
        assert event is not None
        self.assertEqual(event.payload["content"], "before")
        self.assertEqual(event.payload["nested"], {"n": 1})

    def test_non_json_payload_is_rejected_at_append(self) -> None:
        log = SessionLog("s")
        with self.assertRaises(ValueError):
            log.append("tool/result", {"blob": object()})
        with self.assertRaises(ValueError):
            log.append("tool/result", {"n": float("nan")})
        self.assertEqual(len(log), 0)  # 失败的事件没有进入日志

    def test_empty_kind_is_rejected(self) -> None:
        log = SessionLog("s")
        with self.assertRaises(ValueError):
            log.append("", {})

    def test_duplicate_identical_appends_still_get_distinct_seqs(self) -> None:
        """同样的内容追加两次是两条事件：日志记的是「发生了什么」，不是「内容集合」。"""
        log = SessionLog("s")
        first = log.append("tool/result", {"call_id": "c1", "content": "same", "is_error": False})
        second = log.append("tool/result", {"call_id": "c1", "content": "same", "is_error": False})
        self.assertNotEqual(first, second)
        self.assertEqual((first, second), (0, 1))


class SinceTest(unittest.TestCase):
    def test_since_excludes_the_given_seq(self) -> None:
        log = make_log()
        self.assertEqual([event.seq for event in log.since(3)], [4, 5, 6, 7, 8, 9])
        self.assertEqual(log.since(-1), list(log.events))
        self.assertEqual(log.since(9), [])
        self.assertEqual(log.since(99), [])

    def test_since_supports_incremental_consumption(self) -> None:
        """消费者记住自己的游标，只处理新事件；不用反复全量扫描。"""
        log = make_log()
        cursor = -1
        seen: list[int] = []
        for _ in range(3):
            for event in log.since(cursor):
                seen.append(event.seq)
                cursor = event.seq
            log.append("tool/result", {"call_id": "cx", "content": "more", "is_error": False})
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))


class RoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_save_and_load_are_identical(self) -> None:
        log = make_log()
        path = log.save(self.tmp / "session.jsonl")
        restored = SessionLog.load(path)
        self.assertEqual(restored.session_id, log.session_id)
        self.assertEqual(restored.cwd, log.cwd)
        self.assertEqual(len(restored), len(log))
        for original, copy in zip(log.events, restored.events):
            self.assertEqual(original.to_record(), copy.to_record())
        self.assertEqual(restored.warnings, [])

    def test_file_layout_is_one_event_per_line(self) -> None:
        log = make_log()
        path = log.save(self.tmp / "session.jsonl")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(log) + 1)  # 头记录 + 每条事件一行
        self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))
        self.assertEqual(record_at(path, 1)["type"], "session")
        self.assertEqual(record_at(path, 1)["version"], SESSION_FORMAT_VERSION)
        self.assertEqual(record_at(path, 2)["seq"], 0)

    def test_save_is_atomic_and_leaves_no_tmp_file(self) -> None:
        log = make_log()
        path = log.save(self.tmp / "session.jsonl")
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_default_path_carries_a_timestamp(self) -> None:
        log = SessionLog("my session/id")
        path = log.save()  # 默认落在当前目录的 sessions/ 下
        self.addCleanup(path.unlink)
        self.assertEqual(path.parent.name, "sessions")
        self.assertRegex(path.name, r"^my_session_id-[0-9a-f]{8}-\d{8}-\d{6}\.jsonl$")

    def test_round_trip_after_further_appends(self) -> None:
        log = make_log()
        path = log.save(self.tmp / "a.jsonl")
        restored = SessionLog.load(path)
        seq = restored.append("user/message", {"content": "again"})
        self.assertEqual(seq, len(log))
        again = SessionLog.load(restored.save(self.tmp / "b.jsonl"))
        self.assertEqual(again.events[-1].payload, {"content": "again"})


class CorruptLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.log = make_log()
        self.path = self.log.save(self.tmp / "session.jsonl")
        self.lines = self.path.read_text(encoding="utf-8").splitlines()

    def write(self, lines: list[str], *, trailing_newline: bool = True) -> Path:
        target = self.tmp / "broken.jsonl"
        text = "\n".join(lines) + ("\n" if trailing_newline else "")
        target.write_text(text, encoding="utf-8")
        return target

    def test_unparsable_event_reports_its_line_number(self) -> None:
        broken = self.lines.copy()
        broken[4] = '{"seq": 3, "kind": "user/message"'  # 截断的 JSON
        with self.assertRaises(LogCorrupt) as caught:
            SessionLog.load(self.write(broken))
        self.assertEqual(caught.exception.line, 5)
        self.assertIn("not valid JSON", caught.exception.reason)

    def test_missing_field_reports_its_line_number(self) -> None:
        broken = self.lines.copy()
        record = json.loads(broken[6])
        del record["payload"]
        broken[6] = json.dumps(record)
        with self.assertRaises(LogCorrupt) as caught:
            SessionLog.load(self.write(broken))
        self.assertEqual(caught.exception.line, 7)
        self.assertIn("payload", caught.exception.reason)

    def test_non_contiguous_seq_is_refused(self) -> None:
        """seq 跳号意味着回放会漏掉事件，必须在载入时报错而不是照单全收。"""
        broken = self.lines.copy()
        record = json.loads(broken[8])
        record["seq"] = 99
        broken[8] = json.dumps(record)
        with self.assertRaises(LogCorrupt) as caught:
            SessionLog.load(self.write(broken))
        # 事件行 = 文件行号 - 1；第 8 行承载 seq 6，跳号到 99 后下一条（第 9 行）暴露了它。
        self.assertEqual(caught.exception.line, 9)
        self.assertIn("not contiguous", caught.exception.reason)

    def test_last_event_with_a_wrong_seq_is_also_refused(self) -> None:
        """坏在最后一行时没有「下一条」来暴露它，所以要单独验一次。"""
        broken = self.lines.copy()
        record = json.loads(broken[-1])
        record["seq"] = 42
        broken[-1] = json.dumps(record)
        with self.assertRaises(LogCorrupt) as caught:
            SessionLog.load(self.write(broken))
        self.assertEqual(caught.exception.line, len(broken))

    def test_unknown_format_version_is_refused(self) -> None:
        broken = self.lines.copy()
        header = json.loads(broken[0])
        header["version"] = SESSION_FORMAT_VERSION + 1
        broken[0] = json.dumps(header)
        with self.assertRaises(LogCorrupt) as caught:
            SessionLog.load(self.write(broken))
        self.assertEqual(caught.exception.line, 1)
        self.assertIn("unsupported session format version", caught.exception.reason)

    def test_missing_header_is_refused(self) -> None:
        with self.assertRaises(LogCorrupt) as caught:
            SessionLog.load(self.write(self.lines[1:]))
        self.assertIn("before the session header", caught.exception.reason)

    def test_bad_line_does_not_crash_the_loader(self) -> None:
        """最关键的一条：坏日志必须抛出一个能指导修复的异常，而不是崩在解析里。"""
        broken = self.lines.copy()
        broken[3] = "not json at all"
        try:
            SessionLog.load(self.write(broken))
        except LogCorrupt as exc:
            self.assertIsInstance(exc.line, int)
        else:
            self.fail("a corrupt committed line must be refused")


class TornTailTest(unittest.TestCase):
    """进程被杀留下的残行：识别、报告，默认丢弃；严格模式则拒绝。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.lines = make_log().save(self.tmp / "session.jsonl").read_text(encoding="utf-8").splitlines()

    def write(self, text: str) -> Path:
        target = self.tmp / "torn.jsonl"
        target.write_text(text, encoding="utf-8")
        return target

    def test_torn_last_line_is_dropped_and_reported(self) -> None:
        complete = "\n".join(self.lines[:5])
        torn = '{"seq": 4, "kind": "user/messa'
        path = self.write(complete + "\n" + torn)
        log = SessionLog.load(path)
        self.assertEqual(len(log), 4)
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("line 6", log.warnings[0])
        self.assertIn("no trailing newline", log.warnings[0])

    def test_torn_tail_still_loads_everything_before_it(self) -> None:
        """残行后面的提交前缀完好时也要能全部载入——日志的价值就在于前缀可用。"""
        complete = "\n".join(self.lines[:6])
        path = self.write(complete + "\n" + self.lines[6][:20])
        log = SessionLog.load(path)
        self.assertEqual(log.next_seq, 5)
        self.assertEqual([event.seq for event in log.events], [0, 1, 2, 3, 4])

    def test_strict_mode_refuses_a_torn_tail(self) -> None:
        path = self.write("\n".join(self.lines[:4]) + "\n" + '{"seq": 3, "kin')
        with self.assertRaises(LogCorrupt) as caught:
            SessionLog.load(path, strict=True)
        self.assertEqual(caught.exception.line, 5)
        self.assertIn("torn tail", caught.exception.reason)

    def test_a_complete_file_has_no_warnings(self) -> None:
        path = self.write("\n".join(self.lines) + "\n")
        self.assertEqual(SessionLog.load(path).warnings, [])


class ReplayTest(unittest.TestCase):
    def test_replay_rebuilds_the_model_visible_history(self) -> None:
        log = make_log()
        messages = Replayer(log).messages_upto()
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user", "assistant"])
        self.assertEqual(messages[0]["content"], "you are a coding agent")
        assistant = messages[2]["content"]
        self.assertEqual(assistant[0], {"type": "text", "text": "reading"})
        self.assertEqual(assistant[1]["type"], "tool_use")
        self.assertEqual(assistant[1]["input"], {"path": "a.py"})
        results = messages[3]["content"]
        self.assertEqual(results[0]["tool_use_id"], "c1")
        self.assertFalse(results[0]["is_error"])

    def test_replay_is_deterministic(self) -> None:
        log = make_log()
        first = Replayer(log).messages_upto()
        second = Replayer(log).messages_upto()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)  # 每次返回全新的列表

    def test_replay_ignores_bookkeeping_events(self) -> None:
        log = SessionLog("s")
        log.append("turn/start", {"n": 1}, turn=1)
        log.append("user/message", {"content": "hi"})
        log.append("turn/end", {"reason": {"kind": "completed"}}, turn=1)
        self.assertEqual([m["role"] for m in Replayer(log).messages_upto()], ["user"])

    def test_truncated_replay_stops_at_the_given_seq(self) -> None:
        log = make_log()
        replayer = Replayer(log)
        a = replayer.messages_upto(3)
        b = replayer.messages_upto(6)
        self.assertEqual([m["role"] for m in a], ["system", "user"])
        self.assertEqual([m["role"] for m in b], ["system", "user", "assistant", "user"])
        self.assertEqual(replayer.last_dropped_calls, 0)

    def test_truncating_inside_a_step_drops_the_dangling_tool_call(self) -> None:
        """截在 tool/call 与 tool/result 之间：没有结果的那次调用必须从历史里去掉，
        否则重放出来的不是一个合法请求前缀，厂商会直接拒绝。"""
        log = make_log()
        # seq 5 是 assistant/message（含一次 tool_use），seq 6 的 tool/result 还没发生。
        replayer = Replayer(log)
        messages = replayer.messages_upto(5)
        self.assertEqual(replayer.last_dropped_calls, 1)
        self.assertEqual(messages[2]["content"], [{"type": "text", "text": "reading"}])
        self.assertNotIn("tool_use", json.dumps(messages))

    def test_a_closed_step_keeps_its_tool_call(self) -> None:
        """结果已经在日志里时不丢块：截断只处理真正悬空的调用。"""
        log = make_log()
        replayer = Replayer(log)
        messages = replayer.messages_upto(6)
        self.assertEqual(replayer.last_dropped_calls, 0)
        self.assertEqual(messages[2]["content"][1]["type"], "tool_use")
        self.assertEqual(messages[3]["content"][0]["tool_use_id"], "c1")

    def test_consecutive_tool_results_merge_into_one_user_message(self) -> None:
        log = SessionLog("s")
        log.append("assistant/message", {"text": "", "tool_calls": [{"id": "a", "name": "t", "args": {}}, {"id": "b", "name": "t", "args": {}}]})
        log.append("tool/result", {"call_id": "a", "content": "1", "is_error": False})
        log.append("tool/result", {"call_id": "b", "content": "2", "is_error": True})
        messages = Replayer(log).messages_upto()
        self.assertEqual(len(messages), 2)
        self.assertEqual([block["tool_use_id"] for block in messages[1]["content"]], ["a", "b"])
        self.assertTrue(messages[1]["content"][1]["is_error"])

    def test_replay_after_round_trip_matches_replay_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = make_log()
            before = Replayer(log).messages_upto()
            restored = SessionLog.load(log.save(Path(tmp) / "s.jsonl"))
            self.assertEqual(Replayer(restored).messages_upto(), before)

    def test_only_the_log_is_the_source_of_truth(self) -> None:
        """同一份日志，重新打开一个新 Replayer 得到的历史完全一致——
        回放器不带任何跨调用的状态。"""
        log = make_log()
        first = Replayer(log).messages_upto(7)
        log.append("user/message", {"content": "more"})
        second = Replayer(log).messages_upto(7)
        self.assertEqual(first, second)

    def test_last_boundary_finds_a_safe_restart_point(self) -> None:
        log = make_log()
        replayer = Replayer(log)
        self.assertEqual(replayer.last_boundary(6), -1)   # step 还没闭合
        self.assertEqual(replayer.last_boundary(7), 7)    # step/end
        self.assertEqual(replayer.last_boundary(9), 9)    # turn/end
        self.assertEqual(replayer.last_boundary(), 9)


class RequestHeaderTest(unittest.TestCase):
    def test_fingerprint_tracks_order_and_membership(self) -> None:
        self.assertEqual(tools_fingerprint(["read", "bash"]), tools_fingerprint(["read", "bash"]))
        self.assertNotEqual(tools_fingerprint(["read", "bash"]), tools_fingerprint(["bash", "read"]))
        self.assertNotEqual(tools_fingerprint(["read"]), tools_fingerprint(["read", "bash"]))

    def test_unchanged_header_is_not_logged_again(self) -> None:
        log = SessionLog("s")
        first = log.log_request_header(provider="p", model="m", tools=["read"])
        self.assertIsNotNone(first)
        self.assertIsNone(log.log_request_header(provider="p", model="m", tools=["read"]))
        self.assertEqual(len(log.request_headers()), 1)

    def test_header_change_is_logged_with_a_reason(self) -> None:
        log = SessionLog("s")
        log.log_request_header(provider="p", model="m", tools=["read"])
        log.log_request_header(provider="p", model="m", tools=["read", "bash"])
        headers = log.request_headers()
        self.assertEqual([header["reason"] for header in headers], ["initial", "change"])
        self.assertEqual(log.request_header()["toolNames"], ["read", "bash"])

    def test_tool_set_change_is_detectable_from_the_log(self) -> None:
        """工具集变了必须能从日志事后查出来，否则只能看到历史莫名不同。"""
        log = SessionLog("s")
        log.log_request_header(provider="p", model="m", tools=["read"])
        log.log_request_header(provider="p", model="m", tools=["read", "bash"])
        log.log_request_header(provider="p", model="m", tools=["read", "bash", "grep"])
        self.assertEqual(log.tool_set_changes(), ["+grep"])

    def test_request_header_is_folded_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = SessionLog("s")
            log.log_request_header(provider="p", model="m", tools=["read"])
            log.log_request_header(provider="deepseek", model="m2", tools=["read", "bash"])
            restored = SessionLog.load(log.save(Path(tmp) / "s.jsonl"))
            header = restored.request_header()
            self.assertEqual(header["provider"], "deepseek")
            self.assertEqual(header["model"], "m2")
            self.assertEqual(header["toolsFingerprint"], tools_fingerprint(["read", "bash"]))
            self.assertEqual(restored.tool_set_changes(), ["+bash"])

    def test_no_header_yet_returns_none(self) -> None:
        log = SessionLog("s")
        self.assertIsNone(log.request_header())
        self.assertEqual(log.request_headers(), [])
        self.assertEqual(log.tool_set_changes(), [])

    def test_request_context_is_deduplicated(self) -> None:
        log = SessionLog("s")
        self.assertIsNotNone(log.log_request_context(provider="p", model="m", context_window=128000))
        self.assertIsNone(log.log_request_context(provider="p", model="m", context_window=128000))
        self.assertIsNotNone(log.log_request_context(provider="p", model="m", context_window=200000))
        self.assertEqual(log.request_context()["contextWindow"], 200000)

    def test_request_header_event_is_in_the_log_and_replayable(self) -> None:
        log = make_log()
        log.log_request_header(provider="p", model="m", tools=["read"])
        kinds = [event.kind for event in log.events]
        self.assertIn(REQUEST_HEADER, kinds)
        # 请求头不改历史：加了它之后回放结果与不加时一致
        self.assertEqual([m["role"] for m in Replayer(log).messages_upto()],
                         ["system", "user", "assistant", "user", "assistant"])

    def test_canonical_header_shape(self) -> None:
        header = canonical_header(provider="p", model="m", tools=["a"])
        self.assertEqual(header["toolNames"], ["a"])
        self.assertEqual(header["toolsFingerprint"], tools_fingerprint(["a"]))


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_appends_get_unique_contiguous_seqs(self) -> None:
        log = SessionLog("s")
        started = threading.Barrier(8)

        def worker(worker_id: int) -> None:
            started.wait()
            for i in range(50):
                log.append("tool/result", {"call_id": f"{worker_id}-{i}", "content": "", "is_error": False})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        seqs = [event.seq for event in log.events]
        self.assertEqual(len(seqs), 400)
        self.assertEqual(seqs, list(range(400)))  # 不撞车也不跳号

    def test_append_during_replay_does_not_disturb_earlier_results(self) -> None:
        """回放的确定性与并发写入无关：截断点固定的重放结果不会因为又有新事件而变。"""
        log = make_log()
        replayer = Replayer(log)
        fixed = replayer.messages_upto(7)
        log.append("tool/result", {"call_id": "c9", "content": "late", "is_error": False})
        self.assertEqual(replayer.messages_upto(7), fixed)

    def test_concurrent_writers_round_trip(self) -> None:
        log = SessionLog("s")
        threads = [
            threading.Thread(target=lambda n=n: [log.append("user/message", {"content": str(n)}) for _ in range(20)])
            for n in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with tempfile.TemporaryDirectory() as tmp:
            restored = SessionLog.load(log.save(Path(tmp) / "s.jsonl"))
        self.assertEqual([event.seq for event in restored.events], list(range(80)))


class EventTest(unittest.TestCase):
    def test_from_record_rejects_wrong_types_with_line_number(self) -> None:
        base = {"seq": 0, "kind": "user/message", "turn": 0, "step": 0, "ts": 1.0, "payload": {}}
        with self.assertRaises(LogCorrupt) as caught:
            Event.from_record({**base, "seq": "0"}, 7)
        self.assertEqual(caught.exception.line, 7)
        with self.assertRaises(LogCorrupt):
            Event.from_record({**base, "payload": []}, 8)
        with self.assertRaises(LogCorrupt):
            Event.from_record({**base, "kind": ""}, 9)

    def test_record_round_trip_preserves_every_field(self) -> None:
        log = SessionLog("s")
        log.append("tool/result", {"call_id": "c", "content": "x", "is_error": True}, turn=3, step=2)
        event = log.events[0]
        self.assertEqual(Event.from_record(event.to_record(), 1), event)


if __name__ == "__main__":
    unittest.main()
