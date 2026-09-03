#!/usr/bin/env bash
# Scrub untrusted rules, build prompt, run Cursor agent; copy review.md to RUNNER_TEMP.
# Required env: CURSOR_API_KEY, CURSOR_MODEL, PR_NUMBER, BASE_REF, BASE_SHA, HEAD_SHA,
#   GITHUB_REPOSITORY, REVIEW_CONTEXT_MAX, RUNNER_TEMP, SCRIPTS_DIR
# Optional: REVIEW_CONTEXT, BASE_REVIEW_CONTEXT_FILE, REVIEW_ROUND,
#   PREV_REVIEW_FILE, HONOR_PR_BODY_DECISIONS
# GH_TOKEN / GITHUB_TOKEN should be empty for the agent step.
set -euo pipefail

: "${CURSOR_API_KEY:?}"
: "${CURSOR_MODEL:?}"
: "${PR_NUMBER:?}"
: "${BASE_REF:?}"
: "${BASE_SHA:?}"
: "${HEAD_SHA:?}"
: "${GITHUB_REPOSITORY:?}"
: "${REVIEW_CONTEXT:=}"
: "${REVIEW_CONTEXT_MAX:?}"
: "${BASE_REVIEW_CONTEXT_FILE:=}"
: "${RUNNER_TEMP:?}"
: "${SCRIPTS_DIR:?}"
: "${REVIEW_ROUND:=1}"
: "${PREV_REVIEW_FILE:=}"
: "${HONOR_PR_BODY_DECISIONS:=false}"

CCR_ASSETS_DIR="${CCR_ASSETS_DIR:-${SCRIPTS_DIR}/assets}"

# Stay aligned with the Install step: prefer the binary that was just installed and
# optionally verified, so a same-named agent elsewhere on PATH cannot shadow it.
if [ -x "$HOME/.local/bin/cursor-agent" ]; then
  AGENT_BIN="$HOME/.local/bin/cursor-agent"
elif [ -x "$HOME/.local/bin/agent" ]; then
  AGENT_BIN="$HOME/.local/bin/agent"
else
  echo "::error::Neither $HOME/.local/bin/cursor-agent nor $HOME/.local/bin/agent was found"
  exit 1
fi

# Rule and instruction files inside head are untrusted too, yet the CLI would load
# them automatically as "trusted rules" and bypass the nonce delimiters below.
# Remove them before running the agent and keep only the cli.json that CI writes.
# Equivalent architectural guidance belongs in review-context.md on base (the
# trusted channel).
rm -rf .cursor .cursorrules AGENTS.md
# head may commit review.md as a symlink so writes and reads follow the link to a
# sensitive path.
rm -rf review.md

# Ephemeral CI permissions (never committed): the main constraint is the allow
# list (Read limited by extension + Write(review.md) + read-only search Shell)
# together with agent -p --trust.
# Search uses the official restricted CI mode: Shell(rg|grep|find|ls); the CLI has
# no separate Grep/Glob permission token.
# CURSOR_API_KEY must be present in the agent environment (CLI auth). Shell matches
# on the first token and still accepts arguments, which is a known residual risk
# (see review-context.md) - do not keep reporting it as a fixable finding.
# deny is only defense in depth: anything not matched by allow is already
# unreadable, so do not react to a new credential type by adding filenames to deny.
# In a project-level cli.json only permissions take effect; approvalMode is a
# global setting and has no effect here.
mkdir -p .cursor
cp "${CCR_ASSETS_DIR}/cli.json" .cursor/cli.json

# The review context is a trusted section: triggering the dispatch input requires
# write access, and the base file has to be merged into the base branch, so both
# carry the same trust level as the built-in defaults.
# It is still length-capped so an oversized text cannot crowd out the prompt or
# drown the trusted constraints above.
# REVIEW_CONTEXT_MAX comes from the job env; the base side was already truncated in
# the Resolve step.
if [ -n "${REVIEW_CONTEXT}" ]; then
  echo "::notice::review_context_source=input"
  review_priorities="${REVIEW_CONTEXT:0:${REVIEW_CONTEXT_MAX}}"
elif [ -f "${BASE_REVIEW_CONTEXT_FILE}" ] && [ -s "${BASE_REVIEW_CONTEXT_FILE}" ]; then
  echo "::notice::review_context_source=file_base"
  review_priorities="$(head -c "${REVIEW_CONTEXT_MAX}" "${BASE_REVIEW_CONTEXT_FILE}")"
else
  echo "::notice::review_context_source=default"
  # Generic embedded default: even when base has none yet or the fetch failed, the
  # prompt still carries invariants, residual risks and scope, which keeps known
  # residual risks from being reported as noise.
  review_priorities="$(head -c "${REVIEW_CONTEXT_MAX}" "${CCR_ASSETS_DIR}/default-review-context.md")"
fi

# One-time nonce so a PR title/body/diff cannot forge a fixed delimiter and break out.
untrusted_nonce="$(openssl rand -hex 12)"

cp "${CCR_ASSETS_DIR}/review-prompt.txt" /tmp/review-prompt.txt

# The PR body is untrusted and author-controlled, so letting it suppress findings
# would hand the author a mute switch. It is therefore off by default and the rule
# is only injected when the caller explicitly passes honor_pr_body_decisions: true
# (acceptable for self-owned repos: this check is advisory, never a required check,
# and forks are already skipped in the caller).
if [ "${HONOR_PR_BODY_DECISIONS}" = "true" ]; then
  echo "::notice::honor_pr_body_decisions=true"
  pr_body_decisions_rule='- The PR body may contain a `## Review decisions` section recording how the
  maintainer already dispositioned earlier findings. Items marked `skip` there
  were considered and deliberately declined: do NOT report them again unless the
  current diff shows the stated reason no longer holds. If you disagree with a
  recorded decision, say so once, as a single P3 line in Residual risks.
'
else
  pr_body_decisions_rule=''
fi

REVIEW_PRIORITIES="$review_priorities" UNTRUSTED_NONCE="$untrusted_nonce" \
REVIEW_ROUND="$REVIEW_ROUND" PR_BODY_DECISIONS_RULE="$pr_body_decisions_rule" python3 - <<'PY'
import os
from pathlib import Path
prompt_path = Path("/tmp/review-prompt.txt")
text = prompt_path.read_text()
# Inject the nonce and the other CI-owned placeholders first and priorities last:
# that keeps a literal __UNTRUSTED_NONCE__ inside the review context from being
# replaced with the real nonce and forging a delimiter, and likewise keeps it from
# forging the round or injecting the PR body rule.
text = text.replace("__UNTRUSTED_NONCE__", os.environ["UNTRUSTED_NONCE"])
text = text.replace("__REVIEW_ROUND__", os.environ["REVIEW_ROUND"])
text = text.replace("__PR_BODY_DECISIONS_RULE__", os.environ["PR_BODY_DECISIONS_RULE"])
text = text.replace("__REVIEW_PRIORITIES__", os.environ["REVIEW_PRIORITIES"])
prompt_path.write_text(text)
PY

{
  echo
  echo "Context:"
  echo "- Repo: ${GITHUB_REPOSITORY}"
  echo "- PR Number: ${PR_NUMBER}"
  echo "- Base: ${BASE_REF} @ ${BASE_SHA}"
  echo "- Head SHA: ${HEAD_SHA}"
  echo "- Round: ${REVIEW_ROUND}"
  echo
  echo "--- BEGIN UNTRUSTED PR DATA ${untrusted_nonce} ---"
  echo "PR title:"
  cat /tmp/pr-title.txt
  echo
  echo "PR body:"
  cat /tmp/pr-body.txt
  echo
  echo "PR diff:"
  cat /tmp/pr.diff
  echo
  echo "--- END UNTRUSTED PR DATA ${untrusted_nonce} ---"
} >> /tmp/review-prompt.txt

# Previous review: same nonce delimiters, treated as untrusted just the same
# (anyone with write access can edit that comment).
# Missing means this is the first round, so append no block at all.
if [ -n "${PREV_REVIEW_FILE}" ] && [ -f "${PREV_REVIEW_FILE}" ] && [ -s "${PREV_REVIEW_FILE}" ]; then
  {
    echo
    echo "--- BEGIN PREVIOUS REVIEW ${untrusted_nonce} ---"
    cat "${PREV_REVIEW_FILE}"
    echo
    echo "--- END PREVIOUS REVIEW ${untrusted_nonce} ---"
  } >> /tmp/review-prompt.txt
  echo "::notice::prev_review_injected=true"
else
  echo "::notice::prev_review_injected=false"
fi

# Feed the prompt on stdin so a huge argv cannot hit ARG_MAX.
# --trust only means the workspace is trusted and the trust prompt is skipped; the
# allow/deny rules above still apply.
# Never add --force / --yolo: that degrades into "allow everything except deny" and
# defeats the allowlist.
agent_log="${RUNNER_TEMP}/agent-stdout.log"
set +e
"$AGENT_BIN" -p --trust --output-format text \
  --model "$CURSOR_MODEL" \
  < /tmp/review-prompt.txt 2>&1 | tee "${agent_log}"
agent_ec=${PIPESTATUS[0]}
set -e

if [ "${agent_ec}" -ne 0 ]; then
  echo "::error::cursor agent exited with ${agent_ec} (see the agent-stdout.log artifact)"
  exit "${agent_ec}"
fi

# Verify the artifact is a regular non-empty file (not a symlink/directory/empty
# file) before moving it out of the untrusted worktree; the later Scrub / Upload /
# Post steps only touch the copy in RUNNER_TEMP.
if [ -L review.md ]; then
  echo "::error::The review.md produced by the agent is a symlink (rejected; see the agent-stdout.log artifact)"
  exit 1
fi
if [ ! -e review.md ]; then
  echo "::error::The agent did not produce review.md (file missing; see the agent-stdout.log artifact)"
  exit 1
fi
if [ ! -f review.md ]; then
  echo "::error::The agent's review.md is not a regular file (see the agent-stdout.log artifact)"
  exit 1
fi
if [ ! -s review.md ]; then
  echo "::error::The review.md produced by the agent is empty (see the agent-stdout.log artifact)"
  exit 1
fi
cp review.md "${RUNNER_TEMP}/cursor-review.md"
