#!/usr/bin/env bash
# Install the manifest-pinned Lima release on a Linux CI runner.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/lima/tool-versions.json"
version="$(jq -er '.tools.limactl.version' "$manifest")"
case "$(uname -m)" in
  x86_64)
    asset_arch="x86_64"
    checksum_arch="amd64"
    ;;
  aarch64)
    asset_arch="aarch64"
    checksum_arch="arm64"
    ;;
  *)
    printf 'install-lima-ci: unsupported architecture: %s\n' "$(uname -m)" >&2
    exit 2
    ;;
esac

checksum="$(jq -er ".tools.limactl.checksums.$checksum_arch" "$manifest")"
archive="lima-${version}-Linux-${asset_arch}.tar.gz"
url="https://github.com/lima-vm/lima/releases/download/v${version}/${archive}"
tmpdir="$(mktemp -d)"
trap 'rm -rf -- "$tmpdir"' EXIT
curl --fail --location --silent --show-error "$url" --output "$tmpdir/$archive"
printf '%s  %s\n' "$checksum" "$tmpdir/$archive" | sha256sum --check --status
sudo tar --extract --gzip --file "$tmpdir/$archive" --directory /usr/local
limactl --version
