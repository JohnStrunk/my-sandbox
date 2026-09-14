import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    devbox_container_name,
    run_bash_script,
    unique_workspace_dir,
)


def _check_nested_podman_supported(
    devbox_path: Path, tmp_path: Path, env: dict[str, str]
) -> bool:
    test_dir = unique_workspace_dir(tmp_path, "check_nested")
    container_name = devbox_container_name(test_dir)
    try:
        res = run_bash_script(
            devbox_path,
            ["podman", "run", "--rm", "docker.io/library/alpine:latest", "true"],
            env=env,
            cwd=test_dir,
            timeout=40,
        )
        return res.returncode == 0
    finally:
        subprocess.run(
            ["podman", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )


@pytest.fixture
def nested_podman_available(
    devbox_path: Path,
    devbox_image: str,
    isolated_env: dict[str, str],
    tmp_path: Path,
) -> bool:
    if not _check_nested_podman_supported(devbox_path, tmp_path, isolated_env):
        pytest.skip("Nested user namespaces not supported in this host/container env")
    return True


@pytest.mark.integration
def test_nested_podman_run(
    devbox_path: Path,
    devbox_image: str,
    nested_podman_available: bool,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    test_dir = unique_workspace_dir(tmp_path, "nested_run_ws")
    container_name = devbox_container_name(test_dir)

    try:
        # Run nested podman command inside devbox
        res = run_bash_script(
            devbox_path,
            [
                "podman",
                "run",
                "--rm",
                "docker.io/library/alpine:latest",
                "echo",
                "nested-podman-ok",
            ],
            env=isolated_env,
            cwd=test_dir,
            timeout=90,
        )
        assert res.returncode == 0, (
            f"Nested podman run failed:\nStdout: {res.stdout}\nStderr: {res.stderr}"
        )
        assert "nested-podman-ok" in res.stdout
    finally:
        subprocess.run(
            ["podman", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )


@pytest.mark.integration
def test_nested_podman_build(
    devbox_path: Path,
    devbox_image: str,
    nested_podman_available: bool,
    tmp_path: Path,
    isolated_env: dict[str, str],
):
    test_dir = unique_workspace_dir(tmp_path, "nested_build_ws")
    container_name = devbox_container_name(test_dir)

    # Create a minimal Containerfile in the workspace
    containerfile = test_dir / "Containerfile"
    containerfile.write_text("""FROM docker.io/library/alpine:latest
RUN echo "build step inside devbox" > /msg.txt
CMD ["cat", "/msg.txt"]
""")

    try:
        # Build nested image
        build_res = run_bash_script(
            devbox_path,
            ["podman", "build", "-t", "nested-test:v1", "."],
            env=isolated_env,
            cwd=test_dir,
            timeout=120,
        )
        assert build_res.returncode == 0, (
            "Nested podman build failed:\n"
            f"Stdout: {build_res.stdout}\nStderr: {build_res.stderr}"
        )

        # Run the built nested image
        run_res = run_bash_script(
            devbox_path,
            ["podman", "run", "--rm", "nested-test:v1"],
            env=isolated_env,
            cwd=test_dir,
            timeout=60,
        )
        assert run_res.returncode == 0
        assert "build step inside devbox" in run_res.stdout
    finally:
        subprocess.run(
            ["podman", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )
