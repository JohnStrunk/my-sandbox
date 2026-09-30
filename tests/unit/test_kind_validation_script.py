import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_kind_validation_script_runs_ten_create_delete_cycles(
    repo_root: Path, tmp_path: Path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "kind.log"
    kind = fake_bin / "kind"
    kind.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$KIND_LOG"\n')
    kind.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "KIND_LOG": str(log_path),
        "DOCKER_HOST": "unix:///tmp/podman.sock",
    }

    result = subprocess.run(
        ["bash", str(repo_root / "lima/validate-kind.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text().splitlines()
    creates = [line for line in calls if line.startswith("create cluster")]
    deletes = [line for line in calls if line.startswith("delete cluster")]
    assert len(creates) == len(deletes) == 10
    create_names = [line.split("--name ", 1)[1].split()[0] for line in creates]
    delete_names = [line.split("--name ", 1)[1].split()[0] for line in deletes]
    assert create_names == delete_names
    assert "kind validation passed: 10 consecutive clusters" in result.stdout


@pytest.mark.unit
def test_kind_validation_script_rejects_an_invalid_run_count(repo_root: Path):
    result = subprocess.run(
        ["bash", str(repo_root / "lima/validate-kind.sh"), "0"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr
