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
MAX_SHAPES = 32
MAX_FIELDS = 16
TOOL_KINDS = {
    "readToolCall": "read", "Read": "read", "read_file": "read",
    "writeToolCall": "write", "Write": "write",
    "grepToolCall": "search", "globToolCall": "search",
    "Grep": "search", "Glob": "search", "shellToolCall": "shell",
}


def schema_label(value):
    # Schema identifiers only, never a free-form text or path value.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
        return "redacted"
    return value if model_label(value) is not None else "redacted"


def cli_version(command):
    # The installer resolves the executable inside versions/<date>-<hash>.
    # Do not run an extra CLI invocation or persist its absolute path.
    try:
        name = Path(command[0]).resolve().parent.name
        return name if re.fullmatch(r"20\d{2}\.\d{2}\.\d{2}-[0-9a-f]{7,40}", name) else None
    except Exception:
        return None


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
        self.events = 0
        self.shapes = collections.Counter()
        self.diagnostic_overflow = 0
        self.version = None
        self.read_path_events = self.read_content_events = self.search_result_events = 0

    def diagnose(self, event):
        def fields(obj):
            return [schema_label(k) for k in list(obj)[:MAX_FIELDS]]

        shape = {"type": schema_label(event.get("type")),
                 "subtype": schema_label(event["subtype"]) if "subtype" in event else None,
                 "fields": fields(event), "timestamp_fields": {}}
        for key in ("timestamp", "timestamp_ms"):
            if key in event:
                value = event[key]
                shape["timestamp_fields"][key] = "number" if type(value) in (int, float) else "non_numeric"
        call = event.get("tool_call")
        if isinstance(call, dict):
            shape["tool_fields"] = fields(call)
            shape["payload_fields"] = {}
            shape["name_fields"] = {}
            for key in ("name", "tool_name"):
                if key in call:
                    shape["name_fields"][f"tool_call.{key}"] = schema_label(call[key])
            for key, payload in list(call.items())[:MAX_FIELDS]:
                # Never traverse argument/result/message dictionaries. Inspect
                # only immediate structural fields of tool-specific containers.
                if isinstance(payload, dict) and (key == "function" or re.fullmatch(r"[A-Za-z]+ToolCall", key)):
                    safe_key = schema_label(key)
                    shape["payload_fields"][safe_key] = fields(payload)
                    if key == "function" and "name" in payload:
                        shape["name_fields"]["tool_call.function.name"] = schema_label(payload["name"])
        signature = json.dumps(shape, sort_keys=True)
        if signature in self.shapes or len(self.shapes) < MAX_SHAPES:
            self.shapes[signature] += 1
        else:
            self.diagnostic_overflow += 1

    def digest(self, value):
        return hmac.new(self.key, json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def tool(self, event):
        call = event.get("tool_call", {})
        if not isinstance(call, dict):
            return "other", {}, {}
        candidates = [key for key in call if key == "function" or key in TOOL_KINDS
                      or re.fullmatch(r"[A-Za-z]+ToolCall", key)]
        if len(candidates) != 1:
            return "other", {}, {}  # Multiple tool payloads are ambiguous.
        name = candidates[0]
        payload = call[name]
        if not isinstance(payload, dict):
            return "other", {}, {}
        args = payload.get("args", {})
        if name == "function":
            name = payload.get("name")
            args = payload.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
        kind = TOOL_KINDS.get(name, "other")
        return kind, args if isinstance(args, dict) else {}, payload.get("result", {})

    def event(self, event, now):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self.invalid += 1
            return
        if self.first is None:
            self.first = now
        self.events += 1
        self.diagnose(event)
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
                self.read_path_events += 1
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
                    self.read_content_events += 1
                    self.read_bytes += len(success["content"].encode("utf-8"))
                matches = next((success[k] for k in ("numMatches", "matchCount")
                                if type(success.get(k)) is int and success[k] >= 0), None)
                if kind == "search" and matches is not None:
                    self.search_result_events += 1
                    self.empty_searches += int(matches == 0)
        else:
            self.unknown += 1

    def report(self, elapsed, code):
        busy = union_seconds(self.intervals)
        total = sum(t["started"] for t in self.tools.values())
        classified = total - self.tools.get("other", {}).get("started", 0)
        complete = (self.terminal and classified == total and not any((self.invalid,
                    self.oversized, self.unknown, self.unmatched, self.capped, self.starts)))
        read_started = self.tools.get("read", {}).get("started", 0)
        search_started = self.tools.get("search", {}).get("started", 0)
        reads_available = complete and self.read_path_events == read_started
        return {
            "schema": 2, "elapsed_seconds": round(elapsed, 3), "agent_exit_code": code,
            "cli_version": self.version,
            "revisions": {key: value for key in ("BASE_SHA", "HEAD_SHA")
                          if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value := os.environ.get(key, ""))},
            "configured_model": model_label(os.environ.get("CURSOR_MODEL")),
            "first_event_seconds": None if self.first is None else round(self.first, 3),
            "reported_model": self.model, "tool_union_seconds": round(busy, 3),
            "unclassified_seconds": round(max(0, elapsed - busy), 3),
            "tools": {k: dict(v, seconds=round(v["seconds"], 3)) for k, v in self.tools.items()},
            "classified_tool_calls": classified, "observed_tool_calls": total,
            "tool_classification_ratio": round(classified / total, 4) if total else None,
            "observed_events": self.events,
            "unknown_event_ratio": round(self.unknown / self.events, 4) if self.events else None,
            "classification_complete": complete,
            "unique_read_files": len(self.files) if reads_available else None,
            "repeated_read_requests": sum(n - 1 for n in self.reads.values()) if reads_available else None,
            "observed_read_content_bytes": self.read_bytes if self.read_content_events or (complete and not read_started) else None,
            "explicit_zero_match_searches": self.empty_searches if self.search_result_events or (complete and not search_started) else None,
            "read_calls_with_path": self.read_path_events,
            "read_completions_with_content": self.read_content_events,
            "search_completions_with_match_count": self.search_result_events,
            "event_shapes": [dict(json.loads(shape), count=count) for shape, count in self.shapes.items()],
            "diagnostic_overflow_events": self.diagnostic_overflow,
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
        def display(key):
            return "unavailable" if report[key] is None else str(report[key])
        rows = ["### Cursor review performance", "",
                "Timing uses event receipt times. Unclassified time is not inference time.", "",
                f"- Agent elapsed: {report['elapsed_seconds']} s",
                f"- First event: {report['first_event_seconds']} s",
                f"- CLI-reported model: {report['reported_model'] or 'unavailable'}",
                f"- Configured model: {report['configured_model'] or 'unavailable'}",
                f"- CLI version: {report['cli_version'] or 'unavailable'}",
                f"- Classified tools: {report['classified_tool_calls']}/{report['observed_tool_calls']}",
                f"- Unknown events: {report['unknown_events']}/{report['observed_events']}",
                f"- Completed tool intervals (union): {report['tool_union_seconds']} s",
                f"- Unclassified: {report['unclassified_seconds']} s",
                f"- Repeated read requests: {display('repeated_read_requests')} (not necessarily redundant)",
                f"- Observed read content bytes: {display('observed_read_content_bytes')}",
                f"- Explicit zero-match searches: {display('explicit_zero_match_searches')}", "",
                "| Tool | Started | Completed | Sum of durations (s) |",
                "| --- | ---: | ---: | ---: |"]
        for kind, stats in sorted(report["tools"].items()):
            rows.append(f"| {kind} | {stats['started']} | {stats['completed']} | {stats['seconds']} |")
        rows += ["", "Parallel tool durations can overlap; use the union for wall time.",
                 "Counts are observations, not proof of review coverage or wasted work.",
                "Unavailable means the event schema or coverage cannot establish a value; it does not mean zero.",
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
    metrics.version = cli_version(command)
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
