import shlex
import uuid

import pytest

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
