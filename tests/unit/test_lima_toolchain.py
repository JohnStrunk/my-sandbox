import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lima.check_toolchain import _VERSION_COMMANDS, check_toolchain


def _manifest(repo_root: Path) -> dict:
    return json.loads((repo_root / "lima/tool-versions.json").read_text())


def _write_manifest(path: Path, manifest: dict) -> Path:
    path.write_text(json.dumps(manifest))
    return path


@pytest.fixture(autouse=True)
def _gocyclo_is_on_path(monkeypatch: pytest.MonkeyPatch):
    real_which = shutil.which
    monkeypatch.setattr(
        "lima.check_toolchain.shutil.which",
        lambda name: (
            "/var/lib/devbox-toolbuilder/.local/bin/gocyclo"
            if name == "gocyclo"
            else real_which(name)
        ),
    )


def _version_output(name: str, version: str, go_version: str) -> str:
    if name == "kubectl":
        return json.dumps({"clientVersion": {"gitVersion": f"v{version}"}})
    if name == "krew":
        return f"OPTION            VALUE\nGitTag            v{version}\n"
    if name == "go":
        return f"go version go{version} linux/amd64"
    if name == "gocyclo":
        return f"gocyclo:\n\tmod\tgithub.com/fzipp/gocyclo\tv{version}\th1:sum"
    if name == "golangci_lint":
        return f"golangci-lint has version {version} built with go{go_version}"
    if name == "actionlint":
        return f"{version}\ninstalled by downloading from release page"
    if name == "yamllint":
        return f"yamllint {version}"
    if name == "rust":
        return f"rustc {version} (commit 48a229cea 2026-09-01)"
    if name == "ruby":
        return f"ruby {version} (2026-01-01) [x86_64-linux]"
    if name == "kind":
        return f"kind v{version} go{go_version} linux/amd64"
    if name == "minikube":
        return f"minikube version: v{version}\ncommit: abcdef0123456789"
    if name == "node":
        return f"v{version}"
    if name == "pre_commit":
        return f"pre-commit {version}"
    if name in {"markdownlint_cli2", "opencode"}:
        return f"{name} v{version}"
    if name == "mermaid_cli":
        return version
    if name == "coderabbit_cli":
        return f"CodeRabbit CLI v{version}"
    return f"{name} {version}"


def _successful_runner(manifest: dict, overrides: dict[str, str] | None = None):
    command_names = {command: name for name, (command, _) in _VERSION_COMMANDS.items()}
    versions = {
        name: spec["version"].removeprefix("v")
        for name, spec in manifest["tools"].items()
        if isinstance(spec.get("consumers"), dict)
        and spec["consumers"].get("lima") is True
    }
    go_version = versions["go"]
    overrides = overrides or {}

    def run(command):
        command = tuple(command)
        executable = command[0]
        name = command_names.get(command)
        if name is None and command[:3] == _VERSION_COMMANDS["gocyclo"][0]:
            name = "gocyclo"
        if name:
            output = _version_output(
                name, overrides.get(name, versions[name]), go_version
            )
        else:
            output = f"{executable} version 1.0.0"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    return run


@pytest.mark.unit
def test_lima_toolchain_check_verifies_each_manifest_consumer(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)

    successes, errors = check_toolchain(manifest_path, _successful_runner(manifest))

    assert errors == []
    declared = {
        name
        for name, spec in manifest["tools"].items()
        if spec["consumers"].get("lima") is True
    }
    assert {line.split(":", 1)[0] for line in successes} >= declared
    assert any(line.startswith("make:") for line in successes)
    assert any(line.startswith("pip:") for line in successes)


@pytest.mark.unit
@pytest.mark.parametrize("expected_version", ["0.10.0-rc.2", "0.10.0+build.5"])
def test_lima_toolchain_check_parses_coderabbit_prerelease_version(
    repo_root: Path, tmp_path: Path, expected_version: str
):
    manifest = _manifest(repo_root)
    manifest["tools"]["coderabbit_cli"]["version"] = expected_version
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)

    successes, errors = check_toolchain(manifest_path, _successful_runner(manifest))

    assert errors == []
    assert f"coderabbit_cli: {expected_version}" in successes


@pytest.mark.unit
def test_lima_toolchain_check_parses_ruby_version(repo_root: Path, tmp_path: Path):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    expected_version = manifest["tools"]["ruby"]["version"]
    successful = _successful_runner(manifest)
    commands = []

    def runner(command):
        commands.append(tuple(command))
        return successful(command)

    successes, errors = check_toolchain(manifest_path, runner)

    assert errors == []
    assert ("ruby", "--version") in commands
    assert f"ruby: {expected_version}" in successes


@pytest.mark.unit
def test_lima_toolchain_check_reports_missing_ruby(repo_root: Path, tmp_path: Path):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    successful = _successful_runner(manifest)

    def runner(command):
        if command[0] == "ruby":
            raise FileNotFoundError("ruby")
        return successful(command)

    successes, errors = check_toolchain(manifest_path, runner)

    assert not any(line.startswith("ruby:") for line in successes)
    assert any(
        error.startswith("ruby:") and "ruby --version" in error for error in errors
    )


@pytest.mark.unit
def test_lima_toolchain_check_reports_mismatched_ruby(repo_root: Path, tmp_path: Path):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    expected_version = manifest["tools"]["ruby"]["version"]
    mismatched_version = "0.0.1" if expected_version == "0.0.0" else "0.0.0"
    runner = _successful_runner(manifest, {"ruby": mismatched_version})

    _, errors = check_toolchain(manifest_path, runner)

    assert any(
        f"ruby: installed version {mismatched_version}, expected {expected_version}"
        in error
        for error in errors
    )


@pytest.mark.unit
def test_lima_toolchain_check_reports_a_pinned_version_mismatch(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    expected_version = manifest["tools"]["kind"]["version"]
    runner = _successful_runner(manifest, {"kind": "0.0.1"})

    _, errors = check_toolchain(manifest_path, runner)

    assert any(
        f"kind: installed version 0.0.1, expected {expected_version}" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "minikube",
        "coderabbit_cli",
        "golangci_lint",
        "actionlint",
        "yamllint",
        "gocyclo",
        "mermaid_cli",
    ],
)
@pytest.mark.unit
def test_lima_toolchain_check_reports_manifest_version_mismatch(
    repo_root: Path, tmp_path: Path, tool_name: str
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    expected_version = manifest["tools"][tool_name]["version"]
    runner = _successful_runner(manifest, {tool_name: "0.0.0"})

    _, errors = check_toolchain(manifest_path, runner)

    assert any(
        f"{tool_name}: installed version 0.0.0, expected {expected_version}" in error
        for error in errors
    )


@pytest.mark.unit
def test_lima_toolchain_uses_each_linter_version_flag():
    assert _VERSION_COMMANDS["actionlint"][0] == ("actionlint", "-version")
    assert _VERSION_COMMANDS["yamllint"][0] == ("yamllint", "--version")


_DOCKER_RPM_TOOLS = ("docker_ce", "containerd_io", "docker_buildx_plugin")


@pytest.mark.parametrize("tool_name", _DOCKER_RPM_TOOLS)
@pytest.mark.unit
def test_docker_rpm_versions_are_not_lima_toolchain_pins(
    repo_root: Path, tmp_path: Path, tool_name: str
):
    """Docker RPM versions float at VM creation and are never pin-checked.

    Provisioning and readiness verify package identity, architecture, and the
    pinned Docker signing key instead, so the toolchain check must not compare
    the installed RPM version with the manifest.
    """

    manifest = _manifest(repo_root)
    assert manifest["tools"][tool_name]["consumers"]["lima"] is False
    assert tool_name not in _VERSION_COMMANDS
    manifest["tools"][tool_name]["version"] = "0.0.1"
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    successful = _successful_runner(manifest)
    commands: list[tuple[str, ...]] = []

    def runner(command):
        commands.append(tuple(command))
        return successful(command)

    successes, errors = check_toolchain(manifest_path, runner)

    assert errors == []
    assert not any(line.startswith(f"{tool_name}:") for line in successes)
    assert not any(command[0] in {"docker", "containerd"} for command in commands)


@pytest.mark.unit
def test_lima_toolchain_check_reads_gocyclo_module_build_info(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    successful = _successful_runner(manifest)
    commands = []

    def runner(command):
        commands.append(tuple(command))
        return successful(command)

    successes, errors = check_toolchain(manifest_path, runner)

    assert errors == []
    assert (
        "go",
        "version",
        "-m",
        "/var/lib/devbox-toolbuilder/.local/bin/gocyclo",
    ) in commands
    assert ("golangci-lint", "--version") in commands
    assert f"gocyclo: {manifest['tools']['gocyclo']['version']}" in successes


@pytest.mark.parametrize("suffix", ["-rc.1", "+build.5"])
@pytest.mark.unit
def test_lima_toolchain_check_parses_gocyclo_version_suffix(
    repo_root: Path, tmp_path: Path, suffix: str
):
    manifest = _manifest(repo_root)
    expected_version = (
        manifest["tools"]["gocyclo"]["version"]
        .removeprefix("v")
        .split("-", 1)[0]
        .split("+", 1)[0]
        + suffix
    )
    manifest["tools"]["gocyclo"]["version"] = expected_version
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)

    successes, errors = check_toolchain(manifest_path, _successful_runner(manifest))

    assert errors == []
    assert f"gocyclo: {expected_version}" in successes


@pytest.mark.unit
def test_lima_toolchain_check_reports_missing_gocyclo(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    monkeypatch.setattr("lima.check_toolchain.shutil.which", lambda _name: None)

    _, errors = check_toolchain(manifest_path, _successful_runner(manifest))

    assert "gocyclo: executable is not available on PATH" in errors


@pytest.mark.unit
def test_lima_toolchain_check_verifies_the_krew_version(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    expected_version = manifest["tools"]["krew"]["version"]
    runner = _successful_runner(manifest, {"krew": "0.0.1"})

    _, errors = check_toolchain(manifest_path, runner)

    assert any(
        f"krew: installed version 0.0.1, expected {expected_version}" in error
        for error in errors
    )


@pytest.mark.unit
def test_lima_toolchain_check_can_skip_later_user_managed_tools(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    runner = _successful_runner(manifest, {"krew": "0.0.1"})

    successes, errors = check_toolchain(
        manifest_path, runner, skip_tools=frozenset({"krew"})
    )

    assert errors == []
    assert not any(line.startswith("krew:") for line in successes)


@pytest.mark.unit
def test_isolated_toolbuilder_defers_krew_until_user_provisioning(repo_root: Path):
    script = (repo_root / "lima/provision-tools.sh").read_text()

    assert "--skip-tool krew" in script


@pytest.mark.unit
def test_mermaid_cli_install_skips_the_puppeteer_browser_download(
    repo_root: Path,
):
    """The npm install must not fetch a duplicate Chromium."""
    script = (repo_root / "lima/provision-tools.sh").read_text()

    skip_export = "export PUPPETEER_SKIP_DOWNLOAD=1"
    ensure_call = "ensure_npm_package mermaid_cli @mermaid-js/mermaid-cli"

    assert skip_export in script
    assert ensure_call in script
    assert script.index(skip_export) < script.index(ensure_call)


@pytest.mark.unit
def test_mermaid_cli_repair_reinstalls_without_bin_conflicts(repo_root: Path):
    """The mmdc wrapper repair must not depend on npm bin-conflict behavior.

    The rename before the repair keeps a fresh install from running twice.
    The conflict guard removes a non-symlink wrapper before the repair
    install, so npm's bin linking never sees a colliding regular file.
    """

    script = (repo_root / "lima/provision-tools.sh").read_text()

    ensure_call = "ensure_npm_package mermaid_cli @mermaid-js/mermaid-cli"
    rename = 'mv -f "$HOME/.local/bin/mmdc" "$HOME/.local/bin/mmdc-bin"'
    repair_install = '"@mermaid-js/mermaid-cli@$(manifest_version mermaid_cli)"'
    conflict_guard = '[[ -L "$HOME/.local/bin/mmdc" ]] || rm -f "$HOME/.local/bin/mmdc"'

    assert script.count(rename) == 2
    assert conflict_guard in script
    assert script.index(ensure_call) < script.index(rename)
    assert script.index(rename) < script.index(repair_install)
    assert script.index(conflict_guard) < script.index(repair_install)
    # The repair must retry the rename after its reinstall.
    assert script.index(repair_install) < script.rindex(rename)


@pytest.mark.unit
def test_mermaid_wrapper_respects_a_caller_supplied_browser(repo_root: Path):
    script = (repo_root / "lima/provision-tools.sh").read_text()

    caller_check = 'if [[ -n "${PUPPETEER_EXECUTABLE_PATH:-}" ]]; then'
    wrapper_export = 'export PUPPETEER_EXECUTABLE_PATH="$chrome"'

    assert caller_check in script
    assert wrapper_export in script
    assert script.index(caller_check) < script.index(wrapper_export)


@pytest.mark.unit
def test_toolbuilder_pins_rustup_auto_self_update(repo_root: Path):
    tool_script = (repo_root / "lima/provision-tools.sh").read_text()
    system_script = (repo_root / "lima/provision-system.sh").read_text()
    persisted_setting_line = (
        'if ! "$HOME/.cargo/bin/rustup" set auto-self-update disable; then'
    )
    stamp_block_end = '  mv -f "$rustup_stamp.new" "$rustup_stamp"\nfi'
    toolchain_list = "if ! rustup toolchain list"
    toolchain_install = (
        'rustup toolchain install "$rust_version" --profile minimal --no-self-update'
    )

    assert persisted_setting_line in tool_script.splitlines()
    assert stamp_block_end in tool_script
    assert toolchain_list in tool_script
    assert toolchain_install in tool_script
    setting_position = tool_script.index(persisted_setting_line)
    assert tool_script.index(stamp_block_end) < setting_position
    assert setting_position < tool_script.index(toolchain_list)
    assert setting_position < tool_script.index(toolchain_install)
    assert "RUSTUP_AUTO_SELF_UPDATE" not in tool_script
    assert "RUSTUP_AUTO_SELF_UPDATE" not in system_script


@pytest.mark.unit
def test_lima_toolchain_check_requires_a_version_command_for_each_consumer(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest["tools"]["unmapped_lima_tool"] = {
        "version": "1.2.3",
        "consumers": {"lima": True},
    }
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)

    _, errors = check_toolchain(manifest_path, _successful_runner(manifest))

    assert any(
        "no version check is defined for Lima tool 'unmapped_lima_tool'" in error
        for error in errors
    )


@pytest.mark.unit
def test_lima_toolchain_check_reports_operator_tool_failures(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    successful = _successful_runner(manifest)

    def runner(command):
        if command[0] == "make":
            return subprocess.CompletedProcess(
                command, 127, stdout="", stderr="missing"
            )
        return successful(command)

    _, errors = check_toolchain(manifest_path, runner)

    assert any("make: make --version exited 127" in error for error in errors)


_C_CPP_TOOLCHAIN = ("gcc", "g++", "make", "cmake", "gdb", "ninja", "pkg-config")


@pytest.mark.unit
def test_lima_toolchain_check_reports_the_c_cpp_toolchain(
    repo_root: Path, tmp_path: Path
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    successful = _successful_runner(manifest)
    commands: list[tuple[str, ...]] = []

    def runner(command):
        commands.append(tuple(command))
        return successful(command)

    successes, errors = check_toolchain(manifest_path, runner)

    assert errors == []
    reported = {line.split(":", 1)[0] for line in successes}
    assert set(_C_CPP_TOOLCHAIN) <= reported
    assert set(_C_CPP_TOOLCHAIN) <= {command[0] for command in commands}


@pytest.mark.parametrize("tool", _C_CPP_TOOLCHAIN)
@pytest.mark.unit
def test_lima_toolchain_check_reports_missing_c_cpp_tool(
    repo_root: Path, tmp_path: Path, tool: str
):
    manifest = _manifest(repo_root)
    manifest_path = _write_manifest(tmp_path / "manifest.json", manifest)
    successful = _successful_runner(manifest)

    def runner(command):
        if command[0] == tool:
            raise FileNotFoundError(tool)
        return successful(command)

    successes, errors = check_toolchain(manifest_path, runner)

    assert not any(line.startswith(f"{tool}:") for line in successes)
    assert any(
        error.startswith(f"{tool}:") and "could not run" in error for error in errors
    )
