#!/usr/bin/env bash
# Run kind with the devbox's supported rootless-Podman configuration.
#
# The regular nested Podman configuration intentionally disables cgroups and
# falls back to pasta networking when the outer host cannot provide bridge
# sysctls. kind nodes need the stricter rootless requirements, so they use a
# separate configuration and fail before provisioning when those requirements
# are not available.
set -euo pipefail

readonly INFRASTRUCTURE_EXIT=125
readonly CONFIGURATION_EXIT=2
readonly KIND_CONFIG="${DEVBOX_KIND_CONTAINERS_CONF:-/sandbox/.config/containers/kind-containers.conf}"

usage() {
  cat <<'EOF'
Usage: devbox-kind <preflight|kubectl|kind-command> [arg ...]

Run a kind Kubernetes cluster with rootless Podman inside devbox.

Commands:
  preflight             Check the host/runtime capabilities without creating a cluster
  kubectl [arg ...]     Run kubectl without changing its configuration
  create ...            Run kind create after a successful preflight
  delete ...            Run kind delete without a preflight so cleanup remains possible
  <other kind command>  Run another kind command with the supported provider/configuration

The supported provider is rootless Podman. Use `devbox --kind` so the outer
container shares the delegated cgroup namespace. A supported host normally
starts it with:

  systemd-run --scope --user -p Delegate=yes devbox --recreate --kind
EOF
}

failures=()
configuration_failure=false
host_capability_failure=false
runtime_ready=false

record_failure() {
  failures+=("$1")
  configuration_failure=true
}

record_host_failure() {
  failures+=("$1")
  host_capability_failure=true
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    record_failure "'$command_name' is not available on PATH"
    return 1
  fi
  return 0
}

config_contains() {
  local pattern="$1"
  grep -Eq "$pattern" "$KIND_CONFIG"
}

check_provider() {
  local provider="${KIND_EXPERIMENTAL_PROVIDER:-podman}"
  if [[ "$provider" != podman ]]; then
    record_failure "KIND_EXPERIMENTAL_PROVIDER='$provider' is unsupported; use 'podman'"
  fi
}

check_toolchain() {
  local kind_available=true
  local kubectl_available=true
  require_command kind || kind_available=false
  require_command kubectl || kubectl_available=false
  if ! require_command podman; then
    return 0
  fi

  if [[ "$kind_available" == true ]]; then
    local kind_version_output
    if ! kind_version_output="$(kind version 2>&1)"; then
      record_failure "kind is installed but 'kind version' failed: $kind_version_output"
    fi
  fi
  if [[ "$kubectl_available" == true ]]; then
    local kubectl_version_output
    if ! kubectl_version_output="$(kubectl version --client=true 2>&1)"; then
      record_failure "kubectl is installed but its client check failed: $kubectl_version_output"
    fi
  fi
}

check_kind_config() {
  if [[ ! -r "$KIND_CONFIG" ]]; then
    record_failure "kind Podman configuration is missing or unreadable: $KIND_CONFIG"
  else
    for config_requirement in \
      'cgroups[[:space:]]*=[[:space:]]*"enabled"' \
      'cgroupns[[:space:]]*=[[:space:]]*"host"' \
      'default_sysctls[[:space:]]*=[[:space:]]*\[\]' \
      'log_driver[[:space:]]*=[[:space:]]*"k8s-file"' \
      'pids_limit[[:space:]]*=[[:space:]]*65536' \
      'volumes[[:space:]]*=[[:space:]]*\["/proc:/proc"\]' \
      'utsns[[:space:]]*=[[:space:]]*"host"' \
      'netns[[:space:]]*=[[:space:]]*"bridge"' \
      'cgroup_manager[[:space:]]*=[[:space:]]*"cgroupfs"'; do
      if ! config_contains "$config_requirement"; then
        record_failure "kind Podman configuration is missing: $config_requirement"
      fi
    done
  fi
}

check_podman_runtime() {
  if ! command -v podman >/dev/null 2>&1; then
    return 0
  fi

  local podman_info
  if ! podman_info="$(CONTAINERS_CONF="$KIND_CONFIG" podman info --format json 2>&1)"; then
    record_host_failure "nested Podman preflight failed: $podman_info"
    return 0
  fi

  local rootless cgroup_version controllers capability_ok=true
  rootless="$(jq -r '.host.security.rootless // false' <<<"$podman_info" 2>/dev/null || true)"
  cgroup_version="$(jq -r '.host.cgroupVersion // "unknown"' <<<"$podman_info" 2>/dev/null || true)"
  controllers="$(jq -r '.host.cgroupControllers // [] | join(",")' <<<"$podman_info" 2>/dev/null || true)"
  if [[ "$rootless" != true ]]; then
    record_host_failure "nested Podman is not rootless (reported rootless=$rootless)"
    capability_ok=false
  fi
  if [[ "$cgroup_version" != v2 ]]; then
    record_host_failure "rootless kind requires cgroup v2 (reported $cgroup_version)"
    capability_ok=false
  fi
  for controller in cpu memory pids; do
    if [[ ",${controllers}," != *",${controller},"* ]]; then
      record_host_failure "cgroup controller '$controller' is not delegated (reported: ${controllers:-none})"
      capability_ok=false
    fi
  done

  local unshare_output
  if ! unshare_output="$(CONTAINERS_CONF="$KIND_CONFIG" timeout 10 podman unshare true 2>&1)"; then
    record_host_failure "nested user-namespace probe failed: $unshare_output"
    capability_ok=false
  fi
  runtime_ready="$capability_ok"
}

check_bridge_network() {
  if [[ "$runtime_ready" != true ]]; then
    return 0
  fi
  local network_name="devbox-kind-preflight-$$"
  local network_maybe_created=false
  local network_output
  if network_output="$(CONTAINERS_CONF="$KIND_CONFIG" timeout 30 podman network create "$network_name" 2>&1)"; then
    network_maybe_created=true
  else
    record_host_failure "rootless bridge network could not be created: $network_output"
    # A timed-out create may have reached Podman after the client stopped
    # waiting, so always attempt bounded cleanup below.
    network_maybe_created=true
  fi
  if [[ "$network_maybe_created" == true ]]; then
    local remove_output
    local network_removed=false
    for _ in {1..3}; do
      if remove_output="$(CONTAINERS_CONF="$KIND_CONFIG" timeout 10 podman network rm "$network_name" 2>&1)"; then
        network_removed=true
        break
      fi
      sleep 0.2
    done
    if [[ "$network_removed" != true ]]; then
      record_host_failure "kind network preflight could not clean up '$network_name': $remove_output"
    fi
  fi
}

report_preflight() {
  if [[ "${#failures[@]}" -gt 0 ]]; then
    printf '%s\n' 'kind runtime preflight failed; no cluster was created.' >&2
    for failure in "${failures[@]}"; do
      printf '  - %s\n' "$failure" >&2
    done
    if [[ "$configuration_failure" == true ]]; then
      printf '%s\n' \
        'Fix the image/tool/configuration errors above before retrying kind.' \
        'This is a configuration failure, not a host-capability skip.' >&2
      return "$CONFIGURATION_EXIT"
    fi
    if [[ "$host_capability_failure" == true ]]; then
      printf '%s\n' \
        'Run the outer devbox from a delegated cgroup-v2 user scope, for example:' \
        '  systemd-run --scope --user -p Delegate=yes devbox --recreate --kind' \
        "Then retry \`devbox --kind devbox-kind create cluster\`." >&2
      return "$INFRASTRUCTURE_EXIT"
    fi
    return "$CONFIGURATION_EXIT"
  fi

  printf '%s\n' \
    'kind runtime preflight passed: rootless Podman, cgroup v2 delegation,' \
    'k8s-file logging, PID capacity, and bridge networking are available.'
}

run_preflight() {
  failures=()
  configuration_failure=false
  host_capability_failure=false
  runtime_ready=false
  check_provider
  check_toolchain
  check_kind_config
  check_podman_runtime
  check_bridge_network
  report_preflight
}

run_kind() {
  KIND_EXPERIMENTAL_PROVIDER=podman \
    CONTAINERS_CONF="$KIND_CONFIG" \
    kind "$@"
}

case "${1:-}" in
  --help | -h)
    usage
    exit 0
    ;;
  "")
    usage >&2
    exit 2
    ;;
  preflight)
    if [[ $# -ne 1 ]]; then
      printf '%s\n' 'preflight does not accept additional arguments' >&2
      exit 2
    fi
    run_preflight
    ;;
  kubectl)
    shift
    exec kubectl "$@"
    ;;
  create)
    run_preflight
    run_kind "$@"
    ;;
  *)
    run_kind "$@"
    ;;
esac
