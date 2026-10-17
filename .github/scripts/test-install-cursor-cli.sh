#!/usr/bin/env bash
# Offline test: install-cursor-cli.sh fetches the current installer and does not
# checksum it. A caller-supplied installer SHA is ignored. agent_sha256 still
# verifies the CLI binary when it is set.
#
# Usage (from the repository root): bash .github/scripts/test-install-cursor-cli.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="${ROOT}/.github/scripts/install-cursor-cli.sh"
INSTALL_URL="https://cursor.com/install"

pass=0
fail=0
chk() {
  if [ "$2" = "$3" ]; then
    echo "PASS $1"
    pass=$((pass + 1))
  else
    echo "FAIL $1: got [$2] want [$3]"
    fail=$((fail + 1))
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}" /tmp/cursor-install.sh' EXIT

mkdir -p "${TMP}/bin"
cat > "${TMP}/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${CCR_CURL_LOG:?}"
if [ "$1" != "-fsSLo" ] || [ "$3" != "https://cursor.com/install" ]; then
  echo "unexpected curl invocation: $*" >&2
  exit 1
fi
cat > "$2" <<'INSTALLER'
#!/usr/bin/env bash
mkdir -p "$HOME/.local/libexec"
printf '%s\n' '#!/usr/bin/env bash' 'echo agent' > "$HOME/.local/libexec/agent"
printf '%s\n' '#!/usr/bin/env bash' 'echo rg' > "$HOME/.local/libexec/rg"
chmod +x "$HOME/.local/libexec/agent" "$HOME/.local/libexec/rg"
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/.local/libexec/agent" "$HOME/.local/bin/agent"
INSTALLER
chmod +x "$2"
STUB
cat > "${TMP}/bin/sha256sum" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${CCR_SHA_LOG:?}"
exec /usr/bin/sha256sum "$@"
STUB
chmod +x "${TMP}/bin/curl" "${TMP}/bin/sha256sum"

AGENT_BODY="$(printf '%s\n' '#!/usr/bin/env bash' 'echo agent')"
AGENT_SHA="$(printf '%s\n' "${AGENT_BODY}" | /usr/bin/sha256sum | cut -d' ' -f1)"
BAD_SHA="0000000000000000000000000000000000000000000000000000000000000000"

run_install() {
  local name="$1"
  shift
  local home="${TMP}/home-${name}"
  rm -rf "${home}"
  mkdir -p "${home}"
  : > "${TMP}/curl.log"
  : > "${TMP}/sha.log"
  set +e
  # env -i drops inherited CURSOR_* values. Callers pass GITHUB_PATH and any SHAs.
  env -i \
    HOME="${home}" \
    PATH="${TMP}/bin:/usr/bin:/bin" \
    CCR_CURL_LOG="${TMP}/curl.log" \
    CCR_SHA_LOG="${TMP}/sha.log" \
    "$@" \
    bash "${SCRIPT}" > "${TMP}/out-${name}" 2> "${TMP}/err-${name}"
  local rc=$?
  set -e
  printf '%s' "${rc}"
}

rc="$(run_install unset GITHUB_PATH="${TMP}/github-path-unset")"
chk "empty installer sha exits 0" "${rc}" "0"
chk "fetches the current install url" "$(grep -c -F -- "-fsSLo /tmp/cursor-install.sh ${INSTALL_URL}" "${TMP}/curl.log")" "1"
chk "empty installer sha does not checksum" "$(wc -l < "${TMP}/sha.log" | tr -d ' ')" "0"
chk "notice says latest" "$(grep -c '::notice::cursor_installer=latest url=https://cursor.com/install' "${TMP}/out-unset")" "1"
chk "github path gains local bin" "$(grep -c '/.local/bin$' "${TMP}/github-path-unset")" "1"

rc="$(run_install stale GITHUB_PATH="${TMP}/github-path-stale" CURSOR_INSTALLER_SHA256="${BAD_SHA}")"
chk "stale installer sha is ignored" "${rc}" "0"
chk "stale installer sha does not checksum" "$(wc -l < "${TMP}/sha.log" | tr -d ' ')" "0"

rc="$(run_install bad-agent GITHUB_PATH="${TMP}/github-path-bad-agent" CURSOR_INSTALLER_SHA256=not-a-hash CURSOR_AGENT_SHA256="${BAD_SHA}")"
chk "bad agent sha still fails" "${rc}" "1"
chk "agent check is the only checksum" "$(grep -c -- '-c' "${TMP}/sha.log")" "1"

rc="$(run_install good-agent GITHUB_PATH="${TMP}/github-path-good-agent" CURSOR_INSTALLER_SHA256=not-a-hash CURSOR_AGENT_SHA256="${AGENT_SHA}")"
chk "matching agent sha exits 0" "${rc}" "0"
chk "matching agent sha is checksummed" "$(grep -c -- '-c' "${TMP}/sha.log")" "1"
chk "garbage installer sha was not the failure" "$(grep -c 'installer_sha256' "${TMP}/err-good-agent" || true)" "0"

rc="$(run_install no-path)"
chk "missing GITHUB_PATH fails" "$([ "${rc}" -ne 0 ] && echo fail || echo ok)" "fail"

echo
echo "${pass} passed, ${fail} failed"
[ "${fail}" -eq 0 ]
