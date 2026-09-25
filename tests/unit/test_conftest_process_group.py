"""Regression tests for the bounded process-group command runner (issue #211).

A timed-out command must terminate its direct AND descendant processes, so
child Podman/buildah builds cannot leak past the timeout, while normal runs
keep their exact output and exit-status semantics.
"""

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import (
    _terminate_process_group,
    _wait_pid_gone,
    run_bash_script,
    run_in_process_group,
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
def test_sigterm_handler_kills_tracked_session_groups(
    repo_root: Path, tmp_path: Path
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
        f'{str(build_pidfile)!r}; sleep 600 & wait",\n'
        "        ],\n"
        "        timeout=600,\n"
        "    )\n"
        "\n"
        "\n"
        "worker = threading.Thread(target=start_wedge, daemon=True)\n"
        "worker.start()\n"
        f"while not os.path.exists({str(build_pidfile)!r}):\n"
        "    time.sleep(0.05)\n"
        "print('READY', flush=True)\n"
        "time.sleep(600)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(suite_mock)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=repo_root,
    )
    try:
        ready_line = proc.stdout.readline()
        assert ready_line.strip() == "READY", (
            f"suite mock did not start its wedge: {ready_line}"
            f"{proc.stderr.read() if proc.poll() is not None else ''}"
        )
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
