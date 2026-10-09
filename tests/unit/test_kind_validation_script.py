import atexit
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
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_alias = Path("/tmp") / f"k{uuid.uuid4().hex[:8]}"
    runtime_alias.symlink_to(runtime_dir, target_is_directory=True)
    atexit.register(runtime_alias.unlink, missing_ok=True)
    log = tmp_path / "kind.log"
    resources = tmp_path / "resources"
    resources.mkdir()

    _write_executable(
        fake_bin / "kind",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s|%s|%s|%s\\n' "$*" "${KIND_EXPERIMENTAL_PROVIDER:-}" \
  "${DOCKER_HOST:-<unset>}" "${KUBECONFIG:-}" >>"$KIND_LOG"
if [[ "$1 $2" == 'create cluster' ]]; then
  cluster=""
  for ((index=1; index<=$#; index++)); do
    argument="${!index}"
    if [[ "$argument" == --name ]]; then
      next=$((index + 1))
      cluster="${!next}"
    fi
  done
  touch "$RESOURCE_DIR/$cluster"
  if [[ "${LEAVE_KIND_RESOURCE:-}" == volume \\
    || "${LEAVE_KIND_RESOURCE:-}" == network ]]; then
    touch "$RESOURCE_DIR/$cluster.${LEAVE_KIND_RESOURCE}"
  fi
  if [[ "${FAIL_CREATE:-}" == 1 ]]; then exit 23; fi
elif [[ "$1 $2" == 'delete cluster' ]]; then
  cluster=""
  for ((index=1; index<=$#; index++)); do
    argument="${!index}"
    if [[ "$argument" == --name ]]; then
      next=$((index + 1))
      cluster="${!next}"
    fi
  done
  if [[ "${FAIL_DELETE:-}" != 1 ]]; then rm -f -- "$RESOURCE_DIR/$cluster"; fi
fi
""",
    )
    _write_executable(
        fake_bin / "kubectl",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s|%s\\n' "$*" "${KUBECONFIG:-}" >>"$KUBECTL_LOG"
case " $* " in
  *' get nodes '* )
    if [[ "${NODE_STATE:-}" == not-ready ]]; then
      printf 'kind-control-plane False\\n'
    else
      printf 'kind-control-plane True\\n'
    fi
    ;;
  *' get pod '* ) printf '%s\\n' "${POD_STATE:-Running true}" ;;
  *' wait '* | *' run '* ) : ;;
  * ) echo "unexpected kubectl invocation: $*" >&2; exit 64 ;;
esac
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
printf '%s|%s\\n' "$*" "${DOCKER_HOST:-<unset>}" >>"$DOCKER_LOG"
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
    cluster=""
    for argument in "$@"; do
      case "$argument" in
        label=io.x-k8s.kind.cluster=*)
          cluster="${argument#*=}"
          cluster="${cluster#io.x-k8s.kind.cluster=}"
          ;;
      esac
    done
    if [[ -n "$cluster" && -f "$RESOURCE_DIR/$cluster" ]]; then
      printf '%s-control-plane\\n' "$cluster"
    elif [[ -z "$cluster" && "${STALE_KIND_RESOURCE:-}" == container ]]; then
      printf 'devbox-kind-docker-stale-control-plane\\n'
    elif [[ -z "$cluster" ]]; then
      for resource in "$RESOURCE_DIR"/*; do
        [[ -f "$resource" && "$resource" != *.volume \\
          && "$resource" != *.network ]] || continue
        printf '%s-control-plane\\n' "${resource##*/}"
      done
      :
    fi
    ;;
  volume)
    if [[ "${STALE_KIND_RESOURCE:-}" == volume ]]; then
      printf 'devbox-kind-docker-stale-volume\\n'
    else
      for resource in "$RESOURCE_DIR"/*.volume; do
        if [[ -f "$resource" ]]; then printf '%s\\n' "${resource##*/}"; fi
      done
      :
    fi
    ;;
  network)
    if [[ "${STALE_KIND_RESOURCE:-}" == network ]]; then
      printf 'devbox-kind-docker-stale-network\\n'
    else
      for resource in "$RESOURCE_DIR"/*.network; do
        if [[ -f "$resource" ]]; then printf '%s\\n' "${resource##*/}"; fi
      done
      :
    fi
    ;;
  *) echo "unexpected docker invocation: $*" >&2; exit 64 ;;
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
if [[ "$1" == info && "${PODMAN_INFO_WARNING:-}" == 1 ]]; then
  echo 'simulated benign Podman warning' >&2
fi
printf '%s\\n' "$*" >>"$PODMAN_LOG"
case "$1" in
  system)
    if [[ "$2 $3" == 'connection list' ]]; then
      if [[ -n "${PODMAN_DEFAULT_CONNECTION:-}" ]]; then
        printf '%s\\n' "$PODMAN_DEFAULT_CONNECTION"
      fi
    else
      echo "unexpected podman system invocation: $*" >&2
      exit 64
    fi
    ;;
  info) printf '%s\\n' "${PODMAN_ROOTLESS:-true}" ;;
  ps)
    if [[ "${PODMAN_PS_WARNING:-}" == 1 ]]; then
      echo 'simulated Podman ps warning' >&2
    fi
    cluster=""
    for argument in "$@"; do
      case "$argument" in
        label=io.x-k8s.kind.cluster=*)
          cluster="${argument#*=}"
          cluster="${cluster#io.x-k8s.kind.cluster=}"
          ;;
      esac
    done
    if [[ -n "$cluster" && -f "$RESOURCE_DIR/$cluster" ]]; then
      printf '%s-control-plane\\n' "$cluster"
    elif [[ -z "$cluster" && "${STALE_KIND_RESOURCE:-}" == container ]]; then
      printf 'devbox-kind-podman-stale-control-plane\\n'
    elif [[ -z "$cluster" ]]; then
      for resource in "$RESOURCE_DIR"/*; do
        [[ -f "$resource" && "$resource" != *.volume \\
          && "$resource" != *.network ]] || continue
        printf '%s-control-plane\\n' "${resource##*/}"
      done
      :
    fi
    ;;
  volume)
    if [[ "${STALE_KIND_RESOURCE:-}" == volume ]]; then
      printf 'devbox-kind-podman-stale-volume\\n'
    else
      for resource in "$RESOURCE_DIR"/*.volume; do
        if [[ -f "$resource" ]]; then printf '%s\\n' "${resource##*/}"; fi
      done
      :
    fi
    ;;
  network)
    if [[ "${STALE_KIND_RESOURCE:-}" == network ]]; then
      printf 'devbox-kind-podman-stale-network\\n'
    else
      for resource in "$RESOURCE_DIR"/*.network; do
        if [[ -f "$resource" ]]; then printf '%s\\n' "${resource##*/}"; fi
      done
      :
    fi
    ;;
  *) echo "unexpected podman invocation: $*" >&2; exit 64 ;;
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
    _write_executable(
        fake_bin / "date",
        "#!/bin/sh\n"
        "if [ \"$1\" = '+%s' ]; then printf '1234567890\\n'; "
        'else exec /usr/bin/date "$@"; fi\n',
    )
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DOCKER_DAEMON_PID": "1",
        "DOCKER_DAEMON_UID": "0",
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_RUNTIME_DIR": str(runtime_alias),
        "KIND_LOG": str(log),
        "KUBECTL_LOG": str(tmp_path / "kubectl.log"),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "PODMAN_LOG": str(tmp_path / "podman.log"),
        "RESOURCE_DIR": str(resources),
    }
    Path(env["TMPDIR"]).mkdir()
    return log, env


def _run(
    repo_root: Path, env: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo_root / "lima/validate-kind.sh"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["docker", "podman"])
def test_kind_validation_explicitly_uses_backend_ready_node_and_workload(
    repo_root: Path, tmp_path: Path, provider: str
):
    log, env = _fake_tools(tmp_path)
    caller_kubeconfig = tmp_path / "caller-kubeconfig"
    caller_kubeconfig.write_text("caller state must remain untouched")
    if provider == "docker":
        env = {
            **env,
            "DOCKER_HOST": "unix:///untrusted/docker.sock",
            "DOCKER_CONTEXT": "untrusted-context",
            "PREFLIGHT_WARNING": "1",
        }
    else:
        env = {
            **env,
            "DOCKER_HOST": "unix:///untrusted/docker.sock",
            "CONTAINER_HOST": "unix:///untrusted/podman.sock",
            "CONTAINER_CONNECTION": "untrusted-connection",
            "PODMAN_HOST": "unix:///untrusted/legacy-podman.sock",
            "PODMAN_INFO_WARNING": "1",
            "PODMAN_PS_WARNING": "1",
        }
    result = _run(
        repo_root, {**env, "KUBECONFIG": str(caller_kubeconfig)}, provider, "2"
    )

    assert result.returncode == 0, result.stderr
    if provider == "podman":
        assert "simulated benign Podman warning" in result.stderr
        assert "simulated Podman ps warning" in result.stderr
    else:
        for warning in ("docker", "rpm", "jq", "systemctl", "stat"):
            assert f"simulated {warning} warning" in result.stderr
        assert "simulated Docker ps warning" in result.stderr
    assert result.stdout.count("backend identity verified: 1 node(s)") == 2
    assert "backend identity verified" in result.stdout
    assert f"via {provider}" in result.stdout
    assert f"kind validation passed: 2 consecutive {provider} clusters" in result.stdout
    calls = log.read_text().splitlines()
    creates = [
        call.split("|", 1)[0] for call in calls if call.startswith("create cluster")
    ]
    deletes = [
        call.split("|", 1)[0] for call in calls if call.startswith("delete cluster")
    ]
    assert len(creates) == len(deletes) == 2
    assert all(f"|{provider}|" in call for call in calls)
    assert all("--wait 5m" in call for call in creates)
    expected_node_image = (
        "kindest/node:v1.37.0@sha256:"
        "a1ed56cfb0e7b93589bdf97c8cd56640"  # pragma: allowlist secret
        "5a265939e3620fc4f5de89adff580ae5"  # pragma: allowlist secret
    )
    assert all(f"--image={expected_node_image}" in call for call in creates)
    assert all("--kubeconfig=" in call for call in creates)
    create_names = [call.split("--name ", 1)[1].split()[0] for call in creates]
    delete_names = [call.split("--name ", 1)[1].split()[0] for call in deletes]
    assert create_names == delete_names
    assert all(not (Path(env["RESOURCE_DIR"]) / name).exists() for name in create_names)

    kubectl_calls = Path(env["KUBECTL_LOG"]).read_text().splitlines()
    assert any("wait" in call and "condition=Ready" in call for call in kubectl_calls)
    assert any("get nodes" in call for call in kubectl_calls)
    assert any("busybox:1.37.0@sha256:" in call for call in kubectl_calls)
    assert any("get pod" in call for call in kubectl_calls)
    kubeconfigs = [call.rsplit("|", 1)[1] for call in kubectl_calls]
    assert all(Path(path) != caller_kubeconfig for path in kubeconfigs)
    assert all("my-sandbox-kind." in path for path in kubeconfigs)
    assert not Path(kubeconfigs[0]).parent.exists()
    assert caller_kubeconfig.read_text() == "caller state must remain untouched"

    if provider == "docker":
        docker_calls = Path(env["DOCKER_LOG"]).read_text().splitlines()
        assert any(call.startswith("version ") for call in docker_calls)
        assert all("unix:///var/run/docker.sock" in call for call in docker_calls)
    else:
        podman_calls = Path(env["PODMAN_LOG"]).read_text().splitlines()
        assert any(call.startswith("info ") for call in podman_calls)
        assert any(call.startswith("ps ") for call in podman_calls)


@pytest.mark.unit
def test_kind_partial_create_failure_deletes_cluster_and_verifies_backend_cleanup(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, {**env, "FAIL_CREATE": "1"}, "podman", "1")

    assert result.returncode == 23
    calls = log.read_text().splitlines()
    create = next(
        call.split("|", 1)[0] for call in calls if call.startswith("create cluster")
    )
    delete = next(
        call.split("|", 1)[0] for call in calls if call.startswith("delete cluster")
    )
    cluster = create.split("--name ", 1)[1].split()[0]
    assert cluster == delete.split("--name ", 1)[1].split()[0]
    assert not (Path(env["RESOURCE_DIR"]) / cluster).exists()
    assert "preserving private state" not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "resource"),
    [
        ("docker", "container"),
        ("docker", "volume"),
        ("docker", "network"),
        ("podman", "container"),
        ("podman", "volume"),
        ("podman", "network"),
    ],
)
def test_kind_refuses_stale_test_owned_backend_resources(
    repo_root: Path,
    tmp_path: Path,
    provider: str,
    resource: str,
):
    log, env = _fake_tools(tmp_path)
    result = _run(
        repo_root,
        {**env, "STALE_KIND_RESOURCE": resource},
        provider,
        "1",
    )

    assert result.returncode != 0
    assert f"stale {resource}s" in result.stderr
    assert f"devbox-kind-{provider}-" in result.stderr
    assert not log.exists(), "stale resources must stop before kind cluster creation"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("variable", "value", "diagnostic"),
    [
        ("NODE_STATE", "not-ready", "is not Ready"),
        ("POD_STATE", "Pending false", "did not reach Running/Ready"),
    ],
)
def test_kind_unready_node_or_workload_still_cleans_backend_resources(
    repo_root: Path,
    tmp_path: Path,
    variable: str,
    value: str,
    diagnostic: str,
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, {**env, variable: value}, "podman", "1")

    assert result.returncode != 0
    assert diagnostic in result.stderr
    create = next(
        call.split("|", 1)[0]
        for call in log.read_text().splitlines()
        if call.startswith("create cluster")
    )
    cluster = create.split("--name ", 1)[1].split()[0]
    assert not (Path(env["RESOURCE_DIR"]) / cluster).exists()
    assert "preserving private state" not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "resource"),
    [
        ("docker", "volume"),
        ("docker", "network"),
        ("podman", "volume"),
        ("podman", "network"),
    ],
)
def test_kind_fails_and_preserves_state_when_owned_noncontainer_remains(
    repo_root: Path,
    tmp_path: Path,
    provider: str,
    resource: str,
):
    log, env = _fake_tools(tmp_path)
    result = _run(
        repo_root,
        {**env, "LEAVE_KIND_RESOURCE": resource},
        provider,
        "1",
    )

    assert result.returncode != 0
    assert f"{resource}:" in result.stderr
    assert "preserving private state" in result.stderr
    create = next(
        call.split("|", 1)[0]
        for call in log.read_text().splitlines()
        if call.startswith("create cluster")
    )
    cluster = create.split("--name ", 1)[1].split()[0]
    assert (Path(env["RESOURCE_DIR"]) / f"{cluster}.{resource}").exists()


@pytest.mark.unit
def test_kind_cleanup_failure_is_reported_and_private_state_is_preserved(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(
        repo_root,
        {**env, "FAIL_CREATE": "1", "FAIL_DELETE": "1"},
        "podman",
        "1",
    )

    assert result.returncode == 23
    assert "resources for cluster" in result.stderr
    assert "preserving private state" in result.stderr
    create_record = next(
        call
        for call in log.read_text().splitlines()
        if call.startswith("create cluster")
    )
    create = create_record.split("|", 1)[0]
    cluster = create.split("--name ", 1)[1].split()[0]
    assert (Path(env["RESOURCE_DIR"]) / cluster).exists()
    kubeconfig = Path(create_record.rsplit("|", 1)[1])
    assert kubeconfig.parent.is_dir()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arguments", "diagnostic"),
    [
        ([], "usage:"),
        (["docker-desktop"], "unsupported provider"),
        (["docker", "0"], "usage:"),
    ],
)
def test_kind_requires_explicit_supported_provider_and_valid_run_count(
    repo_root: Path,
    tmp_path: Path,
    arguments: list[str],
    diagnostic: str,
):
    _, env = _fake_tools(tmp_path)
    result = _run(repo_root, env, *arguments)

    assert result.returncode == 2
    assert diagnostic in result.stderr


@pytest.mark.unit
def test_kind_docker_provider_rejects_non_ce_endpoint_before_cluster_creation(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, {**env, "DOCKER_VERSION": "29.8.2|5.7.0"}, "docker", "1")

    assert result.returncode != 0
    assert "reported client/server 29.8.2|5.7.0" in result.stderr
    assert "rootful Docker" in result.stderr
    assert not log.exists()


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
def test_kind_docker_preflight_rejects_unready_rootful_engine_before_cluster_creation(
    repo_root: Path,
    tmp_path: Path,
    override: str,
    value: str,
    diagnostic: str,
):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, {**env, override: value}, "docker", "1")

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert "rootful Docker" in result.stderr
    assert not log.exists(), (
        "Docker preflight failures must stop before kind cluster creation"
    )


@pytest.mark.unit
def test_kind_podman_provider_rejects_rootful_runtime(repo_root: Path, tmp_path: Path):
    log, env = _fake_tools(tmp_path)
    result = _run(repo_root, {**env, "PODMAN_ROOTLESS": "false"}, "podman", "1")

    assert result.returncode != 0
    assert "requires rootless Podman" in result.stderr
    assert not log.exists()


@pytest.mark.unit
def test_kind_podman_provider_rejects_configured_default_connection(
    repo_root: Path, tmp_path: Path
):
    log, env = _fake_tools(tmp_path)
    result = _run(
        repo_root,
        {**env, "PODMAN_DEFAULT_CONNECTION": "remote-production"},
        "podman",
        "1",
    )

    assert result.returncode != 0
    assert "configured as the default" in result.stderr
    assert "podman system connection list" in result.stderr
    assert not log.exists(), "a default remote must stop before kind cluster creation"
