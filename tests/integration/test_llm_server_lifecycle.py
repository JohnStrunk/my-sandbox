import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.mark.integration
def test_llm_server_lifecycle(local_llm_dir: Path, is_podman_available: bool):
    if not is_podman_available:
        pytest.skip("Podman is not available")

    container_name = "test-llm-lifecycle-container"
    volume_name = "test-llm-lifecycle-volume"
    port = "19999"

    env = os.environ.copy()
    env["LLM_CONTAINER_NAME"] = container_name
    env["LLM_VOLUME_NAME"] = volume_name
    env["LLM_PORT"] = port

    stop_script = local_llm_dir / "stop-llm-server.sh"
    status_script = local_llm_dir / "status-llm-server.sh"

    try:
        # Create volume and a dummy test container mimicking the Ollama container
        subprocess.run(
            ["podman", "volume", "create", volume_name],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "podman",
                "run",
                "-d",
                "--name",
                container_name,
                "--publish",
                f"127.0.0.1:{port}:11434",
                "--volume",
                f"{volume_name}:/root/.ollama",
                "docker.io/library/alpine:latest",
                "sleep",
                "300",
            ],
            capture_output=True,
            check=True,
        )

        # 1. Test status-llm-server.sh when container is present
        # Note: ollama list will fail on alpine dummy, but container exists passes
        res_status = run_bash_script(status_script, env=env)
        # Should not exit with 1 (which means "container does not exist")
        assert "does not exist" not in res_status.stderr

        # 2. Test stop-llm-server.sh without --purge: stops container, volume retained
        res_stop = run_bash_script(stop_script, env=env)
        assert res_stop.returncode == 0
        assert "model weights kept in volume" in res_stop.stdout

        # Container should be gone
        check_container = subprocess.run(
            ["podman", "container", "exists", container_name],
            capture_output=True,
            check=False,
        )
        assert check_container.returncode != 0

        # Volume should still exist
        check_vol = subprocess.run(
            ["podman", "volume", "exists", volume_name],
            capture_output=True,
            check=False,
        )
        assert check_vol.returncode == 0

        # 3. Test stop-llm-server.sh with --purge: volume removed
        res_purge = run_bash_script(stop_script, ["--purge"], env=env)
        assert res_purge.returncode == 0
        assert "Removing volume" in res_purge.stdout

        # Volume should now be gone
        check_vol_after = subprocess.run(
            ["podman", "volume", "exists", volume_name],
            capture_output=True,
            check=False,
        )
        assert check_vol_after.returncode != 0

    finally:
        subprocess.run(
            ["podman", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["podman", "volume", "rm", volume_name],
            capture_output=True,
            check=False,
        )
