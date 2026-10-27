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
