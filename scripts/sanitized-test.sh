#!/usr/bin/env bash
# Run tests with host credentials and configuration discovery disabled.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: sanitized-test.sh [options] -- command [arg ...]

Run a command with a temporary HOME/XDG tree and a small non-secret
environment allowlist.

Options:
  --resource-preflight
                      Report cgroup resource limits and block constrained
                      parallel test commands before they start.
  --resource-cgroup-root DIR
                      Cgroup hierarchy to inspect (defaults to /sys/fs/cgroup).
  --require-vm        Require accessible /dev/kvm before starting the command.
  --require-recursive-vm
                      Also require host KVM nested virtualization to be enabled.
  --guest-vm          Run in the current guest with a scrubbed environment.
                      Same-UID mounted host files remain readable; use only
                      with trusted source (CI uses an empty-config guest).
  -h, --help          Show this help text.
EOF
}

resource_preflight_enabled=false
require_vm=false
require_recursive_vm=false
guest_vm=false
resource_cgroup_root="/sys/fs/cgroup"
while (($# > 0)); do
  case "$1" in
    --resource-preflight)
      resource_preflight_enabled=true
      shift
      ;;
    --require-vm)
      require_vm=true
      shift
      ;;
    --require-recursive-vm)
      require_vm=true
      require_recursive_vm=true
      shift
      ;;
    --guest-vm)
      guest_vm=true
      shift
      ;;
    --resource-cgroup-root)
      if (($# < 2)) || [[ -z "$2" || "$2" == -* ]]; then
        printf 'sanitized-test: --resource-cgroup-root requires a directory\n' >&2
        exit 2
      fi
      resource_cgroup_root="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -* | "")
      printf 'sanitized-test: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if (($# == 0)); then
  printf 'sanitized-test: a command is required\n' >&2
  usage >&2
  exit 2
fi

host_path="${PATH:-/usr/local/bin:/usr/bin:/bin}"
if [[ "$require_vm" == true ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' \
      'sanitized-test: python3 is required for the VM capability preflight.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    exit 125
  fi
  vm_preflight_args=()
  if [[ "$require_recursive_vm" == true ]]; then
    vm_preflight_args+=(--recursive)
  fi
  if env -i "PATH=$host_path" python3 -I \
    "$SCRIPT_DIR/vm_preflight.py" "${vm_preflight_args[@]}"; then
    :
  else
    preflight_status=$?
    exit "$preflight_status"
  fi
fi

runtime_root="$(mktemp -d "${TMPDIR:-/tmp}/my-sandbox-sanitized.XXXXXX")" || {
  printf '%s\n' \
    'sanitized-test: could not create a temporary isolation directory.' \
    'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
  exit 125
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
  local exit_status=$?
  if ! rm -rf -- "$runtime_root" 2>/dev/null; then
    printf 'sanitized-test: warning: could not remove temporary directory %s\n' \
      "$runtime_root" >&2
  fi
  return "$exit_status"
}
trap cleanup EXIT

isolated_home="$runtime_root/home"
isolated_xdg_config_home="$runtime_root/xdg/config"
isolated_xdg_data_home="$runtime_root/xdg/share"
isolated_xdg_state_home="$runtime_root/xdg/state"
isolated_xdg_cache_home="$runtime_root/xdg/cache"
isolated_xdg_runtime_dir="$runtime_root/xdg/runtime"
isolated_tmp="$runtime_root/tmp"
if ! mkdir -p \
  "$isolated_home" \
  "$isolated_xdg_config_home" \
  "$isolated_xdg_data_home" \
  "$isolated_xdg_state_home" \
  "$isolated_xdg_cache_home" \
  "$isolated_xdg_runtime_dir" \
  "$isolated_tmp"; then
  printf '%s\n' \
    'sanitized-test: could not create the temporary isolation directories.' \
    'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
  exit 125
fi
if ! chmod 700 \
  "$isolated_home" \
  "$isolated_xdg_runtime_dir" \
  "$isolated_tmp"; then
  printf '%s\n' \
    'sanitized-test: could not secure the temporary isolation directories.' \
    'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
  exit 125
fi

safe_env=(
  "HOME=$isolated_home"
  "PATH=$host_path"
  "TMPDIR=$isolated_tmp"
  "XDG_CONFIG_HOME=$isolated_xdg_config_home"
  "XDG_DATA_HOME=$isolated_xdg_data_home"
  "XDG_STATE_HOME=$isolated_xdg_state_home"
  "XDG_CACHE_HOME=$isolated_xdg_cache_home"
  "XDG_RUNTIME_DIR=$isolated_xdg_runtime_dir"
)
if [[ "$guest_vm" == true ]]; then
  safe_env+=("MY_SANDBOX_VM_TEST_IN_GUEST=1")
fi
for name in LANG LC_ALL LC_CTYPE TERM CI DEVBOX_VM_START_TIMEOUT \
  MY_SANDBOX_VM_TEST_FRESH; do
  if [[ -n "${!name-}" ]]; then
    safe_env+=("$name=${!name}")
  fi
done

resource_preflight() {
  if [[ ! -r "$SCRIPT_DIR/resource_preflight.py" ]]; then
    printf '%s\n' \
      'sanitized-test: resource preflight script is unavailable.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    return 125
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' \
      'sanitized-test: python3 is required for the resource preflight.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    return 125
  fi
  env -i -- "${safe_env[@]}" python3 -I "$SCRIPT_DIR/resource_preflight.py" \
    --cgroup-root "$resource_cgroup_root" --fail-on-constrained
}

if [[ "$resource_preflight_enabled" == true ]]; then
  if resource_preflight; then
    :
  else
    preflight_status=$?
    exit "$preflight_status"
  fi
fi

# Bounded interruption forwarding: terminate the whole test command tree and
# escalate to SIGKILL if a child ignores the initial signal.
TERM_GRACE_SECONDS=10
KILL_GRACE_SECONDS=10
command_pid=""

# These helpers are invoked from signal traps rather than directly.
# shellcheck disable=SC2329
command_group_alive() {
  kill -0 -- "-$command_pid" 2>/dev/null || return 1
  [[ -r /proc/1/stat ]] || return 0
  local entry line rest state _ppid pgrp _rest
  for entry in /proc/[0-9]*; do
    IFS= read -r line 2>/dev/null < "$entry/stat" || continue
    rest="${line##*)}"
    read -r state _ppid pgrp _rest <<<"$rest"
    if [[ "$pgrp" == "$command_pid" && "$state" != "Z" ]]; then
      return 0
    fi
  done
  return 1
}

# shellcheck disable=SC2329
wait_command_group_gone() {
  local i
  for ((i = 0; i < $1; i++)); do
    command_group_alive || return 0
    sleep 0.1
  done
  ! command_group_alive
}

# shellcheck disable=SC2329
terminate_command_group() {
  local forwarded="$1"
  kill -"$forwarded" -- "-$command_pid" 2>/dev/null || true
  if ! wait_command_group_gone $((TERM_GRACE_SECONDS * 10)); then
    kill -KILL -- "-$command_pid" 2>/dev/null || true
    if ! wait_command_group_gone $((KILL_GRACE_SECONDS * 10)); then
      printf 'sanitized-test: WARNING: command process group %s still has live members after SIGKILL.\n' \
        "$command_pid" >&2
      return 1
    fi
  fi
  wait "$command_pid" 2>/dev/null || true
  return 0
}

# shellcheck disable=SC2329
forward_signal_to_command() {
  local received="$1"
  printf 'sanitized-test: received SIG%s; terminating the command process group.\n' \
    "$received" >&2
  if [[ -n "$command_pid" ]]; then
    terminate_command_group "$received" || true
  fi
  exit "$((128 + $(kill -l "$received")))"
}

trap 'forward_signal_to_command TERM' TERM
trap 'forward_signal_to_command INT' INT
trap 'forward_signal_to_command HUP' HUP

set +e
set -m
env -i -- "${safe_env[@]}" "$@" &
command_pid=$!
command_status=0
wait "$command_pid" || command_status=$?
set +m
set -e
exit "$command_status"
