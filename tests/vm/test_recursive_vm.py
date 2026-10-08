import shlex
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

from tests.conftest import LimaVM, vm_start_timeout


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
template="$(mktemp --suffix=.yaml)"
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
  rm -f "$template"
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
cat >"$template" <<'YAML'
minimumLimaVersion: "2.1.3"
vmType: qemu
nestedVirtualization: true
cpus: 2
memory: "4GiB"
disk: "24GiB"
images:
  - location: "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
    arch: "x86_64"
    digest: "sha256:28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f"
mounts:
  - location: "{repo_copy}"
    mountPoint: "/workspace/repo"
    writable: false
containerd:
  system: false
  user: false
YAML
limactl start --yes --name "$name" \
  --timeout {vm_start_timeout():g}s "$template"
limactl shell "$name" -- bash -ceu '
  test -r /workspace/repo/lima/devbox.yaml
  bash -n /workspace/repo/devbox
  python3 /workspace/repo/scripts/validate_tool_versions.py
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
                        "location": (
                            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/"
                            "x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2"
                        ),
                        "arch": "x86_64",
                        "digest": (
                            "sha256:28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f"
                        ),
                    }
                ],
                "mounts": [
                    {
                        "location": str(binary_path.parent),
                        "mountPoint": "/opt/opencode",
                        "writable": False,
                    }
                ],
                "containerd": {"system": False, "user": False},
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
  limactl start --yes --name "$name" --timeout "$timeout" "$template" >"$log" 2>&1
}}
if ! start_vm "$name_a" "$template.$name_a.log"; then
  cat "$template.$name_a.log" >&2
  exit 1
fi
if ! start_vm "$name_b" "$template.$name_b.log"; then
  cat "$template.$name_b.log" >&2
  exit 1
fi

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
def test_kind_cluster_runs_inside_the_provisioned_vm(devbox_vm: LimaVM):
    home = shlex.quote(devbox_vm.guest_home)
    validate_kind = shlex.quote(f"{devbox_vm.repo_path}/lima/validate-kind.sh")
    command = f"""
set -euo pipefail
export HOME={home}
export DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock"
export KIND_EXPERIMENTAL_PROVIDER=podman
bash {validate_kind} 1
"""
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=900,
        use_guest_runtime=True,
    )

    assert result.returncode == 0, (
        "A kind cluster did not complete a create/delete cycle in the VM.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "kind validation passed: 1 consecutive clusters" in result.stdout


@pytest.mark.recursive
def test_minikube_kvm2_backend_runs_inside_the_provisioned_vm(devbox_vm: LimaVM):
    home = shlex.quote(devbox_vm.guest_home)
    validator = shlex.quote(f"{devbox_vm.repo_path}/lima/validate-minikube.sh")
    command = f"export HOME={home}; bash {validator} kvm2"
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
    assert "minikube validation passed: kvm2" in result.stdout
