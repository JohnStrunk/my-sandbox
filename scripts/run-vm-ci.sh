#!/usr/bin/env bash
# Start an isolated devbox VM and run one sanitized VM test tier inside it.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
workspace="${GITHUB_WORKSPACE:-$repo_root}"
runner_temp="${RUNNER_TEMP:-/tmp}"
tier="${1:-}"
case "$tier" in
  vm)
    markers="vm or e2e_kind"
    ;;
  recursive)
    markers="recursive"
    # The outer runner already checked the host KVM nested parameter; the L1
    # guest may not expose that host-only sysfs knob to its own process.
    ;;
  *)
    printf 'usage: %s {vm|recursive}\n' "$0" >&2
    exit 2
    ;;
esac

mount_type_is_set="${DEVBOX_VM_TEST_MOUNT_TYPE+x}"
requested_mount_type="${DEVBOX_VM_TEST_MOUNT_TYPE-}"
mount_type_args=()
if [[ "$mount_type_is_set" == x ]]; then
  if [[ "$tier" != vm ]]; then
    printf 'run-vm-ci: DEVBOX_VM_TEST_MOUNT_TYPE is only valid for the vm tier\n' >&2
    exit 2
  fi
  case "$requested_mount_type" in
    9p | virtiofs)
      mount_type_args=(--mount-type "$requested_mount_type")
      ;;
    *)
      printf 'run-vm-ci: DEVBOX_VM_TEST_MOUNT_TYPE must be exactly 9p or virtiofs\n' >&2
      exit 2
      ;;
  esac
fi
# The validated value is now represented only as an argv pair for VM creation;
# do not pass the runner-only control through to later host or guest commands.
unset DEVBOX_VM_TEST_MOUNT_TYPE

# Lima's CLI request is not proof of the mounted filesystem; verify the guest.
verify_requested_mount_type() {
  local checkpoint="$1" effective_mount_type
  if effective_mount_type="$(
    # shellcheck disable=SC2016 # HOME must expand inside the guest shell.
    limactl shell --workdir /workspace/src/my-sandbox devbox \
      bash -c 'findmnt -rn -T "$HOME/.local/share/opencode" -o FSTYPE'
  )"; then
    printf 'run-vm-ci: OpenCode data mount (%s): requested=%s effective=%s\n' \
      "$checkpoint" "$requested_mount_type" "$effective_mount_type" >&2
  else
    printf 'run-vm-ci: OpenCode data mount (%s): requested=%s effective=unavailable (findmnt failed)\n' \
      "$checkpoint" "$requested_mount_type" >&2
    return 1
  fi

  if [[ "$effective_mount_type" != "$requested_mount_type" ]]; then
    printf 'run-vm-ci: OpenCode data mount type mismatch (%s): requested=%s effective=%s\n' \
      "$checkpoint" "$requested_mount_type" "$effective_mount_type" >&2
    return 1
  fi
}

test_root="$(mktemp -d "$runner_temp/my-sandbox-vm-test.XXXXXX")"
host_home="$test_root/home"
lima_home="$test_root/lima"
repo_copy="$host_home/src/my-sandbox"
host_sqlite_helper="$test_root/validate_host_sqlite_mount.py"

export HOME="$host_home"
export LIMA_HOME="$lima_home"
export RUNNER_TEMP="$runner_temp"
preserve_test_root=false
sqlite_helper_started=false

cleanup() {
  local status=$? delete_status cleanup_status=0 delete_output
  trap - EXIT INT TERM HUP QUIT
  set +e
  limactl stop devbox >/dev/null 2>&1
  delete_output="$(limactl delete --force devbox 2>&1)"
  delete_status=$?
  if [[ "$delete_status" -ne 0 ]] \
    && ! grep -Eqi 'no instance found|not found|does not exist' <<<"$delete_output"; then
    printf 'run-vm-ci: failed to delete the test VM; preserving %s\n%s\n' \
      "$test_root" "$delete_output" >&2
    cleanup_status=1
  elif [[ "$preserve_test_root" == true ]] \
    || [[ -e "$test_root/.preserve-live-sqlite-worker" ]] \
    || { [[ "$sqlite_helper_started" == true ]] && [[ "$status" -ge 128 ]]; }; then
    printf 'run-vm-ci: preserving %s after SQLite probe interruption or cleanup uncertainty\n' \
      "$test_root" >&2
    cleanup_status=1
  elif ! rm -rf -- "$test_root"; then
    printf 'run-vm-ci: failed to remove temporary test directory %s\n' \
      "$test_root" >&2
    cleanup_status=1
  fi
  if [[ "$status" -ne 0 ]]; then
    exit "$status"
  fi
  exit "$cleanup_status"
}

signal_exit() {
  local signal_name="$1" exit_status="$2"
  printf 'run-vm-ci: received SIG%s; running cleanup\n' "$signal_name" >&2
  exit "$exit_status"
}

trap cleanup EXIT
trap 'signal_exit INT 130' INT
trap 'signal_exit TERM 143' TERM
trap 'signal_exit HUP 129' HUP
trap 'signal_exit QUIT 131' QUIT

mkdir -p \
  "$repo_copy" \
  "$host_home/kb" \
  "$host_home/.agents" \
  "$host_home/.config/opencode" \
  "$host_home/.config/gh" \
  "$host_home/.config/gcloud" \
  "$host_home/.config/acli" \
  "$host_home/.config/gws" \
  "$host_home/.local/share/opencode" \
  "$host_home/.local/state/opencode" \
  "$host_home/.local/state/devbox-opencode"
printf '# VM-test mount sentinel\n' >"$host_home/kb/kbase.py"
printf 'host-to-L1\n' >"$host_home/.local/share/opencode/issue-287-mount-inbound"
# Copy committed files only: neither local ignored secrets nor the runner's
# .git/config credentials should be present in the guest-visible source mount.
git -C "$workspace" archive --format=tar HEAD | tar -xf - -C "$repo_copy"
if [[ "$tier" == vm ]] \
  && [[ ! -r "$repo_copy/scripts/validate_host_sqlite_mount.py" ]]; then
  printf 'run-vm-ci: SQLite mount helper is missing from the committed source archive\n' >&2
  exit 1
fi
if [[ "$tier" == vm ]]; then
  # The host coordinator is not on any guest mount; only its guest worker is.
  install -m 0600 \
    "$repo_copy/scripts/validate_host_sqlite_mount.py" "$host_sqlite_helper"
fi
# Reinitialize a credential-free, local-only index so recursive fixture copies
# can use the same tracked/non-ignored file filter as local worktrees.
git -C "$repo_copy" init --quiet
git -C "$repo_copy" config user.name CI
git -C "$repo_copy" config user.email ci-test@example.invalid
git -C "$repo_copy" add --all
chmod 700 "$host_home"

if [[ "$tier" == vm ]]; then
  # Prepare the test database before any L1 guest code can access the mount.
  mkdir -p "$lima_home"
  chmod 700 "$lima_home"
  if sqlite_scratch_name="$(timeout --signal=TERM --kill-after=5s 30s \
    python3 -I "$host_sqlite_helper" --prepare)"; then
    :
  else
    prepare_status=$?
    if [[ "$prepare_status" -eq 75 \
      || "$prepare_status" -eq 124 \
      || "$prepare_status" -ge 128 ]]; then
      preserve_test_root=true
    fi
    printf 'run-vm-ci: SQLite scratch preparation failed with status %s\n' \
      "$prepare_status" >&2
    exit "$prepare_status"
  fi
  if [[ ! "$sqlite_scratch_name" =~ ^issue-287-test-only-[0-9a-f]{32}$ ]]; then
    printf 'run-vm-ci: host SQLite helper returned an invalid scratch name\n' >&2
    exit 1
  fi
fi

if [[ "$mount_type_is_set" == x ]]; then
  printf 'run-vm-ci: requested OpenCode data mount type=%s for disposable VM creation\n' \
    "$requested_mount_type" >&2
fi

limactl start \
  --yes \
  --name devbox \
  --cpus 4 \
  --memory 10 \
  --timeout 60m \
  --param SrcPath=/workspace/src \
  --param RepoPath=/workspace/src/my-sandbox \
  --param KbPath=/workspace/kb \
  --param GitUserName=CI \
  --param GitUserEmail=ci-test@example.invalid \
  "${mount_type_args[@]}" \
  "$repo_root/lima/devbox.yaml"

if [[ "$mount_type_is_set" == x ]]; then
  verify_requested_mount_type "first start"
fi

if [[ "$tier" == vm ]]; then
  # Run the host coordinator from the protected temp-root copy before any guest
  # test code has run. The probe stops L1 after its guest verification.
  sqlite_helper_started=true
  if timeout --signal=TERM --kill-after=15s 120s \
    python3 -I "$host_sqlite_helper" --scratch-name "$sqlite_scratch_name"; then
    sqlite_helper_started=false
  else
    helper_status=$?
    if [[ "$helper_status" -eq 75 \
      || "$helper_status" -eq 124 \
      || "$helper_status" -ge 128 ]]; then
      preserve_test_root=true
    fi
    if [[ "$helper_status" -eq 75 ]]; then
      printf 'run-vm-ci: SQLite worker may still be active; preserving the temporary test root\n' >&2
    elif [[ "$helper_status" -eq 124 ]]; then
      printf 'run-vm-ci: SQLite mount helper exceeded its 120-second timeout\n' >&2
    elif [[ "$helper_status" -ge 128 ]]; then
      printf 'run-vm-ci: SQLite mount helper exited on signal (status %s); preserving the temporary test root\n' \
        "$helper_status" >&2
    fi
    exit "$helper_status"
  fi

  # The SQLite probe stopped this disposable instance; restart the existing
  # instance before entering the ordinary VM test tier.
  limactl start --yes --timeout 60m devbox
  if [[ "$mount_type_is_set" == x ]]; then
    verify_requested_mount_type "SQLite probe stop/restart"
  fi
fi

# Run the test wrapper directly in the guest. The host environment is not
# preserved; only explicit non-secret test controls enter through `env`.
# shellcheck disable=SC2016 # `$HOME` must expand inside the guest shell.
limactl shell --workdir /workspace/src/my-sandbox devbox bash -c '
  exec ./scripts/sanitized-test.sh --guest-vm --require-vm -- \
    env MY_SANDBOX_VM_TEST_FRESH=1 DEVBOX_VM_START_TIMEOUT=3600 \
      UV_PROJECT_ENVIRONMENT="$HOME/.cache/my-sandbox-test-venv" \
      uv run --extra test pytest -m "$1"
' run-vm-ci "$markers"

if [[ "$tier" == vm ]]; then
  if ! grep -qx 'L1-to-host' \
    "$host_home/.local/share/opencode/issue-287-mount-outbound"; then
    printf 'run-vm-ci: OpenCode data mount did not expose the L1 write to the host\n' >&2
    exit 1
  fi
fi
