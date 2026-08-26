import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.mark.integration
def test_proxy_server_lifecycle(litellm_proxy_dir: Path, is_podman_available: bool):
    if not is_podman_available:
        pytest.skip("Podman is not available")

    container_name = "test-proxy-lifecycle-container"
    port = "19998"

    env = os.environ.copy()
    env["PROXY_CONTAINER_NAME"] = container_name
    env["PROXY_PORT"] = port

    stop_script = litellm_proxy_dir / "stop-proxy-server.sh"
    status_script = litellm_proxy_dir / "status-proxy-server.sh"

    try:
        # Create a dummy test container mimicking the proxy container
        subprocess.run(
            [
                "podman",
                "run",
                "-d",
                "--name",
                container_name,
                "--publish",
                f"127.0.0.1:{port}:4000",
                "docker.io/library/alpine:latest",
                "sleep",
                "300",
            ],
            capture_output=True,
            check=True,
        )

        # 1. Test status-proxy-server.sh when container is present
        res_status = run_bash_script(status_script, env=env)
        # Should not exit with 1 (which means "container does not exist")
        assert "does not exist" not in res_status.stderr

        # 2. Test stop-proxy-server.sh: stops and removes container
        res_stop = run_bash_script(stop_script, env=env)
        assert res_stop.returncode == 0
        assert "Stopping and removing container" in res_stop.stdout

        # Container should be gone
        check_container = subprocess.run(
            ["podman", "container", "exists", container_name],
            capture_output=True,
            check=False,
        )
        assert check_container.returncode != 0

    finally:
        subprocess.run(
            ["podman", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )
