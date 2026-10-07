import os
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
    kind.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s |%s|%s\\n\' "$*" "${DOCKER_HOST:-}" '
        '"${KIND_EXPERIMENTAL_PROVIDER:-}" >> "$KIND_LOG"\n'
        'if [[ "${FAIL_CREATE:-}" == 1 && "$1 $2" == \'create cluster\' ]]; then\n'
        "  exit 1\n"
        "fi\n"
    )
    kind.chmod(0o755)
    fake_date = fake_bin / "date"
    fake_date.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = '+%s' ]; then printf '1234567890\\n'; "
        'else exec /usr/bin/date "$@"; fi\n'
    )
    fake_date.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "KIND_LOG": str(log_path),
        "DOCKER_HOST": f"unix:///run/user/{os.getuid()}/docker.sock",
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
    expected_podman_host = f"unix:///run/user/{os.getuid()}/podman/podman.sock"
    assert all(call.endswith(f"|{expected_podman_host}|podman") for call in calls)
    create_names = [line.split("--name ", 1)[1].split()[0] for line in creates]
    delete_names = [line.split("--name ", 1)[1].split()[0] for line in deletes]
    assert create_names == delete_names
    assert "kind validation passed: 10 consecutive clusters" in result.stdout

    failed_run = subprocess.run(
        ["bash", str(repo_root / "lima/validate-kind.sh"), "1"],
        check=False,
        capture_output=True,
        text=True,
        env={**env, "FAIL_CREATE": "1"},
        timeout=10,
    )

    assert failed_run.returncode != 0
    calls = log_path.read_text().splitlines()
    creates = [line for line in calls if line.startswith("create cluster")]
    deletes = [line for line in calls if line.startswith("delete cluster")]
    all_create_names = [line.split("--name ", 1)[1].split()[0] for line in creates]
    all_delete_names = [line.split("--name ", 1)[1].split()[0] for line in deletes]
    assert all_create_names[:10] == all_delete_names[:10]
    assert all_create_names[10] not in all_create_names[:10]
    assert all_delete_names[10] == all_create_names[10]

    docker_run = subprocess.run(
        ["bash", str(repo_root / "lima/validate-kind.sh"), "1"],
        check=False,
        capture_output=True,
        text=True,
        env={**env, "KIND_EXPERIMENTAL_PROVIDER": "docker"},
        timeout=10,
    )

    assert docker_run.returncode == 0, docker_run.stderr
    docker_call = log_path.read_text().splitlines()[-2]
    expected_docker_host = f"unix:///run/user/{os.getuid()}/docker.sock"
    assert docker_call.startswith("create cluster")
    assert docker_call.endswith(f"|{expected_docker_host}|docker")


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
