import os
import socket
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _minimal_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath)}


def _fake_sysctl_root(tmp_path: Path, values: dict[str, str]) -> Path:
    root = tmp_path / "proc_sys"
    for key, value in values.items():
        target = root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    return root


SYSCTL_OK = {
    "net/ipv4/conf/default/route_localnet": "1",
    "net/ipv4/conf/default/arp_notify": "1",
    "net/ipv4/conf/default/rp_filter": "2",
    "net/ipv4/ip_forward": "1",
    "net/ipv6/conf/default/accept_dad": "0",
    "net/ipv6/conf/default/accept_ra": "0",
    "net/ipv6/conf/all/forwarding": "1",
}


@pytest.fixture
def check_script(repo_root: Path) -> Path:
    return repo_root / "container" / "devbox-docker-api-check.sh"


@pytest.mark.unit
def test_help(check_script: Path):
    res = run_bash_script(check_script, ["--help"])
    assert res.returncode == 0
    assert "Usage: devbox-docker-api-check" in res.stdout


@pytest.mark.unit
def test_rejects_unknown_option(check_script: Path):
    res = run_bash_script(check_script, ["--bogus"])
    assert res.returncode == 2
    assert "Unknown option" in res.stderr


@pytest.mark.unit
def test_reports_missing_docker_host(check_script: Path, tmp_path: Path):
    env = _minimal_env()

    res = run_bash_script(check_script, env=env, cwd=tmp_path)
    assert res.returncode == 1
    assert "DOCKER_HOST" in res.stderr


@pytest.mark.unit
def test_distinguishes_missing_socket(check_script: Path, tmp_path: Path):
    env = _minimal_env()
    env["DOCKER_HOST"] = f"unix://{tmp_path / 'missing.sock'}"

    res = run_bash_script(check_script, env=env, cwd=tmp_path)
    assert res.returncode == 1
    assert "no socket" in res.stderr
    assert "did not respond" not in res.stderr


@pytest.mark.unit
def test_distinguishes_unresponsive_socket(check_script: Path, tmp_path: Path):
    socket_path = tmp_path / "docker.sock"
    api_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    api_socket.bind(str(socket_path))
    try:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_executable(
            bin_dir / "curl",
            "#!/usr/bin/env bash\nprintf 'connection refused\\n' >&2\nexit 7\n",
        )
        env = _minimal_env()
        env["DOCKER_HOST"] = f"unix://{socket_path}"
        env["PATH"] = f"{bin_dir}:{env['PATH']}"

        res = run_bash_script(check_script, env=env, cwd=tmp_path)
        assert res.returncode == 1
        assert "did not respond" in res.stderr
        assert "no socket" not in res.stderr
    finally:
        api_socket.close()


def _ready_env(
    tmp_path: Path, sysctl_values: dict[str, str]
) -> tuple[dict[str, str], socket.socket]:
    socket_path = tmp_path / "docker.sock"
    api_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    api_socket.bind(str(socket_path))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _make_executable(bin_dir / "curl", "#!/usr/bin/env bash\nprintf 'OK'\n")
    _make_executable(bin_dir / "podman", "#!/usr/bin/env bash\nexit 0\n")
    containers_conf = tmp_path / "containers.conf"
    containers_conf.write_text('netns = "bridge"\n')

    env = _minimal_env()
    env["DOCKER_HOST"] = f"unix://{socket_path}"
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DEVBOX_SYSCTL_ROOT"] = str(_fake_sysctl_root(tmp_path, sysctl_values))
    env["DEVBOX_CONTAINERS_CONF"] = str(containers_conf)
    return env, api_socket


@pytest.mark.unit
def test_ready_with_bridge_sysctls_present(tmp_path: Path, check_script: Path):
    env, api_socket = _ready_env(tmp_path, SYSCTL_OK)
    try:
        res = run_bash_script(check_script, env=env, cwd=tmp_path)
        assert res.returncode == 0, res.stderr
        assert "Docker API ready" in res.stdout
        assert "preflight passed" in res.stdout
    finally:
        api_socket.close()


@pytest.mark.unit
def test_ready_but_bridge_sysctls_missing_soft_warning(
    tmp_path: Path, check_script: Path
):
    env, api_socket = _ready_env(tmp_path, {})
    try:
        res = run_bash_script(check_script, env=env, cwd=tmp_path)
        assert res.returncode == 0, res.stderr
        assert "Docker API ready" in res.stdout
        assert "preflight limited" in res.stderr
    finally:
        api_socket.close()


@pytest.mark.unit
def test_require_user_networks_fails_when_sysctls_missing(
    tmp_path: Path, check_script: Path
):
    env, api_socket = _ready_env(tmp_path, {})
    try:
        res = run_bash_script(
            check_script, ["--require-user-networks"], env=env, cwd=tmp_path
        )
        assert res.returncode == 2
        assert "preflight failed" in res.stderr
        assert "not guaranteed" in res.stderr
        assert "route_localnet is" in res.stderr
        assert "%%s" not in res.stderr
    finally:
        api_socket.close()


@pytest.mark.unit
def test_require_user_networks_passes_when_sysctls_present(
    tmp_path: Path, check_script: Path
):
    env, api_socket = _ready_env(tmp_path, SYSCTL_OK)
    try:
        res = run_bash_script(
            check_script, ["--require-user-networks"], env=env, cwd=tmp_path
        )
        assert res.returncode == 0, res.stderr
        assert "preflight passed" in res.stdout
    finally:
        api_socket.close()


@pytest.mark.unit
def test_require_user_networks_fails_for_pasta_default(
    tmp_path: Path, check_script: Path
):
    env, api_socket = _ready_env(tmp_path, SYSCTL_OK)
    pasta_conf = tmp_path / "pasta.conf"
    pasta_conf.write_text('netns = "pasta"\n')
    env["DEVBOX_CONTAINERS_CONF"] = str(pasta_conf)
    try:
        res = run_bash_script(
            check_script, ["--require-user-networks"], env=env, cwd=tmp_path
        )
        assert res.returncode == 2
        assert "default network mode" in res.stderr
    finally:
        api_socket.close()


@pytest.mark.unit
def test_require_user_networks_fails_when_nested_sysctls_cannot_be_verified(
    tmp_path: Path, check_script: Path
):
    env, api_socket = _ready_env(tmp_path, SYSCTL_OK)
    _make_executable(tmp_path / "bin" / "podman", "#!/usr/bin/env bash\nexit 1\n")
    try:
        res = run_bash_script(
            check_script, ["--require-user-networks"], env=env, cwd=tmp_path
        )
        assert res.returncode == 2
        assert "nested rootless-network sysctls" in res.stderr
    finally:
        api_socket.close()
