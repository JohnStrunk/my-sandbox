"""Regression tests for the bounded process-group command runner (issue #211).

A timed-out command must terminate its direct AND descendant processes, so
child Podman/buildah builds cannot leak past the timeout, while normal runs
keep their exact output and exit-status semantics.
"""

import errno
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import (
    _open_shared_process_signal_test_lock,
    _terminate_process_group,
    _wait_for_shared_process_signal_test_lock,
    _wait_pid_gone,
    run_bash_script,
    run_in_process_group,
)


@pytest.mark.unit
@pytest.mark.parametrize("entry_type", ["symlink", "hardlink"])
def test_shared_process_signal_lock_skips_unsafe_entries(
    tmp_path: Path, entry_type: str
) -> None:
    lock_directory = tmp_path / f"my-sandbox-process-signals-{os.getuid()}"
    lock_directory.mkdir(mode=0o700)
    target = tmp_path / "unrelated-file"
    target.write_text("preserve this content")
    target.chmod(0o644)
    lock_path = lock_directory / "process-signals.lock"
    if entry_type == "symlink":
        lock_path.symlink_to(target)
    else:
        os.link(target, lock_path)

    with pytest.raises(pytest.skip.Exception, match="infrastructure limitation"):
        _open_shared_process_signal_test_lock(tmp_path)

    assert target.read_text() == "preserve this content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.unit
def test_shared_process_signal_lock_timeout_is_infrastructure_skip() -> None:
    times = iter((0.0, 1.0))

    def lock_is_held(_fd: int, _operation: int) -> None:
        raise BlockingIOError

    with pytest.raises(pytest.skip.Exception, match="infrastructure limitation"):
        _wait_for_shared_process_signal_test_lock(
            0,
            Path("/tmp/test-process-signal.lock"),
            timeout=1,
            try_lock=lock_is_held,
            monotonic=lambda: next(times),
            sleep=lambda _seconds: None,
        )


@pytest.mark.unit
def test_shared_process_signal_lock_unavailable_is_infrastructure_skip() -> None:
    def lock_unavailable(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "locking is unavailable")

    with pytest.raises(pytest.skip.Exception, match="lock is unavailable"):
        _wait_for_shared_process_signal_test_lock(
            0,
            Path("/tmp/test-process-signal.lock"),
            try_lock=lock_unavailable,
        )


@pytest.mark.unit
def test_timed_out_command_terminates_long_lived_child(tmp_path: Path):
    script = tmp_path / "spawner.sh"
    pidfile = tmp_path / "child.pid"
    script.write_text('#!/bin/bash\nsleep 300 &\necho "$!" > "$1"\nwait\n')
    script.chmod(0o755)

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_bash_script(script, [str(pidfile)], timeout=5)

    assert excinfo.value.process_group_cleanup == ""
    assert pidfile.exists(), "child never started before the timeout"
    child_pid = int(pidfile.read_text().strip())
    assert _wait_pid_gone(child_pid), (
        f"descendant process {child_pid} survived the timeout cleanup"
    )


@pytest.mark.unit
def test_normal_run_preserves_output_and_exit_status():
    result = run_in_process_group(
        ["bash", "-c", "echo to-stdout; echo to-stderr >&2; exit 3"],
        timeout=15,
    )

    assert result.returncode == 3
    assert result.stdout == "to-stdout\n"
    assert result.stderr == "to-stderr\n"


@pytest.mark.unit
def test_run_bash_script_normal_path(tmp_path: Path):
    script = tmp_path / "greet.sh"
    script.write_text('#!/bin/bash\necho "args: $*"\n')
    script.chmod(0o755)

    result = run_bash_script(script, ["one", "two"], timeout=15)

    assert result.returncode == 0
    assert result.stdout == "args: one two\n"


@pytest.mark.unit
def test_cleanup_failure_is_reported(monkeypatch):
    proc = subprocess.Popen(
        ["sleep", "300"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    def failing_killpg(pgid: int, sig: int) -> None:
        raise OSError("simulated kill failure")

    monkeypatch.setattr("tests.conftest.os.killpg", failing_killpg)
    try:
        error = _terminate_process_group(proc, term_grace=0.1, kill_grace=0.1)
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.communicate()

    assert "SIGTERM" in error
    assert "simulated kill failure" in error
    assert "SIGKILL" in error


@pytest.mark.unit
def test_terminate_process_group_clean_result():
    proc = subprocess.Popen(
        ["sleep", "300"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    error = _terminate_process_group(proc)

    assert error == ""
    assert proc.poll() is not None


@pytest.mark.unit
@pytest.mark.unit_serial
def test_sigterm_handler_kills_tracked_session_groups(
    repo_root: Path,
    tmp_path: Path,
    shared_process_signal_test_lock: None,
) -> None:
    """The suite's SIGTERM handler must kill builds in their own sessions.

    `run_in_process_group` gives every command its own session, so a wedged
    build survives any signal aimed at the suite's own process group. When
    the suite process itself is terminated, the handler installed by
    `pytest_sessionstart` must SIGKILL the tracked groups first (issue #252)
    instead of letting them orphan.
    """
    build_pidfile = tmp_path / "detached-build.pid"
    suite_mock = tmp_path / "suite-mock.py"
    suite_mock.write_text(
        "import os\n"
        "import sys\n"
        "import threading\n"
        "import time\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from tests.conftest import (\n"
        "    install_termination_handlers,\n"
        "    run_in_process_group,\n"
        ")\n"
        "\n"
        "install_termination_handlers()\n"
        "\n"
        "\n"
        "def start_wedge() -> None:\n"
        "    run_in_process_group(\n"
        "        [\n"
        "            'bash',\n"
        "            '-c',\n"
        "            \"trap '' TERM; printf '%s\\\\n' $$ > "
        f"{str(build_pidfile)!r}; "
        "(trap '' TERM; exec sleep 600) & wait\",\n"
        "        ],\n"
        "        timeout=600,\n"
        "    )\n"
        "\n"
        "\n"
        "worker = threading.Thread(target=start_wedge, daemon=True)\n"
        "worker.start()\n"
        f"while not os.path.exists({str(build_pidfile)!r}):\n"
        "    time.sleep(0.05)\n"
        "time.sleep(600)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(suite_mock)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=repo_root,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not build_pidfile.exists():
            assert proc.poll() is None, f"suite mock exited early: {proc.stderr.read()}"
            time.sleep(0.05)
        assert build_pidfile.exists(), "detached build never started"
        build_pid = int(build_pidfile.read_text().strip())

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # The handler re-delivers SIGTERM with the default disposition, so the
    # suite process itself dies by the signal.
    assert proc.returncode == -signal.SIGTERM
    assert _wait_pid_gone(build_pid), (
        f"detached-session build {build_pid} survived the suite's termination"
    )
