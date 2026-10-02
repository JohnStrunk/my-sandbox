#!/usr/bin/env bash
# Run tests with host credentials and configuration discovery disabled.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
original_home="${HOME:-}"
vm_lock_fd="${MY_SANDBOX_VM_TEST_LOCK_FD:-}"
unset MY_SANDBOX_VM_TEST_LOCK_FD

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
  --vm-lock           Serialize full test commands that share the devbox user;
                      requires flock and Python 3.
  --vm-lock-timeout SECONDS
                      Maximum lock wait (0-86400 seconds; default: 3600).
  -h, --help          Show this help text.
EOF
}

resource_preflight_enabled=false
require_vm=false
require_recursive_vm=false
guest_vm=false
resource_cgroup_root="/sys/fs/cgroup"
vm_lock_enabled=false
vm_lock_timeout=3600
vm_lock_timeout_explicit=false
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
    --vm-lock)
      vm_lock_enabled=true
      shift
      ;;
    --vm-lock-timeout)
      if (($# < 2)) || [[ -z "$2" || "$2" == -* ]]; then
        printf 'sanitized-test: --vm-lock-timeout requires whole seconds\n' >&2
        exit 2
      fi
      if [[ ! "$2" =~ ^[0-9]{1,5}$ ]] || ((10#$2 > 86400)); then
        printf 'sanitized-test: --vm-lock-timeout must be an integer from 0 to 86400\n' >&2
        exit 2
      fi
      vm_lock_timeout=$((10#$2))
      vm_lock_timeout_explicit=true
      shift 2
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

if [[ "$vm_lock_timeout_explicit" == true && "$vm_lock_enabled" != true ]]; then
  printf 'sanitized-test: --vm-lock-timeout requires --vm-lock\n' >&2
  exit 2
fi

host_path="${PATH:-/usr/local/bin:/usr/bin:/bin}"

# Acquire the shared-test lock before capability/resource preflights or
# temporary-directory setup so another worktree waiting for a full test run
# does not add work to the active suite. The lock lives outside the private
# HOME/TMPDIR created below.
if [[ "$vm_lock_enabled" == true ]]; then
  if [[ "$original_home" != /* || "$original_home" == "/" || ! -d "$original_home" ]]; then
    printf '%s\n' \
      'sanitized-test: --vm-lock requires an existing absolute original HOME.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    exit 125
  fi
  resolved_home="$(realpath -e -- "$original_home" 2>/dev/null || true)"
  if [[ -z "$resolved_home" ]]; then
    printf 'sanitized-test: could not resolve original HOME for shared-VM lock: %s\n' \
      "$original_home" >&2
    exit 125
  fi
  original_home="$resolved_home"
  current_uid="$(id -u)"
  directory="$original_home"
  while [[ "$directory" != "/" ]]; do
    directory_owner="$(stat -c %u -- "$directory" 2>/dev/null || true)"
    directory_mode="$(stat -c %a -- "$directory" 2>/dev/null || true)"
    if [[ -z "$directory_owner" || -z "$directory_mode" ]]; then
      printf 'sanitized-test: could not inspect original HOME path component %s\n' \
        "$directory" >&2
      exit 125
    fi
    directory_mode_value=$((8#$directory_mode))
    if [[ "$directory" == "$original_home" ]]; then
      if [[ "$directory_owner" != "$current_uid" ]] \
        || (( (directory_mode_value & 0022) != 0 )); then
        printf 'sanitized-test: original HOME must be owned by uid %s and not group/world-writable: %s\n' \
          "$current_uid" "$directory" >&2
        exit 125
      fi
    elif (( (directory_mode_value & 0022) != 0 )) \
      && (( (directory_mode_value & 01000) == 0 )); then
      printf 'sanitized-test: original HOME ancestor is group/world-writable without the sticky bit: %s\n' \
        "$directory" >&2
      exit 125
    fi
    directory="${directory%/*}"
    [[ -n "$directory" ]] || directory="/"
  done
  if ! command -v flock >/dev/null 2>&1; then
    printf '%s\n' \
      'sanitized-test: flock is required for --vm-lock.' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    exit 125
  fi

  vm_lock_dir="$original_home/.cache"
  vm_lock_file="$vm_lock_dir/my-sandbox-vm-tests.lock"
  if ! (umask 077; mkdir -p -- "$vm_lock_dir"); then
    printf 'sanitized-test: could not create shared-VM lock directory %s\n' \
      "$vm_lock_dir" >&2
    exit 125
  fi
  if [[ -L "$vm_lock_dir" || ! -d "$vm_lock_dir" ]]; then
    printf 'sanitized-test: shared-VM lock directory must be a real directory: %s\n' \
      "$vm_lock_dir" >&2
    exit 125
  fi
  lock_dir_owner="$(stat -c %u -- "$vm_lock_dir" 2>/dev/null || true)"
  lock_dir_mode="$(stat -c %a -- "$vm_lock_dir" 2>/dev/null || true)"
  if [[ "$lock_dir_owner" != "$current_uid" || -z "$lock_dir_mode" ]] \
    || (( (8#$lock_dir_mode & 0022) != 0 )); then
    printf 'sanitized-test: shared-VM lock directory must be owned by uid %s and not group/world-writable: %s\n' \
      "$current_uid" "$vm_lock_dir" >&2
    exit 125
  fi
  if [[ -L "$vm_lock_file" || ( -e "$vm_lock_file" && ! -f "$vm_lock_file" ) ]]; then
    printf 'sanitized-test: shared-VM lock path must be a regular file, not a symlink: %s\n' \
      "$vm_lock_file" >&2
    exit 125
  fi

  if [[ -z "$vm_lock_fd" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
      printf '%s\n' \
        'sanitized-test: python3 is required to open the shared-VM test lock safely.' \
        'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
      exit 125
    fi
    lock_reexec_args=()
    if [[ "$resource_preflight_enabled" == true ]]; then
      lock_reexec_args+=(--resource-preflight --resource-cgroup-root "$resource_cgroup_root")
    fi
    if [[ "$require_recursive_vm" == true ]]; then
      lock_reexec_args+=(--require-recursive-vm)
    elif [[ "$require_vm" == true ]]; then
      lock_reexec_args+=(--require-vm)
    fi
    if [[ "$guest_vm" == true ]]; then
      lock_reexec_args+=(--guest-vm)
    fi
    lock_reexec_args+=(--vm-lock --vm-lock-timeout "$vm_lock_timeout" --)
    lock_reexec_args+=("$@")
    exec python3 -I "$SCRIPT_DIR/open_vm_test_lock.py" \
      --lock-directory "$vm_lock_dir" -- \
      "$SCRIPT_DIR/sanitized-test.sh" "${lock_reexec_args[@]}"
  fi

  if [[ ! "$vm_lock_fd" =~ ^([3-9]|[1-9][0-9]+)$ ]]; then
    printf 'sanitized-test: invalid inherited shared-VM lock descriptor: %s\n' \
      "$vm_lock_fd" >&2
    exit 125
  fi
  lock_fd_path="/proc/self/fd/$vm_lock_fd"
  lock_fd_target="$(readlink -f -- "$lock_fd_path" 2>/dev/null || true)"
  lock_file_owner="$(stat -Lc %u -- "$lock_fd_path" 2>/dev/null || true)"
  if [[ ! -f "$lock_fd_path" || "$lock_fd_target" != "$vm_lock_file" \
    || "$lock_file_owner" != "$current_uid" ]]; then
    printf 'sanitized-test: inherited shared-VM lock descriptor is not the expected owned lock file: %s\n' \
      "$vm_lock_file" >&2
    exit 125
  fi

  printf 'sanitized-test: waiting for shared-VM test lock %s (timeout: %ss).\n' \
    "$vm_lock_file" "$vm_lock_timeout" >&2
  if flock --exclusive --wait "$vm_lock_timeout" \
    --conflict-exit-code 124 "$vm_lock_fd"; then
    printf 'sanitized-test: acquired shared-VM test lock.\n' >&2
  else
    lock_status=$?
    if [[ "$lock_status" -eq 124 ]]; then
      printf 'sanitized-test: timed out after %ss waiting for shared-VM test lock %s.\n' \
        "$vm_lock_timeout" "$vm_lock_file" >&2
    else
      printf 'sanitized-test: could not acquire shared-VM test lock %s (flock status %s).\n' \
        "$vm_lock_file" "$lock_status" >&2
    fi
    printf '%s\n' \
      'sanitized-test: this is an infrastructure/runtime configuration failure, not a product test failure.' >&2
    exit 125
  fi
fi

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
  vm_preflight_command=(
    env -i "PATH=$host_path" python3 -I
    "$SCRIPT_DIR/vm_preflight.py" "${vm_preflight_args[@]}"
  )
  if [[ "$vm_lock_enabled" == true ]]; then
    if "${vm_preflight_command[@]}" {vm_lock_fd}>&-; then
      :
    else
      preflight_status=$?
      exit "$preflight_status"
    fi
  elif "${vm_preflight_command[@]}"; then
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
  if [[ "$vm_lock_enabled" == true ]]; then
    env -i -- "${safe_env[@]}" python3 -I "$SCRIPT_DIR/resource_preflight.py" \
      --cgroup-root "$resource_cgroup_root" --fail-on-constrained \
      {vm_lock_fd}>&-
  else
    env -i -- "${safe_env[@]}" python3 -I "$SCRIPT_DIR/resource_preflight.py" \
      --cgroup-root "$resource_cgroup_root" --fail-on-constrained
  fi
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
if [[ "$vm_lock_enabled" == true ]]; then
  # Keep the lock held only by this wrapper, not by test descendants that may
  # outlive the command process.
  env -i -- "${safe_env[@]}" "$@" {vm_lock_fd}>&- &
else
  env -i -- "${safe_env[@]}" "$@" &
fi
command_pid=$!
command_status=0
wait "$command_pid" || command_status=$?
set +m
set -e
exit "$command_status"
