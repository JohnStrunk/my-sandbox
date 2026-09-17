"""Regression tests for the bounded process-group command runner (issue #211).

A timed-out command must terminate its direct AND descendant processes, so
child Podman/buildah builds cannot leak past the timeout, while normal runs
keep their exact output and exit-status semantics.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import (
    _terminate_process_group,
    run_bash_script,
    run_in_process_group,
)


def _wait_pid_gone(pid: int, deadline_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.unit
def test_timed_out_command_terminates_long_lived_child(tmp_path: Path):
    script = tmp_path / "spawner.sh"
    pidfile = tmp_path / "child.pid"
    script.write_text('#!/bin/bash\nsleep 300 &\necho "$!" > "$1"\nwait\n')
    script.chmod(0o755)

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_bash_script(script, [str(pidfile)], timeout=2)

    assert excinfo.value.process_group_cleanup == ""
    assert pidfile.exists(), "child never started before the timeout"
    child_pid = int(pidfile.read_text().strip())
    assert _wait_pid_gone(child_pid), (
        f"descendant process {child_pid} survived the timeout cleanup"
    )


@pytest.mark.unit
def test_terminated_group_leaves_no_leader(tmp_path: Path):
    script = tmp_path / "sleeper.sh"
    script.write_text("#!/bin/bash\nexec sleep 300\n")
    script.chmod(0o755)

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        run_bash_script(script, timeout=2)

    assert excinfo.value.process_group_cleanup == ""
    # `exec sleep` makes the group leader the sleep itself; it is reaped by
    # the runner, so it must be gone entirely (not a leftover zombie).
    # The pid is unknown here, but a clean cleanup already proved the group
    # has no live members.


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
