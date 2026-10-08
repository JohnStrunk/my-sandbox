#!/usr/bin/env bash
# Smoke-test one explicitly selected Minikube backend in isolated state.
set -Eeuo pipefail

usage() {
  printf 'usage: %s {docker|podman|kvm2}\n' "$0" >&2
}

if (($# != 1)); then
  usage
  exit 2
fi

mode="$1"
case "$mode" in
  docker | podman | kvm2) ;;
  *)
    printf "minikube validation: unsupported backend '%s'; choose docker, podman, or kvm2\n" \
      "$mode" >&2
    usage
    exit 2
    ;;
esac

die() {
  printf 'minikube validation: %s\n' "$*" >&2
  exit 1
}

state_tmp_dir="${TMPDIR:-/tmp/opencode}"
if [[ "$mode" == kvm2 ]]; then
  state_tmp_dir="${TMPDIR:-/var/tmp}"
  if ! command -v findmnt >/dev/null 2>&1; then
    die 'KVM2 requires findmnt to verify disk-backed temporary storage'
  fi
  if ! tmp_filesystem="$(findmnt -n -o FSTYPE -T "$state_tmp_dir")"; then
    die "could not inspect KVM2 temporary directory $state_tmp_dir with findmnt"
  fi
  case "$tmp_filesystem" in
    tmpfs | ramfs | devtmpfs | hugetlbfs)
      die "KVM2 needs disk-backed temporary storage for its multi-GiB L2 disk; set TMPDIR=/var/tmp instead of $state_tmp_dir"
      ;;
  esac
  export TMPDIR="$state_tmp_dir"
fi

profile_prefix="devbox-minikube-${mode}-"
kubernetes_version='v1.37.0'
check_no_stale_resources() {
  local resource_kind="$1"
  shift
  local output stale
  if ! output="$("$@")"; then
    die "could not inspect existing $resource_kind (see command diagnostic above)"
  fi
  stale="$(grep -F "$profile_prefix" <<<"$output" || true)"
  if [[ -n "$stale" ]]; then
    printf 'minikube validation: stale %s remain from an interrupted smoke run:\n%s\n' \
      "$resource_kind" "$stale" >&2
    die "remove resources with reserved prefix '$profile_prefix' before retrying"
  fi
}

check_no_stale_container_resources() {
  local backend="$1" display_name="$2" resource_kind
  for resource_kind in containers volumes networks; do
    if ! container_backend_check_no_stale_resources \
      "$backend" "$resource_kind" "$profile_prefix" \
      "minikube validation" "$display_name"; then
      exit 1
    fi
  done
}

for tool in minikube kubectl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    die "$tool is required but was not found on PATH; install the provisioned Minikube toolchain"
  fi
done

libvirt_uri="${LIBVIRT_DEFAULT_URI:-qemu:///system}"
script_path="${BASH_SOURCE[0]}"
script_dir="${script_path%/*}"
if [[ "$script_dir" == "$script_path" ]]; then
  script_dir='.'
fi
script_dir="$(cd -- "$script_dir" && pwd)"
# shellcheck source=lima/container-backend-resources.sh
source "$script_dir/container-backend-resources.sh"
# shellcheck source=lima/podman-local-preflight.sh
source "$script_dir/podman-local-preflight.sh"
case "$mode" in
  docker)
    # shellcheck source=lima/docker-ce-preflight.sh
    source "$script_dir/docker-ce-preflight.sh"
    if ! docker_ce_preflight "$script_dir/tool-versions.json"; then
      die 'Docker mode requires the pinned rootless Docker CE service and socket; see the preflight diagnostic above'
    fi
    check_no_stale_container_resources docker Docker
    driver=docker
    ;;
  podman)
    if ! podman_local_preflight; then
      die 'Podman mode requires local rootless Podman with no default system connection; see the preflight diagnostic above'
    fi
    check_no_stale_container_resources podman Podman
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
    if ! capabilities="$(virsh -c "$LIBVIRT_DEFAULT_URI" capabilities)"; then
      die "cannot query libvirt capabilities at $LIBVIRT_DEFAULT_URI (see virsh diagnostic above)"
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
state_dir=''
cleanup_private_state() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "${state_dir:-}" && -d "$state_dir" ]] && ! rm -rf -- "$state_dir"; then
    printf 'minikube validation: cleanup could not remove private state at %s\n' \
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
state_dir="$(mktemp -d "$state_tmp_dir/my-sandbox-minikube.XXXXXXXXXX")" \
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

audit_container_backend_resources() {
  local backend="$1" display_name="$2" resource_kind output resource_label matching
  local cleanup_failed=0
  for resource_kind in containers volumes networks; do
    if ! output="$(container_backend_list "$backend" "$resource_kind")"; then
      printf 'minikube validation: cleanup could not inspect %s %s (see runtime diagnostic above)\n' \
        "$display_name" "$resource_kind" >&2
      cleanup_failed=1
      continue
    fi
    matching="$(grep -F "$profile" <<<"$output" || true)"
    if [[ -n "$matching" ]]; then
      case "$resource_kind" in
        containers) resource_label="$display_name resources" ;;
        volumes) resource_label="$display_name volumes" ;;
        networks) resource_label="$display_name networks" ;;
      esac
      printf 'minikube validation: %s matching profile %s remain:\n%s\n' \
        "$resource_label" "$profile" "$matching" >&2
      cleanup_failed=1
    fi
  done
  return "$cleanup_failed"
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  local output matching backend_display_name
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

  if [[ "$mode" == docker || "$mode" == podman ]]; then
    if [[ "$mode" == docker ]]; then
      backend_display_name='Docker'
    else
      backend_display_name='Podman'
    fi
    if ! audit_container_backend_resources "$mode" "$backend_display_name"; then
      cleanup_failed=1
    fi
  else
    for resource_kind in virtual-machines networks; do
      if [[ "$resource_kind" == virtual-machines ]]; then
        resource_args=(list --all --name)
      else
        resource_args=(net-list --all --name)
      fi
      if output="$(virsh -c "$LIBVIRT_DEFAULT_URI" "${resource_args[@]}")"; then
        matching="$(grep -F "$profile" <<<"$output" || true)"
        if [[ -n "$matching" ]]; then
          printf 'minikube validation: libvirt %s matching profile %s remain:\n%s\n' \
            "$resource_kind" "$profile" "$matching" >&2
          cleanup_failed=1
        fi
      else
        printf 'minikube validation: cleanup could not inspect libvirt %s (see virsh diagnostic above)\n' \
          "$resource_kind" >&2
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

verify_backend_instance() {
  local output matching domain_xml domain_state
  case "$mode" in
    docker | podman)
      if ! output="$("$mode" ps --all --format '{{.Names}} {{.Labels}}')"; then
        die "could not inspect $mode after Minikube start (see runtime diagnostic above)"
      fi
      matching="$(grep -F "$profile" <<<"$output" || true)"
      if [[ -z "$matching" ]]; then
        die "Minikube profile $profile is absent from the explicitly selected $mode backend; no fallback is allowed"
      fi
      if [[ "$mode" == docker ]]; then
        printf 'minikube validation: backend identity verified: Docker CE container for %s\n' \
          "$profile"
      else
        printf 'minikube validation: backend identity verified: rootless Podman container for %s\n' \
          "$profile"
      fi
      ;;
    kvm2)
      if ! output="$(virsh -c "$LIBVIRT_DEFAULT_URI" list --all --name)"; then
        die 'could not inspect libvirt after Minikube start (see virsh diagnostic above)'
      fi
      if ! grep -Fxq "$profile" <<<"$output"; then
        die "Minikube profile $profile is absent from libvirt; the requested KVM2 L2 backend was not proven"
      fi
      if ! domain_xml="$(virsh -c "$LIBVIRT_DEFAULT_URI" dumpxml "$profile")"; then
        die "could not inspect libvirt domain for Minikube profile $profile (see virsh diagnostic above)"
      fi
      if ! grep -Eq "<domain[[:space:]]+type=['\"]kvm['\"]" <<<"$domain_xml"; then
        die "libvirt domain for Minikube profile $profile is not explicitly KVM-backed"
      fi
      if ! domain_state="$(virsh -c "$LIBVIRT_DEFAULT_URI" domstate "$profile")"; then
        die "could not inspect libvirt state for Minikube profile $profile (see virsh diagnostic above)"
      fi
      if [[ "${domain_state,,}" != running ]]; then
        die "libvirt domain for Minikube profile $profile is not running (state: $domain_state)"
      fi
      printf 'minikube validation: backend identity verified: KVM2 libvirt L2 VM for %s\n' \
        "$profile"
      ;;
  esac
}

printf 'minikube validation: starting isolated %s profile %s\n' "$mode" "$profile"
cluster_may_exist=true
minikube start \
  --profile="$profile" \
  --driver="$driver" \
  --kubernetes-version="$kubernetes_version" \
  --container-runtime=containerd \
  --cpus=2 \
  --memory=4096 \
  --wait=all \
  --wait-timeout=10m
verify_backend_instance

kubectl --kubeconfig="$KUBECONFIG" --context="$profile" wait \
  --for=condition=Ready nodes --all --timeout=5m
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
