"""Verify the command-line sanitized test wrapper (issue #150)."""

import json
import os
import shlex
import signal
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
def test_wrapper_sigint_exits_fast_with_command_cleanup(
    repo_root: Path, tmp_path: Path
) -> None:
    cleanup_marker = tmp_path / "int-cleanup-ran"
    ready_marker = tmp_path / "int-command-ready"
    command = (
        "trap 'touch " + shlex.quote(str(cleanup_marker)) + "' INT\n"
        "touch " + shlex.quote(str(ready_marker)) + "\n"
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
def test_wrapper_sigterm_terminates_command_tree(
    repo_root: Path, tmp_path: Path
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
def test_wrapper_sigterm_terminates_detached_session_commands(
    repo_root: Path, tmp_path: Path
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
