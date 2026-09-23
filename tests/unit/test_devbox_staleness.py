import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.conftest import devbox_context_fingerprint, run_bash_script


def _expected_fingerprint(launcher: Path, context_dir: Path) -> str:
    """Mirror of the launcher's `container_context_fingerprint` (issue #179):
    container build-context entries plus launcher contents and mode."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            """{
    (
        cd "$1"
        find . -mindepth 1 -print0 | LC_ALL=C sort -z |
        while IFS= read -r -d '' path; do
            mode="$(stat -c '%a' -- "$path")"
            if [ -L "$path" ]; then
                printf 'symlink\\0%s\\0%s\\0' "$mode" "$path"
                readlink --zero -- "$path"
            elif [ -d "$path" ]; then
                printf 'directory\\0%s\\0%s\\0' "$mode" "$path"
            elif [ -f "$path" ]; then
                printf 'file\\0%s\\0%s\\0' "$mode" "$path"
                sha256sum < "$path" | cut -d ' ' -f 1
                printf '\\0'
            else
                printf '%s\\0%s\\0%s\\0' "$(stat -c '%F' -- "$path")" "$mode" "$path"
            fi
        done
    )
    printf 'launcher mode=%s hash=%s\\n' \\
        "$(stat -c '%a' -- "$2")" \\
        "$(sha256sum < "$2" | cut -d ' ' -f 1)"
} | sha256sum | cut -d ' ' -f 1
""",
            "bash",
            str(context_dir),
            str(launcher),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _isolated_launcher_copy(devbox_path: Path, tmp_path: Path) -> Path:
    """Copy the launcher plus a minimal container context so the script's
    self-locating `HERE` resolves inside tmp_path and the copy can be
    edited to simulate launcher upgrades."""
    launcher_dir = tmp_path / "launcher-repo"
    (launcher_dir / "container").mkdir(parents=True)
    (launcher_dir / "container" / "Dockerfile").write_text("FROM fedora:latest\n")
    launcher = launcher_dir / "devbox"
    shutil.copy(devbox_path, launcher)
    return launcher


def _mock_podman(
    tmp_path: Path,
    isolated_env: dict[str, str],
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

    env = isolated_env
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["MOCK_RECORDED_FINGERPRINT"] = recorded_fingerprint
    env["MOCK_CONTAINER_IMAGE"] = container_image
    env["MOCK_CURRENT_IMAGE"] = current_image
    return env


@pytest.mark.unit
def test_devbox_warns_before_entering_stale_container(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    env = _mock_podman(
        tmp_path,
        isolated_env,
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
def test_devbox_warns_for_rebuilt_image(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    expected_fingerprint = _expected_fingerprint(
        devbox_path, devbox_path.parent / "container"
    )
    env = _mock_podman(
        tmp_path,
        isolated_env,
        recorded_fingerprint=expected_fingerprint,
        container_image="old-image",
        current_image="current-image",
    )
    res = run_bash_script(devbox_path, ["true"], env=env, cwd=tmp_path)

    assert res.returncode == 0
    assert "is stale" in res.stderr
    assert "devbox --recreate" in res.stderr


@pytest.mark.unit
def test_devbox_does_not_warn_for_current_container(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    expected_fingerprint = _expected_fingerprint(
        devbox_path, devbox_path.parent / "container"
    )
    env = _mock_podman(
        tmp_path,
        isolated_env,
        recorded_fingerprint=expected_fingerprint,
        container_image="current-image",
        current_image="current-image",
    )
    res = run_bash_script(devbox_path, ["true"], env=env, cwd=tmp_path)

    assert res.returncode == 0
    assert res.stderr == ""


@pytest.mark.unit
def test_devbox_warns_when_launcher_script_changes(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    launcher = _isolated_launcher_copy(devbox_path, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    env = _mock_podman(
        tmp_path,
        isolated_env,
        recorded_fingerprint=_expected_fingerprint(
            launcher, launcher.parent / "container"
        ),
        container_image="current-image",
        current_image="current-image",
    )
    baseline = run_bash_script(launcher, ["true"], env=env, cwd=project)
    assert baseline.returncode == 0
    assert baseline.stderr == ""

    launcher.write_text(f"{launcher.read_text()}\n# mount contract changed\n")

    res = run_bash_script(launcher, ["true"], env=env, cwd=project)
    assert res.returncode == 0
    assert "is stale" in res.stderr
    assert "devbox --recreate" in res.stderr


@pytest.mark.unit
def test_devbox_warns_when_container_context_changes(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    launcher = _isolated_launcher_copy(devbox_path, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    env = _mock_podman(
        tmp_path,
        isolated_env,
        recorded_fingerprint=_expected_fingerprint(
            launcher, launcher.parent / "container"
        ),
        container_image="current-image",
        current_image="current-image",
    )
    (launcher.parent / "container" / "Dockerfile").write_text(
        "FROM fedora:latest\nRUN true\n"
    )

    res = run_bash_script(launcher, ["true"], env=env, cwd=project)

    assert res.returncode == 0
    assert "is stale" in res.stderr


@pytest.mark.unit
def test_devbox_not_stale_after_unrelated_file_edit(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    launcher = _isolated_launcher_copy(devbox_path, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    env = _mock_podman(
        tmp_path,
        isolated_env,
        recorded_fingerprint=_expected_fingerprint(
            launcher, launcher.parent / "container"
        ),
        container_image="current-image",
        current_image="current-image",
    )
    (launcher.parent / "README.md").write_text("unrelated documentation edit\n")

    res = run_bash_script(launcher, ["true"], env=env, cwd=project)

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


@pytest.mark.unit
def test_devbox_context_fingerprint_changes_with_file_mode(
    devbox_path: Path, tmp_path: Path
):
    launcher = _isolated_launcher_copy(devbox_path, tmp_path)
    dockerfile = launcher.parent / "container" / "Dockerfile"
    first_fingerprint = _expected_fingerprint(launcher, dockerfile.parent)

    dockerfile.chmod(dockerfile.stat().st_mode | stat.S_IXUSR)

    assert _expected_fingerprint(launcher, dockerfile.parent) != first_fingerprint


@pytest.mark.unit
def test_devbox_context_fingerprint_changes_with_symlink_target(
    devbox_path: Path, tmp_path: Path
):
    launcher = _isolated_launcher_copy(devbox_path, tmp_path)
    context_dir = launcher.parent / "container"
    first_target = context_dir / "first-target"
    second_target = context_dir / "second-target"
    link = context_dir / "linked-file"
    first_target.write_text("first\n")
    second_target.write_text("second\n")
    link.symlink_to(first_target.name)
    first_fingerprint = _expected_fingerprint(launcher, context_dir)

    link.unlink()
    link.symlink_to(second_target.name)

    assert _expected_fingerprint(launcher, context_dir) != first_fingerprint


@pytest.mark.unit
def test_devbox_warns_when_file_mode_changes_in_build_context(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    launcher = _isolated_launcher_copy(devbox_path, tmp_path)
    context_dir = launcher.parent / "container"
    dockerfile = context_dir / "Dockerfile"
    project = tmp_path / "project"
    project.mkdir()
    env = _mock_podman(
        tmp_path,
        isolated_env,
        recorded_fingerprint=_expected_fingerprint(launcher, context_dir),
        container_image="current-image",
        current_image="current-image",
    )
    dockerfile.chmod(dockerfile.stat().st_mode | stat.S_IXUSR)

    result = run_bash_script(launcher, ["true"], env=env, cwd=project)

    assert result.returncode == 0
    assert "is stale" in result.stderr


@pytest.mark.unit
def test_devbox_warns_when_symlink_target_changes_in_build_context(
    devbox_path: Path, tmp_path: Path, isolated_env: dict[str, str]
):
    launcher = _isolated_launcher_copy(devbox_path, tmp_path)
    context_dir = launcher.parent / "container"
    first_target = context_dir / "first-target"
    second_target = context_dir / "second-target"
    link = context_dir / "linked-file"
    project = tmp_path / "project"
    project.mkdir()
    first_target.write_text("first\n")
    second_target.write_text("second\n")
    link.symlink_to(first_target.name)
    env = _mock_podman(
        tmp_path,
        isolated_env,
        recorded_fingerprint=_expected_fingerprint(launcher, context_dir),
        container_image="current-image",
        current_image="current-image",
    )
    link.unlink()
    link.symlink_to(second_target.name)

    result = run_bash_script(launcher, ["true"], env=env, cwd=project)

    assert result.returncode == 0
    assert "is stale" in result.stderr
