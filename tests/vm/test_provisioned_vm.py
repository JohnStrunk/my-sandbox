import json
import os
import re
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
def test_provisioned_l1_exposes_the_vm_level_marker(repo_root: Path, devbox_vm: LimaVM):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    command = r"""
set -euo pipefail
marker=/etc/devbox/vm-level
[[ -f "$marker" && ! -L "$marker" ]]
[[ "$(stat -c '%u:%g:%a' "$marker")" == 0:0:644 ]]
if findmnt -rn -M "$marker" >/dev/null 2>&1; then
  echo "$marker is unexpectedly a mountpoint" >&2
  exit 1
fi
[[ "$(cat "$marker")" == L1 ]]
"""
    result = devbox_vm.run(["bash", "-ceu", command], timeout=30)

    assert result.returncode == 0, (
        "The provisioned L1 does not expose a valid devbox VM level marker.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
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
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    output_lines = result.stdout.splitlines()
    for tool in ("ruby", "gocyclo", "golangci_lint", "actionlint", "yamllint"):
        expected_version = manifest["tools"][tool]["version"]
        assert f"OK: {tool}: {expected_version}" in output_lines
    for tool in ("mermaid_cli", "playwright_cli", "opencode"):
        expected_version = manifest["tools"][tool]["version"]
        assert f"OK: {tool}: {expected_version}" in output_lines
    for tool in ("gcc", "g++", "make", "cmake", "gdb", "ninja", "pkg-config"):
        assert any(line.startswith(f"OK: {tool}:") for line in output_lines), tool

    linter_version = devbox_vm.run(["golangci-lint", "--version"], timeout=60)
    assert linter_version.returncode == 0, (
        "Could not read golangci-lint's embedded Go compiler version.\n"
        f"stdout: {linter_version.stdout}\nstderr: {linter_version.stderr}"
    )
    linter_output = f"{linter_version.stdout}\n{linter_version.stderr}"
    embedded_go = re.search(r"\bbuilt with go(\d+)\.(\d+)(?:\.\d+)?\b", linter_output)
    assert embedded_go, (
        f"Could not parse embedded Go compiler version:\n{linter_output}"
    )
    manifest_go = tuple(
        int(part)
        for part in manifest["tools"]["go"]["version"].removeprefix("v").split(".")[:2]
    )
    embedded_go_major_minor = tuple(int(part) for part in embedded_go.groups())
    # Keep the lint binary compatible with the VM Go language level.
    assert embedded_go_major_minor >= manifest_go, (
        f"golangci-lint embeds Go {'.'.join(map(str, embedded_go_major_minor))}, "
        f"older than manifest Go {'.'.join(map(str, manifest_go))}"
    )


@pytest.mark.vm
def test_provisioned_vm_compiles_and_runs_c_and_cpp(repo_root: Path, devbox_vm: LimaVM):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    command = r"""
set -euo pipefail
task_root="$(mktemp -d /tmp/opencode/issue-469-compile.XXXXXXXX)"
trap 'rm -rf -- "${task_root:?}"' EXIT

for tool in gcc g++ make cmake gdb ninja pkg-config; do
  command -v "$tool" >/dev/null 2>&1 \
    || { echo "$tool is not available on PATH" >&2; exit 1; }
  "$tool" --version >/dev/null 2>&1 \
    || { echo "$tool --version failed" >&2; exit 1; }
done

cat >"$task_root/main.c" <<'EOF'
#include <stdio.h>

int main(void) {
    puts("issue-469-c-ok");
    return 0;
}
EOF
cat >"$task_root/main.cpp" <<'EOF'
#include <iostream>

int main() {
    std::cout << "issue-469-cpp-ok\n";
    return 0;
}
EOF
cat >"$task_root/exit_code.c" <<'EOF'
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    puts("issue-469-exit-code");
    return argc > 1 ? atoi(argv[1]) : 0;
}
EOF

gcc -Wall -Wextra -Werror -std=c17 "$task_root/main.c" -o "$task_root/c-app"
"$task_root/c-app" | grep -qx 'issue-469-c-ok'
g++ -Wall -Wextra -Werror -std=c++17 "$task_root/main.cpp" -o "$task_root/cpp-app"
"$task_root/cpp-app" | grep -qx 'issue-469-cpp-ok'
printf 'compiled and ran C and C++ programs\n'

gdb_banner="$(gdb --version 2>&1)" || {
  echo "gdb --version failed (status $?)" >&2
  printf '%s\n' "$gdb_banner" >&2
  exit 1
}
if ! grep -q '^GNU gdb ' <<<"$gdb_banner"; then
  echo "gdb is not GNU GDB:" >&2
  printf '%s\n' "$gdb_banner" >&2
  exit 1
fi
gcc -Wall -Wextra -Werror -std=c17 "$task_root/exit_code.c" -o "$task_root/exit-code"

# Batch mode suppresses GDB's own "exited" messages, so assert the program's
# marker line and the numeric child status instead of translatable GDB text.
# Fedora's GNU gdb supports --return-child-result, so a missing child marker or
# a status other than the child's own is a failure rather than a skip.
#
# gdb emits lines after the child marker, so capture its output instead of
# piping it into grep: an early grep exit would SIGPIPE gdb under pipefail.
run_under_gdb() {
  local app="$1" expected="$2" log="$3"
  local status=0
  gdb -batch -nx -q -ex run --args "$task_root/$app" >"$log" 2>&1 \
    || status=$?
  if [[ "$status" -ne 0 ]]; then
    echo "gdb failed to run $app (status $status):" >&2
    cat "$log" >&2
    return 1
  fi
  if ! grep -qx "$expected" "$log"; then
    echo "gdb ran $app but did not print '$expected':" >&2
    cat "$log" >&2
    return 1
  fi
}
run_under_gdb c-app issue-469-c-ok "$task_root/gdb-c.log"
run_under_gdb cpp-app issue-469-cpp-ok "$task_root/gdb-cpp.log"
propagation_status=0
propagation_output="$(gdb -batch -nx -q --return-child-result -ex run \
  --args "$task_root/exit-code" 42 2>&1)" || propagation_status=$?
if ! grep -qx 'issue-469-exit-code' <<<"$propagation_output"; then
  echo "gdb did not run the child under --return-child-result" \
    "(status $propagation_status)" >&2
  printf '%s\n' "$propagation_output" >&2
  exit 1
fi
if [[ "$propagation_status" -ne 42 ]]; then
  echo "gdb reported status $propagation_status instead of the child's 42" >&2
  printf '%s\n' "$propagation_output" >&2
  exit 1
fi
printf 'gdb propagated the child exit status\n'
printf 'debugged C and C++ programs under GNU gdb in batch mode\n'
"""
    result = devbox_vm.run(["bash", "-ceu", command], timeout=300)

    assert result.returncode == 0, (
        "The provisioned VM could not compile, link, and run minimal C and C++ "
        "programs with gcc/g++, or could not debug them with GDB.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "compiled and ran C and C++ programs" in result.stdout
    assert "gdb propagated the child exit status" in result.stdout
    assert "debugged C and C++ programs under GNU gdb in batch mode" in result.stdout


@pytest.mark.vm
def test_provisioned_vm_builds_cmake_c_and_cpp_with_make_and_ninja(
    repo_root: Path, devbox_vm: LimaVM
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    command = r"""
set -euo pipefail
task_root="$(mktemp -d /tmp/opencode/issue-469-cmake.XXXXXXXX)"
trap 'rm -rf -- "${task_root:?}"' EXIT

mkdir -p "$task_root/c" "$task_root/cpp"
cat >"$task_root/c/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(issue_469_c LANGUAGES C)
add_executable(c_app main.c)
EOF
cat >"$task_root/c/main.c" <<'EOF'
#include <stdio.h>

int main(void) {
    puts("issue-469-cmake-c-ok");
    return 0;
}
EOF
cat >"$task_root/cpp/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(issue_469_cpp LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_executable(cpp_app main.cpp)
EOF
cat >"$task_root/cpp/main.cpp" <<'EOF'
#include <iostream>

int main() {
    std::cout << "issue-469-cmake-cpp-ok\n";
    return 0;
}
EOF

build_with() {
  local language="$1" generator="$2" slug="$3" app="$4" expected="$5"
  local build_dir="$task_root/build-${language}-${slug}"
  cmake -S "$task_root/$language" -B "$build_dir" -G "$generator"
  cmake --build "$build_dir" --parallel
  "$build_dir/$app" | grep -qx "$expected"
  case "$generator" in
    Ninja) test -f "$build_dir/build.ninja" ;;
    "Unix Makefiles") test -f "$build_dir/Makefile" ;;
    *) echo "unexpected generator: $generator" >&2; return 1 ;;
  esac
  printf 'cmake %s %s: ok\n' "$language" "$generator"
}

build_with c "Unix Makefiles" makefiles c_app issue-469-cmake-c-ok
build_with c Ninja ninja c_app issue-469-cmake-c-ok
build_with cpp "Unix Makefiles" makefiles cpp_app issue-469-cmake-cpp-ok
build_with cpp Ninja ninja cpp_app issue-469-cmake-cpp-ok
"""
    result = devbox_vm.run(["bash", "-ceu", command], timeout=900)

    assert result.returncode == 0, (
        "The provisioned VM could not configure, build, and run minimal C and "
        "C++ CMake projects with both Unix Makefiles and Ninja.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    for language in ("c", "cpp"):
        for generator in ("Unix Makefiles", "Ninja"):
            assert f"cmake {language} {generator}: ok" in result.stdout


@pytest.mark.vm
def test_coderabbit_cli_and_config_mount_are_available_in_l1(
    repo_root: Path, devbox_vm: LimaVM
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    expected_version = json.loads(
        (repo_root / "lima" / "tool-versions.json").read_text()
    )["tools"]["coderabbit_cli"]["version"]
    command = r"""
set -euo pipefail
guest_home="$1"
expected_version="$2"
installed_version="$(coderabbit --version 2>&1 \
  | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+([+-][[:alnum:].]+)?' | head -n1)"
[[ "$installed_version" == "$expected_version" ]]
cr_version="$(cr --version 2>&1 \
  | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+([+-][[:alnum:].]+)?' | head -n1)"
[[ "$cr_version" == "$expected_version" ]]

config_mount="$guest_home/.host-config/coderabbit"
[[ -L "$guest_home/.coderabbit" ]]
[[ "$(readlink "$guest_home/.coderabbit")" == "$config_mount" ]]
findmnt -rn -M "$config_mount" -t 9p,virtiofs >/dev/null
mount_options="$(findmnt -rn -M "$config_mount" -o OPTIONS)"
case ",$mount_options," in
  *,rw,*) ;;
  *) echo "CodeRabbit config mount is not writable: $mount_options" >&2; exit 1 ;;
esac
"""
    result = devbox_vm.run(
        [
            "bash",
            "-ceu",
            command,
            "coderabbit-l1",
            devbox_vm.guest_home,
            expected_version,
        ],
        timeout=60,
    )

    assert result.returncode == 0, (
        "The pinned CodeRabbit CLI or writable host config mount is unavailable "
        "in the L1 devbox VM.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_subsequent_guest_command_has_current_group_access(
    repo_root: Path, devbox_vm: LimaVM
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)

    access_command = r"""
set -euo pipefail
unset DOCKER_HOST DOCKER_CONTEXT CONTAINER_HOST CONTAINER_CONNECTION PODMAN_HOST
groups="$(id -nG)"
for required_group in kvm libvirt docker; do
  printf '%s\n' "$groups" | tr ' ' '\n' | grep -qx "$required_group"
done
[[ -r /dev/kvm && -w /dev/kvm ]]
virsh -c qemu:///system list --all >/dev/null
virsh -c qemu:///system net-list --all >/dev/null
docker_socket=/var/run/docker.sock
podman_socket="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
[[ -S "$docker_socket" && -r "$docker_socket" && -w "$docker_socket" ]]
[[ "$(stat -c '%U:%G:%a' "$docker_socket")" == root:docker:660 ]]
[[ "$(docker context inspect default --format '{{.Endpoints.docker.Host}}')" \
  == unix:///var/run/docker.sock ]]
docker info >/dev/null
[[ -S "$podman_socket" ]]
podman info >/dev/null
"""
    result = devbox_vm.run(
        ["bash", "-ceu", access_command], timeout=180, use_guest_runtime=True
    )
    assert result.returncode == 0, (
        "A subsequent guest command lacks current KVM, libvirt, or Docker group "
        "access, or cannot query the Docker/Podman APIs.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


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

# Docker RPM versions float to whatever the signed Fedora stable repository
# provided when this VM was created, so verify package identity, architecture,
# and the pinned Docker signing key instead of an exact version tuple. The
# reported versions are printed so architecture-specific differences are
# visible in the test log.
host_arch="$(uname -m)"
case "$host_arch" in
  x86_64) docker_api_arch=amd64 ;;
  aarch64) docker_api_arch=arm64 ;;
  *) echo "unsupported architecture: $host_arch" >&2; exit 1 ;;
esac
docker_rpm_packages=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin)
for package in "${docker_rpm_packages[@]}"; do
  package_arch="$(rpm -q --queryformat '%{ARCH}' "$package")"
  [[ "$package_arch" == "$host_arch" ]] \
    || { echo "$package is built for $package_arch, not $host_arch" >&2; exit 1; }
  signature="$(rpm -q --queryformat '%{RSAHEADER:pgpsig}' "$package")"
  [[ "$signature" == *"Key ID c52feb6b621e9f35"* ]] \
    || { echo "$package is not signed by Docker's release key:" \
      "$signature" >&2; exit 1; }
  package_version="$(rpm -q --queryformat '%{VERSION}' "$package")"
  [[ "$package_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.]+)?$ ]] \
    || { echo "$package has an unexpected version: $package_version" >&2; exit 1; }
  printf 'docker rpm: %s %s %s\n' "$package" "$package_version" "$package_arch"
done
[[ "$(rpm -qf --queryformat '%{NAME}' "$docker_cli_path")" == docker-ce-cli ]]
[[ "$(rpm -qf --queryformat '%{NAME}' /usr/bin/dockerd)" == docker-ce ]]
[[ "$(rpm -qf --queryformat '%{NAME}' /usr/bin/containerd)" == containerd.io ]]
[[ "$(rpm -qf --queryformat '%{NAME}' \
  /usr/libexec/docker/cli-plugins/docker-buildx)" == docker-buildx-plugin ]]

# The packages must still come from the signed Fedora stable repository.
docker_repo_file=/etc/yum.repos.d/docker-ce.repo
docker_key_file=/etc/pki/rpm-gpg/RPM-GPG-KEY-docker-ce
[[ -f "$docker_repo_file" && ! -L "$docker_repo_file" ]]
[[ -f "$docker_key_file" && ! -L "$docker_key_file" ]]
grep -Fqx '[docker-ce-stable]' "$docker_repo_file"
grep -Fqx 'repo_gpgcheck=1' "$docker_repo_file"
grep -Fqx 'gpgcheck=1' "$docker_repo_file"
grep -Fq 'baseurl=https://download.docker.com/linux/fedora/' "$docker_repo_file"

if rpm -q podman-docker >/dev/null 2>&1; then
  echo "podman-docker must not provide the Docker CLI" >&2
  exit 1
fi
for plugin in docker-ce-rootless-extras docker-compose-plugin; do
  if rpm -q "$plugin" >/dev/null 2>&1; then
    echo "$plugin is not part of the supported Docker CE profile" >&2
    exit 1
  fi
done
buildx_version="$(docker buildx version)"
[[ "$buildx_version" =~ ^github\.com/docker/buildx\ v[0-9]+\.[0-9]+\.[0-9]+ ]] \
  || { echo "unexpected buildx version: $buildx_version" >&2; exit 1; }
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
# Name the default context explicitly so a user Docker config currentContext
# cannot redirect the identity probe.
client_version="$(docker --context default version --format '{{.Client.Version}}')"
server_version="$(docker --context default version --format '{{.Server.Version}}')"
server_arch="$(docker --context default version --format '{{.Server.Arch}}')"
server_platform="$(
  docker --context default version --format '{{.Server.Platform.Name}}'
)"
[[ "$client_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]
[[ "$server_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]
[[ "$server_arch" == "$docker_api_arch" ]]
[[ "$server_platform" == "Docker Engine"* ]]

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
if ! printf '%s\n' \
  'FROM --platform=$BUILDPLATFORM scratch' \
  'LABEL org.example.devbox-smoke=passed' \
  | DOCKER_BUILDKIT=1 docker build --tag "$build_image" - \
    >"$build_log" 2>&1; then
  cat "$build_log" >&2
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
        "Docker CE and kind did not run independently of Podman, or the "
        "floating signed RPM set failed its identity/architecture checks.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "backend identity verified" in result.stdout
    assert "via docker" in result.stdout
    # Installed RPM versions are reported (they float with the signed repo).
    for package in (
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
    ):
        assert f"docker rpm: {package} " in result.stdout


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
        "Minikube did not use the trusted rootful Docker CE backend and complete "
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
def test_provisioned_vm_renders_mermaid_diagrams(repo_root: Path, devbox_vm: LimaVM):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    expected_version = json.loads(
        (repo_root / "lima" / "tool-versions.json").read_text()
    )["tools"]["mermaid_cli"]["version"]
    command = r"""
set -euo pipefail
expected_version="$1"
task_root="$(mktemp -d /tmp/opencode/issue-487-mermaid.XXXXXXXX)"
trap 'rm -rf -- "${task_root:?}"' EXIT

installed_version="$(mmdc --version)"
# Match check_toolchain's normalization: an optional v prefix is legitimate.
installed_version="${installed_version#v}"
[[ "$installed_version" == "$expected_version" ]]

cat >"$task_root/diagram.mmd" <<'EOF'
flowchart LR
    A[Input] --> B{Decision}
    B -- yes --> C[Output]
    B -- no --> A
EOF

mmdc --input "$task_root/diagram.mmd" --output "$task_root/diagram.svg"
grep -q '<svg' "$task_root/diagram.svg"
test -s "$task_root/diagram.svg"

mmdc --input "$task_root/diagram.mmd" --output "$task_root/diagram.png"
png_description="$(file -b "$task_root/diagram.png")"
grep -qi '^png image data' <<<"$png_description"
test -s "$task_root/diagram.png"

# Puppeteer must reuse the shared Playwright browser, not download its own.
test ! -e /var/lib/devbox-toolbuilder/.cache/puppeteer
"""
    result = devbox_vm.run(
        [
            "bash",
            "-ceu",
            command,
            "mermaid-render",
            expected_version,
        ],
        timeout=300,
    )

    assert result.returncode == 0, (
        "The provisioned VM could not render a Mermaid diagram to SVG and PNG "
        "with the manifest-pinned mmdc and the shared chrome-for-testing "
        "binary.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_actionlint_and_yamllint_are_available(repo_root: Path, devbox_vm: LimaVM):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    manifest = json.loads((repo_root / "lima" / "tool-versions.json").read_text())
    command = r"""
set -euo pipefail
actionlint_expected="$1"
yamllint_expected="$2"
[[ "$(actionlint -version 2>/dev/null | head -n1)" == "$actionlint_expected" ]]
[[ "$(yamllint --version 2>/dev/null)" == "yamllint $yamllint_expected" ]]
"""
    result = devbox_vm.run(
        [
            "bash",
            "-ceu",
            command,
            "yaml-workflow-linters",
            manifest["tools"]["actionlint"]["version"],
            manifest["tools"]["yamllint"]["version"],
        ],
        timeout=60,
    )

    assert result.returncode == 0, (
        "The manifest-pinned actionlint or yamllint is unavailable in the "
        "provisioned L1 devbox VM.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_project_utility_commands_are_discoverable(devbox_vm: LimaVM):
    command = (
        "for tool in devbox-go diff file gocyclo golangci-lint "
        "minikube patch podman virsh "
        'virt-host-validate; do command -v "$tool"; done'
    )
    result = devbox_vm.run(["bash", "-ceu", command], timeout=60)

    assert result.returncode == 0, (
        "VM-provisioned project utilities are unavailable.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_krew_is_available_in_a_login_shell(repo_root: Path, devbox_vm: LimaVM):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    expected_version = json.loads(
        (repo_root / "lima" / "tool-versions.json").read_text()
    )["tools"]["krew"]["version"]
    command = r"""
set -euo pipefail
export HOME="$1"
expected_version="$2"
version_output="$(bash -lc 'kubectl krew version')"
grep -Fq 'GitTag' <<<"$version_output"
grep -Fq "v$expected_version" <<<"$version_output"
printf '%s\n' "$version_output"
"""
    result = devbox_vm.run(
        [
            "bash",
            "-ceu",
            command,
            "krew-login-shell",
            devbox_vm.guest_home,
            expected_version,
        ],
        timeout=60,
    )

    assert result.returncode == 0, (
        "Krew is not available from a provisioned login shell.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_krew_plugin_install_uninstall_uses_temporary_store(
    repo_root: Path, devbox_vm: LimaVM
):
    skip_if_guest_provisioning_is_stale(repo_root, devbox_vm)
    expected_version = json.loads(
        (repo_root / "lima" / "tool-versions.json").read_text()
    )["tools"]["krew"]["version"]
    command = r"""
set -euo pipefail
guest_krew_bin="$1"
persistent_store="$2"
expected_version="$3"
task_root="$(mktemp -d /tmp/opencode/krew-test.XXXXXXXX)"
trap 'rm -rf -- "$task_root"' EXIT
export HOME="$task_root/home"
mkdir -p "$HOME"
unset KUBECONFIG
export KREW_ROOT="$task_root/store"
export PATH="$KREW_ROOT/bin:$guest_krew_bin:$PATH"
test ! -e "$HOME/.kube/config"

persistent_snapshot() {
  if [[ -e "$persistent_store" ]]; then
    find "$persistent_store" -printf '%y %P\n' | sort
    find "$persistent_store" -type f -exec sha256sum -- {} + | sort
  fi
}
persistent_snapshot >"$task_root/persistent-before"

"$guest_krew_bin/kubectl-krew" install krew
"$guest_krew_bin/kubectl-krew" install krew
install -D -m 0755 "$guest_krew_bin/kubectl-krew" \
  "$KREW_ROOT/bin/kubectl-krew"
kubectl krew version | grep -Fq "v$expected_version"
kubectl krew update
kubectl krew install ctx
test -x "$KREW_ROOT/bin/kubectl-ctx"
kubectl krew uninstall ctx
test ! -e "$KREW_ROOT/bin/kubectl-ctx"

persistent_snapshot >"$task_root/persistent-after"
cmp -s "$task_root/persistent-before" "$task_root/persistent-after"
"""
    result = devbox_vm.run(
        [
            "bash",
            "-ceu",
            command,
            "krew-plugin-roundtrip",
            f"{devbox_vm.guest_home}/.krew/bin",
            f"{devbox_vm.guest_home}/.krew",
            expected_version,
        ],
        timeout=600,
    )

    assert result.returncode == 0, (
        "Krew could not install and uninstall a plugin in its temporary store, "
        "or changed the persistent plugin store.\n"
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
def test_docker_buildx_agent_guidance_is_staged(devbox_vm: LimaVM):
    if devbox_vm.name is None and os.environ.get("MY_SANDBOX_VM_TEST_FRESH") != "1":
        source = Path(devbox_vm.repo_path) / "lima/agent-skills/devbox-tools/SKILL.md"
        active = Path(devbox_vm.guest_home) / ".agents/skills/devbox-tools/SKILL.md"
        if not active.is_file() or active.read_bytes() != source.read_bytes():
            pytest.skip("the existing guest has stale staged skills")

    skill_path = f"{devbox_vm.guest_home}/.agents/skills/devbox-tools/SKILL.md"
    result = devbox_vm.run(
        ["grep", "-F", "-q", "docker --context default build", skill_path],
        timeout=60,
    )

    assert result.returncode == 0, (
        "The staged devbox-tools skill does not expose safe Docker Buildx usage.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_go_linter_guidance_is_staged(devbox_vm: LimaVM):
    if devbox_vm.name is None and os.environ.get("MY_SANDBOX_VM_TEST_FRESH") != "1":
        source = Path(devbox_vm.repo_path) / "lima/agent-skills/devbox-tools/SKILL.md"
        active = Path(devbox_vm.guest_home) / ".agents/skills/devbox-tools/SKILL.md"
        if not active.is_file() or active.read_bytes() != source.read_bytes():
            pytest.skip("the existing guest has stale staged skills")

    skill_path = f"{devbox_vm.guest_home}/.agents/skills/devbox-tools/SKILL.md"
    result = devbox_vm.run(
        ["grep", "-F", "-q", "### Go source linters", skill_path], timeout=60
    )

    assert result.returncode == 0, (
        "The staged devbox-tools skill does not expose Go linter guidance.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.vm
def test_c_cpp_agent_guidance_is_staged(devbox_vm: LimaVM):
    if devbox_vm.name is None and os.environ.get("MY_SANDBOX_VM_TEST_FRESH") != "1":
        source = Path(devbox_vm.repo_path) / "lima/agent-skills/devbox-tools/SKILL.md"
        active = Path(devbox_vm.guest_home) / ".agents/skills/devbox-tools/SKILL.md"
        if not active.is_file() or active.read_bytes() != source.read_bytes():
            pytest.skip("the existing guest has stale staged skills")

    skill_path = f"{devbox_vm.guest_home}/.agents/skills/devbox-tools/SKILL.md"
    command = r"""
set -euo pipefail
skill="$1"
grep -F -q '### Fedora-provided C/C++ toolchain' "$skill"
grep -F -q '`pkgconf-pkg-config` provides `pkg-config`' "$skill"
grep -F -q '`devbox --recreate` to adopt package-set changes' "$skill"
"""
    result = devbox_vm.run(
        ["bash", "-ceu", command, "c-cpp-guidance", skill_path], timeout=60
    )

    assert result.returncode == 0, (
        "The staged devbox-tools skill does not expose the C/C++ toolchain entry.\n"
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
grep -q '### Krew plugins' "$active"
grep -q '### GitHub Actions and YAML checks' "$active"
grep -F -q 'actionlint' "$active"
grep -F -q 'yamllint' "$active"
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
