import os
import stat
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.mark.unit
def test_devbox_help(devbox_path: Path):
    res = run_bash_script(devbox_path, ["--help"])
    assert res.returncode == 0
    assert "Usage: devbox" in res.stdout
    assert "-r, --remove" in res.stdout
    assert "--recreate" in res.stdout
    assert "--new" in res.stdout


@pytest.mark.unit
def test_devbox_short_help(devbox_path: Path):
    res = run_bash_script(devbox_path, ["-h"])
    assert res.returncode == 0
    assert "Usage: devbox" in res.stdout


@pytest.mark.unit
def test_devbox_invalid_option(devbox_path: Path):
    res = run_bash_script(devbox_path, ["--invalid-flag-xyz"])
    assert res.returncode == 1
    assert "Unknown option: --invalid-flag-xyz" in res.stderr
    assert "Usage: devbox" in res.stderr


@pytest.mark.unit
def test_devbox_remove_invocation(devbox_path: Path, tmp_path: Path):
    # Create a mock podman script that records all calls
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "podman_calls.log"

    mock_podman = bin_dir / "podman"
    mock_podman.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{log_file}"
if [ "$1" = "container" ] && [ "$2" = "exists" ]; then
    exit 0
fi
exit 0
""")
    mock_podman.chmod(mock_podman.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    res = run_bash_script(devbox_path, ["--remove"], env=env, cwd=tmp_path)
    assert res.returncode == 0
    assert "Removing container" in res.stdout

    logged = log_file.read_text()
    assert "container exists devbox-" in logged
    assert "stop devbox-" in logged
    assert "rm devbox-" in logged
