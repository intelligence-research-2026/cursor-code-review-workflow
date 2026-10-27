#!/usr/bin/env python3
"""Stage pinned diffs and inert copies of files removed before CLI startup."""

import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


CHUNK_CHARS = 12000  # At most 48000 UTF-8 bytes; never split a code point.
PREVIEW_BYTES = 60000
SNAPSHOT_MAX = 2 * 1024 * 1024


def git(*args):
    return subprocess.run(
        ["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def readable(path, permissions):
    """Copies must not turn an otherwise denied file into an allowed .txt file."""
    def matches(rule):
        if not rule.startswith("Read(") or not rule.endswith(")"):
            return False
        pattern = rule[5:-1]
        return fnmatch.fnmatchcase(path, pattern) or (
            pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
        )

    return any(map(matches, permissions["allow"])) and not any(
        map(matches, permissions["deny"])
    )


def main():
    base, head = os.environ["BASE_SHA"], os.environ["HEAD_SHA"]
    if not all(re.fullmatch(r"[0-9a-fA-F]{40,64}", ref) for ref in (base, head)):
        raise ValueError("base/head must be full commit object IDs")
    workspace = Path.cwd()
    if git("rev-parse", "HEAD").decode().strip() != head:
        raise ValueError("checkout does not match the resolved PR head")
    assets = Path(os.environ.get("CCR_ASSETS_DIR", os.environ["SCRIPTS_DIR"] + "/assets"))
    permissions = json.loads((assets / "cli.json").read_text())["permissions"]
    # A fresh direct child cannot be a PR-supplied symlink. Generated names are
    # plain .txt files, never AGENTS.md, skills, CLI configs or executable rules.
    evidence = Path(tempfile.mkdtemp(prefix="ccr-review-data-", dir=workspace))
    manifest = {"base": base, "head": head, "files": [], "removed_head_files": []}

    def save_parts(stem, data):
        text = data.decode("utf-8", errors="replace")
        parts = []
        for offset in range(0, len(text), CHUNK_CHARS):
            name = f"{stem}.{len(parts) + 1:04d}.txt"
            (evidence / name).write_text(text[offset:offset + CHUNK_CHARS], encoding="utf-8")
            parts.append(name)
        return parts

    comparison = f"{base}...{head}"
    diff_args = ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")
    paths = git(*diff_args, "--name-only", "-z", comparison, "--").split(b"\0")
    paths = [os.fsdecode(path) for path in paths if path]
    preview = bytearray()
    diff_bytes = 0
    for number, path in enumerate(paths, 1):
        # Literal pathspecs also handle names starting with ':' or containing '*'.
        patch = git(*diff_args, comparison, "--", ":(literal)" + path)
        diff_bytes += len(patch)
        remaining = max(0, PREVIEW_BYTES - len(preview))
        coverage = "complete" if len(patch) <= remaining else ("partial" if remaining else "omitted")
        preview.extend(patch[:remaining])
        manifest["files"].append({
            "path": path, "inline_preview": coverage,
            "diff_parts": save_parts(f"diff-{number:04d}", patch)
        })

    entries = git("ls-tree", "-r", "-z", head, "--", ".cursor", ".cursorrules", "AGENTS.md")
    for number, entry in enumerate(filter(None, entries.split(b"\0")), 1):
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = os.fsdecode(raw_path)
        record = {"path": path}
        manifest["removed_head_files"].append(record)
        if mode not in ("100644", "100755") or kind != "blob":
            record["unavailable"] = "not a regular tracked file (symlinks are not followed)"
        elif not readable(path, permissions):
            record["unavailable"] = "excluded by the existing Read permissions"
        elif int(git("cat-file", "-s", oid).decode()) > SNAPSHOT_MAX:
            record["unavailable"] = "head snapshot exceeds 2 MiB; use the diff parts"
        else:
            data = git("cat-file", "blob", oid)
            if b"\0" in data:
                record["unavailable"] = "binary head content; use the diff summary"
            else:
                record["head_parts"] = save_parts(f"head-{number:04d}", data)

    index = evidence / "index.txt"
    index.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    preview_text = bytes(preview).decode("utf-8", errors="replace")
    if diff_bytes > PREVIEW_BYTES:
        preview_text += "\n\n[diff truncated: preview only; complete diff parts are in the local evidence index]\n"
    Path("/tmp/pr.diff").write_text(preview_text, encoding="utf-8")
    (Path(os.environ["RUNNER_TEMP"]) / "pr-files.txt").write_text(
        "\n".join(paths) + "\n", encoding="utf-8"
    )
    print(f"::notice::review_inputs files={len(paths)} diff_bytes={diff_bytes} "
          f"preview_truncated={str(diff_bytes > PREVIEW_BYTES).lower()}", file=sys.stderr)
    print(index.relative_to(workspace))


if __name__ == "__main__":
    main()
