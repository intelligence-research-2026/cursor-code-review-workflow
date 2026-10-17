#!/usr/bin/env bash
# Install the current Cursor CLI; optional agent binary SHA; link bundled rg.
# Required env: GITHUB_PATH
# Optional: CURSOR_AGENT_SHA256
# CURSOR_INSTALLER_SHA256 is ignored. Install always fetches https://cursor.com/install.
set -euo pipefail

: "${CURSOR_AGENT_SHA256:=}"
: "${GITHUB_PATH:?}"

require_sha256() {
  local raw="$1" label="$2"
  raw="$(printf '%s' "${raw}" | tr -d '[:space:]')"
  if [[ ! "${raw}" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "::error::${label} must be 64 hex characters (no filename column, no whitespace)"
    exit 1
  fi
  printf '%s' "${raw}"
}

curl -fsSLo /tmp/cursor-install.sh https://cursor.com/install
echo "::notice::cursor_installer=latest url=https://cursor.com/install"
bash /tmp/cursor-install.sh
echo "$HOME/.local/bin" >> "$GITHUB_PATH"

# The executable must resolve after install; pinning its SHA on top is optional.
agent_bin=""
if [ -x "$HOME/.local/bin/cursor-agent" ]; then
  agent_bin="$HOME/.local/bin/cursor-agent"
elif [ -x "$HOME/.local/bin/agent" ]; then
  agent_bin="$HOME/.local/bin/agent"
fi
if [ -z "${agent_bin}" ]; then
  echo "::error::Neither $HOME/.local/bin/cursor-agent nor $HOME/.local/bin/agent was found"
  exit 1
fi
# Shell(rg) is allowlisted, so link the rg bundled in the package onto PATH (~/.local/bin).
agent_dir="$(dirname "$(readlink -f "${agent_bin}")")"
if [ ! -x "${agent_dir}/rg" ]; then
  echo "::error::No executable rg found inside the cursor-agent package (${agent_dir}/rg)"
  exit 1
fi
ln -sfn "${agent_dir}/rg" "$HOME/.local/bin/rg"
echo "::notice::bundled_rg=linked path=$HOME/.local/bin/rg -> ${agent_dir}/rg"
if [ -z "${CURSOR_AGENT_SHA256}" ]; then
  echo "::notice::cursor_agent_sha256=unset path=${agent_bin} (known residual risk: the CLI binary fetched after bootstrap is not pinned on its own)"
else
  CURSOR_AGENT_SHA256="$(require_sha256 "${CURSOR_AGENT_SHA256}" "agent_sha256")"
  printf '%s  %s\n' "${CURSOR_AGENT_SHA256}" "${agent_bin}" | sha256sum -c -
  echo "::notice::cursor_agent_sha256=verified path=${agent_bin}"
fi
