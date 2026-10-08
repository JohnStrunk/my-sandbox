import subprocess
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
    files = {
        "TOOL_LOG": str(log),
        "PROFILE_FILE": str(profile_file),
        "TMPDIR": str(tmp_path / "tmp"),
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
  printf '%s\\n' "$MINIKUBE_HOME" >"$TOOL_LOG.minikube-home"
  printf '%s\\n' "$KUBECONFIG" >"$TOOL_LOG.kubeconfig"
  stat -c '%a' "$(dirname "$MINIKUBE_HOME")" >"$TOOL_LOG.state-mode"
  if [[ "${FAIL_START:-}" == 1 ]]; then exit 23; fi
fi
if [[ "$1" == delete && "${FAIL_DELETE:-}" == 1 ]]; then
  echo 'simulated profile deletion failure' >&2
  exit 1
fi
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
if [[ "$1" == info ]]; then
  printf '%s\n' "${PODMAN_ROOTLESS:-true}"
elif [[ "$1" == ps ]]; then
  if [[ "${STALE_RESOURCE:-}" == container ]]; then
    printf 'devbox-minikube-podman-stale fake-label\\n'
  elif [[ "${LEAVE_RESOURCE:-}" == podman && -f "$PROFILE_FILE" ]]; then
    printf '%s-node fake-label\n' "$(cat "$PROFILE_FILE")"
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
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": files["TMPDIR"],
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
        (["docker"], True),
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
    result = _run(repo_root, "podman", env)

    assert result.returncode == 0, result.stderr
    assert "minikube validation passed: podman" in result.stdout
    calls = log.read_text().splitlines()
    start = next(call for call in calls if call.startswith("minikube start "))
    assert "--driver=podman" in start
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
def test_kvm2_backend_names_explicit_driver_and_resource_checks(repo_root: Path):
    script = (repo_root / "lima/validate-minikube.sh").read_text()

    assert "driver=kvm2" in script
    assert "minikube start \\" in script
    assert '--driver="$driver"' in script
    assert "LIBVIRT_DEFAULT_URI" in script
    assert "qemu-system-$(uname -m)" in script
    assert "type=['\\\"]kvm" in script
    assert "resource_args=(list --all --name)" in script
    assert "resource_args=(net-list --all --name)" in script
    assert 'virsh -c "$LIBVIRT_DEFAULT_URI" "${resource_args[@]}"' in script
    assert 'check_no_stale_resources "libvirt virtual machines"' in script
    assert 'check_no_stale_resources "libvirt networks"' in script
    assert '[[ "$output" == *"$profile"* ]]' in script
    assert "--driver=docker" not in script
    assert "minikube delete --all" not in script
