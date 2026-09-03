#!/usr/bin/env bash
# Fail when a pull request adds CJK characters to file contents.
# Scans added diff lines only; commit messages and PR bodies are out of scope.
# Required env: BASE_SHA, HEAD_SHA
set -euo pipefail

: "${BASE_SHA:?}"
: "${HEAD_SHA:?}"

# Compare against the merge base so commits that only landed on the base branch
# are not attributed to this pull request.
if ! merge_base="$(git merge-base "${BASE_SHA}" "${HEAD_SHA}")"; then
  echo "::error::Could not compute a merge base for ${BASE_SHA}..${HEAD_SHA} (checkout needs fetch-depth: 0)"
  exit 1
fi

diff_file="$(mktemp)"
trap 'rm -f "${diff_file}"' EXIT
git diff --no-color --no-ext-diff --unified=0 --diff-filter=ACMR \
  "${merge_base}" "${HEAD_SHA}" > "${diff_file}"

DIFF_FILE="${diff_file}" python3 - <<'PY'
import os
import re
import sys

# CJK symbols and punctuation, kana, CJK ext A, CJK unified, Hangul,
# compatibility ideographs, halfwidth and fullwidth forms.
# U+2014 (em dash) and U+2026 (ellipsis) are deliberately absent: both are valid
# English punctuation.
RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)

HUNK = re.compile(r"^@@ -\S+ \+(\d+)")


def offenders(text):
    seen = []
    for ch in text:
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in RANGES) and ch not in seen:
            seen.append(ch)
    return seen


path = None
lineno = 0
violations = []

with open(os.environ["DIFF_FILE"], encoding="utf-8", errors="replace") as handle:
    for raw in handle:
        line = raw.rstrip("\n")
        if line.startswith("+++ "):
            target = line[4:]
            if target == "/dev/null":
                path = None
            elif target.startswith("b/"):
                path = target[2:]
            else:
                path = target
            continue
        if line.startswith("--- ") or line.startswith("diff --git "):
            continue
        match = HUNK.match(line)
        if match:
            lineno = int(match.group(1))
            continue
        if line.startswith("+"):
            content = line[1:]
            found = offenders(content)
            if found and path is not None:
                violations.append((path, lineno, "".join(found), content.strip()))
            lineno += 1

for path, lineno, chars, content in violations:
    codepoints = " ".join("U+%04X" % ord(ch) for ch in chars)
    print(
        "::error file=%s,line=%d::Non-English characters (%s) in an added line. "
        "This repository is English only; see AGENTS.md. Line: %s"
        % (path, lineno, codepoints, content[:160])
    )

if violations:
    files = len({item[0] for item in violations})
    print("::error::English-only check failed: %d added line(s) across %d file(s)."
          % (len(violations), files))
    sys.exit(1)

print("English-only check passed: no CJK characters in added lines.")
PY
