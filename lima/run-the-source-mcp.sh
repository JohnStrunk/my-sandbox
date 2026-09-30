#!/usr/bin/env bash
# Install the locked The Source MCP environment without credentials, then run
# it with only its own credentials rather than OpenCode's full service env.
set -euo pipefail

repo_root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
source_project="$repo_root/lima/the-source"
uv_bin="$(command -v uv)" || {
  echo "The Source MCP requires the VM's pinned uv installation" >&2
  exit 127
}
venv="$HOME/.local/state/devbox-toolchain/the-source-mcp"
ca_bundle=/etc/ssl/certs/ca-certificates.crt

/usr/bin/env -i \
  HOME="${HOME:?OpenCode did not provide HOME}" \
  PATH="${PATH:?OpenCode did not provide PATH}" \
  SSL_CERT_FILE="$ca_bundle" \
  REQUESTS_CA_BUNDLE="$ca_bundle" \
  UV_CACHE_DIR="$HOME/.cache/uv" \
  UV_PROJECT_ENVIRONMENT="$venv" \
  "$uv_bin" sync --project "$source_project" --locked

for name in \
  IGLOO_MCP_COMMUNITY \
  IGLOO_MCP_COMMUNITY_KEY \
  IGLOO_MCP_APP_PASS \
  IGLOO_MCP_APP_ID \
  IGLOO_MCP_USERNAME \
  IGLOO_MCP_PASSWORD \
  IGLOO_MCP_SERVER_NAME \
  IGLOO_MCP_SERVER_INSTRUCTIONS; do
  [[ -n "${!name:-}" ]] || {
    echo "The Source configuration is incomplete" >&2
    exit 1
  }
done

# Pass values in the child's environment, not env(1)'s argv where same-UID
# process inspection could see them.
# shellcheck disable=SC2016 # This Python snippet runs in the guest.
exec /usr/bin/python3 -c '
import os
import sys

command = sys.argv[1]
names = (
    "HOME",
    "IGLOO_MCP_COMMUNITY",
    "IGLOO_MCP_COMMUNITY_KEY",
    "IGLOO_MCP_APP_PASS",
    "IGLOO_MCP_APP_ID",
    "IGLOO_MCP_USERNAME",
    "IGLOO_MCP_PASSWORD",
    "IGLOO_MCP_SERVER_NAME",
    "IGLOO_MCP_SERVER_INSTRUCTIONS",
)
env = {name: os.environ[name] for name in names}
env["PATH"] = "/usr/bin:/bin"
env["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"
env["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"
os.execve(command, [command], env)
' "$venv/bin/igloo-mcp"
