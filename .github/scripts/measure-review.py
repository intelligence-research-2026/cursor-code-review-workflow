#!/usr/bin/env python3
"""Observe Cursor NDJSON without retaining prompts, source or tool output.

Event schema: https://cursor.com/docs/cli/reference/output-format
Times are local receipt times, not model inference times. Unknown schemas are
counted, not guessed. The child's exit status remains authoritative.
"""

import collections
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

MAX_RECORD = 1024 * 1024
MAX_CALLS = 10000


def model_label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9 ._:/()+-]{1,100}", value):
        return None
    secrets = [os.environ.get(k, "") for k in ("CURSOR_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")]
    if any(s and s in value for s in secrets) or re.search(r"gh[pousr]_|github_pat_|sk-", value):
        return None
    return value


def union_seconds(intervals):
    end = total = 0.0
    for left, right in sorted(intervals):
        total += max(0.0, right - max(left, end))
        end = max(end, right)
    return total


class Metrics:
    def __init__(self):
        self.key = os.urandom(32)
        self.first = None
        self.model = None
        self.invalid = self.oversized = self.unknown = self.unmatched = 0
        self.capped = False
        self.starts = {}
        self.completed = set()
        self.reads = collections.Counter()
        self.files = set()
        self.intervals = []
        self.tools = collections.defaultdict(lambda: {"started": 0, "completed": 0, "seconds": 0.0})
        self.read_bytes = self.empty_searches = 0
        self.terminal = False

    def digest(self, value):
        return hmac.new(self.key, json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def tool(self, event):
        call = event.get("tool_call", {})
        if not isinstance(call, dict) or len(call) != 1:
            return "other", {}, {}
        name, payload = next(iter(call.items()))
        if not isinstance(payload, dict):
            return "other", {}, {}
        args = payload.get("args", {})
        if name == "function":
            name = payload.get("name")
            args = payload.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
        kind = {
            "readToolCall": "read", "Read": "read", "read_file": "read",
            "writeToolCall": "write", "Write": "write",
            "grepToolCall": "search", "globToolCall": "search",
            "Grep": "search", "Glob": "search", "shellToolCall": "shell",
        }.get(name, "other")
        return kind, args if isinstance(args, dict) else {}, payload.get("result", {})

    def event(self, event, now):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self.invalid += 1
            return
        if self.first is None:
            self.first = now
        event_type = event["type"]
        if event_type == "system" and event.get("subtype") == "init":
            self.model = model_label(event.get("model"))
            return
        if event_type == "result":
            self.terminal = True
            return
        if event_type in ("user", "assistant", "system"):
            return  # Never persist text, including the terminal result text.
        if event_type != "tool_call":
            self.unknown += 1
            return
        raw_id = event.get("call_id")
        if not isinstance(raw_id, str):
            self.invalid += 1
            return
        call_id = self.digest(raw_id)
        kind, args, result = self.tool(event)
        if event.get("subtype") == "started":
            if call_id in self.starts or call_id in self.completed:
                self.invalid += 1
                return
            if len(self.starts) + len(self.completed) >= MAX_CALLS:
                self.capped = True
                return
            self.starts[call_id] = (now, kind)
            self.tools[kind]["started"] += 1
            if kind == "read" and isinstance(args.get("path"), str):
                # Normalize spelling only; never open or resolve the target.
                normalized = dict(args, path=os.path.abspath(args["path"]))
                self.reads[self.digest(normalized)] += 1
                self.files.add(self.digest(normalized["path"]))
        elif event.get("subtype") == "completed":
            if call_id in self.completed:
                self.invalid += 1
                return
            start = self.starts.pop(call_id, None)
            if start is None:
                self.unmatched += 1
                return
            self.completed.add(call_id)
            began, kind = start
            self.intervals.append((began, now))
            self.tools[kind]["completed"] += 1
            self.tools[kind]["seconds"] += max(0, now - began)
            success = result.get("success", {}) if isinstance(result, dict) else {}
            if isinstance(success, dict):
                if kind == "read" and isinstance(success.get("content"), str):
                    self.read_bytes += len(success["content"].encode("utf-8"))
                if kind == "search" and any(type(success.get(k)) is int and success[k] == 0
                                            for k in ("numMatches", "matchCount")):
                    self.empty_searches += 1
        else:
            self.unknown += 1

    def report(self, elapsed, code):
        busy = union_seconds(self.intervals)
        return {
            "schema": 1, "elapsed_seconds": round(elapsed, 3), "agent_exit_code": code,
            "revisions": {key: value for key in ("BASE_SHA", "HEAD_SHA")
                          if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value := os.environ.get(key, ""))},
            "configured_model": model_label(os.environ.get("CURSOR_MODEL")),
            "first_event_seconds": None if self.first is None else round(self.first, 3),
            "reported_model": self.model, "tool_union_seconds": round(busy, 3),
            "unclassified_seconds": round(max(0, elapsed - busy), 3),
            "tools": {k: dict(v, seconds=round(v["seconds"], 3)) for k, v in self.tools.items()},
            "unique_read_files": len(self.files),
            "repeated_read_requests": sum(n - 1 for n in self.reads.values()),
            "observed_read_content_bytes": self.read_bytes,
            "explicit_zero_match_searches": self.empty_searches,
            "invalid_records": self.invalid, "oversized_records": self.oversized,
            "unknown_events": self.unknown, "unmatched_completions": self.unmatched,
            "unfinished_calls": len(self.starts), "tracking_limit_reached": self.capped,
            "terminal_event_seen": self.terminal,
        }


def observe(stream, metrics, started):
    """Bound parsing memory and always drain after malformed/oversized records."""
    while True:
        line = stream.readline(MAX_RECORD + 1)
        if not line:
            return
        if len(line) > MAX_RECORD:
            metrics.oversized += 1
            while line and not line.endswith(b"\n"):
                line = stream.readline(MAX_RECORD + 1)
            continue
        try:
            metrics.event(json.loads(line), time.monotonic() - started)
        except Exception:
            metrics.invalid += 1  # Do not log the offending event or exception.


def save_report(report):
    target = Path(os.environ["RUNNER_TEMP"]) / "cursor-review-performance.json"
    # Exclusive creation prevents following an unexpected existing symlink.
    with target.open("x", encoding="utf-8") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        rows = ["### Cursor review performance", "",
                "Timing uses event receipt times. Unclassified time is not inference time.", "",
                f"- Agent elapsed: {report['elapsed_seconds']} s",
                f"- First event: {report['first_event_seconds']} s",
                f"- CLI-reported model: {report['reported_model'] or 'unavailable'}",
                f"- Configured model: {report['configured_model'] or 'unavailable'}",
                f"- Completed tool intervals (union): {report['tool_union_seconds']} s",
                f"- Unclassified: {report['unclassified_seconds']} s",
                f"- Repeated read requests: {report['repeated_read_requests']} (not necessarily redundant)",
                f"- Observed read content: {report['observed_read_content_bytes']} bytes",
                f"- Explicit zero-match searches: {report['explicit_zero_match_searches']}", "",
                "| Tool | Started | Completed | Sum of durations (s) |",
                "| --- | ---: | ---: | ---: |"]
        for kind, stats in sorted(report["tools"].items()):
            rows.append(f"| {kind} | {stats['started']} | {stats['completed']} | {stats['seconds']} |")
        rows += ["", "Parallel tool durations can overlap; use the union for wall time.",
                 "Counts are observations, not proof of review coverage or wasted work.",
                 f"Record gaps: invalid={report['invalid_records']}, oversized={report['oversized_records']}, "
                 f"unknown={report['unknown_events']}, unfinished={report['unfinished_calls']}, "
                 f"unmatched={report['unmatched_completions']}, capped={report['tracking_limit_reached']}, "
                 f"terminal_seen={report['terminal_event_seen']}.", ""]
        rows += [f"- {key}: {value}" for key, value in sorted(report["revisions"].items())]
        rows.append("")
        with open(summary, "a", encoding="utf-8") as out:
            out.write("\n".join(rows))


def main(command):
    metrics = Metrics()
    started = time.monotonic()
    child = subprocess.Popen(command, stdin=sys.stdin.buffer, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True)

    def forward(signum, _frame):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    try:
        observe(child.stdout, metrics, started)
    except Exception:
        # Even an unexpected collector error must not close the child's pipe.
        metrics.invalid += 1
        while child.stdout.read(65536):
            pass
    code = child.wait()
    code = code if code >= 0 else 128 - code
    try:
        save_report(metrics.report(time.monotonic() - started, code))
    except Exception:
        print("::warning::Review performance report unavailable; agent status is preserved")
    print(f"::notice::review_agent_exit_code={code}")
    return code


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[2:]))  # Usage: measure-review.py -- command args...
