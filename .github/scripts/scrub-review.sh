#!/usr/bin/env bash
# Deterministic scrub of review.md before upload/post (literal secrets fail-closed; common shapes redacted).
# Required env: RUNNER_TEMP
# Optional: CURSOR_API_KEY, RUNNER_GH_TOKEN
set -euo pipefail

: "${RUNNER_TEMP:?}"
: "${CURSOR_API_KEY:=}"
: "${RUNNER_GH_TOKEN:=}"

review_file="${RUNNER_TEMP}/cursor-review.md"
for secret in "${CURSOR_API_KEY}" "${RUNNER_GH_TOKEN}"; do
  if [ -n "${secret}" ] && grep -qF -- "${secret}" "${review_file}"; then
    echo "::error::review.md contains a literal secret; upload and post blocked"
    exit 1
  fi
done
if grep -qE -- '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----' "${review_file}"; then
  echo "::error::review.md contains a private key PEM marker; upload and post blocked"
  exit 1
fi
sed -E -i \
  -e 's/sk-[A-Za-z0-9_-]{16,}/[REDACTED]/g' \
  -e 's/gh[pousr]_[A-Za-z0-9]{20,}/[REDACTED]/g' \
  -e 's/github_pat_[A-Za-z0-9_]{20,}/[REDACTED]/g' \
  -e 's/\bkey_[A-Za-z0-9]{32,}\b/[REDACTED]/g' \
  -e 's/AKIA[0-9A-Z]{16}/[REDACTED]/g' \
  -e 's/(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp):\/\/[^[:space:]]+/[REDACTED_URI]/g' \
  "${review_file}"

# A GitHub issue comment body caps out around 64KB, and Post still prepends the
# marker and appends a footer. Overflowing makes the comment fail and the check
# go red, so truncate after scrubbing and leave a note.
REVIEW_MAX=61440 # 60 KiB
review_bytes="$(wc -c < "${review_file}" | tr -d ' ')"
if [ "${review_bytes}" -gt "${REVIEW_MAX}" ]; then
  head -c "${REVIEW_MAX}" "${review_file}" > "${review_file}.trunc"
  printf '\n\n[review truncated]\n' >> "${review_file}.trunc"
  mv "${review_file}.trunc" "${review_file}"
  echo "::warning::review.md exceeded ${REVIEW_MAX} bytes and was truncated so the comment can be posted"
fi
