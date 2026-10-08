#!/usr/bin/env bash
# Smoke-test one explicitly selected Minikube backend in isolated state.
set -Eeuo pipefail

usage() {
  printf 'usage: %s {podman|kvm2}\n' "$0" >&2
}

if (($# != 1)); then
  usage
  exit 2
fi

mode="$1"
case "$mode" in
  podman | kvm2) ;;
  *)
    printf "minikube validation: unsupported backend '%s'; choose podman or kvm2\n" \
      "$mode" >&2
    usage
    exit 2
    ;;
esac

die() {
  printf 'minikube validation: %s\n' "$*" >&2
  exit 1
}

profile_prefix="devbox-minikube-${mode}-"
check_no_stale_resources() {
  local resource_kind="$1"
  shift
  local output stale
  if ! output="$("$@" 2>&1)"; then
    die "could not inspect existing $resource_kind: $output"
  fi
  stale="$(grep -F "$profile_prefix" <<<"$output" || true)"
  if [[ -n "$stale" ]]; then
    printf 'minikube validation: stale %s remain from an interrupted smoke run:\n%s\n' \
      "$resource_kind" "$stale" >&2
    die "remove resources with reserved prefix '$profile_prefix' before retrying"
  fi
}

for tool in minikube kubectl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    die "$tool is required but was not found on PATH; install the provisioned Minikube toolchain"
  fi
done

libvirt_uri="${LIBVIRT_DEFAULT_URI:-qemu:///system}"
case "$mode" in
  podman)
    if ! command -v podman >/dev/null 2>&1; then
      die "Podman mode requires the podman command; install Podman and retry"
    fi
    if ! rootless="$(podman info --format '{{.Host.Security.Rootless}}' 2>&1)"; then
      die "Podman is unavailable; 'podman info' failed: $rootless"
    fi
    if [[ "$rootless" != true ]]; then
      die "Podman mode requires rootless Podman, but podman reports rootless=$rootless"
    fi
    check_no_stale_resources "Podman containers" \
      podman ps --all --format '{{.Names}} {{.Labels}}'
    check_no_stale_resources "Podman volumes" podman volume ls --format '{{.Name}}'
    check_no_stale_resources "Podman networks" podman network ls --format '{{.Name}}'
    driver=podman
    ;;
  kvm2)
    if [[ "$(uname -m)" != x86_64 ]]; then
      die "Minikube v1.39 KVM2 mode is supported only on x86_64 Linux"
    fi
    if [[ ! -e /dev/kvm ]]; then
      die "KVM2 requires /dev/kvm; enable nested KVM on the host and expose the device to this VM"
    fi
    if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
      die "KVM2 requires read/write access to /dev/kvm; grant the devbox user access through the kvm group"
    fi
    if ! grep -Eq '(^|[[:space:]])(vmx|svm)([[:space:]]|$)' /proc/cpuinfo; then
      die "KVM2 requires VMX or SVM CPU flags on x86_64; enable nested virtualization on the host"
    fi
    if ! command -v virsh >/dev/null 2>&1; then
      die "KVM2 requires virsh and libvirt; install the libvirt client and daemon"
    fi
    if ! command -v "qemu-system-$(uname -m)" >/dev/null 2>&1; then
      die "KVM2 requires QEMU support (qemu-system-$(uname -m) is missing); install the QEMU system emulator"
    fi
    export LIBVIRT_DEFAULT_URI="$libvirt_uri"
    if ! virsh -c "$LIBVIRT_DEFAULT_URI" uri >/dev/null 2>&1 \
      || ! virsh -c "$LIBVIRT_DEFAULT_URI" list --all >/dev/null 2>&1; then
      die "cannot connect to libvirt at $LIBVIRT_DEFAULT_URI; start libvirt and grant the devbox user access"
    fi
    if ! capabilities="$(virsh -c "$LIBVIRT_DEFAULT_URI" capabilities 2>&1)"; then
      die "cannot query libvirt capabilities at $LIBVIRT_DEFAULT_URI: $capabilities"
    fi
    if ! grep -Eq "<domain[[:space:]]+type=['\"]kvm['\"]/?>" <<<"$capabilities"; then
      die "libvirt at $LIBVIRT_DEFAULT_URI does not advertise KVM/QEMU support; install and enable the QEMU KVM emulator"
    fi
    check_no_stale_resources "libvirt virtual machines" \
      virsh -c "$LIBVIRT_DEFAULT_URI" list --all --name
    check_no_stale_resources "libvirt networks" \
      virsh -c "$LIBVIRT_DEFAULT_URI" net-list --all --name
    driver=kvm2
    ;;
esac

umask 077
state_dir="$(mktemp -d "${TMPDIR:-/tmp/opencode}/my-sandbox-minikube.XXXXXXXXXX")" \
  || die 'could not create private temporary state'
if ! chmod 0700 -- "$state_dir"; then
  rmdir -- "$state_dir" 2>/dev/null || true
  die "could not secure private state directory $state_dir"
fi
export MINIKUBE_HOME="$state_dir/minikube"
export KUBECONFIG="$state_dir/kubeconfig"
profile_id="${state_dir##*.}"
profile="${profile_prefix}${profile_id,,}"
cluster_may_exist=false

cleanup() {
  local status=$?
  local cleanup_failed=0
  local output
  local resource_kind
  local -a resource_args
  trap - EXIT HUP INT TERM
  set +e

  if [[ "$cluster_may_exist" == true ]]; then
    if ! output="$(minikube delete --profile "$profile" --purge 2>&1)"; then
      printf 'minikube validation: cleanup could not delete profile %s: %s\n' \
        "$profile" "$output" >&2
      cleanup_failed=1
    fi
  fi

  if [[ "$mode" == podman ]]; then
    if output="$(podman ps --all --format '{{.Names}} {{.Labels}}' 2>&1)"; then
      if [[ "$output" == *"$profile"* ]]; then
        printf 'minikube validation: Podman resources matching profile %s remain:\n%s\n' \
          "$profile" "$output" >&2
        cleanup_failed=1
      fi
    else
      printf 'minikube validation: cleanup could not inspect Podman containers: %s\n' \
        "$output" >&2
      cleanup_failed=1
    fi
    if output="$(podman volume ls --format '{{.Name}}' 2>&1)"; then
      if [[ "$output" == *"$profile"* ]]; then
        printf 'minikube validation: Podman volumes matching profile %s remain:\n%s\n' \
          "$profile" "$output" >&2
        cleanup_failed=1
      fi
    else
      printf 'minikube validation: cleanup could not inspect Podman volumes: %s\n' \
        "$output" >&2
      cleanup_failed=1
    fi
    if output="$(podman network ls --format '{{.Name}}' 2>&1)"; then
      if [[ "$output" == *"$profile"* ]]; then
        printf 'minikube validation: Podman networks matching profile %s remain:\n%s\n' \
          "$profile" "$output" >&2
        cleanup_failed=1
      fi
    else
      printf 'minikube validation: cleanup could not inspect Podman networks: %s\n' \
        "$output" >&2
      cleanup_failed=1
    fi
  else
    for resource_kind in virtual-machines networks; do
      if [[ "$resource_kind" == virtual-machines ]]; then
        resource_args=(list --all --name)
      else
        resource_args=(net-list --all --name)
      fi
      if output="$(virsh -c "$LIBVIRT_DEFAULT_URI" "${resource_args[@]}" 2>&1)"; then
        if [[ "$output" == *"$profile"* ]]; then
          printf 'minikube validation: libvirt %s matching profile %s remain:\n%s\n' \
            "$resource_kind" "$profile" "$output" >&2
          cleanup_failed=1
        fi
      else
        printf 'minikube validation: cleanup could not inspect libvirt %s: %s\n' \
          "$resource_kind" "$output" >&2
        cleanup_failed=1
      fi
    done
  fi

  if ((cleanup_failed)); then
    printf 'minikube validation: preserving private state for diagnosis at %s\n' \
      "$state_dir" >&2
    if ((status == 0)); then
      status=1
    fi
  elif ! rm -rf -- "$state_dir"; then
    printf 'minikube validation: cleanup could not remove private state at %s\n' \
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

mkdir -m 0700 -- "$MINIKUBE_HOME" \
  || die "could not create private Minikube home under $state_dir"

if [[ "$mode" == podman ]]; then
  # Minikube stores this setting under the private MINIKUBE_HOME above.
  minikube config set rootless true
fi

printf 'minikube validation: starting isolated %s profile %s\n' "$mode" "$profile"
cluster_may_exist=true
minikube start \
  --profile="$profile" \
  --driver="$driver" \
  --container-runtime=containerd \
  --cpus=2 \
  --memory=4096 \
  --wait=all \
  --wait-timeout=10m

node_states="$(kubectl --kubeconfig="$KUBECONFIG" --context="$profile" get nodes \
  -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}')"
if [[ -z "$node_states" ]]; then
  die "profile $profile has no cluster nodes"
fi
ready_node_count=0
while IFS=' ' read -r node_name node_ready; do
  [[ -n "$node_name" ]] || continue
  if [[ "$node_ready" != True ]]; then
    die "cluster node $node_name is not Ready (status: ${node_ready:-missing})"
  fi
  ((ready_node_count += 1))
done <<<"$node_states"
if ((ready_node_count == 0)); then
  die "profile $profile has no cluster nodes"
fi

workload="${profile}-smoke"
# Public multi-architecture OCI digest (split for line length; not a secret).
workload_digest_prefix="bdf57e528e45e4433820e045b29b4597" # pragma: allowlist secret
workload_digest_suffix="825a1c9e38353532d90a01445013f82e" # pragma: allowlist secret
workload_image="docker.io/library/busybox:1.37.0@sha256:${workload_digest_prefix}${workload_digest_suffix}"
kubectl --kubeconfig="$KUBECONFIG" --context="$profile" run "$workload" \
  --image="$workload_image" \
  --restart=Never \
  --command -- sh -c 'sleep 300'
kubectl --kubeconfig="$KUBECONFIG" --context="$profile" wait \
  --for=condition=Ready "pod/$workload" --timeout=5m
pod_state="$(kubectl --kubeconfig="$KUBECONFIG" --context="$profile" \
  get pod "$workload" \
  -o 'jsonpath={.status.phase} {.status.containerStatuses[0].ready}')"
if [[ "$pod_state" != 'Running true' ]]; then
  die "BusyBox workload $workload did not reach Running/Ready (state: ${pod_state:-missing})"
fi

printf 'minikube validation passed: %s\n' "$mode"
