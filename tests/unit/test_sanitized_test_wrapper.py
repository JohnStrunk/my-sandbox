"""Verify the command-line sanitized test wrapper (issue #150)."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


def _run_wrapper(
    repo_root: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo_root / "scripts" / "sanitized-test.sh"), *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _make_fake_podman(
    bin_dir: Path,
    log_path: Path,
    *,
    status: int = 0,
    message: str = "",
    run_status: int = 0,
    run_message: str = "",
) -> None:
    log = shlex.quote(str(log_path))
    fake_podman = bin_dir / "podman"
    fake_podman.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'HOME=%s\\n' "${{HOME-}}" > {log}
printf 'XDG_CONFIG_HOME=%s\\n' "${{XDG_CONFIG_HOME-}}" >> {log}
printf 'XDG_DATA_HOME=%s\\n' "${{XDG_DATA_HOME-}}" >> {log}
printf 'XDG_RUNTIME_DIR=%s\\n' "${{XDG_RUNTIME_DIR-}}" >> {log}
printf 'REGISTRY_AUTH_FILE=%s\\n' "${{REGISTRY_AUTH_FILE-}}" >> {log}
printf 'GH_TOKEN=%s\\n' "${{GH_TOKEN-<unset>}}" >> {log}
printf 'CONTAINERS_CONF=%s\\n' "${{CONTAINERS_CONF-<unset>}}" >> {log}
printf 'DOCKER_CONFIG=%s\\n' "${{DOCKER_CONFIG-<unset>}}" >> {log}
printf 'DOCKER_AUTH_CONFIG=%s\\n' "${{DOCKER_AUTH_CONFIG-<unset>}}" >> {log}
printf 'ARGS=%s\\n' "$*" >> {log}
if [[ -f "${{XDG_CONFIG_HOME-}}/containers/containers.conf" ]]; then
  printf 'CONFIG_COPY=present\\n' >> {log}
else
  printf 'CONFIG_COPY=absent\\n' >> {log}
fi
if [[ -f "${{XDG_CONFIG_HOME-}}/containers/auth.json" ]]; then
  printf 'AUTH_COPY=present\\n' >> {log}
else
  printf 'AUTH_COPY=absent\\n' >> {log}
fi
while [[ "${{1-}}" == --root || "${{1-}}" == --runroot ]]; do
  shift 2
done
if [[ "${{1-}}" == run && {run_status} -ne 0 ]]; then
  printf '%s\\n' {shlex.quote(run_message)} >&2
  exit {run_status}
fi
if [[ "${{1-}}" == info ]]; then
  if [[ {status} -ne 0 ]]; then
    printf '%s\\n' {shlex.quote(message)} >&2
    exit {status}
  fi
  printf 'true\\n'
fi
"""
    )
    fake_podman.chmod(0o700)


def _podman_environment(
    tmp_path: Path, fake_bin: Path, host_home: Path
) -> dict[str, str]:
    host_config = tmp_path / "host-config"
    host_data = tmp_path / "host-data"
    host_runtime = tmp_path / "host-runtime"
    host_config.mkdir()
    host_data.mkdir()
    host_runtime.mkdir()
    host_containers = host_config / "containers"
    host_containers.mkdir()
    (host_containers / "containers.conf").write_text(
        "[containers]\ndefault_sysctls = []\n"
    )
    (host_containers / "auth.json").write_text('{"auth":"fixture-auth-sentinel"}\n')
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "HOME": str(host_home),
            "XDG_CONFIG_HOME": str(host_config),
            "XDG_DATA_HOME": str(host_data),
            "XDG_RUNTIME_DIR": str(host_runtime),
            "GH_TOKEN": "host-secret-token",  # pragma: allowlist secret
            "TAVILY_API_KEY": "host-tavily-token",  # pragma: allowlist secret
            "CONTAINERS_CONF": str(tmp_path / "host-secret.conf"),
            "DOCKER_AUTH_CONFIG": '{"auths":{"registry.example":"secret"}}',
        }
    )
    return env


@pytest.mark.unit
def test_wrapper_scrubs_host_environment(repo_root: Path, tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "host-home"),
            "GH_TOKEN": "host-secret-token",  # pragma: allowlist secret
            "AWS_CONFIG_FILE": str(tmp_path / "credentials"),
            "UNSAFE_TEST_VARIABLE": "must-not-cross-boundary",
        }
    )
    result = _run_wrapper(
        repo_root,
        [
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert child_env["HOME"] != env["HOME"]
    assert child_env["XDG_CONFIG_HOME"] != env.get("XDG_CONFIG_HOME")
    assert "GH_TOKEN" not in child_env
    assert "TAVILY_API_KEY" not in child_env
    assert "AWS_CONFIG_FILE" not in child_env
    assert "UNSAFE_TEST_VARIABLE" not in child_env


@pytest.mark.unit
def test_wrapper_restores_only_podman_runtime_allowlist(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(fake_bin, log_path)
    env = _podman_environment(tmp_path, fake_bin, host_home)

    result = _run_wrapper(repo_root, ["--require-podman", "--", "podman", "info"], env)

    assert result.returncode == 0, result.stderr
    logged = log_path.read_text()
    assert f"HOME={host_home}" not in logged
    assert f"XDG_CONFIG_HOME={tmp_path / 'host-config'}" not in logged
    assert f"XDG_DATA_HOME={tmp_path / 'host-data'}" not in logged
    assert f"XDG_RUNTIME_DIR={tmp_path / 'host-runtime'}" not in logged
    assert "GH_TOKEN=<unset>" in logged
    assert "CONTAINERS_CONF=<unset>" in logged
    assert "DOCKER_CONFIG=" in logged
    assert "DOCKER_AUTH_CONFIG=<unset>" in logged
    assert "CONFIG_COPY=present" in logged
    assert "AUTH_COPY=absent" in logged
    assert f"DOCKER_CONFIG={host_home}" not in logged
    assert "REGISTRY_AUTH_FILE=" in logged
    assert str(host_home) not in logged.split("REGISTRY_AUTH_FILE=", 1)[1]
    assert f"--root {tmp_path / 'host-data' / 'containers' / 'storage'}" in logged
    assert f"--runroot {tmp_path / 'host-runtime' / 'containers'}" in logged


@pytest.mark.unit
def test_wrapper_distinguishes_podman_preflight_failure(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(
        fake_bin,
        log_path,
        status=42,
        message="ping_group_range runtime configuration unavailable",
    )
    env = _podman_environment(tmp_path, fake_bin, host_home)
    marker = tmp_path / "product-command-ran"
    command = [sys.executable, "-c", f"Path({str(marker)!r}).touch()"]

    result = _run_wrapper(repo_root, ["--require-podman", "--", *command], env)

    assert result.returncode == 125
    assert "Podman preflight failed" in result.stderr
    assert "infrastructure/runtime configuration failure" in result.stderr
    assert "ping_group_range runtime configuration unavailable" in result.stderr
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_distinguishes_container_preflight_failure(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    log_path = host_home / "podman.log"
    _make_fake_podman(
        fake_bin,
        log_path,
        run_status=42,
        run_message="probe registry configuration unavailable",
    )
    env = _podman_environment(tmp_path, fake_bin, host_home)
    marker = tmp_path / "product-command-ran"
    command = [sys.executable, "-c", f"Path({str(marker)!r}).touch()"]

    result = _run_wrapper(repo_root, ["--require-podman", "--", *command], env)

    assert result.returncode == 125
    assert "Podman container preflight failed" in result.stderr
    assert "probe registry configuration unavailable" in result.stderr
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_propagates_command_status(repo_root: Path) -> None:
    result = _run_wrapper(
        repo_root,
        ["--", sys.executable, "-c", "raise SystemExit(23)"],
        os.environ.copy(),
    )

    assert result.returncode == 23
