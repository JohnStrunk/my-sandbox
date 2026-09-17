import os
from pathlib import Path

import pytest

from tests.conftest import (
    devbox_container_name,
    run_bash_script,
    run_podman_isolated,
    unique_workspace_dir,
)


@pytest.mark.integration
def test_devbox_lifecycle_create_exec_recreate_remove(
    devbox_path: Path,
    devbox_image: str,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    # Per-run unique directory name, hence a per-run unique container name
    # (issue #152): a stale container from an interrupted run can never
    # collide with this run's container.
    test_dir = unique_workspace_dir(tmp_path, "test_workspace")
    container_name = devbox_container_name(test_dir)
    host_agents = Path(isolated_env["HOME"]) / ".agents"
    (host_agents / "skills").mkdir(parents=True)

    try:
        # 1. First run: should create container and execute command
        test_file = test_dir / "test_output.txt"
        res_create = run_bash_script(
            devbox_path,
            ["bash", "-c", "echo 'hello from container' > test_output.txt"],
            env=isolated_env,
            cwd=test_dir,
            # A sanitized runner rebuilds the image in its isolated Podman
            # store; the kind/kubectl downloads make a cold build slower than
            # the normal command-execution budget.
            timeout=600,
        )
        assert res_create.returncode == 0, (
            f"Failed to create/run devbox:\n{res_create.stdout}\n{res_create.stderr}"
        )
        assert "Creating container" in res_create.stdout
        assert test_file.exists()
        assert test_file.read_text().strip() == "hello from container"

        ripwire_version = run_bash_script(
            devbox_path,
            ["ripwire", "--version"],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        assert ripwire_version.returncode == 0
        assert "0.5.0" in ripwire_version.stdout
        assert (host_agents / "skills" / "ripwire-orient" / "SKILL.md").is_file()
        assert (host_agents / "skills" / "devbox-tools" / "SKILL.md").is_file()
        assert (host_agents / "skills" / "ast-grep" / "SKILL.md").is_file()
        assert (host_agents / "skills" / "ast-grep-outline" / "SKILL.md").is_file()

        # Check ownership on host: file should be owned by the current host user
        assert test_file.stat().st_uid == os.getuid()

        # 2. Second run: container exists, should just exec
        res_exec = run_bash_script(
            devbox_path,
            ["cat", "test_output.txt"],
            env=isolated_env,
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
            env=isolated_env,
            cwd=test_dir,
            timeout=600,
        )
        assert res_recreate.returncode == 0
        assert "Removing container" in res_recreate.stdout
        assert "Creating container" in res_recreate.stdout
        assert test_file.exists()
        assert test_file.read_text().strip() == "hello from container"

        # 4. Remove: stops and removes container
        res_remove = run_bash_script(
            devbox_path,
            ["--remove"],
            env=isolated_env,
            cwd=test_dir,
            timeout=30,
        )
        assert res_remove.returncode == 0
        assert "Removing container" in res_remove.stdout

        # Verify container no longer exists in podman. Run through the
        # isolated runtime: a raw call "passes" on its configuration error,
        # not on the container's absence (issue #178).
        names = run_podman_isolated(
            isolated_env, ["ps", "-a", "--format", "{{.Names}}"]
        )
        assert container_name not in names.stdout.splitlines()

    finally:
        # Loud cleanup in case of failure; an already-removed container is
        # the expected success-path state, not a leak.
        run_podman_isolated(
            isolated_env,
            ["rm", "-f", container_name],
            allow_absent=True,
        )
