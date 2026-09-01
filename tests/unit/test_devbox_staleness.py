import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.conftest import devbox_context_fingerprint, run_bash_script


def _shell_context_fingerprint(context_dir: Path) -> str:
    result = subprocess.run(
        [
            "bash",
            "-c",
            """(
    cd "$1"
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
) | sha256sum | cut -d ' ' -f 1
""",
            "bash",
            str(context_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _mock_podman(
    tmp_path: Path,
    recorded_fingerprint: str,
    container_image: str,
    current_image: str,
) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock_podman = bin_dir / "podman"
    mock_podman.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "container" ] && [ "$2" = "exists" ]; then
    exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
    echo "$MOCK_CURRENT_IMAGE"
    exit 0
fi
if [ "$1" = "inspect" ]; then
    if [[ "$*" == *"Config.Labels"* ]]; then
        echo "$MOCK_RECORDED_FINGERPRINT"
    elif [[ "$*" == *"State.Running"* ]]; then
        echo true
    else
        echo "$MOCK_CONTAINER_IMAGE"
    fi
    exit 0
fi
exit 0
"""
    )
    mock_podman.chmod(mock_podman.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["MOCK_RECORDED_FINGERPRINT"] = recorded_fingerprint
    env["MOCK_CONTAINER_IMAGE"] = container_image
    env["MOCK_CURRENT_IMAGE"] = current_image
    return env


@pytest.mark.unit
def test_devbox_warns_before_entering_stale_container(
    devbox_path: Path, tmp_path: Path
):
    env = _mock_podman(
        tmp_path,
        recorded_fingerprint="old-context",
        container_image="current-image",
        current_image="current-image",
    )
    res = run_bash_script(devbox_path, ["true"], env=env, cwd=tmp_path)

    assert res.returncode == 0
    assert "is stale" in res.stderr
    assert "devbox --recreate" in res.stderr
    assert "host-backed project directory is preserved" in res.stderr


@pytest.mark.unit
def test_devbox_warns_for_rebuilt_image(devbox_path: Path, tmp_path: Path):
    expected_fingerprint = _shell_context_fingerprint(devbox_path.parent / "container")
    env = _mock_podman(
        tmp_path,
        recorded_fingerprint=expected_fingerprint,
        container_image="old-image",
        current_image="current-image",
    )
    res = run_bash_script(devbox_path, ["true"], env=env, cwd=tmp_path)

    assert res.returncode == 0
    assert "is stale" in res.stderr
    assert "devbox --recreate" in res.stderr


@pytest.mark.unit
def test_devbox_does_not_warn_for_current_container(devbox_path: Path, tmp_path: Path):
    expected_fingerprint = _shell_context_fingerprint(devbox_path.parent / "container")
    env = _mock_podman(
        tmp_path,
        recorded_fingerprint=expected_fingerprint,
        container_image="current-image",
        current_image="current-image",
    )
    res = run_bash_script(devbox_path, ["true"], env=env, cwd=tmp_path)

    assert res.returncode == 0
    assert res.stderr == ""


@pytest.mark.unit
def test_devbox_test_image_fingerprint_changes_with_context(tmp_path: Path):
    context_dir = tmp_path / "container"
    context_dir.mkdir()
    dockerfile = context_dir / "Dockerfile"
    dockerfile.write_text("FROM fedora:latest\n")
    first_fingerprint = devbox_context_fingerprint(context_dir)

    dockerfile.write_text("FROM fedora:latest\nRUN true\n")

    assert devbox_context_fingerprint(context_dir) != first_fingerprint
