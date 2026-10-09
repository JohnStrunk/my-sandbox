import atexit
import json
import re
import subprocess
import uuid
from pathlib import Path

import pytest


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def _fake_tools(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "tools.log"
    profile_file = tmp_path / "profile"
    active_file = tmp_path / "active"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_alias = Path("/tmp") / f"m{uuid.uuid4().hex[:8]}"
    runtime_alias.symlink_to(runtime_dir, target_is_directory=True)
    atexit.register(runtime_alias.unlink, missing_ok=True)
    files = {
        "TOOL_LOG": str(log),
        "PROFILE_FILE": str(profile_file),
        "ACTIVE_FILE": str(active_file),
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_RUNTIME_DIR": str(runtime_alias),
    }
    Path(files["TMPDIR"]).mkdir()

    _write_executable(
        fake_bin / "minikube",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'minikube %s\\n' "$*" >>"$TOOL_LOG"
if [[ "$1" == start ]]; then
  profile=""
  for argument in "$@"; do
    case "$argument" in --profile=*) profile="${argument#--profile=}" ;; esac
  done
  printf '%s\\n' "$profile" >"$PROFILE_FILE"
  touch "$ACTIVE_FILE"
  printf '%s\\n' "$MINIKUBE_HOME" >"$TOOL_LOG.minikube-home"
  printf '%s\\n' "$KUBECONFIG" >"$TOOL_LOG.kubeconfig"
  stat -c '%a' "$(dirname "$MINIKUBE_HOME")" >"$TOOL_LOG.state-mode"
  if [[ "${FAIL_START:-}" == 1 ]]; then exit 23; fi
fi
if [[ "$1" == delete && "${FAIL_DELETE:-}" == 1 ]]; then
  echo 'simulated profile deletion failure' >&2
  exit 1
fi
if [[ "$1" == delete ]]; then rm -f "$ACTIVE_FILE"; fi
""",
    )
    _write_executable(
        fake_bin / "kubectl",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'kubectl %s\\n' "$*" >>"$TOOL_LOG"
case " $* " in
  *' get nodes '* )
    if [[ "${NODE_STATE:-}" == not-ready ]]; then
      printf 'minikube False\\n'
    else
      printf 'minikube True\\n'
    fi
    ;;
  *' get pod '* )
    printf '%s\\n' "${POD_STATE:-Running true}"
    ;;
  *' wait '* ) : ;;
  *' run '* ) : ;;
  * ) echo "unexpected kubectl invocation: $*" >&2; exit 64 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "podman",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${DOCKER_HOST:-}${CONTAINER_HOST:-}" \
  || -n "${CONTAINER_CONNECTION:-}${PODMAN_HOST:-}" ]]; then
  echo 'unexpected remote Podman endpoint override' >&2
  exit 65
fi
if [[ "$1" == system && "${2:-}" == connection && "${3:-}" == list ]]; then
  if [[ -n "${PODMAN_DEFAULT_CONNECTION:-}" ]]; then
    printf '%s\\n' "$PODMAN_DEFAULT_CONNECTION"
  fi
elif [[ "$1" == info ]]; then
  if [[ "${PODMAN_INFO_WARNING:-}" == 1 ]]; then
    echo 'simulated benign Podman warning' >&2
  fi
  printf '%s\n' "${PODMAN_ROOTLESS:-true}"
elif [[ "$1" == ps ]]; then
  if [[ "${PODMAN_PS_WARNING:-}" == 1 ]]; then
    echo 'simulated Podman ps warning' >&2
  fi
  if [[ "${STALE_RESOURCE:-}" == container ]]; then
    printf 'devbox-minikube-podman-stale fake-label\\n'
  elif [[ "${LEAVE_RESOURCE:-}" == podman || -f "$ACTIVE_FILE" ]] \
    && [[ -f "$PROFILE_FILE" ]]; then
    printf '%s-node fake-label\n' "$(cat "$PROFILE_FILE")"
    printf 'unrelated-tenant private-label\n'
  fi
elif [[ "$1" == volume ]]; then
  if [[ "${STALE_RESOURCE:-}" == volume ]]; then
    printf 'devbox-minikube-podman-stale\\n'
  elif [[ "${LEAVE_RESOURCE:-}" == volume && -f "$PROFILE_FILE" ]]; then
    cat "$PROFILE_FILE"
  fi
elif [[ "$1" == network ]]; then
  if [[ "${STALE_RESOURCE:-}" == network ]]; then
    printf 'devbox-minikube-podman-stale\\n'
  elif [[ "${LEAVE_RESOURCE:-}" == network && -f "$PROFILE_FILE" ]]; then
    cat "$PROFILE_FILE"
  fi
else
  echo "unexpected podman invocation: $*" >&2
  exit 64
fi
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${DOCKER_CONTEXT:-}" ]]; then
  echo 'unexpected Docker context override' >&2
  exit 65
fi
if [[ "${DOCKER_HOST:-}" != unix:///var/run/docker.sock ]]; then
  echo 'Docker did not target the rootful system socket' >&2
  exit 65
fi
printf 'docker %s|%s\\n' "$*" "${DOCKER_HOST:-<unset>}" >>"$TOOL_LOG"
case "$1" in
  version)
    if [[ "${PREFLIGHT_WARNING:-}" == 1 ]]; then
      echo 'simulated docker warning' >&2
    fi
    printf '%s\\n' "${DOCKER_VERSION:-29.8.2|29.8.2}"
    ;;
  ps)
    if [[ "${PREFLIGHT_WARNING:-}" == 1 ]]; then
      echo 'simulated Docker ps warning' >&2
    fi
    if [[ "${STALE_RESOURCE:-}" == container ]]; then
      printf 'devbox-minikube-docker-stale fake-label\\n'
    elif [[ "${LEAVE_RESOURCE:-}" == container || -f "$ACTIVE_FILE" ]] \
      && [[ -f "$PROFILE_FILE" ]]; then
      printf '%s-node fake-label\\n' "$(cat "$PROFILE_FILE")"
      printf 'unrelated-tenant private-label\\n'
    fi
    ;;
  volume)
    if [[ "${STALE_RESOURCE:-}" == volume ]]; then
      printf 'devbox-minikube-docker-stale\\n'
    elif [[ "${LEAVE_RESOURCE:-}" == volume && -f "$PROFILE_FILE" ]]; then
      cat "$PROFILE_FILE"
    fi
    ;;
  network)
    if [[ "${STALE_RESOURCE:-}" == network ]]; then
      printf 'devbox-minikube-docker-stale\\n'
    elif [[ "${LEAVE_RESOURCE:-}" == network && -f "$PROFILE_FILE" ]]; then
      cat "$PROFILE_FILE"
    fi
    ;;
  *) echo "unexpected docker invocation: $*" >&2; exit 64 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "rpm",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${PREFLIGHT_WARNING:-}" == 1 ]]; then
  echo 'simulated rpm warning' >&2
fi
case "$*" in
  *'/docker') printf 'docker-ce-cli\\n' ;;
  *'/dockerd') printf 'docker-ce\\n' ;;
  *'/containerd') printf 'containerd.io\\n' ;;
  *' podman-docker') exit 1 ;;
  *' docker-ce') printf '%s\\n' "${DOCKER_PACKAGE_VERSION:-29.8.2}" ;;
  *' docker-ce-cli') printf '%s\\n' "${DOCKER_PACKAGE_VERSION:-29.8.2}" ;;
  *' containerd.io') printf '%s\\n' "${CONTAINERD_PACKAGE_VERSION:-2.3.6}" ;;
  *) echo "unexpected rpm invocation: $*" >&2; exit 64 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "jq",
        "#!/bin/sh\n"
        'if [ "${PREFLIGHT_WARNING:-}" = 1 ]; then '
        "echo 'simulated jq warning' >&2; fi\n"
        'case "$*" in *containerd_io*) printf "2.3.6\\n" ;; '
        '*) printf "29.8.2\\n" ;; esac\n',
    )
    _write_executable(
        fake_bin / "stat",
        "#!/usr/bin/env bash\n"
        'if [[ "${PREFLIGHT_WARNING:-}" == 1 ]]; then '
        "echo 'simulated stat warning' >&2; fi\n"
        'if [[ "$*" == *"/proc/${DOCKER_DAEMON_PID}"* ]]; then '
        'printf "%s\\n" "${DOCKER_DAEMON_UID:-0}"; exit 0; fi\n'
        'exec /usr/bin/stat "$@"\n',
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
if [[ "${PREFLIGHT_WARNING:-}" == 1 ]]; then
  echo 'simulated systemctl warning' >&2
fi
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
  show) printf '%s\\n' "$DOCKER_DAEMON_PID" ;;
  *) exit 64 ;;
esac
""",
    )
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DOCKER_DAEMON_PID": "1",
        "DOCKER_DAEMON_UID": "0",
        "TMPDIR": files["TMPDIR"],
        "XDG_RUNTIME_DIR": files["XDG_RUNTIME_DIR"],
        **files,
    }
    return log, env


def _run(
    repo_root: Path,
    mode: str,
    env: dict[str, str],
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo_root / "lima/validate-minikube.sh"), mode, *extra_args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arguments", "unsupported"),
    [
        ([], False),
        (["podman", "kvm2"], False),
        (["--driver=podman"], True),
    ],
)
def test_minikube_validation_requires_exactly_one_supported_mode(
    repo_root: Path, arguments: list[str], unsupported: bool
):
    result = subprocess.run(
        ["bash", str(repo_root / "lima/validate-minikube.sh"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr
    if unsupported:
        assert "unsupported backend" in result.stderr


@pytest.mark.unit
def test_minikube_validation_reports_missing_minikube_or_kubectl(
    repo_root: Path, tmp_path: Path
):
    no_tools = tmp_path / "no-tools"
    no_tools.mkdir()
    no_minikube = subprocess.run(
        ["/bin/bash", str(repo_root / "lima/validate-minikube.sh"), "podman"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": str(no_tools)},
        timeout=10,
    )
    assert no_minikube.returncode != 0
    assert "minikube is required" in no_minikube.stderr

    fake_bin = tmp_path / "kubectl-only"
    fake_bin.mkdir()
    _write_executable(fake_bin / "minikube", "#!/bin/sh\nexit 0\n")
    no_kubectl = subprocess.run(
        ["/bin/bash", str(repo_root / "lima/validate-minikube.sh"), "podman"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": str(fake_bin)},
        timeout=10,
    )
    assert no_kubectl.returncode != 0
    assert "kubectl is required" in no_kubectl.stderr


@pytest.mark.unit
def test_podman_preflight_rejects_unavailable_or_rootful_podman(
    repo_root: Path, tmp_path: Path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "minikube", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "kubectl", "#!/bin/sh\nexit 0\n")
    unavailable = subprocess.run(
        ["/bin/bash", str(repo_root / "lima/validate-minikube.sh"), "podman"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": str(fake_bin)},
        timeout=10,
    )
    assert unavailable.returncode != 0
    assert "Podman mode requires the podman command" in unavailable.stderr

    rootful_tmp = tmp_path / "rootful"
    rootful_tmp.mkdir()
    _, env = _fake_tools(rootful_tmp)
    rootful = _run(repo_root, "podman", {**env, "PODMAN_ROOTLESS": "false"})
    assert rootful.returncode != 0
    assert "requires rootless Podman" in rootful.stderr


@pytest.mark.unit
def test_podman_preflight_rejects_configured_default_connection(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(
        repo_root,
        "podman",
        {**env, "PODMAN_DEFAULT_CONNECTION": "remote-production"},
    )

    assert result.returncode != 0
    assert "configured as the default" in result.stderr
    assert "podman system connection list" in result.stderr
    assert not log.exists(), "a default remote must stop before Minikube starts"


@pytest.mark.unit
def test_kvm2_preflight_reports_unsupported_architecture(
    repo_root: Path, tmp_path: Path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "minikube", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "kubectl", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "uname", "#!/bin/sh\nprintf 'aarch64\\n'\n")

    result = subprocess.run(
        ["/bin/bash", str(repo_root / "lima/validate-minikube.sh"), "kvm2"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        timeout=10,
    )

    assert result.returncode != 0
    assert "supported only on x86_64 Linux" in result.stderr


@pytest.mark.unit
def test_podman_backend_is_explicit_isolated_and_checks_cluster_and_workload(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(
        repo_root,
        "podman",
        {
            **env,
            "DOCKER_HOST": "unix:///untrusted/docker.sock",
            "CONTAINER_HOST": "unix:///untrusted/podman.sock",
            "CONTAINER_CONNECTION": "untrusted-connection",
            "PODMAN_HOST": "unix:///untrusted/legacy-podman.sock",
            "PODMAN_INFO_WARNING": "1",
            "PODMAN_PS_WARNING": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "simulated benign Podman warning" in result.stderr
    assert "simulated Podman ps warning" in result.stderr
    assert "minikube validation passed: podman" in result.stdout
    calls = log.read_text().splitlines()
    start = next(call for call in calls if call.startswith("minikube start "))
    assert "--driver=podman" in start
    assert "--kubernetes-version=v1.37.0" in start
    assert "--container-runtime=containerd" in start
    assert "--cpus=2" in start
    assert "--memory=4096" in start
    assert "--wait=all" in start
    assert "--wait-timeout=10m" in start
    assert "minikube config set rootless true" in calls
    assert "--driver=docker" not in start
    assert "--driver=kvm2" not in start

    profile = Path(env["PROFILE_FILE"]).read_text().strip()
    minikube_home = Path(f"{log}.minikube-home").read_text().strip()
    kubeconfig = Path(f"{log}.kubeconfig").read_text().strip()
    state_dir = Path(minikube_home).parent
    assert state_dir.parent == Path(env["TMPDIR"])
    assert Path(f"{log}.state-mode").read_text().strip() == "700"
    assert Path(minikube_home).name == "minikube"
    assert Path(kubeconfig) == state_dir / "kubeconfig"
    assert profile.startswith("devbox-minikube-podman-")
    assert profile == profile.lower()
    assert not state_dir.exists(), "successful cleanup should remove private state"

    kubectl_calls = [call for call in calls if call.startswith("kubectl ")]
    assert any("get nodes" in call for call in kubectl_calls)
    assert any(
        "run " in call and "busybox:1.37.0@sha256:" in call for call in kubectl_calls
    )
    assert any("wait " in call and "condition=Ready" in call for call in kubectl_calls)
    assert any("get pod " in call for call in kubectl_calls)
    assert all(f"--kubeconfig={kubeconfig}" in call for call in kubectl_calls)
    assert all(f"--context={profile}" in call for call in kubectl_calls)

    second_run = _run(repo_root, "podman", env)
    assert second_run.returncode == 0, second_run.stderr
    second_profile = Path(env["PROFILE_FILE"]).read_text().strip()
    assert second_profile != profile, "each run must use a unique Minikube profile"


@pytest.mark.unit
def test_docker_backend_is_pinned_docker_ce_and_checks_cluster_and_workload(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    original_kubeconfig = tmp_path / "caller-kubeconfig"
    original_kubeconfig.write_text("caller state must remain untouched")
    result = _run(
        repo_root,
        "docker",
        {
            **env,
            "DOCKER_HOST": "unix:///untrusted/docker.sock",
            "DOCKER_CONTEXT": "untrusted-context",
            "PREFLIGHT_WARNING": "1",
            "KUBECONFIG": str(original_kubeconfig),
        },
    )

    assert result.returncode == 0, result.stderr
    for warning in ("docker", "rpm", "jq", "systemctl", "stat"):
        assert f"simulated {warning} warning" in result.stderr
    assert "simulated Docker ps warning" in result.stderr
    assert "backend identity verified: Docker CE container" in result.stdout
    assert "minikube validation passed: docker" in result.stdout
    calls = log.read_text().splitlines()
    start = next(call for call in calls if call.startswith("minikube start "))
    assert "--driver=docker" in start
    assert "--kubernetes-version=v1.37.0" in start
    assert "--container-runtime=containerd" in start
    assert "minikube config set rootless true" not in calls
    assert any(
        call.startswith(
            "docker version --format {{.Client.Version}}|{{.Server.Version}}|"
        )
        for call in calls
    )
    assert any(call.startswith("docker ps --all") for call in calls)
    assert all(
        call.endswith("|unix:///var/run/docker.sock")
        for call in calls
        if call.startswith("docker ")
    )
    assert original_kubeconfig.read_text() == "caller state must remain untouched"

    profile = Path(env["PROFILE_FILE"]).read_text().strip()
    minikube_home = Path(f"{log}.minikube-home").read_text().strip()
    kubeconfig = Path(f"{log}.kubeconfig").read_text().strip()
    state_dir = Path(minikube_home).parent
    assert state_dir.parent == Path(env["TMPDIR"])
    assert Path(kubeconfig) == state_dir / "kubeconfig"
    assert profile.startswith("devbox-minikube-docker-")
    assert not state_dir.exists()

    kubectl_calls = [call for call in calls if call.startswith("kubectl ")]
    assert any("get nodes" in call for call in kubectl_calls)
    assert any("busybox:1.37.0@sha256:" in call for call in kubectl_calls)
    assert any("condition=Ready" in call for call in kubectl_calls)
    assert all(f"--kubeconfig={kubeconfig}" in call for call in kubectl_calls)
    assert all(f"--context={profile}" in call for call in kubectl_calls)


@pytest.mark.unit
def test_docker_mode_rejects_wrong_server_identity_before_start(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, "docker", {**env, "DOCKER_VERSION": "29.8.2|5.7.0"})

    assert result.returncode != 0
    assert "reported client/server 29.8.2|5.7.0" in result.stderr
    assert "rootful Docker" in result.stderr
    assert not any(
        call.startswith("minikube start ") for call in log.read_text().splitlines()
    ), "a non-Docker-CE endpoint must stop before Minikube starts"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("override", "value", "diagnostic"),
    [
        ("SYSTEMD_INACTIVE_UNIT", "docker.service", "docker.service"),
        ("SYSTEMD_INACTIVE_UNIT", "docker.socket", "docker.socket"),
        ("SYSTEMD_INACTIVE_UNIT", "containerd.service", "containerd.service"),
        ("SYSTEMD_DISABLED_UNIT", "docker.service", "docker.service"),
        ("SYSTEMD_DISABLED_UNIT", "docker.socket", "docker.socket"),
        ("SYSTEMD_DISABLED_UNIT", "containerd.service", "containerd.service"),
        ("DOCKER_PACKAGE_VERSION", "9.9.9", "manifest pins 29.8.2"),
        ("CONTAINERD_PACKAGE_VERSION", "9.9.9", "manifest pins 2.3.6"),
        ("DOCKER_DAEMON_UID", "1000", "1000"),
    ],
)
def test_docker_preflight_rejects_unready_rootful_engine_before_minikube_start(
    repo_root: Path,
    tmp_path: Path,
    override: str,
    value: str,
    diagnostic: str,
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, "docker", {**env, override: value})

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert "rootful Docker" in result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert not any(call.startswith("minikube start ") for call in calls), (
        "Docker preflight failures must stop before Minikube starts"
    )


@pytest.mark.unit
def test_docker_mode_fails_if_profile_resources_remain_after_delete(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, "docker", {**env, "LEAVE_RESOURCE": "container"})

    assert result.returncode != 0
    assert "Docker resources matching profile" in result.stderr
    assert "unrelated-tenant" not in result.stderr
    assert "preserving private state" in result.stderr
    minikube_home = Path(f"{env['TOOL_LOG']}.minikube-home").read_text().strip()
    assert Path(minikube_home).parent.is_dir()
    assert any(
        call.startswith("minikube delete ") for call in log.read_text().splitlines()
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("resource", "diagnostic"),
    [
        ("container", "stale Docker containers"),
        ("volume", "stale Docker volumes"),
        ("network", "stale Docker networks"),
    ],
)
def test_docker_mode_rejects_stale_resources(
    repo_root: Path,
    tmp_path: Path,
    resource: str,
    diagnostic: str,
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, "docker", {**env, "STALE_RESOURCE": resource})

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert "reserved prefix" in result.stderr
    assert not any(
        call.startswith("minikube start ") for call in log.read_text().splitlines()
    ), "stale resources must stop before cluster creation"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("variable", "value", "diagnostic"),
    [
        ("NODE_STATE", "not-ready", "is not Ready"),
        ("POD_STATE", "Pending false", "did not reach Running/Ready"),
        ("POD_STATE", "Running false", "did not reach Running/Ready"),
    ],
)
def test_minikube_validation_rejects_unready_nodes_or_workloads(
    repo_root: Path,
    tmp_path: Path,
    variable: str,
    value: str,
    diagnostic: str,
):
    _, env = _fake_tools(tmp_path)
    result = _run(repo_root, "podman", {**env, variable: value})

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert "preserving private state" not in result.stderr


@pytest.mark.unit
def test_minikube_validation_preserves_failure_and_state_when_cleanup_fails(
    repo_root: Path, tmp_path: Path
):
    _, env = _fake_tools(tmp_path)
    result = _run(
        repo_root,
        "podman",
        {**env, "FAIL_START": "1", "FAIL_DELETE": "1"},
    )

    assert result.returncode == 23, result.stderr
    assert "simulated profile deletion failure" in result.stderr
    assert "preserving private state" in result.stderr
    minikube_home = Path(f"{env['TOOL_LOG']}.minikube-home").read_text().strip()
    assert Path(minikube_home).parent.is_dir()


@pytest.mark.unit
def test_minikube_validation_fails_and_retains_state_for_leftover_podman_resources(
    repo_root: Path, tmp_path: Path
):
    _, env = _fake_tools(tmp_path)
    result = _run(repo_root, "podman", {**env, "LEAVE_RESOURCE": "podman"})

    assert result.returncode != 0
    assert "Podman resources matching profile" in result.stderr
    assert "unrelated-tenant" not in result.stderr
    assert "preserving private state" in result.stderr
    minikube_home = Path(f"{env['TOOL_LOG']}.minikube-home").read_text().strip()
    assert Path(minikube_home).parent.is_dir()


@pytest.mark.unit
def test_minikube_validation_fails_and_retains_state_for_leftover_podman_volumes(
    repo_root: Path, tmp_path: Path
):
    _, env = _fake_tools(tmp_path)
    result = _run(repo_root, "podman", {**env, "LEAVE_RESOURCE": "volume"})

    assert result.returncode != 0
    assert "Podman volumes matching profile" in result.stderr
    assert "preserving private state" in result.stderr


@pytest.mark.unit
def test_minikube_validation_fails_and_retains_state_for_leftover_podman_networks(
    repo_root: Path, tmp_path: Path
):
    _, env = _fake_tools(tmp_path)
    result = _run(repo_root, "podman", {**env, "LEAVE_RESOURCE": "network"})

    assert result.returncode != 0
    assert "Podman networks matching profile" in result.stderr
    assert "preserving private state" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("resource", "diagnostic"),
    [
        ("container", "stale Podman containers"),
        ("volume", "stale Podman volumes"),
        ("network", "stale Podman networks"),
    ],
)
def test_minikube_validation_rejects_stale_podman_resources(
    repo_root: Path,
    tmp_path: Path,
    resource: str,
    diagnostic: str,
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, "podman", {**env, "STALE_RESOURCE": resource})

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert "reserved prefix" in result.stderr
    assert not log.exists(), "stale resources must stop the run before cluster creation"


@pytest.mark.unit
def test_kind_and_minikube_server_versions_match_pinned_kubectl_minor(
    repo_root: Path,
):
    versions = json.loads((repo_root / "lima/tool-versions.json").read_text())
    kubectl_version = versions["tools"]["kubectl"]["version"]
    kubectl_minor = ".".join(kubectl_version.split(".")[:2])

    kind_script = (repo_root / "lima/validate-kind.sh").read_text()
    minikube_script = (repo_root / "lima/validate-minikube.sh").read_text()
    kind_image = re.search(r"kindest/node:v(\d+\.\d+)\.\d+@sha256:", kind_script)
    minikube_version = re.search(
        r"kubernetes_version='v(\d+\.\d+)\.\d+'", minikube_script
    )

    assert kind_image is not None, "kind must pin its Kubernetes node image"
    assert minikube_version is not None, "Minikube must pin its Kubernetes version"
    assert kind_image.group(1) == kubectl_minor
    assert minikube_version.group(1) == kubectl_minor


@pytest.mark.unit
def test_kvm2_backend_names_explicit_driver_and_resource_checks(repo_root: Path):
    script = (repo_root / "lima/validate-minikube.sh").read_text()

    assert "driver=kvm2" in script
    assert "minikube start \\" in script
    assert '--driver="$driver"' in script
    assert "LIBVIRT_DEFAULT_URI" in script
    assert "qemu-system-$(uname -m)" in script
    assert 'state_tmp_dir="${TMPDIR:-/var/tmp}"' in script
    assert "tmpfs | ramfs | devtmpfs | hugetlbfs)" in script
    assert "type=['\\\"]kvm" in script
    assert "resource_args=(list --all --name)" in script
    assert "resource_args=(net-list --all --name)" in script
    assert 'virsh -c "$LIBVIRT_DEFAULT_URI" "${resource_args[@]}"' in script
    assert 'check_no_stale_resources "libvirt virtual machines"' in script
    assert 'check_no_stale_resources "libvirt networks"' in script
    assert 'virsh -c "$LIBVIRT_DEFAULT_URI" dumpxml "$profile"' in script
    assert 'virsh -c "$LIBVIRT_DEFAULT_URI" domstate "$profile"' in script
    assert 'matching="$(grep -F "$profile" <<<"$output" || true)"' in script
    assert '[[ -n "$matching" ]]' in script
    assert "verify_backend_instance" in script
    assert "kubernetes_version='v1.37.0'" in script
    assert "KVM2 libvirt L2 VM" in script
    assert "profile $profile is absent from libvirt" in script
    assert "--driver=docker" not in script
    assert "minikube delete --all" not in script


@pytest.mark.unit
def test_kvm2_preflight_refuses_tmpfs_before_creating_state(
    repo_root: Path, tmp_path: Path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "findmnt", "#!/bin/sh\nprintf 'tmpfs\\n'\n")

    result = subprocess.run(
        ["/bin/bash", str(repo_root / "lima/validate-minikube.sh"), "kvm2"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": str(fake_bin), "TMPDIR": str(tmp_path)},
        timeout=10,
    )

    assert result.returncode != 0
    assert "disk-backed temporary storage" in result.stderr
    assert not list(tmp_path.glob("my-sandbox-minikube.*"))
