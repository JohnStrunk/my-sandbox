#!/usr/bin/env bash
# Validate repeated kind create/workload/delete cycles with one explicit backend.
set -Eeuo pipefail

usage() {
  printf 'usage: %s {docker|podman} [runs: 1-100, default 10]\n' "$0" >&2
}

if (($# < 1 || $# > 2)); then
  usage
  exit 2
fi

provider="$1"
case "$provider" in
  docker | podman) ;;
  *)
    printf "kind validation: unsupported provider '%s'; choose docker or podman\n" \
      "$provider" >&2
    usage
    exit 2
    ;;
esac

runs="${2:-10}"
if [[ ! "$runs" =~ ^[1-9][0-9]*$ || "$runs" -gt 100 ]]; then
  usage
  exit 2
fi

die() {
  printf 'kind validation: %s\n' "$*" >&2
  exit 1
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lima/container-backend-resources.sh
source "$script_dir/container-backend-resources.sh"
# shellcheck source=lima/podman-local-preflight.sh
source "$script_dir/podman-local-preflight.sh"
for tool in kind kubectl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    die "$tool is required but was not found on PATH; install the provisioned kind toolchain"
  fi
done

case "$provider" in
  docker)
    # shellcheck source=lima/docker-ce-preflight.sh
    source "$script_dir/docker-ce-preflight.sh"
    if ! docker_ce_preflight "$script_dir/tool-versions.json"; then
      die 'Docker provider requires the pinned rootful Docker CE system service and socket; see the preflight diagnostic above'
    fi
    ;;
  podman)
    if ! podman_local_preflight; then
      die 'Podman provider requires local rootless Podman with no default system connection; see the preflight diagnostic above'
    fi
    ;;
esac

reserved_prefix="devbox-kind-${provider}-"
# Official v0.33.0 release image, multi-arch for amd64/arm64:
# https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0
node_image_digest='a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5' # pragma: allowlist secret
node_image="kindest/node:v1.37.0@sha256:${node_image_digest}"
check_no_stale_resources() {
  local resource_kind="$1"
  if ! container_backend_check_no_stale_resources \
    "$provider" "$resource_kind" "$reserved_prefix" "kind validation" ""; then
    exit 1
  fi
}

check_no_stale_backend_resources() {
  local resource_kind
  for resource_kind in containers volumes networks; do
    check_no_stale_resources "$resource_kind"
  done
}

# kind's provider-wide default network is shared; only cluster-scoped
# networks with a test run's name are owned and audited below.
check_no_stale_backend_resources

export KIND_EXPERIMENTAL_PROVIDER="$provider"
umask 077
state_dir=''
cleanup_private_state() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "${state_dir:-}" && -d "$state_dir" ]] && ! rm -rf -- "$state_dir"; then
    printf 'kind validation: cleanup could not remove private state at %s\n' \
      "$state_dir" >&2
    if ((status == 0)); then
      status=1
    fi
  fi
  exit "$status"
}
trap cleanup_private_state EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
state_dir="$(mktemp -d "${TMPDIR:-/tmp/opencode}/my-sandbox-kind.XXXXXXXXXX")" \
  || die 'could not create private kind state'
if ! chmod 0700 -- "$state_dir"; then
  rmdir -- "$state_dir" 2>/dev/null || true
  die "could not secure private state directory $state_dir"
fi
export KUBECONFIG="$state_dir/kubeconfig"
prefix="devbox-kind-${provider}-$(date +%s)-$$"
active_cluster=''

backend_cluster_nodes() {
  local cluster="$1"
  "$provider" ps --all --filter "label=io.x-k8s.kind.cluster=$cluster" \
    --format '{{.Names}}'
}

backend_cluster_resources() {
  local cluster="$1" output matching resource_kind resource_label
  for resource_kind in containers volumes networks; do
    if ! output="$(container_backend_list "$provider" "$resource_kind")"; then
      return 1
    fi
    matching="$(grep -F "$cluster" <<<"$output" || true)"
    if [[ -n "$matching" ]]; then
      case "$resource_kind" in
        containers) resource_label=container ;;
        volumes) resource_label=volume ;;
        networks) resource_label=network ;;
      esac
      printf '%s: %s\n' "$resource_label" "$matching"
    fi
  done
}

verify_backend_cluster() {
  local cluster="$1" resources
  if ! resources="$(backend_cluster_nodes "$cluster")"; then
    die "could not inspect $provider resources for cluster $cluster (see runtime diagnostic above)"
  fi
  if [[ -z "$resources" ]]; then
    die "the explicitly selected $provider backend has no node labeled for cluster $cluster; the request was not proven on that backend"
  fi
  printf 'kind validation: backend identity verified: %s node(s) for %s via %s\n' \
    "$(wc -l <<<"$resources" | tr -d ' ')" "$cluster" "$provider"
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  local output
  trap - EXIT HUP INT TERM
  set +e

  if [[ -n "$active_cluster" ]]; then
    if ! output="$(kind delete cluster --name "$active_cluster" 2>&1)"; then
      printf 'kind validation: cleanup could not delete cluster %s: %s\n' \
        "$active_cluster" "$output" >&2
    fi
    if output="$(backend_cluster_resources "$active_cluster")"; then
      if [[ -n "$output" ]]; then
        printf 'kind validation: %s resources for cluster %s remain after cleanup:\n%s\n' \
          "$provider" "$active_cluster" "$output" >&2
        cleanup_failed=1
      else
        active_cluster=''
      fi
    else
      printf 'kind validation: cleanup could not inspect %s resources for %s (see runtime diagnostic above)\n' \
        "$provider" "$active_cluster" >&2
      cleanup_failed=1
    fi
  fi

  if ((cleanup_failed)); then
    printf 'kind validation: preserving private state for diagnosis at %s\n' \
      "$state_dir" >&2
    if ((status == 0)); then
      status=1
    fi
  elif ! rm -rf -- "$state_dir"; then
    printf 'kind validation: cleanup could not remove private state at %s\n' \
      "$state_dir" >&2
    if ((status == 0)); then
      status=1
    fi
  fi

  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for ((run = 1; run <= runs; run++)); do
  cluster="${prefix}-${run}"
  active_cluster="$cluster"
  printf 'kind validation %d/%d: creating %s with %s\n' \
    "$run" "$runs" "$cluster" "$provider"
  kind create cluster --name "$cluster" --image="$node_image" \
    --kubeconfig="$KUBECONFIG" --wait 5m
  verify_backend_cluster "$cluster"

  context="kind-${cluster}"
  kubectl --kubeconfig="$KUBECONFIG" --context="$context" wait \
    --for=condition=Ready nodes --all --timeout=5m
  node_states="$(kubectl --kubeconfig="$KUBECONFIG" --context="$context" \
    get nodes -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}')"
  if [[ -z "$node_states" ]]; then
    die "cluster $cluster has no nodes after kind reported it ready"
  fi
  while IFS=' ' read -r node_name node_ready; do
    [[ -n "$node_name" ]] || continue
    if [[ "$node_ready" != True ]]; then
      die "cluster node $node_name is not Ready (status: ${node_ready:-missing})"
    fi
  done <<<"$node_states"

  workload="${cluster}-smoke"
  # Public multi-architecture OCI digest (split for line length; not a secret).
  workload_digest_prefix='bdf57e528e45e4433820e045b29b4597' # pragma: allowlist secret
  workload_digest_suffix='825a1c9e38353532d90a01445013f82e' # pragma: allowlist secret
  workload_image="docker.io/library/busybox:1.37.0@sha256:${workload_digest_prefix}${workload_digest_suffix}"
  kubectl --kubeconfig="$KUBECONFIG" --context="$context" run "$workload" \
    --image="$workload_image" \
    --restart=Never \
    --command -- sh -c 'sleep 300'
  kubectl --kubeconfig="$KUBECONFIG" --context="$context" wait \
    --for=condition=Ready "pod/$workload" --timeout=5m
  pod_state="$(kubectl --kubeconfig="$KUBECONFIG" --context="$context" \
    get pod "$workload" \
    -o 'jsonpath={.status.phase} {.status.containerStatuses[0].ready}')"
  if [[ "$pod_state" != 'Running true' ]]; then
    die "BusyBox workload $workload did not reach Running/Ready (state: ${pod_state:-missing})"
  fi

  kind delete cluster --name "$cluster"
  if ! resources="$(backend_cluster_resources "$cluster")"; then
    die "could not verify $provider cleanup for cluster $cluster (see runtime diagnostic above)"
  fi
  if [[ -n "$resources" ]]; then
    die "$provider resources for cluster $cluster remain after kind delete: $resources"
  fi
  active_cluster=''
done

trap - EXIT HUP INT TERM
if ! rm -rf -- "$state_dir"; then
  die "cleanup could not remove private state at $state_dir"
fi
printf 'kind validation passed: %s consecutive %s clusters with Ready nodes and BusyBox workloads\n' \
  "$runs" "$provider"
