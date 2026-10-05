import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

DEBUG_SCRIPT = Path(__file__).resolve().parents[2] / "lima" / "opencode-debug-logs.sh"


def _start_fake_service(log_level: str | None) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.pop("OPENCODE_LOG_LEVEL", None)
    if log_level is not None:
        env["OPENCODE_LOG_LEVEL"] = log_level
    child = (
        "import os; os.execve('/bin/bash', "
        "[b'opencode', b'-c', b'while :; do sleep 60; done', "
        "b'serve', b'--service'], os.environb)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    command_line = Path(f"/proc/{process.pid}/cmdline")
    for _ in range(100):
        if command_line.exists() and b"--service" in command_line.read_bytes():
            return process
        if process.poll() is not None:
            break
        time.sleep(0.01)
    process.terminate()
    process.wait(timeout=5)
    raise AssertionError("fake managed service did not start")


def _run_toggle(
    tmp_path: Path,
    mode: str,
    *,
    initial_preference: str | None = None,
    service_status: str = "stopped",
    service_pid: int | None = None,
    service_env: dict[str, str] | None = None,
    service_status_exit: int = 0,
    service_stop_exit: int = 0,
    service_config_json: str | None = None,
    service_registration_json: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    state = tmp_path / ".local/state/opencode/devbox-debug-logs"
    calls = tmp_path / "opencode-calls"
    if any(
        value is not None
        for value in (
            initial_preference,
            service_pid,
            service_config_json,
            service_registration_json,
        )
    ):
        state.parent.mkdir(parents=True, exist_ok=True)
    if initial_preference is not None:
        state.write_text(initial_preference)
    if service_registration_json is not None:
        (state.parent / "service.json").write_text(service_registration_json)
    elif service_pid is not None:
        (state.parent / "service.json").write_text(json.dumps({"pid": service_pid}))
    if service_config_json is not None or service_env is not None:
        service_config = tmp_path / ".config/opencode/service.json"
        service_config.parent.mkdir(parents=True, exist_ok=True)
        contents = service_config_json or json.dumps({"env": service_env})
        service_config.write_text(contents)

    opencode = bin_dir / "opencode"
    opencode.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'args="$*"',
                'printf "%s\\n" "$args" >>"$MOCK_OPENCODE_CALLS"',
                'case "$args" in',
                '  "service status")',
                '    printf "%s\\n" "$MOCK_SERVICE_STATUS"',
                '    exit "$MOCK_SERVICE_STATUS_EXIT"',
                "    ;;",
                '  "service stop")',
                '    exit "$MOCK_SERVICE_STOP_EXIT"',
                "    ;;",
                "  *)",
                '    echo "unexpected opencode call: $args" >&2',
                "    exit 98",
                "    ;;",
                "esac",
                "",
            ]
        )
    )
    opencode.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MOCK_OPENCODE_CALLS": str(calls),
        "MOCK_SERVICE_STATUS": service_status,
        "MOCK_SERVICE_STATUS_EXIT": str(service_status_exit),
        "MOCK_SERVICE_STOP_EXIT": str(service_stop_exit),
    }
    result = subprocess.run(
        ["bash", str(DEBUG_SCRIPT), mode],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, state, calls


@pytest.mark.unit
def test_no_flag_defaults_to_off_without_writing_a_preference(tmp_path: Path):
    result, state, calls = _run_toggle(tmp_path, "unchanged")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "disabled\n"
    assert not state.exists()
    assert calls.read_text().splitlines() == ["service status"]


@pytest.mark.unit
def test_no_flag_preserves_enabled_preference_without_restarting_service(
    tmp_path: Path,
):
    result, state, calls = _run_toggle(
        tmp_path, "unchanged", initial_preference="enabled"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "enabled\n"
    assert state.read_text() == "enabled"
    assert calls.read_text().splitlines() == ["service status"]


@pytest.mark.unit
def test_existing_log_file_is_restricted_before_toggle(tmp_path: Path):
    log_file = tmp_path / ".local/share/opencode/log/opencode.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("sensitive prior session data\n")
    log_file.chmod(0o644)

    result, _, _ = _run_toggle(tmp_path, "disabled")

    assert result.returncode == 0, result.stderr
    assert log_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
def test_enabling_debug_creates_private_log_file(tmp_path: Path):
    result, _, _ = _run_toggle(tmp_path, "enabled")
    log_file = tmp_path / ".local/share/opencode/log/opencode.log"

    assert result.returncode == 0, result.stderr
    assert log_file.is_file()
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert log_file.parent.stat().st_mode & 0o777 == 0o700
    assert log_file.read_text() == ""


@pytest.mark.unit
def test_debug_enable_persists_preference_in_private_state(tmp_path: Path):
    result, state, calls = _run_toggle(tmp_path, "enabled")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "enabled\n"
    assert state.read_text() == "enabled\n"
    assert calls.read_text().splitlines() == ["service status"]
    assert state.is_relative_to(tmp_path / ".local/state/opencode")


@pytest.mark.unit
def test_debug_disable_removes_enabled_preference(tmp_path: Path):
    result, state, calls = _run_toggle(
        tmp_path, "disabled", initial_preference="enabled"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "disabled\n"
    assert not state.exists()
    assert calls.read_text().splitlines() == ["service status"]


@pytest.mark.unit
def test_debug_disable_is_noop_when_already_disabled(tmp_path: Path):
    result, state, calls = _run_toggle(tmp_path, "disabled")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "disabled\n"
    assert not state.exists()
    assert calls.read_text().splitlines() == ["service status"]


@pytest.mark.unit
def test_no_flag_refuses_running_mismatched_service_without_changing_it(
    tmp_path: Path,
):
    service = _start_fake_service(log_level="DEBUG")
    try:
        result, state, calls = _run_toggle(
            tmp_path,
            "unchanged",
            service_status="http://127.0.0.1:49374",
            service_pid=service.pid,
        )
    finally:
        service.terminate()
        service.wait(timeout=5)

    assert result.returncode == 1
    assert "no flag leaves the service unchanged" in result.stderr
    assert not state.exists()
    assert calls.read_text().splitlines() == ["service status"]


@pytest.mark.unit
def test_no_flag_preserves_matching_running_service(tmp_path: Path):
    service = _start_fake_service(log_level="DEBUG")
    try:
        result, state, calls = _run_toggle(
            tmp_path,
            "unchanged",
            initial_preference="enabled",
            service_status="http://127.0.0.1:49374",
            service_pid=service.pid,
        )
    finally:
        service.terminate()
        service.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "enabled\n"
    assert state.read_text() == "enabled"
    assert calls.read_text().splitlines() == ["service status"]


@pytest.mark.unit
def test_no_flag_rejects_conflicting_persisted_service_env(tmp_path: Path):
    result, state, calls = _run_toggle(
        tmp_path,
        "unchanged",
        initial_preference="disabled",
        service_env={"OPENCODE_LOG_LEVEL": "DEBUG"},
    )

    assert result.returncode == 1
    assert "conflicting OPENCODE_LOG_LEVEL" in result.stderr
    assert state.read_text() == "disabled"
    assert not calls.exists()


@pytest.mark.unit
def test_service_status_failure_is_reported(tmp_path: Path):
    result, state, calls = _run_toggle(tmp_path, "enabled", service_status_exit=1)

    assert result.returncode == 1
    assert "could not query managed OpenCode service status" in result.stderr
    assert not state.exists()
    assert calls.read_text().splitlines() == ["service status"]


@pytest.mark.unit
def test_non_object_service_config_fails_without_traceback(tmp_path: Path):
    result, state, calls = _run_toggle(tmp_path, "enabled", service_config_json="[]")

    assert result.returncode == 1
    assert "could not safely inspect" in result.stderr
    assert "Traceback" not in result.stderr
    assert not state.exists()
    assert not calls.exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "configured_level"),
    [("disabled", "DEBUG"), ("enabled", "INFO")],
)
def test_conflicting_persisted_service_log_level_fails_clearly(
    tmp_path: Path, mode: str, configured_level: str
):
    result, state, calls = _run_toggle(
        tmp_path,
        mode,
        initial_preference="enabled" if mode == "disabled" else "disabled",
        service_env={"OPENCODE_LOG_LEVEL": configured_level},
    )

    assert result.returncode == 1
    assert "conflicting OPENCODE_LOG_LEVEL" in result.stderr
    assert "opencode service unset env OPENCODE_LOG_LEVEL" in result.stderr
    assert state.read_text() == ("enabled" if mode == "disabled" else "disabled")
    assert not calls.exists()


@pytest.mark.unit
def test_non_object_service_registration_fails_closed_without_traceback(
    tmp_path: Path,
):
    service = _start_fake_service(log_level=None)
    try:
        result, state, calls = _run_toggle(
            tmp_path,
            "enabled",
            service_status="http://127.0.0.1:49374",
            service_registration_json="[]",
        )
    finally:
        service.terminate()
        service.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert state.read_text() == "enabled\n"
    assert calls.read_text().splitlines() == ["service status", "service stop"]


@pytest.mark.unit
def test_requested_level_restarts_running_service_when_preference_matches(
    tmp_path: Path,
):
    service = _start_fake_service(log_level="DEBUG")
    try:
        result, state, calls = _run_toggle(
            tmp_path,
            "disabled",
            initial_preference="disabled",
            service_status="http://127.0.0.1:49374",
            service_pid=service.pid,
        )
    finally:
        service.terminate()
        service.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "disabled\n"
    assert not state.exists()
    assert calls.read_text().splitlines() == ["service status", "service stop"]
    assert "active sessions may have been interrupted" in result.stderr


@pytest.mark.unit
def test_stop_failure_does_not_claim_or_persist_requested_level(tmp_path: Path):
    service = _start_fake_service(log_level=None)
    try:
        result, state, calls = _run_toggle(
            tmp_path,
            "enabled",
            service_status="http://127.0.0.1:49374",
            service_pid=service.pid,
            service_stop_exit=1,
        )
    finally:
        service.terminate()
        service.wait(timeout=5)

    assert result.returncode == 1
    assert "could not stop the running OpenCode service" in result.stderr
    assert "stopped the running OpenCode service" not in result.stderr
    assert not state.exists()
    assert calls.read_text().splitlines() == ["service status", "service stop"]


@pytest.mark.unit
def test_requested_level_keeps_running_service_when_process_matches(
    tmp_path: Path,
):
    service = _start_fake_service(log_level="DEBUG")
    try:
        result, state, calls = _run_toggle(
            tmp_path,
            "enabled",
            initial_preference="enabled",
            service_status="http://127.0.0.1:49374",
            service_pid=service.pid,
        )
    finally:
        service.terminate()
        service.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "enabled\n"
    assert state.read_text() == "enabled"
    assert calls.read_text().splitlines() == ["service status"]
    assert "already has debug logging enabled" in result.stderr
