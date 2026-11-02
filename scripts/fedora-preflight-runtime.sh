#!/bin/bash
# Runtime checks for the Fedora container preflight image. The image build
# installs packages and tools in cacheable layers; this entry point always
# refreshes DNF metadata and re-resolves the exact package pins so a cached
# image cannot make the CI check vacuous.
set -euo pipefail

MANIFEST="${DEVBOX_MANIFEST:-/opt/devbox/tool-versions.json}"
# shellcheck source=/dev/null
source /opt/devbox/fedora-packages.sh

manifest_version() {
  jq -er --arg tool "$1" '.tools[$tool].version' "$MANIFEST"
}

verify_command_version() {
  local command="$1" expected="$2" actual="$3"
  if [[ "$actual" != "$expected" ]]; then
    printf 'fedora-preflight: %s %s, expected %s\n' \
      "$command" "${actual:-unavailable}" "$expected" >&2
    return 1
  fi
}

printf 'fedora-preflight: refreshing DNF metadata and resolving package pins\n'
devbox_refresh_dnf_metadata
devbox_verify_dnf_package_resolution "${DEVBOX_FEDORA_PACKAGES[@]}"
devbox_verify_manifest_pinned_ruby_available "$MANIFEST"

RUBY_VERSION="$(manifest_version ruby)"
RUBY_VERSION="${RUBY_VERSION#v}"
ruby_output="$(/usr/bin/ruby --version 2>/dev/null)" || ruby_output=""
if [[ "$ruby_output" != "ruby ${RUBY_VERSION} "* ]]; then
  printf 'fedora-preflight: ruby %s, expected %s\n' \
    "${ruby_output:-unavailable}" "$RUBY_VERSION" >&2
  exit 1
fi

HADOLINT_VERSION="$(manifest_version hadolint)"
hadolint_installed="$(hadolint --version 2>/dev/null \
  | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
verify_command_version hadolint "$HADOLINT_VERSION" "$hadolint_installed"

UV_VERSION="$(manifest_version uv)"
uv_installed="$(uv --version 2>/dev/null | awk '{print $2}' || true)"
verify_command_version uv "$UV_VERSION" "$uv_installed"

KIND_VERSION="$(manifest_version kind)"
kind_installed="$(kind version 2>/dev/null \
  | grep -Eo 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n1 | sed 's/^v//' || true)"
verify_command_version kind "$KIND_VERSION" "$kind_installed"

printf 'fedora-preflight: packages, Ruby, hadolint, uv, and kind match manifest pins\n'
printf 'fedora-preflight: packages/tools only; no VM provisioning or readiness was checked\n'
