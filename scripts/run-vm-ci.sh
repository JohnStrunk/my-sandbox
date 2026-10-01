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

test_root="$(mktemp -d "$runner_temp/my-sandbox-vm-test.XXXXXX")"
host_home="$test_root/home"
lima_home="$test_root/lima"
repo_copy="$host_home/src/my-sandbox"

export HOME="$host_home"
export LIMA_HOME="$lima_home"

cleanup() {
  local status=$? delete_status cleanup_status=0 delete_output
  trap - EXIT
  set +e
  limactl stop devbox >/dev/null 2>&1
  delete_output="$(limactl delete --force devbox 2>&1)"
  delete_status=$?
  if [[ "$delete_status" -ne 0 ]] \
    && ! grep -Eqi 'no instance found|not found|does not exist' <<<"$delete_output"; then
    printf 'run-vm-ci: failed to delete the test VM; preserving %s\n%s\n' \
      "$test_root" "$delete_output" >&2
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
trap cleanup EXIT

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
# Reinitialize a credential-free, local-only index so recursive fixture copies
# can use the same tracked/non-ignored file filter as local worktrees.
git -C "$repo_copy" init --quiet
git -C "$repo_copy" config user.name CI
git -C "$repo_copy" config user.email ci-test@example.invalid
git -C "$repo_copy" add --all
chmod 700 "$host_home"

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
  "$repo_root/lima/devbox.yaml"

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
