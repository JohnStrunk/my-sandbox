import json
import subprocess
from pathlib import Path

import pytest

from lima.check_toolchain import _VERSION_COMMANDS, check_toolchain


def _manifest(repo_root: Path) -> dict:
    return json.loads((repo_root / "lima/tool-versions.json").read_text())


def _write_manifest(path: Path, manifest: dict) -> Path:
    path.write_text(json.dumps(manifest))
    return path


def _version_output(name: str, version: str) -> str:
    if name == "kubectl":
        return json.dumps({"clientVersion": {"gitVersion": f"v{version}"}})
    if name == "krew":
        return f"OPTION            VALUE\nGitTag            v{version}\n"
    if name == "go":
        return f"go version go{version} linux/amd64"
    if name == "rust":
        return f"rustc {version} (commit 48a229cea 2026-09-01)"
    if name == "ruby":
        return f"ruby {version} (2026-01-01) [x86_64-linux]"
    if name == "kind":
        return f"kind v{version} go1.27.1 linux/amd64"
    if name == "minikube":
        return f"minikube version: v{version}\ncommit: abcdef0123456789"
    if name == "node":
        return f"v{version}"
    if name == "docker_ce":
        return f"Docker version {version}, build 0000000"
    if name == "containerd_io":
        return f"containerd containerd v{version} deadbeef"
    if name == "docker_buildx_plugin":
        return f"github.com/docker/buildx v{version} deadbeef"
    if name == "pre_commit":
        return f"pre-commit {version}"
    if name in {"markdownlint_cli2", "opencode"}:
        return f"{name} v{version}"
    return f"{name} {version}"


def _successful_runner(manifest: dict, overrides: dict[str, str] | None = None):
    command_names = {command: name for name, (command, _) in _VERSION_COMMANDS.items()}
    versions = {
        name: spec["version"].removeprefix("v")
        for name, spec in manifest["tools"].items()
        if isinstance(spec.get("consumers"), dict)
        and spec["consumers"].get("lima") is True
    }
    overrides = overrides or {}

    def run(command):
        command = tuple(command)
        executable = command[0]
        name = command_names.get(command)
        if name:
            output = _version_output(name, overrides.get(name, versions[name]))
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
    runner = _successful_runner(manifest, {"kind": "0.0.1"})

    _, errors = check_toolchain(manifest_path, runner)

    assert any(
        "kind: installed version 0.0.1, expected 0.33.0" in error for error in errors
    )


@pytest.mark.parametrize(
    "tool_name",
    ["minikube", "docker_ce", "containerd_io", "docker_buildx_plugin"],
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
