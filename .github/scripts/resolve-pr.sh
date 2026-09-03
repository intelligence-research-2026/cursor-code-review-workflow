#!/usr/bin/env bash
# Resolve PR metadata, fork skip/fail, base review context, truncate untrusted inputs.
# Required env: PR_NUMBER, CALLER_EVENT, GH_TOKEN, GH_REPO, REVIEW_CONTEXT_FILE, REVIEW_CONTEXT_MAX
# Optional: GITHUB_OUTPUT, RUNNER_TEMP (defaults used in CI)
set -euo pipefail

: "${PR_NUMBER:?}"
: "${CALLER_EVENT:=}"
: "${GH_TOKEN:?}"
: "${GH_REPO:?}"
: "${REVIEW_CONTEXT_FILE:?}"
: "${REVIEW_CONTEXT_MAX:?}"
: "${GITHUB_OUTPUT:?}"
: "${RUNNER_TEMP:?}"

echo "skip=false" >> "$GITHUB_OUTPUT"
if [ -z "${PR_NUMBER}" ]; then
  echo "::error::Could not resolve the PR number"
  exit 1
fi
case "${PR_NUMBER}" in
  ''|*[!0-9]*)
    echo "::error::PR number must be digits only"
    exit 1
    ;;
esac
meta="$(gh pr view "$PR_NUMBER" --json baseRefName,headRefOid,baseRefOid,title,body,isCrossRepository,headRepository)"
if [ "$(echo "$meta" | jq -r .isCrossRepository)" = "true" ]; then
  head_repo="$(echo "$meta" | jq -r '.headRepository.nameWithOwner // "unknown"')"
  # Only skip when the caller event is explicitly pull_request (fork PRs are common
  # and cannot post comments). Everything else (workflow_dispatch / empty / unset)
  # is fail-closed so nobody can turn the check green by hand-picking a fork PR.
  if [ "${CALLER_EVENT}" = "pull_request" ]; then
    echo "skip=true" >> "$GITHUB_OUTPUT"
    echo "::notice::Skipping fork PR (head=${head_repo}): this workflow only supports same-repo heads, and GITHUB_TOKEN usually cannot post comments on forks."
    exit 0
  fi
  echo "::error::The requested PR comes from a fork and caller_event is not pull_request: unsupported, review not executed."
  exit 1
fi

# Branch names are user-controlled (they may contain %0A and friends), so write them
# with a delimiter to avoid clobbering earlier outputs such as skip.
# head_sha / base_sha are strict hex OIDs, so key=value is fine for them.
base_ref="$(echo "$meta" | jq -r .baseRefName)"
head_sha="$(echo "$meta" | jq -r .headRefOid)"
base_sha="$(echo "$meta" | jq -r .baseRefOid)"
delim="$(uuidgen)"
{
  echo "base_ref<<${delim}"
  printf '%s\n' "$base_ref"
  echo "${delim}"
  printf 'head_sha=%s\n' "$head_sha"
  printf 'base_sha=%s\n' "$base_sha"
} >> "$GITHUB_OUTPUT"

# Read the review context from the base branch only, so a PR author cannot tamper
# with the trusted prompt by editing a file on head.
# REVIEW_CONTEXT_FILE comes from an action input and is treated as untrusted:
# allowlist-validate it before requesting it.
# Write the content to RUNNER_TEMP rather than GITHUB_OUTPUT, which would decode
# escapes such as %0A / %25 and pollute the trusted text.
case "${REVIEW_CONTEXT_FILE}" in
  ''|*[!A-Za-z0-9._/-]*|*..*)
    echo "::error::review_context_file is invalid (only letters, digits, . _ / - are allowed, and .. is forbidden)"
    exit 1
    ;;
esac
api_err="${RUNNER_TEMP}/gh-contents.err"
set +e
base_review_context="$(gh api --method GET \
  -H "Accept: application/vnd.github.raw" \
  "repos/${GH_REPO}/contents/${REVIEW_CONTEXT_FILE}" \
  -f ref="${base_ref}" 2>"${api_err}")"
api_rc=$?
set -e
if [ "${api_rc}" -ne 0 ]; then
  if grep -Eqi 'HTTP[[:space:]]*404|Not Found \(HTTP 404\)' "${api_err}"; then
    echo "::notice::base_review_context=missing"
  else
    echo "::error::Failed to read the base review context (not a 404)"
    cat "${api_err}" >&2 || true
    exit 1
  fi
elif [ -n "${base_review_context}" ]; then
  printf '%s' "${base_review_context:0:${REVIEW_CONTEXT_MAX}}" \
    > "${RUNNER_TEMP}/base-review-context.md"
  echo "has_base_review_context=true" >> "$GITHUB_OUTPUT"
  echo "::notice::base_review_context=loaded"
fi
# Cap every untrusted text the same way: keeps the prompt small and avoids blowing
# up argv/ARG_MAX and the agent context. The truncation marker tells the agent
# about it inside the trusted constraints.
truncate_untrusted() {
  local path="$1" max="$2" label="$3"
  if [ "$(wc -c < "$path")" -gt "$max" ]; then
    echo "::warning::PR ${label} exceeded ${max} bytes and was truncated for the agent"
    head -c "$max" "$path" > "${path}.trunc"
    mv "${path}.trunc" "$path"
    printf '\n\n[%s truncated]\n' "$label" >> "$path"
  fi
}

echo "$meta" | jq -r .title > /tmp/pr-title.txt
echo "$meta" | jq -r '.body // ""' > /tmp/pr-body.txt
gh pr diff --allow-escape-sequences "$PR_NUMBER" > /tmp/pr.diff
truncate_untrusted /tmp/pr-title.txt 1000 title
truncate_untrusted /tmp/pr-body.txt 20000 body
truncate_untrusted /tmp/pr.diff 60000 diff
