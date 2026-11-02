"""Unit tests for lima/docker-ce-preflight.sh under fake system tools.

The preflight pins the Docker repository definition and RPM key to fixed
``/etc`` paths. These tests source the real preflight and then point those two
globals at fixture files, so every origin and identity branch is exercised
without a provisioned VM.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_TRUSTED_KEY_ID = "c52feb6b621e9f35"


def _host_arch() -> str:
    return subprocess.run(
        ["uname", "-m"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _server_arch() -> str:
    return "arm64" if _host_arch() == "aarch64" else "amd64"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def _write_fake_tools(fake_bin: Path, omit: frozenset[str] = frozenset()) -> None:
    fake_bin.mkdir(parents=True, exist_ok=True)
    tools = {
        "rpm": r"""#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *' podman-docker '* ]]; then
  [[ "${PODMAN_DOCKER_INSTALLED:-}" == 1 ]] && exit 0
  exit 1
fi
[[ "$*" == *--queryformat* ]] || {
  echo "unexpected rpm invocation: $*" >&2; exit 64; }
if [[ "$1" == "-qf" ]]; then
  path="${!#}"
  if [[ "${RPM_OWNER_OVERRIDE_PATH:-}" == "$path" ]]; then
    printf '%s\n' "${RPM_OWNER_OVERRIDE_NAME}"
    exit 0
  fi
  case "$path" in
    */docker) printf 'docker-ce-cli\n' ;;
    */dockerd) printf 'docker-ce\n' ;;
    */containerd) printf 'containerd.io\n' ;;
    */docker-buildx) printf 'docker-buildx-plugin\n' ;;
    *) echo "unexpected rpm -qf path: $path" >&2; exit 64 ;;
  esac
  exit 0
fi
format="$3"
case "$format" in
  *RSAHEADER*)
    [[ "${RPM_KEY_UNREADABLE:-}" == 1 ]] && exit 1
    printf 'RSA/SHA256, Tue 20 Jan 2026 03:22:11 AM EST, Key ID %s\n' \
      "${RPM_KEY_ID:-c52feb6b621e9f35}"
    printf '(none)\n(none)\n(none)\n'
    if [[ -n "${RPM_EXTRA_KEY_ID:-}" ]]; then
      printf 'DSA/SHA256, Tue 20 Jan 2026 03:22:11 AM EST, Key ID %s\n' \
        "$RPM_EXTRA_KEY_ID"
    fi
    ;;
  *ARCH*)
    [[ "${RPM_PACKAGE_MISSING:-}" == 1 ]] && exit 1
    printf '%s\n' "${RPM_PACKAGE_ARCH:-$(uname -m)}"
    ;;
  *) echo "unexpected rpm query format: $format" >&2; exit 64 ;;
esac
""",
        "stat": r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "${DOCKER_DAEMON_UID:-0}"
""",
        "systemctl": r"""#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  is-active)
    for argument in "$@"; do
      [[ "$argument" != "${SYSTEMD_INACTIVE_UNIT:-}" ]] || exit 1
    done
    exit 0
    ;;
  is-enabled)
    for argument in "$@"; do
      [[ "$argument" != "${SYSTEMD_DISABLED_UNIT:-}" ]] || exit 1
    done
    exit 0
    ;;
  show) printf '%s\n' "${DOCKER_DAEMON_PID:-1}" ;;
  *) echo "unexpected systemctl invocation: $*" >&2; exit 64 ;;
esac
""",
        "docker": r"""#!/usr/bin/env bash
set -euo pipefail
[[ -z "${DOCKER_CONTEXT:-}" ]] || {
  echo 'caller Docker context was not cleared' >&2; exit 65; }
[[ "${DOCKER_HOST:-}" == unix:///var/run/docker.sock ]] || {
  echo "unexpected Docker endpoint: ${DOCKER_HOST:-<unset>}" >&2; exit 65; }
if [[ -n "${DOCKER_LOG:-}" ]]; then
  printf '%s|%s\n' "$*" "${DOCKER_HOST:-<unset>}" >>"$DOCKER_LOG"
fi
# A user ~/.docker/config.json currentContext must not redirect the probe.
[[ "${1:-}" == --context && "${2:-}" == default ]] || {
  echo "docker must select the explicit default context: $*" >&2; exit 65; }
shift 2
[[ "$1" == version ]] || {
  echo "unexpected docker invocation: $*" >&2; exit 64; }
[[ "${DOCKER_VERSION_FAIL:-}" == 1 ]] && exit 1
if [[ -n "${DOCKER_VERSION:-}" ]]; then
  printf '%s\n' "$DOCKER_VERSION"
else
  printf '%s\n' "29.9.2|29.9.2|${SERVER_ARCH}|Docker Engine - Community"
fi
""",
    }
    for name, contents in tools.items():
        if name in omit:
            continue
        _write_executable(fake_bin / name, contents)


class _Preflight:
    def __init__(self, tmp_path: Path, *, omit_tools: frozenset[str] = frozenset()):
        self.fake_bin = tmp_path / "bin"
        _write_fake_tools(self.fake_bin, omit_tools)
        self.key_file = tmp_path / "RPM-GPG-KEY-docker-ce"
        self.key_file.write_text("fixture Docker release key\n")
        self.repo_file = tmp_path / "docker-ce.repo"
        self.write_repo()
        self.env = {
            "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            "SERVER_ARCH": _server_arch(),
        }

    def write_repo(self, settings: list[str] | None = None) -> None:
        if settings is None:
            settings = [
                "[docker-ce-stable]",
                "name=Docker CE Stable - $basearch",
                "baseurl=https://download.docker.com/linux/fedora/"
                "$releasever/$basearch/stable",
                "enabled=0",
                "gpgcheck=1",
                "repo_gpgcheck=1",
                f"gpgkey=file://{self.key_file}",
            ]
        self.repo_file.write_text("\n".join(settings) + "\n")

    def run(
        self,
        repo_root: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        preflight = repo_root / "lima" / "docker-ce-preflight.sh"
        harness = (
            "set -euo pipefail\n"
            f'source "{preflight}"\n'
            f'DOCKER_CE_REPO_FILE="{self.repo_file}"\n'
            f'DOCKER_CE_KEY_FILE="{self.key_file}"\n'
            "docker_ce_preflight\n"
        )
        return subprocess.run(
            ["bash", "-c", harness],
            check=False,
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
            timeout=15,
        )


@pytest.fixture
def preflight(tmp_path: Path) -> _Preflight:
    return _Preflight(tmp_path)


@pytest.mark.unit
def test_preflight_accepts_a_trusted_signed_install(
    repo_root: Path, preflight: _Preflight
):
    result = preflight.run(repo_root)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.unit
def test_preflight_requires_every_package(repo_root: Path, preflight: _Preflight):
    result = preflight.run(repo_root, env={"RPM_PACKAGE_MISSING": "1"})

    assert result.returncode != 0
    assert "required package docker-ce is missing" in result.stderr


@pytest.mark.unit
def test_preflight_rejects_a_package_for_another_architecture(
    repo_root: Path, preflight: _Preflight
):
    foreign_arch = "aarch64" if _host_arch() != "aarch64" else "x86_64"

    result = preflight.run(repo_root, env={"RPM_PACKAGE_ARCH": foreign_arch})

    assert result.returncode != 0
    assert f"docker-ce is built for {foreign_arch}, expected {_host_arch()}" in (
        result.stderr
    )


@pytest.mark.unit
def test_preflight_reads_one_signature_from_multi_signature_rpm_output(
    repo_root: Path, preflight: _Preflight
):
    """rpm reports each stored signature tag; the first key ID is authoritative.

    ``grep -m1`` must tolerate several ``Key ID`` lines without a head pipeline
    that could fail under ``set -o pipefail``.
    """

    accepted = preflight.run(repo_root, env={"RPM_EXTRA_KEY_ID": "deadbeefdeadbeef"})
    assert accepted.returncode == 0, accepted.stderr

    rejected = preflight.run(
        repo_root,
        env={
            "RPM_KEY_ID": "deadbeefdeadbeef",
            "RPM_EXTRA_KEY_ID": _TRUSTED_KEY_ID,
        },
    )
    assert rejected.returncode != 0
    assert "signed by key deadbeefdeadbeef" in rejected.stderr


@pytest.mark.unit
def test_preflight_rejects_a_package_from_an_unexpected_signing_key(
    repo_root: Path, preflight: _Preflight
):
    result = preflight.run(repo_root, env={"RPM_KEY_ID": "deadbeefdeadbeef"})

    assert result.returncode != 0
    assert "docker-ce is signed by key deadbeefdeadbeef" in result.stderr
    assert f"not the Docker release key {_TRUSTED_KEY_ID}" in result.stderr
    assert "recreate the VM from a trusted template" in result.stderr


@pytest.mark.unit
def test_preflight_requires_a_readable_rpm_signature(
    repo_root: Path, preflight: _Preflight
):
    result = preflight.run(repo_root, env={"RPM_KEY_UNREADABLE": "1"})

    assert result.returncode != 0
    assert "cannot read the RPM signing key for docker-ce" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("package_path", ["/usr/bin/dockerd", "docker-buildx"])
def test_preflight_rejects_a_binary_owned_by_another_package(
    repo_root: Path, preflight: _Preflight, package_path: str
):
    path = (
        "/usr/libexec/docker/cli-plugins/docker-buildx"
        if package_path == "docker-buildx"
        else package_path
    )

    result = preflight.run(
        repo_root,
        env={
            "RPM_OWNER_OVERRIDE_PATH": path,
            "RPM_OWNER_OVERRIDE_NAME": "docker-ce-rootless-extras",
        },
    )

    assert result.returncode != 0
    assert f"{path} is owned by docker-ce-rootless-extras" in result.stderr


@pytest.mark.unit
def test_preflight_rejects_a_missing_or_symlinked_repository(
    repo_root: Path, preflight: _Preflight
):
    preflight.repo_file.unlink()

    result = preflight.run(repo_root)

    assert result.returncode != 0
    assert "repository definition" in result.stderr
    assert "missing or symlinked" in result.stderr


@pytest.mark.unit
def test_preflight_rejects_a_symlinked_repository(
    repo_root: Path, preflight: _Preflight
):
    moved = preflight.repo_file.with_name("docker-ce.repo.real")
    preflight.repo_file.rename(moved)
    preflight.repo_file.symlink_to(moved)

    result = preflight.run(repo_root)

    assert result.returncode != 0
    assert "missing or symlinked" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "setting",
    [
        "[docker-ce-stable]",
        "enabled=0",
        "gpgcheck=1",
        "repo_gpgcheck=1",
    ],
)
def test_preflight_requires_each_repository_signing_setting(
    repo_root: Path, preflight: _Preflight, setting: str
):
    settings = [
        line for line in preflight.repo_file.read_text().splitlines() if line != setting
    ]
    preflight.write_repo(settings)

    result = preflight.run(repo_root)

    assert result.returncode != 0
    assert f"does not contain {setting}" in result.stderr


@pytest.mark.unit
def test_preflight_requires_the_pinned_gpg_key_path(
    repo_root: Path, preflight: _Preflight
):
    settings = [
        line
        for line in preflight.repo_file.read_text().splitlines()
        if not line.startswith("gpgkey=")
    ]
    preflight.write_repo([*settings, "gpgkey=file:///etc/pki/rpm-gpg/other.asc"])

    result = preflight.run(repo_root)

    assert result.returncode != 0
    assert f"does not contain gpgkey=file://{preflight.key_file}" in result.stderr


@pytest.mark.unit
def test_preflight_rejects_a_missing_pinned_rpm_key(
    repo_root: Path, preflight: _Preflight
):
    preflight.key_file.unlink()

    result = preflight.run(repo_root)

    assert result.returncode != 0
    assert "pinned Docker RPM key" in result.stderr


@pytest.mark.unit
def test_preflight_rejects_podman_docker(repo_root: Path, preflight: _Preflight):
    result = preflight.run(repo_root, env={"PODMAN_DOCKER_INSTALLED": "1"})

    assert result.returncode != 0
    assert "podman-docker is installed and can shadow Docker CE" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("override", "value", "diagnostic"),
    [
        ("SYSTEMD_INACTIVE_UNIT", "docker.service", "is not active"),
        ("SYSTEMD_DISABLED_UNIT", "docker.socket", "is not enabled"),
        ("DOCKER_DAEMON_PID", "not-a-pid", "no valid MainPID"),
        ("DOCKER_DAEMON_UID", "1000", "expected rootful uid 0"),
    ],
)
def test_preflight_rejects_an_unhealthy_rootful_daemon(
    repo_root: Path,
    preflight: _Preflight,
    override: str,
    value: str,
    diagnostic: str,
):
    result = preflight.run(repo_root, env={override: value})

    assert result.returncode != 0
    assert diagnostic in result.stderr


@pytest.mark.unit
def test_preflight_rejects_a_failing_docker_api(repo_root: Path, preflight: _Preflight):
    result = preflight.run(repo_root, env={"DOCKER_VERSION_FAIL": "1"})

    assert result.returncode != 0
    assert "request to unix:///var/run/docker.sock failed" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "diagnostic"),
    [
        ("dev|29.9.2|amd64|Docker Engine", "invalid client version (dev)"),
        ("29.9.2|dev|amd64|Docker Engine", "invalid server version (dev)"),
        ("29.9.2|29.9.2|arm64|Podman Engine", "expected Docker Engine"),
    ],
)
def test_preflight_rejects_an_unexpected_docker_endpoint(
    repo_root: Path,
    preflight: _Preflight,
    value: str,
    diagnostic: str,
):
    if "arm64|Podman Engine" in value:
        value = value.replace("arm64", _server_arch())

    result = preflight.run(repo_root, env={"DOCKER_VERSION": value})

    assert result.returncode != 0
    assert diagnostic in result.stderr


@pytest.mark.unit
def test_preflight_rejects_a_missing_required_tool(tmp_path: Path, repo_root: Path):
    preflight = _Preflight(tmp_path, omit_tools=frozenset({"rpm"}))
    # Keep only the utilities the other preflight steps need so the host's own
    # rpm cannot satisfy the PATH check.
    deps = tmp_path / "deps"
    deps.mkdir()
    for name in ("bash", "uname", "grep", "head"):
        executable = shutil.which(name)
        assert executable
        (deps / name).symlink_to(executable)

    result = preflight.run(repo_root, env={"PATH": f"{preflight.fake_bin}:{deps}"})

    assert result.returncode != 0
    assert "rpm is required but was not found on PATH" in result.stderr


@pytest.mark.unit
def test_preflight_targets_the_rootful_socket_not_the_caller_endpoint(
    repo_root: Path, preflight: _Preflight
):
    """The fake docker fails unless the caller endpoint is cleared."""

    result = preflight.run(
        repo_root,
        env={
            "DOCKER_HOST": "unix:///untrusted/docker.sock",
            "DOCKER_CONTEXT": "untrusted-context",
        },
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_preflight_selects_the_explicit_default_context(
    repo_root: Path, preflight: _Preflight, tmp_path: Path
):
    """A user Docker config currentContext cannot redirect the identity probe.

    The preflight must clear caller overrides, pin the rootful system socket,
    and pass ``--context default`` explicitly rather than relying on the CLI's
    configured current context.
    """

    log = tmp_path / "docker.log"
    result = preflight.run(
        repo_root,
        env={
            "DOCKER_HOST": "unix:///untrusted/docker.sock",
            "DOCKER_CONTEXT": "untrusted-context",
            "DOCKER_LOG": str(log),
        },
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "--context default version --format "
        "{{.Client.Version}}|{{.Server.Version}}|{{.Server.Arch}}|"
        "{{.Server.Platform.Name}}|unix:///var/run/docker.sock"
    ]
