"""Verify the command-line sanitized test wrapper (issue #150)."""

import fcntl
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import _wait_pid_gone, run_in_process_group


def _run_wrapper(
    repo_root: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return run_in_process_group(
        [str(repo_root / "scripts" / "sanitized-test.sh"), *args],
        timeout=30,
        env=env,
        cwd=repo_root,
    )


@pytest.mark.unit
def test_wrapper_scrubs_host_environment(repo_root: Path, tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "host-home"),
            "GH_TOKEN": "host-secret-token",  # pragma: allowlist secret
            "AWS_CONFIG_FILE": str(tmp_path / "credentials"),
            "UNSAFE_TEST_VARIABLE": "must-not-cross-boundary",
        }
    )
    result = _run_wrapper(
        repo_root,
        [
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert child_env["HOME"] != env["HOME"]
    assert child_env["XDG_CONFIG_HOME"] != env.get("XDG_CONFIG_HOME")
    assert child_env["XDG_RUNTIME_DIR"] != env.get("XDG_RUNTIME_DIR")
    assert "GH_TOKEN" not in child_env
    assert "TAVILY_API_KEY" not in child_env
    assert "AWS_CONFIG_FILE" not in child_env
    assert "UNSAFE_TEST_VARIABLE" not in child_env


@pytest.mark.unit
def test_guest_vm_marker_does_not_forward_runtime_sockets_or_credentials(
    repo_root: Path,
) -> None:
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"
    env["DOCKER_HOST"] = "unix:///run/user/1000/podman/podman.sock"
    env["GH_TOKEN"] = "mock-guest-token"  # pragma: allowlist secret

    result = _run_wrapper(
        repo_root,
        [
            "--guest-vm",
            "--",
            sys.executable,
            "-c",
            (
                "import json, os; print(json.dumps({key: os.environ.get(key) "
                "for key in ('HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', "
                "'DOCKER_HOST', 'MY_SANDBOX_VM_TEST_IN_GUEST', 'GH_TOKEN')}))"
            ),
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert child_env["HOME"] != env["HOME"]
    assert child_env["XDG_RUNTIME_DIR"] != "/run/user/1000"
    assert child_env["DBUS_SESSION_BUS_ADDRESS"] is None
    assert child_env["DOCKER_HOST"] is None
    assert child_env["MY_SANDBOX_VM_TEST_IN_GUEST"] == "1"
    assert child_env["GH_TOKEN"] is None


@pytest.mark.unit
def test_wrapper_passes_only_vm_timeout_tuning(repo_root: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DEVBOX_VM_START_TIMEOUT": "678.5",
            "MY_SANDBOX_VM_TEST_FRESH": "1",
            "UNRELATED_BUILD_TIMEOUT": "123.5",
            "GH_TOKEN": "mock-timeout-token",  # pragma: allowlist secret
        }
    )

    result = _run_wrapper(
        repo_root,
        [
            "--",
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert child_env["DEVBOX_VM_START_TIMEOUT"] == "678.5"
    assert child_env["MY_SANDBOX_VM_TEST_FRESH"] == "1"
    assert "UNRELATED_BUILD_TIMEOUT" not in child_env
    assert "GH_TOKEN" not in child_env


@pytest.mark.unit
@pytest.mark.parametrize(
    "mount_type",
    ["9p", "virtiofs", "", None],
    ids=["9p", "virtiofs", "set-empty", "unset"],
)
def test_wrapper_preserves_mount_type_runner_control(
    repo_root: Path, mount_type: str | None
) -> None:
    env = os.environ.copy()
    if mount_type is None:
        env.pop("DEVBOX_VM_TEST_MOUNT_TYPE", None)
    else:
        env["DEVBOX_VM_TEST_MOUNT_TYPE"] = mount_type

    result = _run_wrapper(
        repo_root,
        [
            "--",
            sys.executable,
            "-c",
            (
                "import json, os; print(json.dumps({"
                "'is_set': 'DEVBOX_VM_TEST_MOUNT_TYPE' in os.environ, "
                "'value': os.environ.get('DEVBOX_VM_TEST_MOUNT_TYPE')}))"
            ),
        ],
        env,
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    assert child_env["is_set"] is (mount_type is not None)
    assert child_env["value"] == mount_type


@pytest.mark.unit
def test_wrapper_rejects_retired_podman_options(repo_root: Path) -> None:
    env = os.environ.copy()

    result = _run_wrapper(repo_root, ["--require-podman", "--", "true"], env)

    assert result.returncode == 2
    assert "unknown option: --require-podman" in result.stderr


@pytest.mark.unit
def test_wrapper_stops_before_test_command_on_vm_capability_limit(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    preflight_env = tmp_path / "preflight-env"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        f"env > {shlex.quote(str(preflight_env))}\n"
        "printf '%s\\n' "
        "'vm-preflight: infrastructure limitation: /dev/kvm unavailable' >&2\n"
        "exit 125\n"
    )
    fake_python.chmod(0o755)
    marker = tmp_path / "command-ran"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["GH_TOKEN"] = "mock-preflight-token"  # pragma: allowlist secret
    env["PYTHONPATH"] = str(tmp_path / "untrusted-python")

    result = _run_wrapper(
        repo_root,
        ["--require-vm", "--", "touch", str(marker)],
        env,
    )

    assert result.returncode == 125
    assert "infrastructure limitation" in result.stderr
    assert not marker.exists()
    preflight_environment = preflight_env.read_text()
    assert "GH_TOKEN" not in preflight_environment
    assert "PYTHONPATH" not in preflight_environment


@pytest.mark.unit
def test_recursive_vm_preflight_receives_recursive_flag(
    repo_root: Path, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "preflight-args"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {shlex.quote(str(args_file))}\n"
    )
    fake_python.chmod(0o755)
    marker = tmp_path / "command-ran"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = _run_wrapper(
        repo_root,
        ["--require-recursive-vm", "--", "touch", str(marker)],
        env,
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert "--recursive" in args_file.read_text().splitlines()


@pytest.mark.unit
def test_wrapper_propagates_command_status(repo_root: Path) -> None:
    result = _run_wrapper(
        repo_root,
        ["--", sys.executable, "-c", "raise SystemExit(23)"],
        os.environ.copy(),
    )

    assert result.returncode == 23
    assert result.stderr == ""


@pytest.mark.unit
def test_wrapper_serializes_commands_with_the_shared_vm_lock(
    repo_root: Path, tmp_path: Path
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    host_tmp = tmp_path / "host-tmp"
    host_tmp.mkdir()
    active = tmp_path / "command-active"
    overlap = tmp_path / "commands-overlapped"
    first_started = tmp_path / "first-started"
    second_started = tmp_path / "second-started"
    release_first = tmp_path / "release-first"
    first_stderr_path = tmp_path / "first.stderr"
    second_stderr_path = tmp_path / "second.stderr"
    env = os.environ.copy()
    env.update({"HOME": str(host_home), "TMPDIR": str(host_tmp)})

    def launch(started: Path, stderr_path: Path) -> subprocess.Popen[str]:
        command = (
            "import os, pathlib, time\n"
            f"active = pathlib.Path({str(active)!r})\n"
            f"overlap = pathlib.Path({str(overlap)!r})\n"
            f"started = pathlib.Path({str(started)!r})\n"
            f"release_first = pathlib.Path({str(release_first)!r})\n"
            "try:\n"
            "    fd = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)\n"
            "except FileExistsError:\n"
            "    owner = False\n"
            "    overlap.touch()\n"
            "else:\n"
            "    owner = True\n"
            "    os.close(fd)\n"
            "started.write_text('started')\n"
            "while not release_first.exists():\n"
            "    time.sleep(0.01)\n"
            "if owner:\n"
            "    active.unlink()\n"
        )
        with stderr_path.open("w") as stderr_file:
            return subprocess.Popen(
                [
                    str(repo_root / "scripts" / "sanitized-test.sh"),
                    "--vm-lock",
                    "--vm-lock-timeout",
                    "30",
                    "--",
                    sys.executable,
                    "-c",
                    command,
                ],
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
                cwd=repo_root,
                env=env,
            )

    processes: list[subprocess.Popen[str]] = []
    try:
        first = launch(first_started, first_stderr_path)
        processes.append(first)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not first_started.exists():
            assert first.poll() is None, "first command exited before it started"
            time.sleep(0.025)
        assert first_started.exists(), "first command never started"

        second = launch(second_started, second_stderr_path)
        processes.append(second)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert second.poll() is None, (
                "second wrapper exited while the first command held the lock: "
                f"{second_stderr_path.read_text()}"
            )
            stderr_text = second_stderr_path.read_text()
            if "waiting for shared-VM test lock" in stderr_text:
                break
            time.sleep(0.025)
        else:
            raise AssertionError("second wrapper never reached the shared-VM lock")

        settle_deadline = time.monotonic() + 1
        while time.monotonic() < settle_deadline:
            stderr_text = second_stderr_path.read_text()
            assert "acquired shared-VM test lock" not in stderr_text, (
                "second wrapper acquired the shared-VM lock before "
                "the first released it"
            )
            assert not second_started.exists(), (
                "second command started before lock release"
            )
            time.sleep(0.025)

        release_first.touch()
        first_stdout, _ = first.communicate(timeout=60)
        second_stdout, _ = second.communicate(timeout=60)
        first_stderr = first_stderr_path.read_text()
        second_stderr = second_stderr_path.read_text()

        assert first.returncode == 0, first_stderr or first_stdout
        assert second.returncode == 0, second_stderr or second_stdout
        assert second_started.exists(), "second command never started"
        assert not overlap.exists(), "shared-VM commands ran concurrently"
        assert "waiting for shared-VM test lock" in second_stderr
        assert "acquired shared-VM test lock" in second_stderr
        cache_dir = host_home / ".cache"
        lock_file = cache_dir / "my-sandbox-vm-tests.lock"
        assert lock_file.is_file()
        assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    finally:
        for proc in processes:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
                proc.communicate()


@pytest.mark.unit
def test_wrapper_vm_lock_timeout_is_bounded_and_skips_the_command(
    repo_root: Path, tmp_path: Path
) -> None:
    host_home = tmp_path / "host-home"
    cache_dir = host_home / ".cache"
    cache_dir.mkdir(parents=True)
    lock_path = cache_dir / "my-sandbox-vm-tests.lock"
    marker = tmp_path / "command-ran"
    env = os.environ.copy()
    env["HOME"] = str(host_home)

    with lock_path.open("w") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        result = _run_wrapper(
            repo_root,
            [
                "--vm-lock",
                "--vm-lock-timeout",
                "1",
                "--",
                "touch",
                str(marker),
            ],
            env,
        )
        elapsed = time.monotonic() - start

    assert result.returncode == 125, result.stderr
    assert "timed out after 1s waiting for shared-VM test lock" in result.stderr
    assert 0.8 <= elapsed < 5, f"lock timeout took {elapsed:.2f}s"
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_rejects_invalid_inherited_vm_lock_descriptor(
    repo_root: Path, tmp_path: Path
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    marker = tmp_path / "command-ran"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(host_home),
            "MY_SANDBOX_VM_TEST_LOCK_FD": "0",
        }
    )

    result = _run_wrapper(
        repo_root,
        ["--vm-lock", "--", "touch", str(marker)],
        env,
    )

    assert result.returncode == 125, result.stderr
    assert "invalid inherited shared-VM lock descriptor" in result.stderr
    assert not marker.exists()


@pytest.mark.unit
def test_wrapper_rejects_vm_lock_timeout_above_one_day(repo_root: Path) -> None:
    result = _run_wrapper(
        repo_root,
        ["--vm-lock", "--vm-lock-timeout", "86401", "--", "true"],
        os.environ.copy(),
    )

    assert result.returncode == 2
    assert "must be an integer from 0 to 86400" in result.stderr


@pytest.mark.unit
def test_wrapper_rejects_symlinked_vm_lock_without_touching_target(
    repo_root: Path, tmp_path: Path
) -> None:
    host_home = tmp_path / "host-home"
    cache_dir = host_home / ".cache"
    cache_dir.mkdir(parents=True, mode=0o700)
    target = tmp_path / "unrelated-file"
    target.write_text("preserve this content")
    lock_path = cache_dir / "my-sandbox-vm-tests.lock"
    lock_path.symlink_to(target)
    env = os.environ.copy()
    env["HOME"] = str(host_home)

    result = _run_wrapper(repo_root, ["--vm-lock", "--", "true"], env)

    assert result.returncode == 125
    assert "must be a regular file, not a symlink" in result.stderr
    assert target.read_text() == "preserve this content"


@pytest.mark.unit
@pytest.mark.parametrize("target_exists", [True, False], ids=["existing", "dangling"])
def test_lock_opener_rejects_symlink_without_touching_target(
    repo_root: Path, tmp_path: Path, target_exists: bool
) -> None:
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir(mode=0o700)
    target = tmp_path / "unrelated-file"
    if target_exists:
        target.write_text("preserve this content")
    (cache_dir / "my-sandbox-vm-tests.lock").symlink_to(target)

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(repo_root / "scripts" / "open_vm_test_lock.py"),
            "--lock-directory",
            str(cache_dir),
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 125, result.stderr
    assert "must be a regular file, not a symlink" in result.stderr
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_text() == "preserve this content"


@pytest.mark.unit
def test_lock_opener_rejects_hardlink_without_changing_target_permissions(
    repo_root: Path, tmp_path: Path
) -> None:
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir(mode=0o700)
    target = tmp_path / "unrelated-file"
    target.write_text("preserve this content")
    target.chmod(0o644)
    os.link(target, cache_dir / "my-sandbox-vm-tests.lock")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(repo_root / "scripts" / "open_vm_test_lock.py"),
            "--lock-directory",
            str(cache_dir),
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 125, result.stderr
    assert "with one link" in result.stderr
    assert target.read_text() == "preserve this content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.unit
def test_wrapper_rejects_group_writable_vm_lock_directory(
    repo_root: Path, tmp_path: Path
) -> None:
    host_home = tmp_path / "host-home"
    cache_dir = host_home / ".cache"
    cache_dir.mkdir(parents=True)
    cache_dir.chmod(0o777)
    env = os.environ.copy()
    env["HOME"] = str(host_home)

    result = _run_wrapper(repo_root, ["--vm-lock", "--", "true"], env)

    assert result.returncode == 125
    assert "not group/world-writable" in result.stderr
    assert not (cache_dir / "my-sandbox-vm-tests.lock").exists()


@pytest.mark.unit
def test_wrapper_rejects_writable_nonsticky_home_ancestor(
    repo_root: Path, tmp_path: Path
) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o777)
    host_home = unsafe_parent / "host-home"
    host_home.mkdir(mode=0o700)
    env = os.environ.copy()
    env["HOME"] = str(host_home)

    result = _run_wrapper(repo_root, ["--vm-lock", "--", "true"], env)

    assert result.returncode == 125
    assert "original HOME ancestor is group/world-writable" in result.stderr
    assert not (host_home / ".cache").exists()


@pytest.mark.unit
def test_wrapper_does_not_let_command_descendant_retain_vm_lock(
    repo_root: Path, tmp_path: Path
) -> None:
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    child_pidfile = tmp_path / "child.pid"
    env = os.environ.copy()
    env["HOME"] = str(host_home)
    command = (
        "import pathlib, subprocess\n"
        "child = subprocess.Popen(\n"
        "    ['sleep', '10'],\n"
        "    close_fds=False,\n"
        "    start_new_session=True,\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        f"pathlib.Path({str(child_pidfile)!r}).write_text(str(child.pid))\n"
    )

    first = _run_wrapper(
        repo_root,
        ["--vm-lock", "--", sys.executable, "-c", command],
        env,
    )
    assert first.returncode == 0, first.stderr
    assert child_pidfile.exists()
    child_pid = int(child_pidfile.read_text())

    try:
        second = _run_wrapper(
            repo_root,
            ["--vm-lock", "--vm-lock-timeout", "1", "--", "true"],
            env,
        )
        assert second.returncode == 0, second.stderr
        assert "acquired shared-VM test lock" in second.stderr
    finally:
        try:
            os.kill(child_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        assert _wait_pid_gone(child_pid), (
            f"background test child {child_pid} survived lock-fd validation"
        )


@pytest.mark.unit
@pytest.mark.unit_serial
def test_wrapper_sigint_exits_fast_with_command_cleanup(
    repo_root: Path,
    tmp_path: Path,
    shared_process_signal_test_lock: None,
) -> None:
    cleanup_marker = tmp_path / "int-cleanup-ran"
    ready_marker = tmp_path / "int-command-ready"
    command = (
        "trap 'printf cleanup > " + shlex.quote(str(cleanup_marker)) + "' INT\n"
        "(sleep 0.1; printf ready > " + shlex.quote(str(ready_marker)) + ") &\n"
        "sleep 30\n"
    )
    with (tmp_path / "out").open("w") as out, (tmp_path / "err").open("w") as err:
        proc = subprocess.Popen(
            [
                str(repo_root / "scripts" / "sanitized-test.sh"),
                "--",
                "bash",
                "-c",
                command,
            ],
            stdout=out,
            stderr=err,
            start_new_session=True,
            cwd=repo_root,
            env=os.environ.copy(),
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready_marker.exists():
            assert proc.poll() is None, (
                "command exited before the signal was sent: "
                f"{(tmp_path / 'err').read_text()}"
            )
            time.sleep(0.05)
        assert ready_marker.exists(), "command never became ready"

        start = time.monotonic()
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
        elapsed = time.monotonic() - start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert proc.returncode == 130, (tmp_path / "err").read_text()
    assert cleanup_marker.exists(), "command INT trap never ran"
    assert elapsed < 5, f"graceful INT took {elapsed:.1f}s to shut down"


@pytest.mark.unit
@pytest.mark.unit_serial
def test_wrapper_sigterm_terminates_command_tree(
    repo_root: Path,
    tmp_path: Path,
    shared_process_signal_test_lock: None,
) -> None:
    """A SIGTERM-ignoring command tree must not outlive the wrapper."""
    pidfile = tmp_path / "command-pids"
    command = (
        "trap '' TERM INT\n"
        "(trap '' TERM INT; exec sleep 600) &\n"
        f"printf '%s\\n%s\\n' $$ $! > {shlex.quote(str(pidfile))}\n"
        "wait\n"
    )
    stdout_path = tmp_path / "wrapper.stdout"
    stderr_path = tmp_path / "wrapper.stderr"
    with stdout_path.open("w") as out, stderr_path.open("w") as err:
        proc = subprocess.Popen(
            [
                str(repo_root / "scripts" / "sanitized-test.sh"),
                "--",
                "bash",
                "-c",
                command,
            ],
            stdout=out,
            stderr=err,
            start_new_session=True,
            cwd=repo_root,
            env=os.environ.copy(),
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if pidfile.exists():
                break
            assert proc.poll() is None, "wrapper exited before the command started"
            time.sleep(0.05)
        assert pidfile.exists(), "command never started before the signal"
        command_pid, child_pid = (int(value) for value in pidfile.read_text().split())

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    stderr = stderr_path.read_text()
    assert proc.returncode == 143, stderr
    assert "received SIGTERM" in stderr
    assert "terminating the command process group" in stderr
    assert _wait_pid_gone(command_pid), (
        f"command pid {command_pid} survived the wrapper interruption"
    )
    assert _wait_pid_gone(child_pid), (
        f"SIGTERM-ignoring child {child_pid} survived the wrapper interruption"
    )


@pytest.mark.unit
@pytest.mark.unit_serial
def test_wrapper_sigterm_terminates_detached_session_commands(
    repo_root: Path,
    tmp_path: Path,
    shared_process_signal_test_lock: None,
) -> None:
    """The suite runner must reap detached child sessions on interruption."""
    command_pidfile = tmp_path / "detached-command.pid"
    suite_mock = tmp_path / "suite-mock.py"
    suite_mock.write_text(
        "import sys\n"
        "import threading\n"
        "import time\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from tests.conftest import (\n"
        "    install_termination_handlers,\n"
        "    run_in_process_group,\n"
        ")\n"
        "install_termination_handlers()\n"
        "def start_wedge():\n"
        "    run_in_process_group(\n"
        "        ['bash', '-c', \"trap '' TERM; "
        f"printf '%s\\\\n' $$ > {str(command_pidfile)!r}; "
        "(trap '' TERM; exec sleep 600) & wait\"],\n"
        "        timeout=600,\n"
        "    )\n"
        "worker = threading.Thread(target=start_wedge, daemon=True)\n"
        "worker.start()\n"
        "while not __import__('os').path.exists("
        f"{str(command_pidfile)!r}):\n"
        "    time.sleep(0.05)\n"
        "print('READY', flush=True)\n"
        "time.sleep(600)\n"
    )
    stdout_path = tmp_path / "suite.stdout"
    stderr_path = tmp_path / "suite.stderr"
    with stdout_path.open("w") as out, stderr_path.open("w") as err:
        proc = subprocess.Popen(
            [
                str(repo_root / "scripts" / "sanitized-test.sh"),
                "--",
                sys.executable,
                str(suite_mock),
            ],
            stdout=out,
            stderr=err,
            start_new_session=True,
            cwd=repo_root,
            env=os.environ.copy(),
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not command_pidfile.exists():
            assert proc.poll() is None, "wrapper exited before the command started"
            time.sleep(0.05)
        assert command_pidfile.exists(), "detached command never started"
        command_pid = int(command_pidfile.read_text().strip())

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    stderr = stderr_path.read_text()
    assert proc.returncode == 143, stderr
    assert "received SIGTERM" in stderr
    assert "terminating the command process group" in stderr
    assert _wait_pid_gone(command_pid), (
        f"detached command {command_pid} survived the wrapper interruption"
    )
