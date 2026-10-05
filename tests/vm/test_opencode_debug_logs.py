"""Managed OpenCode service environment propagation tests."""

import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest


@pytest.mark.vm
@pytest.mark.parametrize("log_level", ["DEBUG", None])
def test_managed_service_inherits_debug_log_level(
    tmp_path: Path, log_level: str | None
):
    opencode = shutil.which("opencode")
    assert opencode is not None, "the provisioned OpenCode binary is unavailable"

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config_home = home / ".config"
    data_home = home / ".local/share"
    state_home = home / ".local/state"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_STATE_HOME": str(state_home),
            "OPENCODE_CONFIG_CONTENT": "{}",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
    )
    env.pop("OPENCODE_LOG_LEVEL", None)
    if log_level is not None:
        env["OPENCODE_LOG_LEVEL"] = log_level

    # Do not collide with the managed devbox service on OpenCode's default
    # port when this test runs in a developer's already-provisioned VM.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        service_port = listener.getsockname()[1]
    configure = subprocess.run(
        [opencode, "service", "set", "port", str(service_port)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert configure.returncode == 0, (
        f"Could not configure isolated service port. stdout: {configure.stdout}; "
        f"stderr: {configure.stderr}"
    )
    log_file = data_home / "opencode/log/opencode.log"
    log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_file.parent.chmod(0o700)
    log_file.touch(mode=0o600)
    log_file.chmod(0o600)

    stopped_status = subprocess.run(
        [opencode, "service", "status"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert stopped_status.returncode == 0, stopped_status.stderr
    assert stopped_status.stdout.strip() == "stopped"

    try:
        start = subprocess.run(
            [opencode, "service", "start"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            preexec_fn=(lambda: os.umask(0o077)) if log_level is not None else None,
        )
        assert start.returncode == 0, (
            f"OpenCode service did not start. stdout: {start.stdout}; "
            f"stderr: {start.stderr}"
        )
        running_status = subprocess.run(
            [opencode, "service", "status"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert running_status.returncode == 0, running_status.stderr
        assert running_status.stdout.strip() == f"http://127.0.0.1:{service_port}"

        registration_path = state_home / "opencode/service.json"
        registration = json.loads(registration_path.read_text(encoding="utf-8"))
        pid = registration.get("pid")
        assert isinstance(pid, int) and pid > 1

        process = Path("/proc") / str(pid)
        command = (process / "cmdline").read_bytes().split(b"\0")
        assert all(
            any(part in argument for argument in command)
            for part in (b"opencode", b"serve", b"--service")
        )
        process_environment = (process / "environ").read_bytes().split(b"\0")
        log_variables = [
            entry.partition(b"=")[2]
            for entry in process_environment
            if entry.startswith(b"OPENCODE_LOG_LEVEL=")
        ]
        if log_level is None:
            assert not log_variables, "disabled service inherited a debug log level"
        else:
            assert log_variables == [log_level.encode()]
        assert log_file.is_file()
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(log_file.parent.stat().st_mode) == 0o700
    finally:
        stop = subprocess.run(
            [opencode, "service", "stop"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert stop.returncode == 0, (
            f"OpenCode service cleanup failed. stdout: {stop.stdout}; "
            f"stderr: {stop.stderr}"
        )
