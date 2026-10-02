#!/bin/bash
# Install manifest-pinned language/package tools as the credential-isolated
# builder account. The guest user's host mounts are intentionally inaccessible.
set -euo pipefail

TOOL_HOME=/var/lib/devbox-toolbuilder
MANIFEST="${1:?usage: provision-tools.sh MANIFEST GUEST_HOME GUEST_UID TOOL_ASSETS}"
GUEST_HOME="${2:?usage: provision-tools.sh MANIFEST GUEST_HOME GUEST_UID TOOL_ASSETS}"
GUEST_UID="${3:?usage: provision-tools.sh MANIFEST GUEST_HOME GUEST_UID TOOL_ASSETS}"
TOOL_ASSETS="${4:?usage: provision-tools.sh MANIFEST GUEST_HOME GUEST_UID TOOL_ASSETS SRC_ROOT}"
SRC_ROOT="${5:?usage: provision-tools.sh MANIFEST GUEST_HOME GUEST_UID TOOL_ASSETS SRC_ROOT}"

if [[ "$(id -un)" != devbox-toolbuilder || "$HOME" != "$TOOL_HOME" ]]; then
  echo "devbox: package provisioning must run as devbox-toolbuilder" >&2
  exit 1
fi
if [[ ! "$GUEST_UID" =~ ^[0-9]+$ ]]; then
  echo "devbox: guest UID must be numeric" >&2
  exit 1
fi
if [[ -x "$GUEST_HOME/.host-config" ]]; then
  echo "devbox: toolbuilder can traverse the protected host-config mount parent" >&2
  exit 1
fi
if [[ -x "$SRC_ROOT" ]]; then
  echo "devbox: toolbuilder can traverse the protected source mount" >&2
  exit 1
fi
if [[ ! -r "$MANIFEST" || ! -r "$TOOL_ASSETS/devbox-go" \
  || ! -r "$TOOL_ASSETS/check_toolchain.py" || ! -r "$TOOL_ASSETS/semble" ]]; then
  echo "devbox: manifest or root-owned tool assets are unavailable" >&2
  exit 1
fi

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/node/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin"
export UV_CACHE_DIR="$HOME/.cache/uv"
export CARGO_HOME="$HOME/.cargo"
export RUSTUP_HOME="$HOME/.rustup"
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"
export HF_HOME="$HOME/.cache/semble/huggingface"
mkdir -p "$HOME/.cache/uv" "$HOME/.cache/semble" "$CARGO_HOME" "$RUSTUP_HOME" \
  "$PLAYWRIGHT_BROWSERS_PATH" "$HF_HOME" "$HOME/.local/bin"

declare -A VERSION_ONLY_DIAGNOSTICS=()

manifest_integrity_policy() {
  local tool="$1" policy
  if ! policy="$(jq -er --arg tool "$tool" '.tools[$tool].integrity' "$MANIFEST")"; then
    echo "devbox: missing integrity policy for manifest tool '$tool'" >&2
    return 1
  fi
  case "$policy" in
    sha256|version-only) ;;
    *)
      echo "devbox: unknown integrity policy '$policy' for manifest tool '$tool'" >&2
      return 1
      ;;
  esac
  printf '%s\n' "$policy"
}

report_version_only() {
  local artifact="$1"
  if [[ -z "${VERSION_ONLY_DIAGNOSTICS[$artifact]:-}" ]]; then
    echo "devbox: $artifact uses version-only integrity; SHA-256 verification is skipped" >&2
    VERSION_ONLY_DIAGNOSTICS["$artifact"]=1
  fi
}

manifest_version() {
  local tool="$1" version policy
  policy="$(manifest_integrity_policy "$tool")" || return 1
  if [[ "$policy" == version-only ]]; then
    report_version_only "$tool"
  fi
  version="$(jq -er --arg tool "$tool" '.tools[$tool].version' "$MANIFEST")"
  version="${version#v}"
  if [[ ! "$version" =~ ^[0-9]+(\.[0-9]+){1,3}([+-][A-Za-z0-9.]+)?$ ]]; then
    echo "devbox: invalid version for manifest tool '$tool'" >&2
    return 1
  fi
  printf '%s\n' "$version"
}

manifest_checksum() {
  local tool="$1" arch="$2" checksum policy
  policy="$(manifest_integrity_policy "$tool")" || return 1
  if [[ "$policy" != sha256 ]]; then
    echo "devbox: refusing checksum lookup for version-only tool '$tool'" >&2
    return 1
  fi
  checksum="$(jq -er --arg tool "$tool" --arg arch "$arch" \
    '.tools[$tool].artifacts[$arch].sha256' "$MANIFEST")"
  if [[ ! "$checksum" =~ ^[0-9a-f]{64}$ ]]; then
    echo "devbox: invalid SHA-256 digest for manifest tool '$tool' ($arch)" >&2
    return 1
  fi
  printf '%s\n' "$checksum"
}

manifest_artifact_version() {
  local tool="$1" arch="$2" policy version artifact_version
  policy="$(manifest_integrity_policy "$tool")" || return 1
  if [[ "$policy" != sha256 ]]; then
    echo "devbox: refusing artifact tag lookup for version-only tool '$tool'" >&2
    return 1
  fi
  version="$(manifest_version "$tool")" || return 1
  artifact_version="$(jq -er --arg tool "$tool" --arg arch "$arch" \
    '.tools[$tool].artifacts[$arch].version' "$MANIFEST")" || {
    echo "devbox: missing $arch release tag for manifest tool '$tool'" >&2
    return 1
  }
  if [[ ! "$artifact_version" =~ ^v?[0-9]+(\.[0-9]+){1,3}([+-][A-Za-z0-9.]+)?$ ]] \
    || [[ "${artifact_version#v}" != "$version" ]]; then
    echo "devbox: invalid or unsynchronized $arch release tag for manifest tool '$tool'" >&2
    return 1
  fi
  printf '%s\n' "$artifact_version"
}

verify_download() {
  local tool="$1" arch="$2" path="$3" checksum policy
  policy="$(manifest_integrity_policy "$tool")" || return 1
  if [[ "$policy" == version-only ]]; then
    report_version_only "$tool"
    return 0
  fi
  checksum="$(manifest_checksum "$tool" "$arch")"
  printf '%s  %s\n' "$checksum" "$path" | sha256sum -c -
}

manifest_integrity_fingerprint() {
  local tool="$1" arch="$2" policy version artifact_version checksum
  policy="$(manifest_integrity_policy "$tool")" || return 1
  version="$(manifest_version "$tool")" || return 1
  if [[ "$policy" == version-only ]]; then
    printf '%s|version-only\n' "$version"
    return 0
  fi
  artifact_version="$(manifest_artifact_version "$tool" "$arch")" || return 1
  checksum="$(manifest_checksum "$tool" "$arch")" || return 1
  printf '%s|sha256|%s|%s\n' "$version" "$artifact_version" "$checksum"
}

ensure_npm_package() {
  local tool="$1" package="$2" version installed
  version="$(manifest_version "$tool")"
  installed="$(npm ls --global --prefix "$HOME/.local" --depth=0 --json 2>/dev/null \
    | jq -r --arg package "$package" '.dependencies[$package].version // ""' || true)"
  if [[ "$installed" != "$version" ]]; then
    npm install --global --prefix "$HOME/.local" --no-fund \
      "${package}@${version}"
  fi
}

ensure_uv_tool() {
  local tool="$1" package_spec="$2" command="$3" expected installed=""
  expected="$(manifest_version "$tool")"
  if [[ -x "$HOME/.local/bin/$command" ]]; then
    installed="$("$HOME/.local/bin/$command" --version 2>/dev/null \
      | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+([.-][[:alnum:].-]+)?' \
      | head -n1 || true)"
  fi
  if [[ "$installed" != "$expected" ]]; then
    uv tool install --force "$package_spec"
  fi
}

ensure_npm_package opencode @opencode/cli
ensure_npm_package markdownlint_cli2 markdownlint-cli2
ensure_npm_package playwright_cli @playwright/cli
ensure_npm_package repomix repomix
ensure_npm_package google_workspace_cli @googleworkspace/cli

ensure_uv_tool pre_commit "pre-commit==$(manifest_version pre_commit)" pre-commit
ensure_uv_tool pipenv "pipenv==$(manifest_version pipenv)" pipenv
semble_version="$(manifest_version semble)"
semble_installed="$("$HOME/.local/bin/semble-bin" --version 2>/dev/null \
  | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
if [[ "$semble_installed" != "$semble_version" ]]; then
  uv tool install --force "semble[mcp]==${semble_version}"
  mv -f "$HOME/.local/bin/semble" "$HOME/.local/bin/semble-bin"
fi

rustup_version="$(manifest_version rustup)"
rust_version="$(manifest_version rust)"
case "$(uname -m)" in
  x86_64) rustup_arch=x86_64-unknown-linux-gnu; checksum_arch=amd64 ;;
  aarch64) rustup_arch=aarch64-unknown-linux-gnu; checksum_arch=arm64 ;;
  *) echo "devbox: unsupported architecture for rustup: $(uname -m)" >&2; exit 1 ;;
esac
rustup_fingerprint="$(manifest_integrity_fingerprint rustup "$checksum_arch")"
rustup_installed="$(rustup --version 2>/dev/null | awk '{print $2}' || true)"
rustup_stamp="$HOME/.cache/tool-integrity/rustup"
if [[ "$rustup_installed" != "$rustup_version" ]] \
  || [[ ! -f "$rustup_stamp" ]] \
  || [[ "$(cat "$rustup_stamp")" != "$rustup_fingerprint" ]]; then
  install -d -m 0700 "${rustup_stamp%/*}"
  tmp="$(mktemp -d "$HOME/.cache/rustup.XXXXXX")"
  curl --retry 3 --retry-connrefused -fsSL \
    "https://static.rust-lang.org/rustup/archive/${rustup_version}/${rustup_arch}/rustup-init" \
    -o "$tmp/rustup-init"
  verify_download rustup "$checksum_arch" "$tmp/rustup-init"
  chmod 0755 "$tmp/rustup-init"
  "$tmp/rustup-init" -y --no-modify-path --default-toolchain none
  rm -rf -- "$tmp"
  printf '%s\n' "$rustup_fingerprint" >"$rustup_stamp.new"
  mv -f "$rustup_stamp.new" "$rustup_stamp"
fi
if ! rustup toolchain list | grep -q "^${rust_version}-"; then
  rustup toolchain install "$rust_version" --profile minimal
fi
rustup default "$rust_version"

# The browser and model caches live on the VM disk under the builder home.
# ACLs grant the guest access only to these non-secret cache contents.
playwright-cli install-browser chrome-for-testing
fixture="$(mktemp -d "$HOME/.cache/semble/prefetch.XXXXXX")"
prefetch_index="$(mktemp -d "$HOME/.cache/semble/prefetch-index.XXXXXX")"
trap 'rm -rf -- "$fixture" "$prefetch_index"' EXIT
printf '%s\n' \
  'def retry_failed_request(request):' \
  '    for attempt in range(3):' \
  '        try:' \
  '            return request()' \
  '        except TimeoutError:' \
  '            continue' \
  >"$fixture/retry.py"
SEMBLE_CACHE_LOCATION="$prefetch_index" \
  "$HOME/.local/bin/semble-bin" search "retry failed requests" \
  "$fixture" --format json >/dev/null
rm -rf -- "$fixture" "$prefetch_index"
trap - EXIT

install -D -m 0755 "$TOOL_ASSETS/semble" "$HOME/.local/bin/semble"

/usr/bin/python3 -I -S "$TOOL_ASSETS/check_toolchain.py" \
  --manifest "$MANIFEST"

# Runtime can execute the read-only browser and read/update only the model
# cache. This account remains unable to traverse the guest's host-config parent.
chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"
chmod -R a+rX "$HF_HOME"
find "$HF_HOME" -type d \
  -exec setfacl -m "u:${GUEST_UID}:rwx,d:u:${GUEST_UID}:rwx" {} +
find "$HF_HOME" -type f -exec setfacl -m "u:${GUEST_UID}:rw" {} +
