from pathlib import Path

import pytest

from tests.conftest import run_bash_script


@pytest.mark.unit
def test_devbox_help_describes_only_the_vm_workflow(devbox_path: Path):
    result = run_bash_script(devbox_path, ["--help"])

    assert result.returncode == 0
    assert "Usage: devbox" in result.stdout
    assert "shared Lima VM" in result.stdout
    assert "--stop" in result.stdout
    assert "--reset" in result.stdout
    assert "--reprovision" in result.stdout
    assert "--container" not in result.stdout
    assert "--remove" not in result.stdout


@pytest.mark.unit
def test_devbox_short_help(devbox_path: Path):
    result = run_bash_script(devbox_path, ["-h"])

    assert result.returncode == 0
    assert "Usage: devbox" in result.stdout


@pytest.mark.unit
def test_devbox_rejects_the_retired_container_mode(devbox_path: Path):
    result = run_bash_script(devbox_path, ["--container", "true"])

    assert result.returncode == 2
    assert "--container is no longer supported" in result.stderr
    assert "shared Lima VM" in result.stderr


@pytest.mark.unit
def test_devbox_rejects_unknown_options(devbox_path: Path):
    for option in ("--invalid-flag-xyz", "--kind"):
        result = run_bash_script(devbox_path, [option])
        assert result.returncode == 2
        assert f"Unknown option: {option}" in result.stderr
        assert "Usage: devbox" in result.stderr
