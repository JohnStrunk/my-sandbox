#!/usr/bin/env bash
# Build and run the Fedora 44 container preflight.
#
# Local iterations reuse BuildKit's local layer cache for unchanged
# Containerfile/RUN layers. CI builds the same image through
# docker/build-push-action with the GitHub Actions BuildKit cache, then runs
# this script with --no-build. This checks DNF package/pin resolution and
# representative manifest-pinned tool installs; it does not replace VM or
# recursive-VM provisioning and readiness validation.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
containerfile="$repo_root/lima/fedora-preflight.Containerfile"
image="${FEDORA_PREFLIGHT_IMAGE:-my-sandbox-fedora-preflight:local}"
docker_bin="${DOCKER:-docker}"
build=true

usage() {
  cat <<'EOF'
Usage: scripts/fedora-preflight.sh [--no-build] [--image IMAGE]

Build the Fedora 44 container preflight image and run it, or with --no-build
run an image that CI already built. Set DOCKER to override the container CLI
and FEDORA_PREFLIGHT_IMAGE to override the default local image tag.
EOF
}

while (($# > 0)); do
  case "$1" in
    --no-build)
      build=false
      shift
      ;;
    --image)
      if (($# < 2)) || [[ -z "$2" ]]; then
        printf 'fedora-preflight: --image requires a value\n' >&2
        exit 2
      fi
      image="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'fedora-preflight: unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v "$docker_bin" >/dev/null 2>&1; then
  printf 'fedora-preflight: %s is required to run the container preflight\n' \
    "$docker_bin" >&2
  exit 1
fi

if [[ "$build" == true ]]; then
  "$docker_bin" buildx build \
    --file "$containerfile" \
    --tag "$image" \
    --load \
    "$repo_root"
fi

exec "$docker_bin" run --rm "$image"
