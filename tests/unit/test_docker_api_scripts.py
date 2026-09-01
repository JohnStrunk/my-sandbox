import os
import socket
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


def _run_api_check(
    check_script: Path,
    env: dict[str, str],
    args: list[str] | None = None,
):
    return run_bash_script(
        Path("/bin/bash"),
        [str(check_script), *(args or [])],
        env=env,
    )


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _mock_api_environment(
    tmp_path: Path, network_backend: str = "netavark"
) -> tuple[dict[str, str], socket.socket]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    socket_path = tmp_path / "docker.sock"
    api_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    api_socket.bind(str(socket_path))

    _make_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\nprintf 'OK\\n'\n",
    )
    _make_executable(
        bin_dir / "podman",
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == info ]]; then\n'
        f"  printf 'true {network_backend} pasta\\n'\n"
        "fi\n",
    )
    env = os.environ.copy()
    env["DOCKER_HOST"] = f"unix://{socket_path}"
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env, api_socket


@pytest.mark.unit
def test_api_check_distinguishes_missing_socket(repo_root: Path, tmp_path: Path):
    env = os.environ.copy()
    env["DOCKER_HOST"] = f"unix://{tmp_path / 'missing.sock'}"

    result = _run_api_check(repo_root / "container" / "devbox-docker-api-check.sh", env)

    assert result.returncode == 1
    assert "no socket exists" in result.stderr
    assert "exists, but the service did not respond" not in result.stderr


@pytest.mark.unit
def test_api_check_distinguishes_unresponsive_socket(repo_root: Path, tmp_path: Path):
    env, api_socket = _mock_api_environment(tmp_path)
    try:
        _make_executable(
            tmp_path / "bin" / "curl",
            "#!/usr/bin/env bash\nprintf 'connection refused\\n' >&2\nexit 7\n",
        )
        result = _run_api_check(
            repo_root / "container" / "devbox-docker-api-check.sh", env
        )
    finally:
        api_socket.close()

    assert result.returncode == 1
    assert "socket" in result.stderr
    assert "exists, but the service did not respond" in result.stderr
    assert "no socket exists" not in result.stderr


@pytest.mark.unit
def test_api_check_reports_user_network_limitation(repo_root: Path, tmp_path: Path):
    env, api_socket = _mock_api_environment(tmp_path, network_backend="unsupported")
    try:
        result = _run_api_check(
            repo_root / "container" / "devbox-docker-api-check.sh",
            env,
            ["--require-user-networks"],
        )
    finally:
        api_socket.close()

    assert result.returncode == 2
    assert "active backend is unsupported" in result.stderr
    assert "Docker API ready" in result.stdout
