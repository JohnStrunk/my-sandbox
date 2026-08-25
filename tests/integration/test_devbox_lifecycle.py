import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.mark.integration
def test_devbox_lifecycle_create_exec_recreate_remove(
    devbox_path: Path, devbox_image: str, tmp_path: Path
):
    # Unique directory name for this test run
    test_dir = tmp_path / "test_workspace"
    test_dir.mkdir()
    container_name = f"devbox-{test_dir.name}"

    try:
        # 1. First run: should create container and execute command
        test_file = test_dir / "test_output.txt"
        res_create = run_bash_script(
            devbox_path,
            ["bash", "-c", "echo 'hello from container' > test_output.txt"],
            cwd=test_dir,
            timeout=300,
        )
        assert res_create.returncode == 0, (
            f"Failed to create/run devbox:\n{res_create.stdout}\n{res_create.stderr}"
        )
        assert "Creating container" in res_create.stdout
        assert test_file.exists()
        assert test_file.read_text().strip() == "hello from container"

        # Check ownership on host: file should be owned by the current host user
        assert test_file.stat().st_uid == os.getuid()

        # 2. Second run: container exists, should just exec
        res_exec = run_bash_script(
            devbox_path,
            ["cat", "test_output.txt"],
            cwd=test_dir,
            timeout=60,
        )
        assert res_exec.returncode == 0
        assert "Creating container" not in res_exec.stdout
        assert "Entering container" in res_exec.stdout
        assert res_exec.stdout.strip().endswith("hello from container")

        # 3. Recreate: removes and re-creates container
        res_recreate = run_bash_script(
            devbox_path,
            ["--recreate", "echo", "recreated"],
            cwd=test_dir,
            timeout=300,
        )
        assert res_recreate.returncode == 0
        assert "Removing container" in res_recreate.stdout
        assert "Creating container" in res_recreate.stdout

        # 4. Remove: stops and removes container
        res_remove = run_bash_script(
            devbox_path,
            ["--remove"],
            cwd=test_dir,
            timeout=30,
        )
        assert res_remove.returncode == 0
        assert "Removing container" in res_remove.stdout

        # Verify container no longer exists in podman
        check_exists = subprocess.run(
            ["podman", "container", "exists", container_name],
            capture_output=True,
            check=False,
        )
        assert check_exists.returncode != 0

    finally:
        # Cleanup in case of failure
        subprocess.run(
            ["podman", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )
