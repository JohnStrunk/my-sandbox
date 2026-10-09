import os
import shlex
from pathlib import Path

import pytest

from tests.conftest import (
    LimaVM,
    expected_lima_provisioning_fingerprint,
    expected_lima_system_script_sha256,
    runtime_offline_guard,
    skip_if_guest_provisioning_is_stale,
)


@pytest.mark.vm
def test_provisioned_vm_toolchain_matches_manifest(repo_root: Path, devbox_vm: LimaVM):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    result = devbox_vm.run(["devbox-toolchain-check"], timeout=300)

    assert result.returncode == 0, (
        "The Lima VM toolchain does not match its pinned manifest.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ERROR:" not in result.stdout


@pytest.mark.vm
def test_docker_ce_and_kind_run_while_podman_is_stopped(
    repo_root: Path, devbox_vm: LimaVM
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)

    command = r"""
set -euo pipefail
repo_path="$1"
docker_socket=/var/run/docker.sock
rootless_docker_socket="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
podman_socket="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
expected_version="$(
  jq -er '.tools.docker_ce.version | sub("^v"; "")' \
    /etc/devbox/tool-versions.json
)"
expected_containerd_version="$(
  jq -er '.tools.containerd_io.version | sub("^v"; "")' \
    /etc/devbox/tool-versions.json
)"
unset DOCKER_HOST DOCKER_CONTEXT
docker_cli_path="$(command -v docker)"
# shellcheck disable=SC1091
source /etc/profile.d/devbox-toolchain.sh

[[ "$docker_socket" != "$podman_socket" ]]
[[ ! -S "$rootless_docker_socket" ]]
[[ -S "$docker_socket" ]]
[[ -z "${DOCKER_HOST:-}" ]]
[[ -z "${DOCKER_CONTEXT:-}" ]]
[[ "$(docker context show)" == default ]]
[[ "$(docker context inspect default --format '{{.Endpoints.docker.Host}}')" \
  == unix:///var/run/docker.sock ]]
[[ "$docker_cli_path" == /usr/bin/docker ]]
[[ "$(rpm -qf --queryformat '%{NAME}' "$docker_cli_path")" == docker-ce-cli ]]
[[ "$(rpm -qf --queryformat '%{NAME}' /usr/bin/dockerd)" == docker-ce ]]
[[ "$(rpm -qf --queryformat '%{NAME}' /usr/bin/containerd)" == containerd.io ]]
[[ "$(rpm -q --queryformat '%{VERSION}' docker-ce)" == "$expected_version" ]]
[[ "$(rpm -q --queryformat '%{VERSION}' docker-ce-cli)" == "$expected_version" ]]
[[ "$(rpm -q --queryformat '%{VERSION}' containerd.io)" \
  == "$expected_containerd_version" ]]
if rpm -q podman-docker >/dev/null 2>&1; then
  echo "podman-docker must not provide the Docker CLI" >&2
  exit 1
fi
for plugin in docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin; do
  if rpm -q "$plugin" >/dev/null 2>&1; then
    echo "$plugin is not part of the supported Docker CE profile" >&2
    exit 1
  fi
done
for unit in containerd.service docker.socket docker.service; do
  systemctl is-enabled --quiet "$unit"
  systemctl is-active --quiet "$unit"
done
if systemctl --user is-active --quiet docker.service \
  || systemctl --user is-enabled --quiet docker.service; then
  echo "the obsolete rootless Docker user unit must not be enabled or active" >&2
  exit 1
fi
id -nG | tr ' ' '\n' | grep -qx docker
docker_service_pid="$(systemctl show --property=MainPID --value docker.service)"
[[ "$docker_service_pid" =~ ^[1-9][0-9]*$ ]]
[[ "$(stat -c %u "/proc/${docker_service_pid}")" == 0 ]]
[[ "$(stat -c '%U:%G:%a' "$docker_socket")" == root:docker:660 ]]
[[ -r "$docker_socket" && -w "$docker_socket" ]]
client_version="$(docker version --format '{{.Client.Version}}')"
server_version="$(docker version --format '{{.Server.Version}}')"
[[ "$client_version" == "$expected_version" ]]
[[ "$server_version" == "$expected_version" ]]

__PODMAN_OFFLINE_GUARD__
container_id=""
build_image=""
build_log=""
restore_services() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "$container_id" ]]; then
    docker rm --force "$container_id" >/dev/null 2>&1 || true
  fi
  if [[ -n "$build_image" ]]; then
    docker image rm "$build_image" >/dev/null 2>&1 || true
  fi
  if [[ -n "$build_log" ]]; then
    rm -f -- "$build_log"
  fi
  restore_runtime "$status"
}
trap restore_services EXIT

# Public multi-architecture OCI digest (split for line length; not a secret).
smoke_digest_prefix="bdf57e528e45e4433820e045b29b4597" # pragma: allowlist secret
smoke_digest_suffix="825a1c9e38353532d90a01445013f82e" # pragma: allowlist secret
smoke_image="docker.io/library/busybox:1.37.0@sha256:${smoke_digest_prefix}${smoke_digest_suffix}"
container_id="$(docker create --pull=missing "$smoke_image" \
  sh -c 'printf "docker-ce-smoke\\n"')"
docker start --attach "$container_id" | grep -qx 'docker-ce-smoke'
docker rm "$container_id" >/dev/null
container_id=""
build_image="docker-ce-build-smoke:latest"
build_log="$(mktemp)"
if ! printf 'FROM scratch\nLABEL org.example.devbox-smoke=passed\n' \
  | env -u DOCKER_BUILDKIT docker build --tag "$build_image" - \
    >"$build_log" 2>&1; then
  cat "$build_log" >&2
  exit 1
fi
if ! grep -qi 'deprecated' "$build_log" \
  || ! grep -qi 'legacy builder' "$build_log"; then
  cat "$build_log" >&2
  echo "docker build did not use the expected legacy-builder fallback" >&2
  exit 1
fi
docker image rm "$build_image" >/dev/null
build_image=""
rm -f -- "$build_log"
build_log=""
"$repo_path/lima/validate-kind.sh" docker 1
podman info >/dev/null
verify_runtime_offline
"""
    command = command.replace(
        "__PODMAN_OFFLINE_GUARD__", runtime_offline_guard("podman")
    )
    result = devbox_vm.run(
        ["bash", "-ceu", command, "docker-ce-smoke", devbox_vm.repo_path],
        timeout=1200,
        use_guest_runtime=True,
    )

    assert result.returncode == 0, (
        "Docker CE and kind did not run independently of Podman.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "backend identity verified" in result.stdout
    assert "via docker" in result.stdout


@pytest.mark.vm
def test_minikube_docker_ce_backend_runs_inside_the_provisioned_vm(
    repo_root: Path,
    devbox_vm: LimaVM,
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    validator = shlex.quote(f"{devbox_vm.repo_path}/lima/validate-minikube.sh")
    home = shlex.quote(devbox_vm.guest_home)
    command = (
        f"export HOME={home}; "
        + runtime_offline_guard("podman")
        + f"\nbash {validator} docker\nverify_runtime_offline"
    )
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=1800,
        use_guest_runtime=True,
    )

    assert result.returncode == 0, (
        "Minikube did not use the pinned rootful Docker CE backend and complete "
        "its cluster/workload smoke test. Check the strict Docker CE preflight "
        "diagnostic; this backend test is not skipped when dependencies are "
        "missing.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "backend identity verified: Docker CE container" in result.stdout
    assert "minikube validation passed: docker" in result.stdout


@pytest.mark.vm
def test_minikube_podman_backend_runs_inside_the_provisioned_vm(
    repo_root: Path,
    devbox_vm: LimaVM,
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    validator = shlex.quote(f"{devbox_vm.repo_path}/lima/validate-minikube.sh")
    home = shlex.quote(devbox_vm.guest_home)
    command = (
        f"export HOME={home}; "
        + runtime_offline_guard("docker")
        + r"""
export DOCKER_HOST=unix:///var/run/docker.sock
export CONTAINER_HOST="unix:///nonexistent/podman.sock"
export CONTAINER_CONNECTION=issue-319-untrusted-connection
export PODMAN_HOST="unix:///nonexistent/legacy-podman.sock"
"""
        + f"\nbash {validator} podman\nverify_runtime_offline"
    )
    result = devbox_vm.run(
        ["bash", "-ceu", command],
        timeout=1800,
        use_guest_runtime=True,
    )

    assert result.returncode == 0, (
        "The required rootless-Podman Minikube backend failed its preflight or "
        "cluster/workload smoke test. Check the validator's actionable "
        "diagnostic; this backend test is not skipped when dependencies are "
        "missing.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "backend identity verified: rootless Podman container" in result.stdout
    assert "minikube validation passed: podman" in result.stdout


@pytest.mark.vm
def test_fresh_vm_fingerprint_matches_the_current_checkout(
    repo_root: Path, devbox_vm: LimaVM
):
    fresh_vm = (
        os.environ.get("MY_SANDBOX_VM_TEST_FRESH") == "1" or devbox_vm.name is not None
    )
    system_stamp = devbox_vm.run(
        ["cat", "/var/lib/devbox-vm/system-provision.sha256"], timeout=30
    )
    assert system_stamp.returncode == 0, system_stamp.stderr
    expected_system_sha = expected_lima_system_script_sha256(repo_root)
    if system_stamp.stdout.strip() != expected_system_sha:
        if not fresh_vm:
            pytest.skip(
                "the existing guest embeds a different system provisioner; "
                "use a fresh VM to validate the provisioning fingerprint"
            )
        pytest.fail(
            "fresh VM system-script digest differs from the current rendered "
            "provision-system.sh"
        )

    guest_stamp = devbox_vm.run(
        [
            "cat",
            f"{devbox_vm.guest_home}/.local/share/devbox-toolchain/provisioning.fingerprint",
        ],
        timeout=30,
    )
    assert guest_stamp.returncode == 0, guest_stamp.stderr
    expected_fingerprint = expected_lima_provisioning_fingerprint(repo_root)
    if guest_stamp.stdout.strip() != expected_fingerprint:
        if not fresh_vm:
            pytest.skip(
                "the existing guest has stale provisioning inputs; use a fresh "
                "VM to validate the complete fingerprint"
            )
        pytest.fail(
            "fresh VM provisioning fingerprint differs from the current checkout"
        )


@pytest.mark.vm
def test_project_utility_commands_are_discoverable(devbox_vm: LimaVM):
    command = (
        "for tool in devbox-go diff file minikube patch podman virsh "
        'virt-host-validate; do command -v "$tool"; done'
    )
    result = devbox_vm.run(["bash", "-ceu", command], timeout=60)

    assert result.returncode == 0, (
        "VM-provisioned project utilities are unavailable.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_minikube_agent_guidance_is_staged(devbox_vm: LimaVM):
    skill_path = f"{devbox_vm.guest_home}/.agents/skills/devbox-tools/SKILL.md"
    result = devbox_vm.run(
        ["grep", "-F", "-q", "### Minikube in Lima", skill_path], timeout=60
    )

    assert result.returncode == 0, (
        "The VM-owned devbox-tools skill does not expose Minikube guidance.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_guest_can_use_private_opencode_task_scratch(devbox_vm: LimaVM):
    command = r"""
set -euo pipefail
scratch=/tmp/opencode
current_user="$(id -un)"
if [[ -L "$scratch" || ! -d "$scratch" ]]; then
  echo "$scratch is not a real directory for guest user '$current_user'" >&2
  exit 1
fi
metadata="$(stat -c '%u:%g:%a' -- "$scratch")"
if [[ "$metadata" != 0:0:1777 ]]; then
  echo "$scratch is $metadata, expected root:root mode 01777 for '$current_user'" >&2
  exit 1
fi
mount_check_status=0
/usr/bin/python3 -I -S - "$scratch" \
  >/dev/null 2>&1 <<'PY' || mount_check_status=$?
import sys

target = sys.argv[1]
try:
    with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
        for line in mountinfo:
            fields = line.split()
            if len(fields) < 5:
                raise SystemExit(2)
            if fields[4] == target:
                raise SystemExit(0)
except UnicodeError:
    raise SystemExit(2)
except OSError:
    raise SystemExit(2)
raise SystemExit(1)
PY
case "$mount_check_status" in
  0) echo "$scratch is unexpectedly a mountpoint" >&2; exit 1 ;;
  1) ;;
  *)
    echo "cannot inspect mount status for $scratch" >&2
    echo "(exit $mount_check_status)" >&2
    exit 1
    ;;
esac

task_dir="$(mktemp -d "$scratch/issue-298.XXXXXXXX")"
cleanup_task_dir() {
  rm -f -- "$task_dir/sentinel"
  rmdir -- "$task_dir"
}
trap cleanup_task_dir EXIT
task_metadata="$(stat -c '%u:%g:%a' -- "$task_dir")"
expected_task_metadata="$(id -u):$(id -g):700"
if [[ "$task_metadata" != "$expected_task_metadata" ]]; then
  echo "task directory is $task_metadata, expected $expected_task_metadata" >&2
  exit 1
fi
printf '%s\n' 'issue-298 scratch round trip' >"$task_dir/sentinel"
read -r actual <"$task_dir/sentinel"
[[ "$actual" == 'issue-298 scratch round trip' ]]
"""
    result = devbox_vm.run(["bash", "-ceu", command], timeout=30)

    assert result.returncode == 0, (
        "The guest cannot use private task scratch beneath /tmp/opencode, or "
        "the root-owned sticky parent has unexpected permissions.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_agent_guidance_and_skills_are_visible_in_vm(devbox_vm: LimaVM):
    if devbox_vm.name is None and os.environ.get("MY_SANDBOX_VM_TEST_FRESH") != "1":
        source = Path(devbox_vm.repo_path) / "lima/agent-skills/devbox-tools/SKILL.md"
        active = Path(devbox_vm.guest_home) / ".agents/skills/devbox-tools/SKILL.md"
        if not active.is_file() or active.read_bytes() != source.read_bytes():
            pytest.skip(
                "the existing guest has stale staged skills; use a fresh VM "
                "to verify skill provisioning"
            )

    source_path = f"{devbox_vm.repo_path}/lima/agent-skills/devbox-tools/SKILL.md"
    active_path = f"{devbox_vm.guest_home}/.agents/skills/devbox-tools/SKILL.md"
    ast_grep_path = f"{devbox_vm.guest_home}/.agents/skills/ast-grep/SKILL.md"
    outline_path = f"{devbox_vm.guest_home}/.agents/skills/ast-grep-outline/SKILL.md"
    command = f"""
set -euo pipefail
source={shlex.quote(source_path)}
active={shlex.quote(active_path)}
test -r "$source"
test -r "$active"
cmp -s "$source" "$active"
grep -q 'name: "devbox-tools"' "$active"
test -r {shlex.quote(ast_grep_path)}
test -r {shlex.quote(outline_path)}
"""
    result = devbox_vm.run(["bash", "-ceu", command], timeout=60)

    assert result.returncode == 0, (
        "The VM did not stage agent guidance and skills.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_knowledge_base_mount_is_readable_by_path_tools(devbox_vm: LimaVM):
    sentinel = Path(devbox_vm.guest_home) / "kb" / "kbase.py"
    result = devbox_vm.run(
        [
            "python3",
            "-c",
            "from pathlib import Path; import sys; p = Path(sys.argv[1]); "
            "assert p.is_file(); assert p.read_bytes()",
            str(sentinel),
        ],
        timeout=30,
    )

    assert result.returncode == 0, (
        "The canonical ~/kb mount is not searchable/readable by filesystem tools.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_opencode_data_mount_is_visible_from_host_and_l1(devbox_vm: LimaVM):
    if os.environ.get("MY_SANDBOX_VM_TEST_FRESH") != "1":
        pytest.skip(
            "bidirectional mount probe is limited to the disposable CI host home"
        )

    data_dir = Path(devbox_vm.guest_home) / ".local/share/opencode"
    inbound = data_dir / "issue-287-mount-inbound"
    outbound = data_dir / "issue-287-mount-outbound"
    try:
        inbound_content = inbound.read_text()
    except OSError as error:
        pytest.fail(
            f"The host-visible OpenCode data seed cannot be read in L1: {error}"
        )
    assert inbound_content == "host-to-L1\n"
    outbound.write_text("L1-to-host\n")


@pytest.mark.vm
def test_host_credential_environment_is_not_forwarded_to_vm_processes(
    devbox_vm: LimaVM, monkeypatch: pytest.MonkeyPatch
):
    """The fixture's test-process environment must not carry host secrets.

    In local guest mode, this does not hide same-UID files under
    ``~/.host-config``; only trusted source may be run with ``--guest-vm``.
    """
    secret = "mock-vm-credential-must-not-cross-boundary"  # pragma: allowlist secret
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    result = devbox_vm.run(["bash", "-ceu", "env"], timeout=30)

    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "GH_TOKEN=" not in result.stdout
    assert "ANTHROPIC_API_KEY=" not in result.stdout
    assert "GH_TOKEN" not in devbox_vm.env
    assert "ANTHROPIC_API_KEY" not in devbox_vm.env
