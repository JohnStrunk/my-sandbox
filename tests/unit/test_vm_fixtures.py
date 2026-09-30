import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.conftest import (
    DEFAULT_VM_START_TIMEOUT,
    VM_START_TIMEOUT_ENV_VAR,
    LimaVM,
    _cleanup_private_lima_home,
    copy_repository_for_vm,
    guest_runtime_environment,
    lima_vm_start_command,
    vm_start_timeout,
    vm_test_environment,
)


@pytest.mark.unit
def test_vm_test_environment_contains_only_runtime_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "mock-credential-value"  # pragma: allowlist secret
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("UNRELATED_HOST_SETTING", "must-not-pass")
    monkeypatch.setenv(VM_START_TIMEOUT_ENV_VAR, "900")
    monkeypatch.setenv("CI", "true")

    env = vm_test_environment(tmp_path)

    assert env["HOME"] == str(tmp_path)
    assert env["LIMA_HOME"] == str(tmp_path / ".lima")
    assert env[VM_START_TIMEOUT_ENV_VAR] == "900"
    assert env["CI"] == "true"
    assert "GH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "UNRELATED_HOST_SETTING" not in env
    assert (tmp_path / "run").stat().st_mode & 0o777 == 0o700


@pytest.mark.unit
def test_guest_runtime_allowlist_contains_only_unix_socket_endpoints(
    tmp_path: Path,
):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "bus").touch()

    runtime_env = guest_runtime_environment(runtime_dir)

    assert runtime_env == {
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir / 'bus'}",
    }
    assert "GH_TOKEN" not in runtime_env


@pytest.mark.unit
@pytest.mark.parametrize("value", ["invalid", "0", "-1", "nan", "inf"])
def test_vm_start_timeout_uses_safe_default_for_invalid_values(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv(VM_START_TIMEOUT_ENV_VAR, value)

    assert vm_start_timeout() == DEFAULT_VM_START_TIMEOUT


@pytest.mark.unit
def test_vm_start_timeout_accepts_positive_seconds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(VM_START_TIMEOUT_ENV_VAR, "600.5")

    assert vm_start_timeout() == 600.5


@pytest.mark.unit
def test_vm_repository_copy_excludes_ignored_secrets_and_external_symlinks(
    tmp_path: Path,
):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    (source / ".gitignore").write_text(".env\n")
    (source / "tracked.txt").write_text("tracked source\n")
    subprocess.run(
        ["git", "-C", str(source), "add", ".gitignore", "tracked.txt"],
        check=True,
    )
    (source / ".env").write_text("local credential must stay outside the guest\n")
    (source / "working-tree-change.py").write_text("print('include live edits')\n")
    outside = tmp_path / "outside-credential"
    outside.write_text("not part of the VM source\n")
    (source / "external-link").symlink_to(outside)

    destination = tmp_path / "guest-source"
    copy_repository_for_vm(source, destination)

    assert (destination / "tracked.txt").read_text() == "tracked source\n"
    assert (destination / "working-tree-change.py").is_file()
    assert not (destination / ".env").exists()
    assert not (destination / "external-link").exists()
    assert not (destination / ".git").exists()


@pytest.mark.unit
def test_vm_start_command_uses_the_pinned_template_and_timeout(tmp_path: Path):
    repo = tmp_path / "repo"

    command = lima_vm_start_command(repo, "vm-test", 123.5)

    assert command[:4] == ["limactl", "start", "--yes", "--name"]
    assert "vm-test" in command
    assert "--timeout" in command
    assert "123.5s" in command
    assert "SrcPath=/workspace/src" in command
    assert f"RepoPath=/workspace/src/{repo.name}" in command
    assert command[-1] == str(repo / "lima" / "devbox.yaml")


@pytest.mark.unit
def test_lima_vm_snapshot_restore_and_clone_use_bounded_lima_calls(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tests.conftest.run_in_process_group", fake_run)
    vm = LimaVM(
        "source",
        {"PATH": "/bin"},
        "/workspace/source/repo",
        "/home/test.guest",
    )

    vm.snapshot()
    vm.restore_snapshot()
    clone = vm.clone("clean-copy")

    assert clone == "clean-copy"
    assert calls == [
        ["limactl", "snapshot", "create", "source", "--tag", "clean"],
        ["limactl", "stop", "source"],
        ["limactl", "snapshot", "apply", "source", "--tag", "clean"],
        ["limactl", "start", "source"],
        ["limactl", "clone", "source", "clean-copy", "--start"],
    ]


@pytest.mark.unit
def test_private_lima_cleanup_removes_only_instances_from_its_home(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        stdout = "test-one\ntest-two\n" if command[1:] == ["list", "-q"] else ""
        return CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("tests.conftest.run_in_process_group", fake_run)

    errors = _cleanup_private_lima_home({"PATH": "/bin", "LIMA_HOME": "/private/lima"})

    assert not errors
    assert calls == [
        ["limactl", "list", "-q"],
        ["limactl", "stop", "test-one"],
        ["limactl", "delete", "--force", "test-one"],
        ["limactl", "stop", "test-two"],
        ["limactl", "delete", "--force", "test-two"],
    ]


@pytest.mark.unit
def test_private_lima_cleanup_reports_list_failure_without_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(command, **kwargs):
        return CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("tests.conftest.run_in_process_group", fake_run)

    errors = _cleanup_private_lima_home({"PATH": "/bin", "LIMA_HOME": "/private/lima"})

    assert errors == ["limactl list failed (exit 1) without diagnostics"]


@pytest.mark.unit
def test_private_lima_cleanup_does_not_trust_partial_instance_list(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return CompletedProcess(command, 1, "possibly-live-vm\n", "partial failure")

    monkeypatch.setattr("tests.conftest.run_in_process_group", fake_run)

    errors = _cleanup_private_lima_home({"PATH": "/bin", "LIMA_HOME": "/private/lima"})

    assert errors == ["limactl list failed (exit 1): partial failure"]
    assert calls == [["limactl", "list", "-q"]]
