#!/usr/bin/env bash
# Preserve an existing VM-local OpenCode state tree before recreating the VM.
set -euo pipefail

instance="${1:-devbox}"
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [LIMA_INSTANCE]" >&2
  exit 2
fi
if ! command -v limactl >/dev/null 2>&1; then
  echo "devbox: limactl is not installed or not on PATH" >&2
  exit 127
fi

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
guest_state="/home/$(id -un).guest/.local/state/opencode"
host_state="$HOME/.local/state/devbox-opencode"
if ! instances="$(limactl list --format '{{.Name}} {{.Status}}')"; then
  echo "devbox: could not inspect Lima instances before state migration" >&2
  exit 1
fi
instance_status="$(awk -v name="$instance" '$1 == name { print $2 }' <<<"$instances")"
if [[ "$instance_status" != Running ]]; then
  echo "devbox: start Lima VM '$instance' before migrating its OpenCode state" >&2
  exit 1
fi
if ! limactl shell "$instance" -- test -d "$guest_state"; then
  echo "devbox: VM '$instance' has no OpenCode state directory to migrate" >&2
  exit 0
fi
if ! limactl shell "$instance" -- python3 -c '
import json
import sys
from pathlib import Path

state = Path(sys.argv[1])
for registration in state.glob("service*.json"):
    if registration.is_symlink():
        raise SystemExit("cannot verify a symlinked service registration")
    try:
        pid = json.loads(registration.read_text()).get("pid")
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot verify service registration: {error}")
    if not isinstance(pid, int) or pid <= 0:
        continue
    command_line = Path(f"/proc/{pid}/cmdline")
    if not command_line.exists():
        continue
    try:
        command = command_line.read_bytes()
    except OSError:
        continue
    is_managed_service = (
        b"opencode" in command and b"serve" in command and b"--service" in command
    )
    if is_managed_service:
        raise SystemExit("managed OpenCode service is still running")
' "$guest_state"; then
  echo "devbox: stop OpenCode and retry state migration; live state will not be copied" >&2
  exit 1
fi

stage_root="$HOME/.local/state/devbox-opencode-migration"
for directory in "$host_state" "$stage_root"; do
  if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
    echo "devbox: refusing unexpected OpenCode migration path '$directory'" >&2
    exit 1
  fi
done
mkdir -p -- "$stage_root"
chmod 700 "$stage_root"
lock_file="$stage_root/migration.lock"
if [[ -L "$lock_file" || ( -e "$lock_file" && ! -f "$lock_file" ) ]]; then
  echo "devbox: refusing unexpected OpenCode migration lock '$lock_file'" >&2
  exit 1
fi
exec {lock_fd}>"$lock_file"
chmod 600 "$lock_file"
if ! flock --wait 60 "$lock_fd"; then
  echo "devbox: timed out waiting for OpenCode state migration lock" >&2
  exit 1
fi
# A forced kill can leave a private copy behind; reap only our own staging
# directories while holding the migration lock.
for stale_stage in "$stage_root"/copy.*; do
  [[ -e "$stale_stage" || -L "$stale_stage" ]] || continue
  if [[ -L "$stale_stage" || ! -d "$stale_stage" ]]; then
    echo "devbox: refusing unexpected OpenCode migration staging path '$stale_stage'" >&2
    exit 1
  fi
  rm -rf -- "$stale_stage"
done
stage="$(mktemp -d "$stage_root/copy.XXXXXX")"
chmod 700 "$stage"
cleanup() {
  rm -rf -- "$stage"
}
trap cleanup EXIT

mkdir -p -- "$host_state"
chmod 700 "$host_state"
echo "==> Copying safe OpenCode preferences from Lima VM '$instance'"
if ! limactl copy --backend=scp --recursive "$instance:$guest_state" "$stage"; then
  echo "devbox: OpenCode state copy failed; keep the VM and resolve the copy error before recreating it" >&2
  exit 1
fi

source="$stage/opencode"
if [[ ! -d "$source" ]]; then
  echo "devbox: limactl copy did not produce the expected OpenCode state directory" >&2
  exit 1
fi
python3 "$here/seed-opencode-state.py" --replace-existing "$source" "$host_state"
