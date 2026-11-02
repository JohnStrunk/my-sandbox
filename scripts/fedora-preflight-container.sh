#!/bin/bash
# Fedora container preflight: install the shared non-VM package set, the
# manifest-pinned Ruby RPM, and representative manifest-pinned release tools.
# Run inside lima/fedora-preflight.Containerfile; this never starts a VM, KVM,
# systemd, or the full Lima provisioner.
set -euo pipefail

MANIFEST=/opt/devbox/tool-versions.json
# shellcheck source=/dev/null
source /opt/devbox/fedora-packages.sh

manifest_version() {
  jq -er --arg tool "$1" '.tools[$tool].version' "$MANIFEST"
}

manifest_artifact_version() {
  jq -er --arg tool "$1" --arg arch "$2" \
    '.tools[$tool].artifacts[$arch].version' "$MANIFEST"
}

manifest_sha256() {
  jq -er --arg tool "$1" --arg arch "$2" \
    '.tools[$tool].artifacts[$arch].sha256' "$MANIFEST"
}

verify_sha256() {
  local checksum="$1" path="$2"
  printf '%s  %s\n' "$checksum" "$path" | sha256sum -c -
}

download() {
  local url="$1" destination="$2"
  curl --retry 3 --retry-connrefused -fsSL "$url" -o "$destination"
}

case "$(uname -m)" in
  x86_64)
    checksum_arch=amd64
    hadolint_arch=x86_64
    kind_arch=amd64
    uv_arch=x86_64
    ;;
  aarch64)
    checksum_arch=arm64
    hadolint_arch=arm64
    kind_arch=arm64
    uv_arch=aarch64
    ;;
  *)
    printf 'fedora-preflight: unsupported architecture: %s\n' "$(uname -m)" >&2
    exit 1
    ;;
esac

work="$(mktemp -d /tmp/fedora-preflight.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT

printf 'fedora-preflight: installing shared non-VM Fedora packages\n'
printf 'fedora-preflight: excluding VM-only packages: %s\n' \
  "${DEVBOX_FEDORA_VM_PACKAGES[*]}"
devbox_install_fedora_packages preflight
devbox_install_manifest_pinned_ruby "$MANIFEST"

HADOLINT_VERSION="$(manifest_version hadolint)"
HADOLINT_ARTIFACT_VERSION="$(manifest_artifact_version hadolint "$checksum_arch")"
download \
  "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_ARTIFACT_VERSION}/hadolint-linux-${hadolint_arch}" \
  "$work/hadolint"
verify_sha256 "$(manifest_sha256 hadolint "$checksum_arch")" "$work/hadolint"
install -m 0755 "$work/hadolint" /usr/local/bin/hadolint
hadolint_installed="$(hadolint --version 2>/dev/null \
  | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
if [[ "$hadolint_installed" != "$HADOLINT_VERSION" ]]; then
  printf 'fedora-preflight: hadolint %s, expected %s\n' \
    "${hadolint_installed:-unavailable}" "$HADOLINT_VERSION" >&2
  exit 1
fi

UV_VERSION="$(manifest_version uv)"
UV_ARTIFACT_VERSION="$(manifest_artifact_version uv "$checksum_arch")"
uv_dir="uv-${uv_arch}-unknown-linux-gnu"
download \
  "https://github.com/astral-sh/uv/releases/download/${UV_ARTIFACT_VERSION}/${uv_dir}.tar.gz" \
  "$work/uv.tar.gz"
verify_sha256 "$(manifest_sha256 uv "$checksum_arch")" "$work/uv.tar.gz"
tar -C "$work" -xzf "$work/uv.tar.gz"
install -m 0755 "$work/$uv_dir/uv" /usr/local/bin/uv
install -m 0755 "$work/$uv_dir/uvx" /usr/local/bin/uvx
uv_installed="$(uv --version 2>/dev/null | awk '{print $2}' || true)"
if [[ "$uv_installed" != "$UV_VERSION" ]]; then
  printf 'fedora-preflight: uv %s, expected %s\n' \
    "${uv_installed:-unavailable}" "$UV_VERSION" >&2
  exit 1
fi

KIND_VERSION="$(manifest_version kind)"
KIND_ARTIFACT_VERSION="$(manifest_artifact_version kind "$checksum_arch")"
download \
  "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_ARTIFACT_VERSION}/kind-linux-${kind_arch}" \
  "$work/kind"
verify_sha256 "$(manifest_sha256 kind "$checksum_arch")" "$work/kind"
install -m 0755 "$work/kind" /usr/local/bin/kind
kind_installed="$(kind version 2>/dev/null \
  | grep -Eo 'v[0-9]+\.[0-9]+\.[0-9]+' | head -n1 | sed 's/^v//' || true)"
if [[ "$kind_installed" != "$KIND_VERSION" ]]; then
  printf 'fedora-preflight: kind %s, expected %s\n' \
    "${kind_installed:-unavailable}" "$KIND_VERSION" >&2
  exit 1
fi

printf 'fedora-preflight: packages, Ruby, hadolint, uv, and kind match manifest pins\n'
printf 'fedora-preflight: packages/tools only; no VM provisioning or readiness was checked\n'
