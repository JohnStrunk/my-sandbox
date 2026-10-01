import os
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from tests.conftest import (
    DEFAULT_VM_START_TIMEOUT,
    VM_START_TIMEOUT_ENV_VAR,
    LimaVM,
    _cleanup_private_lima_home,
    _guest_opencode_scratch_problem,
    _private_lima_home,
    copy_repository_for_vm,
    guest_runtime_environment,
    lima_vm_start_command,
    pytest_terminal_summary,
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
def test_private_lima_home_is_created_under_opencode_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "opencode"
    root.mkdir()
    monkeypatch.setattr("tests.conftest._PRIVATE_LIMA_ROOT", root)
    monkeypatch.setattr(
        "tests.conftest._guest_opencode_scratch_problem", lambda path: None
    )

    lima_home = _private_lima_home(in_guest=True)

    assert lima_home.parent == root
    assert lima_home.name.startswith("lima-")
    assert lima_home.stat().st_mode & 0o777 == 0o700


@pytest.mark.unit
def test_private_lima_home_fails_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    missing_root = tmp_path / "unavailable-opencode"
    monkeypatch.setattr("tests.conftest._PRIVATE_LIMA_ROOT", missing_root)
    calls: list[dict[str, object]] = []

    def record_mkdtemp(*, prefix: str, dir: Path) -> str:
        calls.append({"prefix": prefix, "dir": dir})
        raise AssertionError("mkdtemp must not be called without the scratch root")

    monkeypatch.setattr("tests.conftest.tempfile.mkdtemp", record_mkdtemp)

    with pytest.raises(RuntimeError) as error:
        _private_lima_home(in_guest=True)

    assert str(missing_root) in str(error.value)
    assert "current user" in str(error.value)
    assert "recreate the VM" in str(error.value)
    assert calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "expected_problem"),
    [
        (0o777, "expected 01777"),
        pytest.param(
            0o1777,
            "expected root:root",
            marks=pytest.mark.skipif(
                os.getuid() == 0,
                reason="a root-owned scratch directory satisfies the owner check",
            ),
        ),
    ],
)
def test_private_lima_home_rejects_writable_untrusted_guest_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    expected_problem: str,
):
    root = tmp_path / "untrusted-opencode"
    root.mkdir(mode=mode)
    root.chmod(mode)
    monkeypatch.setattr("tests.conftest._PRIVATE_LIMA_ROOT", root)
    calls: list[dict[str, object]] = []

    def record_mkdtemp(*, prefix: str, dir: Path) -> str:
        calls.append({"prefix": prefix, "dir": dir})
        raise AssertionError("mkdtemp must not use an invalid scratch root")

    monkeypatch.setattr("tests.conftest.tempfile.mkdtemp", record_mkdtemp)

    with pytest.raises(RuntimeError) as error:
        _private_lima_home(in_guest=True)

    assert expected_problem in str(error.value)
    assert "current user" in str(error.value)
    assert calls == []


@pytest.mark.unit
def test_private_lima_home_rejects_mountpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("tests.conftest._PRIVATE_LIMA_ROOT", Path("/proc"))
    calls: list[dict[str, object]] = []

    def record_mkdtemp(*, prefix: str, dir: Path) -> str:
        calls.append({"prefix": prefix, "dir": dir})
        raise AssertionError("mkdtemp must not use a mountpoint")

    monkeypatch.setattr("tests.conftest.tempfile.mkdtemp", record_mkdtemp)

    with pytest.raises(RuntimeError) as error:
        _private_lima_home(in_guest=True)

    assert "is a mountpoint" in str(error.value)
    assert calls == []


@pytest.mark.unit
def test_guest_scratch_rejects_invalid_mountinfo_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "opencode"
    root.mkdir(mode=0o1777)
    root.chmod(0o1777)
    original_open = Path.open

    def invalid_mountinfo(self: Path, *args, **kwargs):
        if self == Path("/proc/self/mountinfo"):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", invalid_mountinfo)

    problem = _guest_opencode_scratch_problem(root)

    assert problem == "cannot decode /proc/self/mountinfo"


@pytest.mark.unit
def test_private_lima_home_uses_host_temp_outside_guest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    host_tmp = tmp_path / "host-tmp"
    host_tmp.mkdir()
    missing_guest_root = tmp_path / "unavailable-opencode"
    monkeypatch.setattr("tests.conftest._PRIVATE_LIMA_ROOT", missing_guest_root)
    monkeypatch.setattr("tests.conftest.tempfile.gettempdir", lambda: str(host_tmp))

    lima_home = _private_lima_home(in_guest=False)

    assert lima_home.parent == host_tmp
    assert lima_home.name.startswith("lima-")
    assert lima_home.stat().st_mode & 0o777 == 0o700


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


@pytest.mark.unit
def test_vm_skip_summary_handles_pytest_report_and_legacy_tuple_entries():
    class TerminalSummaryRecorder:
        stats = {
            "skipped": [
                SimpleNamespace(longrepr="VM infrastructure limitation: no KVM"),
                (SimpleNamespace(longrepr="VM infrastructure limitation: no nesting"),),
                SimpleNamespace(longrepr="optional test skipped"),
            ]
        }
        messages: list[tuple[str, str]] = []

        def write_sep(self, separator: str, message: str) -> None:
            self.messages.append((separator, message))

    reporter = TerminalSummaryRecorder()

    pytest_terminal_summary(reporter)  # type: ignore[arg-type]

    assert reporter.messages == [
        (
            "=",
            "VM infrastructure limitations: 2 test(s) skipped because required "
            "host capabilities were unavailable",
        )
    ]
