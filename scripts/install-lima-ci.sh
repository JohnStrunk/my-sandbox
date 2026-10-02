#!/usr/bin/env bash
# Install the manifest-pinned Lima release on a Linux CI runner.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/lima/tool-versions.json"
if ! integrity="$(jq -er '.tools.limactl.integrity' "$manifest")" \
  || [[ "$integrity" != sha256 ]]; then
  printf 'install-lima-ci: limactl must declare sha256 integrity\n' >&2
  exit 2
fi
version="$(jq -er '.tools.limactl.version' "$manifest")"
version="${version#v}"
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

artifact_version="$(jq -er ".tools.limactl.artifacts.$checksum_arch.version" "$manifest")"
if [[ "${artifact_version#v}" != "$version" ]]; then
  printf 'install-lima-ci: limactl artifact tag does not match tool version\n' >&2
  exit 2
fi
checksum="$(jq -er ".tools.limactl.artifacts.$checksum_arch.sha256" "$manifest")"
if [[ ! "$checksum" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'install-lima-ci: invalid limactl SHA-256 digest\n' >&2
  exit 2
fi
archive="lima-${version}-Linux-${asset_arch}.tar.gz"
url="https://github.com/lima-vm/lima/releases/download/${artifact_version}/${archive}"
tmpdir="$(mktemp -d)"
trap 'rm -rf -- "$tmpdir"' EXIT
curl --fail --location --silent --show-error "$url" --output "$tmpdir/$archive"
printf '%s  %s\n' "$checksum" "$tmpdir/$archive" | sha256sum --check --status
sudo tar --extract --gzip --file "$tmpdir/$archive" --directory /usr/local
limactl --version
