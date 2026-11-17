import shlex
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

from tests.conftest import (
    LimaVM,
    runtime_offline_guard,
    skip_if_guest_provisioning_is_stale,
    vm_start_timeout,
)

# The x86_64 Fedora 44 cloud image shared by every nested-L2 template here.
# Keep it in sync with the digest-pinned image in lima/devbox.yaml.
FEDORA_44_X86_64_IMAGE_LOCATION = (
    "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/"
    "x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
)
FEDORA_44_X86_64_IMAGE_DIGEST = (
    "sha256:28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f"
)


@pytest.mark.recursive
def test_lima_can_start_an_l2_without_stacked_project_mounts(devbox_vm: LimaVM):
    instance = f"recursive-{uuid.uuid4().hex[:10]}"
    repo_copy = f"/var/tmp/{instance}-repo"
    guest_repo = shlex.quote(devbox_vm.repo_path)
    quoted_repo_copy = shlex.quote(repo_copy)
    command = rf"""
set -euo pipefail
name={instance}
repo_copy={repo_copy}
template_dir="$(mktemp -d)"
template="$template_dir/devbox-l2.yaml"
cleanup() {{
  status=$?
  trap - EXIT
  set +e
  limactl stop "$name" >/dev/null 2>&1
  delete_output="$(limactl delete --force "$name" 2>&1)"
  delete_status=$?
  if [[ "$delete_status" -ne 0 ]] \
    && ! grep -Eqi \
      'no instance found|not found|does not exist' <<<"$delete_output"; then
    printf 'recursive test: failed to delete L2 %s: %s\n' "$name" "$delete_output" >&2
    if [[ "$status" -eq 0 ]]; then status=1; fi
  else
    rm -rf -- "$repo_copy"
  fi
  rm -rf -- "$template_dir"
  exit "$status"
}}
trap cleanup EXIT
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/my-sandbox-recursive-unit-venv"
export UV_CACHE_DIR="$HOME/.cache/uv"
uv run --project {guest_repo} --extra test pytest -q \
  {guest_repo}/tests/unit/test_devbox_lima.py \
  {guest_repo}/tests/unit/test_lima_template.py
uv run --project {guest_repo} --extra test python -c '
import sys
from pathlib import Path
from tests.conftest import copy_repository_for_vm
copy_repository_for_vm(Path(sys.argv[1]), Path(sys.argv[2]))
' {guest_repo} {quoted_repo_copy}
install -m 0644 "$repo_copy/lima/provision-vm-level.sh" \
  "$template_dir/provision-vm-level.sh"
cat >"$template" <<'YAML'
minimumLimaVersion: "2.1.3"
vmType: qemu
nestedVirtualization: true
cpus: 2
memory: "4GiB"
disk: "24GiB"
images:
  - location: "{FEDORA_44_X86_64_IMAGE_LOCATION}"
    arch: "x86_64"
    digest: "{FEDORA_44_X86_64_IMAGE_DIGEST}"
mounts:
  - location: "{repo_copy}"
    mountPoint: "/workspace/repo"
    writable: false
containerd:
  system: false
  user: false
param:
  VmLevel: ""
provision:
  - mode: system
    file: provision-vm-level.sh
YAML
limactl start --yes --name "$name" \
  --timeout {vm_start_timeout():g}s \
  --param VmLevel=L2 "$template"
limactl shell "$name" -- bash -ceu '
  test -r /workspace/repo/lima/devbox.yaml
  marker=/etc/devbox/vm-level
  [[ -f "$marker" && ! -L "$marker" ]]
  [[ "$(stat -c "%u:%g:%a" "$marker")" == 0:0:644 ]]
  [[ "$(cat "$marker")" == L2 ]]
  bash -n /workspace/repo/devbox
  python3 /workspace/repo/scripts/validate_tool_versions.py
  for target in "$HOME/.coderabbit" "$HOME/.host-config/coderabbit"; do
    if findmnt -rn -M "$target" -t 9p,virtiofs >/dev/null 2>&1; then
      echo "L2 unexpectedly mounts host CodeRabbit config at $target" >&2
      exit 1
    fi
  done
  [[ ! -L "$HOME/.coderabbit" ]]
  uname -s
  grep -Eq "(^| )(vmx|svm)( |$)" /proc/cpuinfo
'
"""
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=vm_start_timeout() + 120,
    )

    assert result.returncode == 0, (
        "Lima-in-Lima did not run the copied-repository checks in a minimal L2.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Linux" in result.stdout


@pytest.mark.recursive
def test_virtiofs_shared_dir_requires_a_resolved_location(devbox_vm: LimaVM):
    """A nested virtiofs VM rejects a symlinked location; a resolved one works.

    Rust virtiofsd rejects a shared directory whose final path component is
    a symlink (#486). The check starts an L2 whose mount location is a
    symlink and asserts the failure, then starts the same L2 shape with the
    resolved directory and asserts the virtiofs mount plus guest create/write
    and host-side visibility.
    """
    instance = f"recursive-virtiofs-{uuid.uuid4().hex[:10]}"
    guest_repo = shlex.quote(devbox_vm.repo_path)
    command = rf"""
set -euo pipefail
name={instance}
task_root="$(mktemp -d /var/tmp/my-sandbox-virtiofs.XXXXXXXX)"
chmod 700 "$task_root"
shared="$task_root/shared"
mkdir -m 700 -p "$shared"
shared_link="$task_root/shared-link"
ln -s "$shared" "$shared_link"
printf 'host-to-guest\n' >"$shared/from-host.txt"
template_dir="$task_root"
template="$template_dir/virtiofs-l2.yaml"
lima_home="${{LIMA_HOME:-$HOME/.lima}}"
cleanup() {{
  status=$?
  trap - EXIT INT TERM HUP QUIT
  set +e
  limactl stop "$name" >/dev/null 2>&1
  delete_output="$(limactl delete --force "$name" 2>&1)"
  delete_status=$?
  if [[ "$delete_status" -ne 0 ]] \
    && ! grep -Eqi \
      'no instance found|not found|does not exist' <<<"$delete_output"; then
    printf 'recursive test: failed to delete L2 %s: %s\n' "$name" "$delete_output" >&2
    if [[ "$status" -eq 0 ]]; then status=1; fi
  else
    rm -rf -- "$task_root"
  fi
  exit "$status"
}}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 131' QUIT
install -m 0644 {guest_repo}/lima/provision-vm-level.sh \
  "$template_dir/provision-vm-level.sh"
write_template() {{
  local location="$1"
  cat >"$template" <<YAML
minimumLimaVersion: "2.1.3"
vmType: qemu
nestedVirtualization: true
cpus: 2
memory: "4GiB"
disk: "24GiB"
images:
  - location: "{FEDORA_44_X86_64_IMAGE_LOCATION}"
    arch: "x86_64"
    digest: "{FEDORA_44_X86_64_IMAGE_DIGEST}"
mountType: virtiofs
mounts:
  - location: "$location"
    mountPoint: /workspace/shared
    writable: true
containerd:
  system: false
  user: false
param:
  VmLevel: ""
provision:
  - mode: system
    file: provision-vm-level.sh
YAML
}}

# A symlinked location must fail before QEMU starts: Rust virtiofsd rejects
# a shared directory whose final path component is a symlink.
write_template "$shared_link"
if limactl start --yes --name "$name" --timeout {vm_start_timeout():g}s \
  --param VmLevel=L2 "$template" >"$task_root/symlink-start.log" 2>&1; then
  echo 'the symlinked virtiofs location unexpectedly started' >&2
  exit 1
fi
if ! grep -q 'BindMountSharedDir' "$lima_home/$name/ha.stderr.log"; then
  echo 'the symlinked-location start failed without the virtiofsd' >&2
  echo 'BindMountSharedDir error; the Rust virtiofsd package must be' >&2
  echo 'installed (recreate the VM from the current template)' >&2
  cat "$task_root/symlink-start.log" >&2 || true
  cat "$lima_home/$name/ha.stderr.log" >&2 || true
  exit 1
fi
if ! delete_output="$(limactl delete --force "$name" 2>&1)"; then
  if ! grep -Eqi \
    'no instance found|not found|does not exist' <<<"$delete_output"; then
    printf 'recursive test: failed to delete L2 %s: %s\n' \
      "$name" "$delete_output" >&2
    exit 1
  fi
fi

# The resolved location must start, mount virtiofs, and pass guest writes.
write_template "$shared"
limactl start --yes --name "$name" --timeout {vm_start_timeout():g}s \
  --param VmLevel=L2 "$template"
fstype="$(limactl shell "$name" -- findmnt -rn -T /workspace/shared -o FSTYPE)"
if [[ "$fstype" != virtiofs ]]; then
  echo "expected the shared mount to be virtiofs, got '$fstype'" >&2
  exit 1
fi
limactl shell "$name" -- bash -ceu '
  [[ "$(cat /etc/devbox/vm-level)" == L2 ]]
  [[ "$(cat /workspace/shared/from-host.txt)" == host-to-guest ]]
  printf "guest-to-host\n" > /workspace/shared/from-guest.txt
  [[ "$(cat /workspace/shared/from-guest.txt)" == guest-to-host ]]
'
grep -qx 'guest-to-host' "$shared/from-guest.txt"
"""
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=2 * vm_start_timeout() + 300,
    )

    assert result.returncode == 0, (
        "The nested virtiofs check failed: the symlinked location must fail "
        "with the virtiofsd BindMountSharedDir error, and the resolved "
        "location must mount virtiofs, write the L2 marker, and pass guest "
        "create/write with host-side visibility.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.recursive
def test_virtiofs_guest_gid_must_match_host_gid(devbox_vm: LimaVM):
    """A virtiofs guest write fails with EINVAL on an unmapped guest gid.

    Lima v2.2.1 passes only --socket-path and --shared-dir to virtiofsd, so
    virtiofsd keeps its --sandbox=namespace default. That user namespace
    maps exactly one uid and one gid: the identity of the user that started
    limactl (upstream lima-vm/lima#2152). Lima sets the guest user's uid
    from the host but has no user.gid field, so a guest user whose primary
    gid is not the host gid asks virtiofsd to create files with a gid the
    namespace cannot map, and the write fails with EINVAL. The check records
    the virtiofsd uid_map/gid_map, reproduces the failure with a guest
    primary gid the host does not map, and proves the workaround: set the
    guest user's primary gid to the host gid (#317).
    """
    instance = f"recursive-virtiofs-gid-{uuid.uuid4().hex[:10]}"
    guest_repo = shlex.quote(devbox_vm.repo_path)
    command = rf"""
set -euo pipefail
name={instance}
host_uid="$(id -u)"
host_gid="$(id -g)"
task_root="$(mktemp -d /var/tmp/my-sandbox-virtiofs-gid.XXXXXXXX)"
chmod 700 "$task_root"
shared="$task_root/shared"
mkdir -m 700 -p "$shared"
printf 'host-to-guest\n' >"$shared/from-host.txt"
template_dir="$task_root"
template="$template_dir/virtiofs-gid-l2.yaml"
lima_home="${{LIMA_HOME:-$HOME/.lima}}"
cleanup() {{
  status=$?
  trap - EXIT INT TERM HUP QUIT
  set +e
  limactl stop "$name" >/dev/null 2>&1
  delete_output="$(limactl delete --force "$name" 2>&1)"
  delete_status=$?
  if [[ "$delete_status" -ne 0 ]] \
    && ! grep -Eqi \
      'no instance found|not found|does not exist' <<<"$delete_output"; then
    printf 'recursive test: failed to delete L2 %s: %s\n' "$name" "$delete_output" >&2
    if [[ "$status" -eq 0 ]]; then status=1; fi
  else
    rm -rf -- "$task_root"
  fi
  exit "$status"
}}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 131' QUIT
install -m 0644 {guest_repo}/lima/provision-vm-level.sh \
  "$template_dir/provision-vm-level.sh"
cat >"$template" <<YAML
minimumLimaVersion: "2.1.3"
vmType: qemu
nestedVirtualization: true
cpus: 2
memory: "4GiB"
disk: "24GiB"
images:
  - location: "{FEDORA_44_X86_64_IMAGE_LOCATION}"
    arch: "x86_64"
    digest: "{FEDORA_44_X86_64_IMAGE_DIGEST}"
mountType: virtiofs
mounts:
  - location: "$shared"
    mountPoint: /workspace/shared
    writable: true
containerd:
  system: false
  user: false
param:
  VmLevel: ""
provision:
  - mode: system
    file: provision-vm-level.sh
YAML
printf 'virtiofs-gid-evidence: host uid=%s gid=%s\n' "$host_uid" "$host_gid"
limactl start --yes --name "$name" --timeout {vm_start_timeout():g}s \
  --param VmLevel=L2 "$template"

# Find the daemon that serves this mount by its unique socket path. Do not
# rely on the optional <socket>.pid file or on process ordering.
virtiofsd_socket="$lima_home/$name/virtiofsd-0.sock"
virtiofsd_pids="$(pgrep -f -- "--socket-path $virtiofsd_socket" || true)"
if [[ -z "$virtiofsd_pids" ]]; then
  echo "no virtiofsd process serves $virtiofsd_socket" >&2
  echo 'the Rust virtiofsd package must be installed (recreate the VM' >&2
  echo 'from the current template)' >&2
  pgrep -a virtiofsd >&2 || true
  ls -la "$lima_home/$name" >&2 || true
  exit 1
fi
virtiofsd_pid="$(head -n 1 <<<"$virtiofsd_pids")"
raw_uid_map="$(cat "/proc/$virtiofsd_pid/uid_map")"
raw_gid_map="$(cat "/proc/$virtiofsd_pid/gid_map")"
printf 'virtiofs-gid-evidence: virtiofsd uid_map=[%s]\n' "$raw_uid_map"
printf 'virtiofs-gid-evidence: virtiofsd gid_map=[%s]\n' "$raw_gid_map"
# The namespace sandbox maps exactly one uid and one gid, both the identity
# of the user that started limactl; /proc pads each map line, so normalize
# the whitespace before comparing.
uid_map="$(awk '{{$1=$1}} 1' <<<"$raw_uid_map")"
gid_map="$(awk '{{$1=$1}} 1' <<<"$raw_gid_map")"
expected_uid_map="$host_uid $host_uid 1"
expected_gid_map="$host_gid $host_gid 1"
if [[ "$uid_map" != "$expected_uid_map" ]]; then
  echo "the virtiofsd user namespace must map exactly one uid: the host" >&2
  echo "uid. expected uid_map [$expected_uid_map], got [$uid_map]." >&2
  exit 1
fi
if [[ "$gid_map" != "$expected_gid_map" ]]; then
  echo "the virtiofsd user namespace must map exactly one gid: the host" >&2
  echo "gid. expected gid_map [$expected_gid_map], got [$gid_map]." >&2
  exit 1
fi

guest_user="$(limactl shell "$name" -- id -un)"
guest_id="$(limactl shell "$name" -- id)"
guest_mount="$(limactl shell "$name" -- \
  findmnt -rn -T /workspace/shared -o FSTYPE)"
guest_dir_stat="$(limactl shell "$name" -- stat -c '%u:%g' /workspace/shared)"
printf 'virtiofs-gid-evidence: guest user=%s id=[%s]\n' "$guest_user" "$guest_id"
printf 'virtiofs-gid-evidence: guest mount fstype=[%s] shared stat=[%s]\n' \
  "$guest_mount" "$guest_dir_stat"
if [[ "$guest_mount" != virtiofs ]]; then
  echo "expected the shared mount to be virtiofs, got '$guest_mount'" >&2
  exit 1
fi
expected_dir_stat="$host_uid:$host_gid"
if [[ "$guest_dir_stat" != "$expected_dir_stat" ]]; then
  echo "the guest must see the shared dir owned by the host identity:" >&2
  echo "expected '$expected_dir_stat', got '$guest_dir_stat'" >&2
  exit 1
fi
guest_default_gid="$(limactl shell "$name" -- id -g)"
printf 'virtiofs-gid-evidence: guest default gid=%s\n' "$guest_default_gid"
if ! limactl shell "$name" -- sudo -n true; then
  echo 'sudo -n does not work in the L2; the gid workaround needs it' >&2
  exit 1
fi
limactl shell "$name" -- bash -ceu '
  [[ "$(cat /etc/devbox/vm-level)" == L2 ]]
'

set_guest_primary_gid() {{
  local gid="$1" group="$2"
  limactl shell "$name" -- sudo -n bash -ceu '
    if ! getent group "$1" >/dev/null; then
      groupadd -g "$1" "$2"
    fi
    usermod -g "$1" "$3"
  ' _ "$gid" "$group" "$guest_user"
}}
# A limactl shell session keeps the credential that its session machinery
# resolved at boot, so a usermod never shows in plain `id -g` (only a VM
# restart refreshes it). Resolving the user again through sudo gives the
# current /etc/passwd identity, which is what the gid phases must check.
guest_primary_gid() {{
  limactl shell "$name" -- id -g
}}
guest_resolved_gid() {{
  limactl shell "$name" -- sudo -n -u "$guest_user" id -g
}}

# A guest default gid that differs from the host gid is the physical-host
# failure mode: the create carries a gid that virtiofsd cannot map.
if [[ "$guest_default_gid" != "$host_gid" ]]; then
  default_gid_err="$task_root/default-gid-write.err"
  if limactl shell "$name" -- bash -ceu \
    'printf "default-gid\n" > /workspace/shared/from-default-gid.txt' \
    2>"$default_gid_err"; then
    echo "guest default gid $guest_default_gid differs from host gid" >&2
    echo "$host_gid, but an ordinary create still succeeded" >&2
    exit 1
  fi
  printf 'virtiofs-gid-evidence: default-gid write stderr: %s\n' \
    "$(cat "$default_gid_err")"
  grep -q 'Invalid argument' "$default_gid_err" || {{
    echo 'the default-gid create failed without EINVAL' >&2
    cat "$default_gid_err" >&2
    exit 1
  }}
fi

# Workaround: the guest user's primary gid must be the host gid.
set_guest_primary_gid "$host_gid" devbox-host-gid
aligned_gid="$(guest_resolved_gid)"
printf 'virtiofs-gid-evidence: aligned guest gid=%s\n' "$aligned_gid"
if [[ "$aligned_gid" != "$host_gid" ]]; then
  echo "usermod -g $host_gid did not take effect for the guest user:" >&2
  echo "resolved id -g is $aligned_gid" >&2
  exit 1
fi
limactl shell "$name" -- sudo -n -u "$guest_user" bash -ceu '
  [[ "$(cat /workspace/shared/from-host.txt)" == host-to-guest ]]
  printf "aligned\n" > /workspace/shared/from-aligned.txt
  [[ "$(cat /workspace/shared/from-aligned.txt)" == aligned ]]
'
grep -qx 'aligned' "$shared/from-aligned.txt"

# Deterministic reproduction: a primary gid that differs from the host gid
# is not mapped in the virtiofsd namespace, so the create must fail EINVAL.
# 4242 is arbitrary; 4243 avoids colliding with a host gid of 4242.
mismatch_gid_choice=4242
if [[ "$host_gid" == 4242 ]]; then
  mismatch_gid_choice=4243
fi
set_guest_primary_gid "$mismatch_gid_choice" virtiofs-gid-mismatch
mismatch_gid="$(guest_resolved_gid)"
printf 'virtiofs-gid-evidence: mismatch guest gid=%s\n' "$mismatch_gid"
printf 'virtiofs-gid-evidence: mismatch session gid=%s (pinned at boot)\n' \
  "$(guest_primary_gid)"
if [[ "$mismatch_gid" != "$mismatch_gid_choice" ]]; then
  echo "usermod -g $mismatch_gid_choice did not take effect for the guest user:" >&2
  echo "resolved id -g is $mismatch_gid" >&2
  exit 1
fi
mismatch_err="$task_root/mismatch-write.err"
if limactl shell "$name" -- sudo -n -u "$guest_user" bash -ceu \
  'printf "mismatch\n" > /workspace/shared/from-mismatch.txt' \
  2>"$mismatch_err"; then
  echo "the write with guest primary gid $mismatch_gid_choice" >&2
  echo 'unexpectedly succeeded' >&2
  exit 1
fi
printf 'virtiofs-gid-evidence: mismatch write stderr: %s\n' \
  "$(cat "$mismatch_err")"
if ! grep -q 'Invalid argument' "$mismatch_err"; then
  echo 'the mismatched-gid write failed without EINVAL (Invalid argument)' >&2
  cat "$mismatch_err" >&2
  exit 1
fi
if [[ -e "$shared/from-mismatch.txt" ]]; then
  echo 'the mismatched-gid write failed but the file exists host-side' >&2
  exit 1
fi

# Restore the workaround and prove it end to end.
set_guest_primary_gid "$host_gid" devbox-host-gid
restored_gid="$(guest_resolved_gid)"
printf 'virtiofs-gid-evidence: restored guest gid=%s\n' "$restored_gid"
if [[ "$restored_gid" != "$host_gid" ]]; then
  echo "usermod -g $host_gid did not take effect for the guest user:" >&2
  echo "resolved id -g is $restored_gid" >&2
  exit 1
fi
limactl shell "$name" -- sudo -n -u "$guest_user" bash -ceu '
  printf "restored\n" > /workspace/shared/from-restored.txt
  [[ "$(cat /workspace/shared/from-restored.txt)" == restored ]]
'
grep -qx 'restored' "$shared/from-restored.txt"
restored_host_gid="$(stat -c %g "$shared/from-restored.txt")"
printf 'virtiofs-gid-evidence: restored file host gid=%s\n' "$restored_host_gid"
if [[ "$restored_host_gid" != "$host_gid" ]]; then
  echo "the restored-gid file has host gid $restored_host_gid; expected" >&2
  echo "the host gid $host_gid" >&2
  exit 1
fi
"""
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=2 * vm_start_timeout() + 300,
    )

    assert result.returncode == 0, (
        "The virtiofs guest-gid check failed: virtiofsd must map exactly "
        "one uid and one gid (the limactl user's identity), an ordinary "
        "guest create with an unmapped primary gid must fail with EINVAL "
        "(Invalid argument), and setting the guest primary gid to the host "
        "gid must make guest writes succeed with host-side gid visibility.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    evidence = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("virtiofs-gid-evidence:")
    ]
    assert evidence, (
        "The virtiofs guest-gid check recorded no evidence lines.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    print("\n".join(evidence))


@pytest.mark.recursive
def test_concurrent_l2_opencode_services_have_private_state(
    devbox_vm: LimaVM, tmp_path: Path
):
    """Two nested OpenCode services must not share registration or state."""
    if devbox_vm.name is not None:
        pytest.skip("OpenCode soak must run inside the provisioned L1 guest")
    binary = shutil.which("opencode")
    if not binary:
        pytest.fail("the provisioned L1 OpenCode binary is missing")
    binary_path = Path(binary).resolve(strict=True)
    if not binary_path.is_file():
        pytest.fail("the provisioned OpenCode command does not resolve to a file")

    names = [f"recursive-opencode-{uuid.uuid4().hex[:10]}" for _ in range(2)]
    template = tmp_path / f"{names[0]}.yaml"
    shutil.copy2(
        Path(devbox_vm.repo_path) / "lima" / "provision-vm-level.sh",
        tmp_path / "provision-vm-level.sh",
    )
    template.write_text(
        yaml.safe_dump(
            {
                "minimumLimaVersion": "2.1.3",
                "vmType": "qemu",
                "nestedVirtualization": True,
                "cpus": 2,
                "memory": "4GiB",
                "disk": "24GiB",
                "images": [
                    {
                        "location": FEDORA_44_X86_64_IMAGE_LOCATION,
                        "arch": "x86_64",
                        "digest": FEDORA_44_X86_64_IMAGE_DIGEST,
                    }
                ],
                "mounts": [
                    {
                        "location": str(binary_path.parent),
                        "mountPoint": "/opt/opencode",
                        "writable": False,
                    },
                ],
                "containerd": {"system": False, "user": False},
                "param": {"VmLevel": ""},
                "provision": [
                    {
                        "mode": "system",
                        "file": "provision-vm-level.sh",
                    }
                ],
            },
            sort_keys=False,
        )
    )

    quoted_template = shlex.quote(str(template))
    first, second = map(shlex.quote, names)
    timeout = f"{vm_start_timeout():g}s"
    command = rf"""
set -euo pipefail
template={quoted_template}
name_a={first}
name_b={second}
timeout={shlex.quote(timeout)}
start_vm() {{
  local name="$1" log="$2"
  limactl start --yes --name "$name" --timeout "$timeout" \
    --param VmLevel=L2 "$template" >"$log" 2>&1
}}
if ! start_vm "$name_a" "$template.$name_a.log"; then
  cat "$template.$name_a.log" >&2
  exit 1
fi
if ! start_vm "$name_b" "$template.$name_b.log"; then
  cat "$template.$name_b.log" >&2
  exit 1
fi
for name in "$name_a" "$name_b"; do
  limactl shell "$name" -- bash -ceu '
    marker=/etc/devbox/vm-level
    [[ -f "$marker" && ! -L "$marker" ]]
    [[ "$(stat -c "%u:%g:%a" "$marker")" == 0:0:644 ]]
    [[ "$(cat "$marker")" == L2 ]]
  '
done

start_service() {{
  local name="$1" log="$2"
  local options
  limactl shell "$name" -- test -x /opt/opencode/opencode.exe
  options="$(limactl shell "$name" -- findmnt -rn -M /opt/opencode -o OPTIONS)"
  case ",$options," in
    *,ro,*) ;;
    *) echo "OpenCode test binary mount is writable" >&2; return 1 ;;
  esac
  if ! limactl shell "$name" -- env OPENCODE_DISABLE_AUTOUPDATE=1 \
    timeout 90 /opt/opencode/opencode.exe service start >"$log" 2>&1; then
    cat "$log" >&2
    return 1
  fi
}}
registration_id() {{
  limactl shell "$1" -- python3 -c '
import json
from pathlib import Path
path = Path.home() / ".local/state/opencode/service.json"
assert path.is_file() and not path.is_symlink(), (
    "service registration is missing or linked"
)
assert Path("/opt/opencode").is_dir()
assert Path(path).resolve().is_relative_to(Path.home()), "state escaped L2 home"
assert Path(path).stat().st_mode & 0o077 == 0, (
    "service state is accessible to other users"
)
print(json.loads(path.read_text())["id"])
'
}}
service_status() {{
  local name="$1" log="$2"
  if ! limactl shell "$name" -- env OPENCODE_DISABLE_AUTOUPDATE=1 \
    /opt/opencode/opencode.exe service status >"$log" 2>&1; then
    cat "$log" >&2
    return 1
  fi
}}

start_service "$name_a" "$template.$name_a.start.log"
start_service "$name_b" "$template.$name_b.start.log"
id_a="$(registration_id "$name_a")"
id_b="$(registration_id "$name_b")"
if [[ -z "$id_a" || -z "$id_b" || "$id_a" == "$id_b" ]]; then
  echo 'nested OpenCode services did not get distinct registrations' >&2
  exit 1
fi
sleep 15
service_status "$name_a" "$template.$name_a.status.log"
service_status "$name_b" "$template.$name_b.status.log"
if [[ "$(registration_id "$name_a")" != "$id_a" ]]; then
  echo 'first nested OpenCode service registration changed during soak' >&2
  exit 1
fi
if [[ "$(registration_id "$name_b")" != "$id_b" ]]; then
  echo 'second nested OpenCode service registration changed during soak' >&2
  exit 1
fi
for name in "$name_a" "$name_b"; do
  limactl shell "$name" -- python3 -c '
import subprocess
from pathlib import Path
state = Path.home() / ".local/state/opencode"
result = subprocess.run(
    [
        "findmnt",
        "-rn",
        "-T",
        str(state / "service.json"),
        "-o",
        "TARGET,FSTYPE,SOURCE",
    ],
    check=True,
    capture_output=True,
    text=True,
)
target, filesystem, source = result.stdout.split()
assert filesystem not in {{"9p", "virtiofs"}} and source.startswith("/dev/"), (
    "OpenCode state is not on the L2-local block filesystem: "
    f"target={{target!r}}, filesystem={{filesystem!r}}, source={{source!r}}"
)
logs = list((Path.home() / ".local/share/opencode/log").glob("*.log"))
assert logs, "OpenCode produced no service logs to inspect"
for log in logs:
    assert b"managed service registration replaced" not in log.read_bytes()
'
done
"""
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=2 * vm_start_timeout() + 300,
    )

    assert result.returncode == 0, (
        "Concurrent OpenCode services in isolated nested L2 VMs "
        "did not remain stable.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.e2e_kind
def test_kind_cluster_runs_inside_the_provisioned_vm(
    repo_root: Path, devbox_vm: LimaVM
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    home = shlex.quote(devbox_vm.guest_home)
    validate_kind = shlex.quote(f"{devbox_vm.repo_path}/lima/validate-kind.sh")
    command = (
        f"export HOME={home}; "
        + runtime_offline_guard("docker")
        + r"""
export DOCKER_HOST=unix:///var/run/docker.sock
export CONTAINER_HOST="unix:///nonexistent/podman.sock"
export CONTAINER_CONNECTION=issue-319-untrusted-connection
export PODMAN_HOST="unix:///nonexistent/legacy-podman.sock"
"""
        + f"\nbash {validate_kind} podman 1\nverify_runtime_offline"
    )
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=1200,
        use_guest_runtime=True,
    )

    assert result.returncode == 0, (
        "kind did not prove the rootless Podman backend and complete its "
        "cluster/workload/delete cycle in the VM.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "backend identity verified" in result.stdout
    assert "via podman" in result.stdout
    assert "kind validation passed: 1 consecutive podman clusters" in result.stdout


@pytest.mark.recursive
def test_minikube_kvm2_backend_runs_inside_the_provisioned_vm(devbox_vm: LimaVM):
    home = shlex.quote(devbox_vm.guest_home)
    validator = shlex.quote(f"{devbox_vm.repo_path}/lima/validate-minikube.sh")
    command = f"export HOME={home}; export TMPDIR=/var/tmp; bash {validator} kvm2"
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        # Leave time beyond the 10-minute start and 5-minute pod waits.
        timeout=1800,
        use_guest_runtime=True,
    )

    assert result.returncode == 0, (
        "The required KVM2 Minikube backend failed inside the provisioned VM. "
        "Check the validator's KVM/libvirt/QEMU preflight diagnostic; this "
        "recursive backend test is not skipped when a prerequisite is "
        "missing.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "backend identity verified: KVM2 libvirt L2 VM" in result.stdout
    assert "minikube validation passed: kvm2" in result.stdout
