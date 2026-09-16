import json
import os
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script

KIND_CONFIG = """\
[containers]
cgroups = "enabled"
cgroupns = "host"
default_sysctls = []
log_driver = "k8s-file"
pids_limit = 65536
volumes = ["/proc:/proc"]
utsns = "host"
netns = "bridge"

[engine]
cgroup_manager = "cgroupfs"
"""


def _kind_environment(
    tmp_path: Path, *, controllers: list[str] | None = None
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.log"
    config_path = tmp_path / "kind-containers.conf"
    config_path.write_text(KIND_CONFIG)
    info_path = tmp_path / "podman-info.json"
    info_path.write_text(
        json.dumps(
            {
                "host": {
                    "security": {"rootless": True},
                    "cgroupVersion": "v2",
                    "cgroupControllers": controllers or ["cpu", "memory", "pids"],
                }
            }
        )
    )

    fake_commands = {
        "kind": f"""#!/usr/bin/env bash
printf 'kind CONTAINERS_CONF=%s ARGS=%s\\n' "${{CONTAINERS_CONF-}}" "$*" >> {log_path}
if [ "${{1-}}" = version ]; then
  printf '%s\\n' 'kind v0.33.0'
fi
""",
        "kubectl": f"""#!/usr/bin/env bash
printf 'kubectl ARGS=%s\\n' "$*" >> {log_path}
""",
        "podman": f"""#!/usr/bin/env bash
printf 'podman CONTAINERS_CONF=%s ARGS=%s\\n' "${{CONTAINERS_CONF-}}" "$*" >> {log_path}
if [ "${{DEVBOX_KIND_FAIL_UNSHARE:-}}" = 1 ] && [ "${{1-}}" = unshare ]; then
  printf '%s\\n' 'user namespace unavailable' >&2
  exit 1
fi
if [ "${{DEVBOX_KIND_FAIL_NETWORK_RM:-}}" = 1 ] \
  && [ "${{1-}}" = network ] && [ "${{2-}}" = rm ]; then
  printf '%s\\n' 'network cleanup unavailable' >&2
  exit 1
fi
case "${{1-}} ${{2-}} ${{3-}}" in
  "info --format json")
    cat {info_path}
    ;;
  "network create")
    printf '%s\\n' "${{4-}}"
    ;;
  "network rm")
    printf '%s\\n' "${{4-}}"
    ;;
esac
""",
    }
    for command_name, content in fake_commands.items():
        command_path = bin_dir / command_name
        command_path.write_text(content)
        command_path.chmod(command_path.stat().st_mode | stat.S_IEXEC)

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', os.defpath)}",
        "DEVBOX_KIND_CONTAINERS_CONF": str(config_path),
    }
    return env, log_path


def _run_kind(repo_root: Path, args: list[str], env: dict[str, str]):
    return run_bash_script(repo_root / "container" / "devbox-kind.sh", args, env=env)


@pytest.mark.unit
def test_kind_preflight_checks_capabilities(tmp_path: Path, repo_root: Path):
    env, log_path = _kind_environment(tmp_path)

    result = _run_kind(repo_root, ["preflight"], env)

    assert result.returncode == 0, result.stderr
    assert "kind runtime preflight passed" in result.stdout
    logged = log_path.read_text()
    assert "network create" in logged
    assert "network rm" in logged
    assert "unshare true" in logged
    assert str(tmp_path / "kind-containers.conf") in logged


@pytest.mark.unit
def test_kind_preflight_reports_unsupported_cgroup(tmp_path: Path, repo_root: Path):
    env, _ = _kind_environment(tmp_path, controllers=["cpu", "pids"])

    result = _run_kind(repo_root, ["preflight"], env)

    assert result.returncode == 125
    assert "kind runtime preflight failed" in result.stderr
    assert "memory" in result.stderr
    assert "no cluster was created" in result.stderr


@pytest.mark.unit
def test_kind_create_runs_preflight_then_kind(tmp_path: Path, repo_root: Path):
    env, log_path = _kind_environment(tmp_path)

    result = _run_kind(repo_root, ["create", "cluster", "--name", "demo"], env)

    assert result.returncode == 0, result.stderr
    logged = log_path.read_text()
    assert "kind CONTAINERS_CONF=" in logged
    assert "ARGS=create cluster --name demo" in logged


@pytest.mark.unit
def test_kind_delete_does_not_require_preflight(tmp_path: Path, repo_root: Path):
    env, log_path = _kind_environment(tmp_path, controllers=[])

    result = _run_kind(repo_root, ["delete", "cluster", "--name", "demo"], env)

    assert result.returncode == 0, result.stderr
    logged = log_path.read_text()
    assert "ARGS=delete cluster --name demo" in logged
    assert "podman" not in logged


@pytest.mark.unit
def test_kind_rejects_non_podman_provider(tmp_path: Path, repo_root: Path):
    env, _ = _kind_environment(tmp_path)
    env["KIND_EXPERIMENTAL_PROVIDER"] = "docker"

    result = _run_kind(repo_root, ["preflight"], env)

    assert result.returncode == 2
    assert "unsupported" in result.stderr


@pytest.mark.unit
def test_kind_preflight_configuration_failure_is_not_skippable(
    tmp_path: Path, repo_root: Path
):
    env, _ = _kind_environment(tmp_path)
    env["DEVBOX_KIND_CONTAINERS_CONF"] = str(tmp_path / "missing.conf")

    result = _run_kind(repo_root, ["preflight"], env)

    assert result.returncode == 2
    assert "configuration failure" in result.stderr


@pytest.mark.unit
def test_kind_preflight_reports_namespace_probe_failure(
    tmp_path: Path, repo_root: Path
):
    env, _ = _kind_environment(tmp_path)
    env["DEVBOX_KIND_FAIL_UNSHARE"] = "1"

    result = _run_kind(repo_root, ["preflight"], env)

    assert result.returncode == 125
    assert "user-namespace probe failed" in result.stderr


@pytest.mark.unit
def test_kind_preflight_reports_network_cleanup_failure(
    tmp_path: Path, repo_root: Path
):
    env, _ = _kind_environment(tmp_path)
    env["DEVBOX_KIND_FAIL_NETWORK_RM"] = "1"

    result = _run_kind(repo_root, ["preflight"], env)

    assert result.returncode == 125
    assert "could not clean up" in result.stderr
