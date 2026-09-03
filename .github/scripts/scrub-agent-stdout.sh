#!/usr/bin/env bash
# Scrub agent-stdout.log before failure artifact upload (literal secrets skip upload; common shapes redacted).
# Required env: RUNNER_TEMP, GITHUB_OUTPUT
# Optional: CURSOR_API_KEY, RUNNER_GH_TOKEN
# Outputs: upload=true|false
set -euo pipefail

: "${RUNNER_TEMP:?}"
: "${GITHUB_OUTPUT:?}"
: "${CURSOR_API_KEY:=}"
: "${RUNNER_GH_TOKEN:=}"

log_file="${RUNNER_TEMP}/agent-stdout.log"
set_upload() {
  echo "upload=$1" >> "${GITHUB_OUTPUT}"
}

if [ ! -f "${log_file}" ] || [ ! -s "${log_file}" ]; then
  echo "::notice::agent-stdout.log is missing or empty; skipping upload"
  set_upload false
  exit 0
fi

for secret in "${CURSOR_API_KEY}" "${RUNNER_GH_TOKEN}"; do
  if [ -n "${secret}" ] && grep -qF -- "${secret}" "${log_file}"; then
    echo "::error::agent-stdout.log contains a literal secret; upload skipped (fail-closed)"
    rm -f "${log_file}"
    set_upload false
    exit 0
  fi
done
if grep -qE -- '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----' "${log_file}"; then
  echo "::error::agent-stdout.log contains a private key PEM marker; upload skipped (fail-closed)"
  rm -f "${log_file}"
  set_upload false
  exit 0
fi

sed -E -i \
  -e 's/sk-[A-Za-z0-9_-]{16,}/[REDACTED]/g' \
  -e 's/gh[pousr]_[A-Za-z0-9]{20,}/[REDACTED]/g' \
  -e 's/github_pat_[A-Za-z0-9_]{20,}/[REDACTED]/g' \
  -e 's/\bkey_[A-Za-z0-9]{32,}\b/[REDACTED]/g' \
  -e 's/AKIA[0-9A-Z]{16}/[REDACTED]/g' \
  -e 's/(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp):\/\/[^[:space:]]+/[REDACTED_URI]/g' \
  "${log_file}"

set_upload true
