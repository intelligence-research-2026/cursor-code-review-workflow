#!/usr/bin/env python3
"""Offline tests for observation-only timing and fail-open collection."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("measure-review.py")
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("metrics", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def event(call_id, subtype, tool="readToolCall", args=None, result=None):
    return {"type": "tool_call", "subtype": subtype, "call_id": call_id,
            "tool_call": {tool: {"args": args or {"path": "src/example.py"}, "result": result or {}}}}


class PerformanceTest(unittest.TestCase):
    def test_live_cli_envelopes_with_metadata_and_thinking(self):
        # Reproduce shapes/counts from run 40444807957 using synthetic bodies.
        # Do not save source, arguments or thinking text from a real review.
        m = module.Metrics()
        for subtype, count in (("delta", 994), ("completed", 8)):
            for _ in range(count):
                m.event({"type": "thinking", "subtype": subtype,
                         "text": "SECRET_THINKING", "timestamp_ms": 100}, 1)
        for tool, count in (("readToolCall", 22), ("grepToolCall", 2),
                            ("editToolCall", 1), ("getMcpToolsToolCall", 1)):
            for i in range(count):
                for subtype, now in (("started", 2), ("completed", 3)):
                    sample = event(f"{tool}-{i}", subtype, tool)
                    sample["tool_call"].update(hookAdditionalContexts=[],
                                               toolCallId="SECRET_ID", startedAtMs=100)
                    if subtype == "completed":
                        sample["tool_call"]["completedAtMs"] = 101
                    m.event(sample, now)
        m.event({"type": "result", "subtype": "success"}, 4)
        report = m.report(4, 0)
        self.assertEqual(report["classified_tool_calls"], 26)
        self.assertEqual(report["observed_tool_calls"], 26)
        self.assertEqual(report["unknown_events"], 0)
        self.assertTrue(report["classification_complete"])
        self.assertEqual(report["recognized_thinking_events"], {"delta": 994, "completed": 8})
        self.assertEqual(report["tools"]["edit"]["completed"], 1)
        self.assertEqual(report["tools"]["tool_discovery"]["completed"], 1)
        self.assertEqual(report["unique_read_files"], 1)
        self.assertEqual(report["repeated_read_requests"], 21)
        # Recognizing a search does not establish its result count.
        self.assertIsNone(report["explicit_zero_match_searches"])
        self.assertIsNone(report["observed_read_content_bytes"])
        self.assertNotIn("SECRET_", json.dumps(report))

    def test_future_thinking_subtype_remains_unknown(self):
        m = module.Metrics()
        m.event({"type": "thinking", "subtype": "future", "text": "SECRET_BODY"}, 1)
        m.event({"type": "result"}, 2)
        report = m.report(2, 0)
        self.assertEqual(report["unknown_events"], 1)
        self.assertFalse(report["classification_complete"])
        self.assertIsNone(report["unique_read_files"])
        self.assertNotIn("SECRET_BODY", json.dumps(report))

    def test_unknown_calls_are_unavailable_not_zero(self):
        m = module.Metrics()
        m.event(event("a", "started", "futureToolCall"), 1)
        m.event(event("a", "completed", "futureToolCall"), 2)
        m.event({"type": "result"}, 3)
        report = m.report(3, 0)
        self.assertEqual(report["classified_tool_calls"], 0)
        self.assertEqual(report["observed_tool_calls"], 1)
        for key in ("unique_read_files", "repeated_read_requests",
                    "observed_read_content_bytes", "explicit_zero_match_searches"):
            self.assertIsNone(report[key])
        with tempfile.TemporaryDirectory() as temp:
            summary = Path(temp) / "summary.md"
            with patch.dict(os.environ, RUNNER_TEMP=temp, GITHUB_STEP_SUMMARY=str(summary)):
                module.save_report(report)
            self.assertIn("Repeated read requests: unavailable", summary.read_text())

    def test_metadata_is_tolerated_but_ambiguous_tools_are_not(self):
        m = module.Metrics()
        sample = event("a", "started")
        sample["tool_call"]["metadata"] = {"private": "SECRET_METADATA"}
        self.assertEqual(m.tool(sample)[0], "read")
        sample["tool_call"]["grepToolCall"] = {}
        self.assertEqual(m.tool(sample)[0], "other")
        del sample["tool_call"]["grepToolCall"]
        sample["tool_call"]["futureToolCall"] = {}
        self.assertEqual(m.tool(sample)[0], "other")

    def test_diagnostics_are_bounded_and_do_not_traverse_content(self):
        m = module.Metrics()
        sample = {"type": "tool_call", "subtype": "started", "call_id": "a", "timestamp": 100,
                  "message": {"SECRET_MESSAGE": "SECRET_BODY"},
                  "tool_call": {"function": {"name": "read_file",
                      "arguments": {"path": "SECRET_PATH", "SECRET_ARGUMENT": "value"},
                      "result": {"SECRET_RESULT": "SECRET_SOURCE"}}}}
        m.event(sample, 1)
        report = m.report(2, 0)
        self.assertNotIn("SECRET_", json.dumps(report))
        shape = report["event_shapes"][0]
        self.assertEqual(shape["timestamp_fields"], {"timestamp": "number"})
        self.assertEqual(shape["name_fields"], {"tool_call.function.name": "read_file"})
        with patch.object(module, "MAX_SHAPES", 1):
            m.event({"type": "future_event"}, 2)
        self.assertEqual(m.report(3, 0)["diagnostic_overflow_events"], 1)
        self.assertEqual(len(m.shapes), 1)

    def test_schema_labels_filter_credentials_and_free_form_text(self):
        with patch.dict(os.environ, CURSOR_API_KEY="sensitive_token"):
            self.assertEqual(module.schema_label("sensitive_token"), "redacted")
            self.assertEqual(module.schema_label("secret/path/value"), "redacted")
            self.assertEqual(module.schema_label("readToolCall"), "readToolCall")

    def test_cli_version_uses_install_directory_only(self):
        self.assertEqual(module.cli_version(["/tmp/versions/2026.10.22-27dcf5f/cursor-agent"]),
                         "2026.10.22-27dcf5f")
        self.assertIsNone(module.cli_version(["/tmp/private-folder/cursor-agent"]))

    def test_overlap_repetition_and_explicit_empty_search(self):
        m = module.Metrics()
        m.event({"type": "system", "subtype": "init", "model": "Example Model"}, 1)
        m.event(event("a", "started"), 2)
        m.event(event("b", "started"), 3)
        m.event(event("a", "completed", result={"success": {"content": "abc"}}), 5)
        m.event(event("b", "completed", result={"success": {"content": "abc"}}), 6)
        m.event(event("c", "started", args={"path": "src/example.py", "offset": 10}), 7)
        m.event(event("c", "completed"), 8)
        m.event(event("d", "started", "grepToolCall"), 8)
        m.event(event("d", "completed", "grepToolCall", result={"success": {"numMatches": 0}}), 9)
        m.event({"type": "result", "duration_api_ms": 10000}, 10)
        report = m.report(10, 0)
        self.assertEqual(report["tool_union_seconds"], 6)
        self.assertEqual(report["unclassified_seconds"], 4)
        self.assertEqual(report["tools"]["read"]["seconds"], 7)
        self.assertEqual(report["repeated_read_requests"], 1)
        self.assertEqual(report["observed_read_content_bytes"], 6)
        self.assertEqual(report["unique_read_files"], 1)
        self.assertEqual(report["explicit_zero_match_searches"], 1)
        self.assertNotIn("src/example.py", json.dumps(report))

    def test_gaps_duplicates_and_tracking_limit(self):
        m = module.Metrics()
        m.event(event("missing", "completed"), 1)
        m.event(event("a", "started"), 2)
        m.event(event("a", "started"), 3)
        m.event({"type": "future_event"}, 4)
        with patch.object(module, "MAX_CALLS", 1):
            m.event(event("b", "started"), 5)
        report = m.report(6, 7)
        self.assertEqual(report["unfinished_calls"], 1)
        self.assertEqual(report["unmatched_completions"], 1)
        self.assertEqual(report["invalid_records"], 1)
        self.assertEqual(report["unknown_events"], 1)
        self.assertTrue(report["tracking_limit_reached"])
        self.assertEqual(report["unclassified_seconds"], 6)

    def test_invalid_and_oversized_records_are_drained(self):
        m = module.Metrics()
        data = b"not JSON\n" + b"x" * (module.MAX_RECORD + 10) + b'\n{"type":"result"}\n'
        module.observe(io.BytesIO(data), m, module.time.monotonic())
        self.assertEqual(m.invalid, 1)
        self.assertEqual(m.oversized, 1)
        self.assertTrue(m.terminal)

    def test_parser_exception_keeps_reading(self):
        m = module.Metrics()
        original = m.event
        def fail_once(e, now):
            if e.get("type") == "assistant":
                raise ValueError("sensitive body must never be logged")
            original(e, now)
        m.event = fail_once
        module.observe(io.BytesIO(b'{"type":"assistant"}\n{"type":"result"}\n'), m, module.time.monotonic())
        self.assertTrue(m.terminal)
        self.assertEqual(m.invalid, 1)

    def run_child(self, exit_code=0, blocked_report=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = root / "summary.md"
            target = root / "cursor-review-performance.json"
            if blocked_report:
                target.mkdir()
            child = root / "child.py"
            child.write_text('''import json, pathlib, sys
prompt = sys.stdin.read()
pathlib.Path("review.md").write_text("review unchanged: " + prompt)
print(json.dumps({"type":"system", "subtype":"init", "model":"Example Model"}))
print(json.dumps({"type":"user", "message":{"content":"SECRET_PROMPT"}}))
print(json.dumps({"type":"tool_call", "subtype":"started", "call_id":"private-id",
                  "tool_call":{"readToolCall":{"args":{"path":"SECRET_PATH.py"}}}}))
print(json.dumps({"type":"tool_call", "subtype":"completed", "call_id":"private-id",
                  "tool_call":{"readToolCall":{"result":{"success":{"content":"SECRET_SOURCE"}}}}}))
print(json.dumps({"type":"result", "result":"SECRET_RESULT"}))
print("SECRET_STDERR", file=sys.stderr)
sys.exit(int(sys.argv[1]))
''')
            result = subprocess.run([sys.executable, str(SCRIPT), "--", sys.executable, str(child), str(exit_code)],
                                    cwd=root, env=dict(os.environ, RUNNER_TEMP=temp, GITHUB_STEP_SUMMARY=str(summary)),
                                    input="unchanged input", text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, exit_code)
            self.assertEqual((root / "review.md").read_text(), "review unchanged: unchanged input")
            saved = result.stdout + result.stderr
            for p in (target, summary):
                if p.is_file():
                    saved += p.read_text()
            self.assertNotIn("SECRET_", saved)
            if not blocked_report:
                self.assertIn("Cursor review performance", summary.read_text())
                self.assertEqual(json.loads(target.read_text())["agent_exit_code"], exit_code)

    def test_review_and_input_are_preserved_without_content_logging(self):
        self.run_child()

    def test_agent_failure_remains_failure(self):
        self.run_child(exit_code=7)

    def test_report_write_failure_does_not_fail_review(self):
        self.run_child(blocked_report=True)

    def test_secret_model_value_is_rejected(self):
        with patch.dict(os.environ, {"CURSOR_API_KEY": "secret-value"}):
            m = module.Metrics()
            m.event({"type": "system", "subtype": "init", "model": "secret-value"}, 1)
            self.assertIsNone(m.model)


if __name__ == "__main__":
    unittest.main()
