import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "container" / "devbox-go"


def _run(
    project: Path, args: list[str], fake_bin: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("GOWORK", None)
    env.pop("GOTOOLCHAIN", None)
    env.pop("GOBIN", None)
    if fake_bin is not None:
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _fake_command(fake_bin: Path, name: str) -> None:
    command = fake_bin / name
    command.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "${GOTOOLCHAIN:-unset}" > "$DEVBOX_GO_TEST_LOG"\n'
        "printf 'fake %s\\n' \"$*\"\n"
    )
    command.chmod(command.stat().st_mode | stat.S_IEXEC)


def _test_env(fake_bin: Path | None, log_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DEVBOX_GO_TEST_LOG"] = str(log_file)
    env.pop("GOWORK", None)
    env.pop("GOTOOLCHAIN", None)
    env.pop("GOBIN", None)
    if fake_bin is not None:
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return env


@pytest.mark.unit
def test_go_mod_version_selects_and_propagates_toolchain(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text("module example.test/project\n\ngo 1.26\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_file = tmp_path / "toolchain.log"
    _fake_command(fake_bin, "go")

    result = subprocess.run(
        ["bash", str(SCRIPT), "version"],
        cwd=project,
        capture_output=True,
        text=True,
        env=_test_env(fake_bin, log_file),
        check=False,
    )

    assert result.returncode == 0
    assert "fake version" in result.stdout
    assert log_file.read_text().strip() == "go1.26.0+auto"


@pytest.mark.unit
def test_cached_go_binary_does_not_shadow_image_toolchain(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text("module example.test/project\n\ngo 1.26.0\n")
    image_bin = tmp_path / "image-bin"
    cache_bin = tmp_path / "go-cache" / "bin"
    image_bin.mkdir()
    cache_bin.mkdir(parents=True)
    log_file = tmp_path / "toolchain.log"
    for directory, label in ((image_bin, "image"), (cache_bin, "cached")):
        command = directory / "go"
        command.write_text(
            f"#!/usr/bin/env bash\nprintf '%s\\n' {label} > \"$DEVBOX_GO_TEST_LOG\"\n"
        )
        command.chmod(command.stat().st_mode | stat.S_IEXEC)

    env = _test_env(image_bin, log_file)
    env["GOPATH"] = str(tmp_path / "go-cache")
    result = subprocess.run(
        ["bash", str(SCRIPT), "version"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert log_file.read_text().strip() == "image"


@pytest.mark.unit
def test_pre_1_21_go_version_uses_legacy_toolchain_name(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text("module example.test/project\n\ngo 1.20\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_file = tmp_path / "toolchain.log"
    _fake_command(fake_bin, "go")

    result = subprocess.run(
        ["bash", str(SCRIPT), "version"],
        cwd=project,
        capture_output=True,
        text=True,
        env=_test_env(fake_bin, log_file),
        check=False,
    )

    assert result.returncode == 0
    assert log_file.read_text().strip() == "go1.20+auto"


@pytest.mark.unit
def test_workspace_toolchain_takes_precedence_over_module(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.work").write_text("go 1.25.0\ntoolchain go1.25.3\nuse .\n")
    (project / "go.mod").write_text("module example.test/project\n\ngo 1.27.0\n")

    result = _run(project, ["--doctor"])

    assert result.returncode == 0
    assert f"Workspace: {project / 'go.work'}" in result.stdout
    assert "Toolchain directive: go1.25.3" in result.stdout
    assert "Selected toolchain: go1.25.3" in result.stdout
    assert "GOTOOLCHAIN: go1.25.3+auto" in result.stdout


@pytest.mark.unit
def test_quoted_crlf_directive_is_normalized(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_bytes(
        b'module example.test/project\r\n\r\ngo "1.26.0"\r\n'
    )

    result = _run(project, ["--doctor"])

    assert result.returncode == 0
    assert "Selected toolchain: go1.26.0" in result.stdout
    assert "GOTOOLCHAIN: go1.26.0+auto" in result.stdout


@pytest.mark.unit
def test_raw_string_directive_is_normalized(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text("module example.test/project\n\ngo `1.26.0`\n")

    result = _run(project, ["--doctor"])

    assert result.returncode == 0
    assert "Selected toolchain: go1.26.0" in result.stdout
    assert "GOTOOLCHAIN: go1.26.0+auto" in result.stdout


@pytest.mark.unit
def test_custom_toolchain_name_is_reported(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text(
        "module example.test/project\n\ngo 1.26.0\ntoolchain go1.26.0-custom\n"
    )

    result = _run(project, ["--doctor"])

    assert result.returncode == 0
    assert "Selected toolchain: go1.26.0-custom" in result.stdout
    assert "GOTOOLCHAIN: go1.26.0-custom+auto" in result.stdout


@pytest.mark.unit
def test_run_applies_selection_to_go_based_tools(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text("module example.test/project\n\ngo 1.26.0\n")
    go_cache = tmp_path / "go-cache"
    fake_bin = go_cache / "bin"
    fake_bin.mkdir(parents=True)
    log_file = tmp_path / "toolchain.log"
    _fake_command(fake_bin, "fake-linter")

    env = _test_env(None, log_file)
    env["GOPATH"] = str(go_cache)
    result = subprocess.run(
        ["bash", str(SCRIPT), "run", "fake-linter", "check"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert "fake check" in result.stdout
    assert log_file.read_text().strip() == "go1.26.0+auto"


@pytest.mark.unit
def test_workspace_without_directives_reports_go_default(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.work").write_text("use .\n")

    result = _run(project, ["--doctor"])

    assert result.returncode == 0
    assert "Selection source:" in result.stdout
    assert "Go directive: <none>" in result.stdout
    assert "Toolchain directive: <none>" in result.stdout
    assert "Selected toolchain: local" in result.stdout
    assert "GOTOOLCHAIN: auto" in result.stdout


@pytest.mark.unit
def test_module_without_directives_reports_go_default(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text("module example.test/project\n")

    result = _run(project, ["--doctor"])

    assert result.returncode == 0
    assert "Go directive: <none>" in result.stdout
    assert "Toolchain directive: <none>" in result.stdout
    assert "Selected toolchain: local" in result.stdout
    assert "GOTOOLCHAIN: auto" in result.stdout


@pytest.mark.unit
def test_relative_gowork_is_rejected(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "go.mod").write_text("module example.test/project\n\ngo 1.26\n")
    env = os.environ.copy()
    env["GOWORK"] = "go.work"
    env.pop("GOTOOLCHAIN", None)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--doctor"],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "GOWORK must be an absolute path" in result.stderr


@pytest.mark.unit
def test_missing_project_is_an_explicit_error(tmp_path: Path):
    result = _run(tmp_path, ["--doctor"])

    assert result.returncode == 1
    assert "no go.work or go.mod found" in result.stderr
