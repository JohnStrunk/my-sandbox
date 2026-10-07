#!/usr/bin/env bash
# Validate repeated kind create/delete cycles against an explicitly selected
# rootless container backend.
set -euo pipefail

runs="${1:-10}"
if [[ ! "$runs" =~ ^[1-9][0-9]*$ || "$runs" -gt 100 ]]; then
  echo "usage: $0 [runs: 1-100, default 10]" >&2
  exit 2
fi

provider="${KIND_EXPERIMENTAL_PROVIDER:-docker}"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
case "$provider" in
  podman)
    DOCKER_HOST="unix://${runtime_dir}/podman/podman.sock"
    ;;
  docker)
    DOCKER_HOST="unix://${runtime_dir}/docker.sock"
    ;;
  *)
    echo "unsupported kind provider '$provider'; choose podman or docker" >&2
    exit 2
    ;;
esac
export DOCKER_HOST
export KIND_EXPERIMENTAL_PROVIDER="$provider"
prefix="devbox-kind-validation-$(date +%s)-$$"
clusters=()
cleanup() {
  local status=$?
  trap - EXIT
  for cluster in "${clusters[@]}"; do
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
  done
  exit "$status"
}
trap cleanup EXIT

for ((run = 1; run <= runs; run++)); do
  cluster="${prefix}-${run}"
  clusters+=("$cluster")
  printf 'kind validation %d/%d: creating %s\n' "$run" "$runs" "$cluster"
  kind create cluster --name "$cluster" --wait 5m
  kind delete cluster --name "$cluster"
done

clusters=()
trap - EXIT
printf 'kind validation passed: %s consecutive clusters\n' "$runs"
