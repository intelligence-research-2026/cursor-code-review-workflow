#!/usr/bin/env python3
"""Extract, validate and strip the machine-readable findings block from review.md.

The agent ends review.md with one fenced block tagged `cursor-review-findings`.
This deterministic step removes it from the visible review, validates it, and
on success writes the single HTML comment line that post-review-comment.sh embeds.

Content problems never fail the step: the review is still posted, the block is
simply not embedded, and the footer records `findings=invalid:<reason>`. Only
internal errors (missing review file, unwritable outputs) exit non-zero.

Required env: RUNNER_TEMP
Optional env: GITHUB_OUTPUT, REVIEW_MAX (defaults to 61440 bytes)
Inputs:  $RUNNER_TEMP/cursor-review.md, $RUNNER_TEMP/pr-files.txt,
         $RUNNER_TEMP/prev-findings.json
Outputs: $RUNNER_TEMP/cursor-review.md (block and look-alike embed lines removed, size-capped),
         $RUNNER_TEMP/cursor-review-raw.md (scrubbed original, for the artifact),
         $RUNNER_TEMP/cursor-findings.json and cursor-findings.line (only for v1),
         step output status=v1|absent|invalid:<reason>
"""

import base64
import json
import os
import re
import sys
from pathlib import Path

INFO_STRING = "cursor-review-findings"
MAX_JSON_BYTES = 8192
MAX_TITLE_CHARS = 200
VERDICTS = {"approve", "comment", "request_changes"}
STATUSES = {"open", "fixed", "withdrawn", "superseded"}
SEVERITIES = {"P0", "P1", "P2"}
CONFIDENCES = {"high", "medium"}
FIELDS = {"id", "status", "severity", "confidence", "path", "line", "title"}

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)\s*$")
VERDICT_RE = re.compile(r"^\s*[-*]*\s*\**\s*Verdict\**\s*:\s*(.*)$")
# Any whole line shaped like the embed. Only this step may produce that line, so
# the agent's copies are dropped; inline mentions inside prose are left alone.
# post-review-comment.sh applies the same pattern; keep the two in sync.
FORGED_EMBED_RE = re.compile(r"^\s*<!--\s*cursor-cli-findings\b.*-->\s*$")


class Invalid(Exception):
    """A content problem; the message is the reason code."""


def split_blocks(text):
    """Return (visible lines, list of block bodies) for every tagged fence."""
    lines = text.splitlines()
    visible, blocks = [], []
    i = 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if m and m.group(2) == INFO_STRING:
            fence = m.group(1)
            body = []
            i += 1
            closed = False
            while i < len(lines):
                stripped = lines[i].strip()
                if stripped and stripped[0] == fence[0] and set(stripped) == {fence[0]} \
                        and len(stripped) >= len(fence):
                    closed = True
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            blocks.append("\n".join(body) if closed else None)
            continue
        visible.append(lines[i])
        i += 1
    while visible and not visible[-1].strip():
        visible.pop()
    return visible, blocks


def normalize_verdict(line_value):
    value = re.split(r"[(\uff08]", line_value, maxsplit=1)[0]
    value = re.sub(r"[^a-z ]", " ", value.lower())
    value = " ".join(value.split())
    if value in ("approve", "approved"):
        return "approve"
    if value == "comment":
        return "comment"
    if value in ("request changes", "request change", "changes requested"):
        return "request_changes"
    return None


def visible_verdict(lines):
    for line in lines:
        m = VERDICT_RE.match(line)
        if m:
            return normalize_verdict(m.group(1))
    return None


def valid_path(path):
    return (
        isinstance(path, str)
        and path != ""
        and len(path) <= 1024
        and "\n" not in path
        and not path.startswith("/")
        and ".." not in path.split("/")
    )


def parse_findings(body, verdict_line, changed_files):
    if body is None:
        raise Invalid("json")
    try:
        doc = json.loads(body)
    except ValueError:
        raise Invalid("json")
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise Invalid("schema")
    verdict = doc.get("verdict")
    findings = doc.get("findings")
    if verdict not in VERDICTS or not isinstance(findings, list) or len(findings) > 50:
        raise Invalid("schema")
    if verdict_line != verdict:
        raise Invalid("verdict_mismatch")

    seen = set()
    normalized = []
    for item in findings:
        if not isinstance(item, dict) or not set(item) <= FIELDS:
            raise Invalid("schema")
        fid = item.get("id")
        line = item.get("line")
        title = item.get("title")
        if (
            not isinstance(fid, int) or isinstance(fid, bool) or fid < 1 or fid in seen
            or item.get("status") not in STATUSES
            or item.get("severity") not in SEVERITIES
            or item.get("confidence") not in CONFIDENCES
            or not valid_path(item.get("path"))
            or not (line is None or (isinstance(line, int) and not isinstance(line, bool) and line >= 1))
            or not isinstance(title, str) or not title.strip()
            or len(title) > MAX_TITLE_CHARS or "\n" in title
        ):
            raise Invalid("schema")
        seen.add(fid)
        normalized.append({
            "id": fid,
            "status": item["status"],
            "severity": item["severity"],
            "confidence": item["confidence"],
            "path": item["path"],
            "line": line,
            "title": title.strip(),
        })

    normalized.sort(key=lambda f: f["id"])
    open_items = [f for f in normalized if f["status"] == "open"]
    if verdict == "approve" and any(f["severity"] in ("P0", "P1") for f in open_items):
        raise Invalid("approve_with_blocking")
    if changed_files is not None and any(f["path"] not in changed_files for f in open_items):
        raise Invalid("path_not_in_diff")

    result = {"schema": 1, "verdict": verdict, "findings": normalized}
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise Invalid("too_large")
    return result, encoded


def read_changed_files(path):
    if not path.is_file():
        print("::warning::pr-files.txt is missing; open findings are not checked against the diff")
        return None
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def report_continuity(prev_path, result):
    if not prev_path.is_file():
        return
    try:
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
        prev_open = {f["id"] for f in prev.get("findings", []) if f.get("status") == "open"}
    except (ValueError, AttributeError, KeyError, TypeError):
        print("::notice::previous findings could not be parsed; continuity not checked")
        return
    current = {f["id"] for f in result["findings"]}
    missing = sorted(prev_open - current)
    if missing:
        print("::notice::previously open findings without a disposition: "
              + ",".join(str(i) for i in missing))


def cap_review(text, budget):
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    print("::warning::review.md exceeded %d bytes and was truncated so the comment can be posted"
          % budget)
    return data[:budget].decode("utf-8", errors="ignore") + "\n\n[review truncated]\n"


def main():
    temp = Path(os.environ["RUNNER_TEMP"])
    review_max = int(os.environ.get("REVIEW_MAX", "61440"))
    review_path = temp / "cursor-review.md"
    raw_path = temp / "cursor-review-raw.md"
    json_path = temp / "cursor-findings.json"
    line_path = temp / "cursor-findings.line"
    for stale in (json_path, line_path):
        if stale.exists():
            stale.unlink()

    raw = review_path.read_text(encoding="utf-8", errors="replace")
    raw_path.write_text(raw, encoding="utf-8")
    visible, blocks = split_blocks(raw)
    forged = sum(1 for line in visible if FORGED_EMBED_RE.match(line))
    if forged:
        print("::warning::dropped %d line(s) shaped like the embedded findings line" % forged)
        visible = [line for line in visible if not FORGED_EMBED_RE.match(line)]

    embed_line = ""
    if not blocks:
        status = "absent"
    elif len(blocks) > 1:
        status = "invalid:multiple_blocks"
    else:
        try:
            result, encoded = parse_findings(
                blocks[0], visible_verdict(visible), read_changed_files(temp / "pr-files.txt"))
        except Invalid as reason:
            status = "invalid:%s" % reason
        else:
            status = "v1"
            embed_line = "<!-- cursor-cli-findings v1 %s -->" % base64.b64encode(encoded).decode("ascii")
            json_path.write_bytes(encoded + b"\n")
            line_path.write_text(embed_line + "\n", encoding="ascii")
            report_continuity(temp / "prev-findings.json", result)

    budget = review_max - (len(embed_line) + 2 if embed_line else 0)
    review_path.write_text(cap_review("\n".join(visible) + "\n", budget), encoding="utf-8")

    if status.startswith("invalid:"):
        print("::warning::findings_status=%s (the block is not embedded)" % status)
    else:
        print("::notice::findings_status=%s" % status)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("status=%s\n" % status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
